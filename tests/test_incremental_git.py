"""Incremental git ingest: --since-commit, :Repository.lastCommit, no duplicates."""

import re
import shutil
import subprocess

import pytest

from graphforge.cli import build_parser
from graphforge.core.config import GitSettings
from graphforge.core.neo4j_writer import Neo4jWriter
from graphforge.git import history as history_mod
from graphforge.git.ingest import GitIngestor

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

_MERGE = re.compile(r"MERGE \(n:(?P<label>\w+) \{id: '(?P<id>[^']*)'\}\)(?P<set>\nSET)?")


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _sha(cwd):
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(cwd), check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def _ingest(repo_dir, out_path, clone_dir, **kw):
    with Neo4jWriter(settings=None, emit_path=str(out_path)) as w:
        GitIngestor(w, GitSettings(repo_dir=str(clone_dir))).ingest_repo(
            {"path": str(repo_dir), "name": "demo"}, **kw
        )
    return out_path.read_text()


def _nodes(text, label, only_full=False):
    """Node ids MERGEd for a label; `only_full` keeps just the ones carrying props."""
    return [
        m.group("id")
        for m in _MERGE.finditer(text)
        if m.group("label") == label and (m.group("set") or not only_full)
    ]


def _scanned_files(text):
    """File ids written by the structure pass (history also merges File stubs)."""
    return sorted(
        {
            stmt.split("'")[1]
            for stmt in text.split("MERGE (n:File {id: ")[1:]
            if "n.totalLines" in stmt.split(";")[0]
        }
    )


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / "src"
    (d / "app").mkdir(parents=True)
    _git(tmp_path, "init", "-q", "-b", "main", str(d))
    _git(d, "config", "user.email", "alice@example.com")
    _git(d, "config", "user.name", "Alice")
    (d / "app" / "models.py").write_text("class Order:\n    def total(self):\n        return 0\n")
    (d / "app" / "util.py").write_text("def helper():\n    return 1\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "first commit")
    return d


def test_since_commit_round_trip(repo, tmp_path):
    first = _ingest(repo, tmp_path / "full.cypher", tmp_path / "c1")
    base_sha = _sha(repo)

    # a full run records where it stopped, so the next one can continue
    assert f"r.lastCommit = '{base_sha}'" in first
    assert "demo/app/models.py" in first and "demo/app/util.py" in first

    (repo / "app" / "orders.py").write_text(
        "class Invoice:\n    def pay(self):\n        return 2\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "second commit")
    new_sha = _sha(repo)

    second = _ingest(repo, tmp_path / "inc.cypher", tmp_path / "c2", since_commit=base_sha)

    # only the new commit is materialised; the old one survives as a parent stub
    assert _nodes(second, "Commit", only_full=True) == [f"demo@{new_sha}"]
    assert "first commit" not in second
    assert "second commit" in second
    # only the file touched by that commit is re-scanned
    assert _scanned_files(second) == ["demo/app/orders.py"]
    assert "demo/app/util.py" not in second
    # and the watermark advances
    assert f"r.lastCommit = '{new_sha}'" in second


def test_re_ingest_creates_no_duplicate_ids(repo, tmp_path):
    base_sha = _sha(repo)
    (repo / "app" / "orders.py").write_text("class Invoice:\n    pass\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "second commit")

    full = _ingest(repo, tmp_path / "a.cypher", tmp_path / "c1")
    inc = _ingest(repo, tmp_path / "b.cypher", tmp_path / "c2", since_commit=base_sha)

    # nothing is ever CREATEd, so replaying either script is idempotent
    assert "CREATE (" not in full and "CREATE (" not in inc
    for label in ("Commit", "File", "Class", "Method", "Repository"):
        ids = _nodes(inc, label, only_full=True)
        # every id the incremental run touches already exists from the full run,
        # so a re-run updates nodes rather than adding new ones
        assert set(ids) <= set(_nodes(full, label, only_full=True)), label
    # the structure pass writes each file exactly once per run
    assert len(_scanned_files(full)) == 3
    assert _scanned_files(inc) == ["demo/app/orders.py"]
    # one node id must never be MERGEd under two different labels
    by_id = {}
    for m in _MERGE.finditer(full):
        by_id.setdefault(m.group("id"), set()).add(m.group("label"))
    assert all(len(labels) == 1 for labels in by_id.values())


def test_unknown_sha_falls_back_to_a_full_ingest(repo, tmp_path):
    text = _ingest(repo, tmp_path / "o.cypher", tmp_path / "c", since_commit="0" * 40)
    assert "first commit" in text
    assert "demo/app/util.py" in text


def test_auto_without_a_stored_watermark_is_a_full_ingest(repo, tmp_path):
    # emit mode cannot read the graph back, so 'auto' degrades to a full run
    text = _ingest(repo, tmp_path / "o.cypher", tmp_path / "c", since_commit="auto")
    assert "first commit" in text
    assert _scanned_files(text) == ["demo/app/models.py", "demo/app/util.py"]


def test_replace_wins_over_since_commit(repo, tmp_path):
    base_sha = _sha(repo)
    (repo / "app" / "orders.py").write_text("class Invoice:\n    pass\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "second commit")
    text = _ingest(repo, tmp_path / "o.cypher", tmp_path / "c", since_commit=base_sha, replace=True)
    assert "DETACH DELETE" in text
    assert "first commit" in text  # a wipe must be followed by a full reload


def test_history_helpers(repo):
    head = history_mod.head_commit(str(repo))
    assert len(head) == 40
    assert history_mod.commit_exists(str(repo), head) is True
    assert history_mod.commit_exists(str(repo), "0" * 40) is False
    assert history_mod.commit_exists(str(repo), "") is False

    (repo / "app" / "new.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "third")
    assert history_mod.changed_paths(str(repo), head) == {"app/new.py"}
    assert history_mod.changed_paths(str(repo), "") == set()

    window = history_mod.extract_history(str(repo), since_commit=head)
    assert [c["message"] for c in window.commits] == ["third"]
    assert window.since == head
    assert window.changed_paths == {"app/new.py"}


def test_cli_exposes_since_commit():
    args = build_parser().parse_args(["git", "/tmp/x"])
    assert args.since_commit == ""  # off by default
    args = build_parser().parse_args(["git", "/tmp/x", "--since-commit", "auto"])
    assert args.since_commit == "auto"
