import shutil
import subprocess

import pytest

from graphforge.git.history import extract_history

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / "r"
    d.mkdir()
    _git(d, "init", "-q", "-b", "main")
    _git(d, "config", "user.email", "alice@example.com")
    _git(d, "config", "user.name", "Alice")
    (d / "f.txt").write_text("hello\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "first")
    (d / "g.txt").write_text("world\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "second", "--author", "Bob <bob@example.com>")
    _git(d, "tag", "v1")
    return str(d)


def test_commits_and_authors(repo):
    h = extract_history(repo)
    assert len(h.commits) == 2
    emails = {a["email"] for a in h.authors.values()}
    assert {"alice@example.com", "bob@example.com"} <= emails


def test_file_changes(repo):
    h = extract_history(repo)
    changed = {f["path"] for c in h.commits for f in c["files"]}
    assert {"f.txt", "g.txt"} <= changed
    # the commit that introduced f.txt records it as Added (select by message,
    # not position: the two commits can share an authored timestamp)
    first = next(c for c in h.commits if c["message"] == "first")
    assert any(f["path"] == "f.txt" and f["changeType"] == "A" for f in first["files"])


def test_branches_and_tags(repo):
    h = extract_history(repo)
    assert any(b["name"] == "main" for b in h.branches)
    assert any(t["name"] == "v1" for t in h.tags)


def test_history_limit(repo):
    h = extract_history(repo, limit=1)
    assert len(h.commits) == 1
