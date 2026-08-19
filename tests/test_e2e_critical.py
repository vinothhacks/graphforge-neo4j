"""Live Neo4j CLI/HTTP (no browser). Skipped unless GF_E2E_CRITICAL=1."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from test_query_guard import MUST_ALLOW, MUST_REJECT

from graphforge.cli import main
from graphforge.core.config import Neo4jSettings, Settings
from graphforge.mcp.server import GraphQuery
from graphforge.ui import server as ui

pytestmark = pytest.mark.e2e_critical

_URI = os.getenv("GF_IT_NEO4J_URI", os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687"))
_USER = os.getenv("GF_IT_NEO4J_USER", os.getenv("NEO4J_USER", "neo4j"))
_PASSWORD = os.getenv("GF_IT_NEO4J_PASSWORD", os.getenv("NEO4J_PASSWORD", "graphforge-ci-password"))
_DB = os.getenv("GF_IT_NEO4J_DATABASE", os.getenv("NEO4J_DATABASE", "neo4j"))


def _neo():
    return Neo4jSettings(uri=_URI, user=_USER, password=_PASSWORD, database=_DB)


def _flags():
    return ["--neo4j-uri", _URI, "--neo4j-user", _USER,
            "--neo4j-password", _PASSWORD, "--neo4j-database", _DB]


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def tiny_repo(tmp_path):
    d = tmp_path / "app"
    d.mkdir()
    _git(tmp_path, "init", "-q", "-b", "main", str(d))
    _git(d, "config", "user.email", "e2e@example.com")
    _git(d, "config", "user.name", "E2E")
    (d / "Hello.py").write_text("class Hello:\n    def hi(self):\n        return 1\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "e2e")
    return d


def test_write_guard_must_allow_on_live_neo4j():
    gq = GraphQuery.connect(_neo())
    try:
        for cypher in MUST_ALLOW:
            rows = gq.read_cypher(cypher, limit=5)
            assert isinstance(rows, list), cypher
    finally:
        gq.close()


def test_write_guard_must_reject_does_not_mutate():
    gq = GraphQuery.connect(_neo())
    try:
        before = gq.read_cypher("MATCH (n) RETURN count(n) AS c", limit=1)[0]["c"]
        for cypher in MUST_REJECT:
            with pytest.raises(ValueError, match="read-only"):
                gq.read_cypher(cypher)
        after = gq.read_cypher("MATCH (n) RETURN count(n) AS c", limit=1)[0]["c"]
        assert after == before
    finally:
        gq.close()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_ingest_twice_no_dupes(tiny_repo):
    name = "e2e-twice"
    argv = ["git", str(tiny_repo), "--name", name, "--replace", *_flags()]
    assert main(argv) == 0
    gq = GraphQuery.connect(_neo())
    try:
        q = ("MATCH (n) WHERE n.repo = $r RETURN labels(n)[0] AS label, count(n) AS c "
             "ORDER BY label")
        first = {r["label"]: r["c"] for r in gq.read_cypher(q, {"r": name}, limit=50)}
        assert main(["git", str(tiny_repo), "--name", name, *_flags()]) == 0
        second = {r["label"]: r["c"] for r in gq.read_cypher(q, {"r": name}, limit=50)}
        assert first == second
        assert first
    finally:
        gq.close()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_replace_removes(tiny_repo):
    name = "e2e-replace"
    assert main(["git", str(tiny_repo), "--name", name, *_flags()]) == 0
    (tiny_repo / "Hello.py").unlink()
    (tiny_repo / "Gone.py").write_text("class Gone:\n    pass\n")
    _git(tiny_repo, "add", "-A")
    _git(tiny_repo, "commit", "-qm", "gone")
    assert main(["git", str(tiny_repo), "--name", name, "--replace", *_flags()]) == 0
    gq = GraphQuery.connect(_neo())
    try:
        rows = gq.read_cypher(
            "MATCH (f:File) WHERE f.repo = $r RETURN f.path AS p",
            {"r": name}, limit=50)
        paths = {r["p"] for r in rows}
        assert any(p and p.endswith("Gone.py") for p in paths)
        hello = [p for p in paths if p and p.endswith("Hello.py")]
        assert len(hello) == 1
        file_orphans = gq.read_cypher(
            "MATCH (f:File) WHERE f.repo = $r AND NOT EXISTS { "
            "MATCH ()-[:CONTAINS_FILE]->(f) } AND NOT EXISTS { "
            "MATCH (:Commit)-[:CHANGED]->(f) } RETURN count(f) AS c",
            {"r": name}, limit=1)
        commit_orphans = gq.read_cypher(
            "MATCH (c:Commit) WHERE c.repo = $r AND NOT EXISTS { "
            "MATCH (:Repository {id: $r})-[:HAS_COMMIT]->(c) } "
            "RETURN count(c) AS c",
            {"r": name}, limit=1)
        assert file_orphans[0]["c"] == 0
        assert commit_orphans[0]["c"] == 0
    finally:
        gq.close()


def test_password_never_served_and_writes_rejected_over_http():
    settings = Settings(neo4j=_neo())
    server = ThreadingHTTPServer(("127.0.0.1", 0), ui._handler(settings))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        for path in ("/", "/api/status", "/api/schema"):
            with urllib.request.urlopen(base + path, timeout=15) as resp:
                raw = resp.read()
            assert _PASSWORD.encode() not in raw, path
        body = json.dumps({"cypher": "CREATE (n)"}).encode()
        req = urllib.request.Request(
            base + "/api/query", method="POST", data=body,
            headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=15)
        assert err.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
