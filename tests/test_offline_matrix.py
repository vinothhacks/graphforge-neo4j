"""Offline --emit / --dry-run: Neo4j driver is never opened."""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from graphforge.cli import main
from graphforge.core.config import GitSettings
from graphforge.core.neo4j_writer import Neo4jWriter
from graphforge.db.ingest import DbIngestor
from graphforge.db.vds import VdsIngestor
from graphforge.git.ingest import GitIngestor

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    _git(tmp_path, "init", "-q", "-b", "main", str(d))
    _git(d, "config", "user.email", "a@example.com")
    _git(d, "config", "user.name", "A")
    (d / "a.py").write_text("class A:\n    pass\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "init")
    return d


def _patch_driver(monkeypatch):
    hits = []

    def boom(*_a, **_k):
        hits.append(True)
        raise AssertionError("Neo4j driver was opened")

    import neo4j
    monkeypatch.setattr(neo4j.GraphDatabase, "driver", boom)
    return hits


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_init_link_emit_and_dry_run_never_open_neo4j(monkeypatch, repo, tmp_path):
    hits = _patch_driver(monkeypatch)
    assert main(["init", "--emit", str(tmp_path / "init.cypher")]) == 0
    assert "CONSTRAINT" in (tmp_path / "init.cypher").read_text() or "INDEX" in (tmp_path / "init.cypher").read_text()
    assert main(["init", "--dry-run"]) == 0
    assert main(["git", str(repo), "--name", "demo", "--emit", str(tmp_path / "git.cypher"),
                 "--no-schema"]) == 0
    text = (tmp_path / "git.cypher").read_text()
    assert "MERGE (n:Repository" in text or "MERGE (n:File" in text
    assert main(["git", str(repo), "--name", "demo", "--dry-run", "--no-schema"]) == 0
    assert main(["link", "--emit", str(tmp_path / "link.cypher"), "--no-schema"]) == 0
    assert main(["link", "--dry-run", "--no-schema"]) == 0
    assert hits == []


def test_db_and_vds_emit_dry_run_never_open_neo4j(monkeypatch, tmp_path):
    from graphforge.db.base import DatabaseMeta, SchemaMeta

    hits = _patch_driver(monkeypatch)
    meta = DatabaseMeta(name="shop", engine="mysql", host="h")
    sm = SchemaMeta(name="shop")
    sm.tables = [{"name": "orders"}]
    meta.schemas = [sm]
    out = tmp_path / "db.cypher"
    with Neo4jWriter(settings=None, emit_path=str(out)) as w:
        DbIngestor(w).write_database(meta)
    assert "MERGE (n:Table" in out.read_text()
    with Neo4jWriter(settings=None, dry_run=True) as w:
        DbIngestor(w).write_database(meta)
        assert w.ops_written > 0
    rows = [{"sid": 1, "servicename": "s", "query": "select 1", "coretable": "orders",
             "groupby": "", "orderby": "", "tablename": "orders",
             "columnname": "id", "fieldname": "id"}]
    vds_out = tmp_path / "vds.cypher"
    with Neo4jWriter(settings=None, emit_path=str(vds_out)) as w:
        VdsIngestor(w).write_rows(rows, "mysql", "h", "shop")
    assert "VDSService" in vds_out.read_text()
    with Neo4jWriter(settings=None, dry_run=True) as w:
        VdsIngestor(w).write_rows(rows, "mysql", "h", "shop")
        assert w.ops_written > 0
    assert hits == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_since_commit_auto_emit_logs_why(repo, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    out = tmp_path / "inc.cypher"
    with Neo4jWriter(settings=None, emit_path=str(out)) as w:
        GitIngestor(w, GitSettings(repo_dir=str(tmp_path / "clone"))).ingest_repo(
            {"path": str(repo), "name": "demo"}, since_commit="auto")
    assert "cannot read :Repository.lastCommit" in caplog.text
    assert "emit" in caplog.text
    assert Path(out).read_text()  # full ingest still produced Cypher
