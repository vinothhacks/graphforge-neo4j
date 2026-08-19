"""Playground E2E: Postgres + Neo4j + graphforge MCP stdio. GF_E2E_FULL=1."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess

import pytest
from test_query_guard import MUST_REJECT

from graphforge.cli import main
from graphforge.core.config import Neo4jSettings
from graphforge.db.base import SchemaExtractor
from graphforge.git.parsers import golang, java, python, typescript
from graphforge.mcp.server import DEAD_CODE_CAVEAT, GraphQuery

pytestmark = pytest.mark.e2e_full

_URI = os.getenv("GF_IT_NEO4J_URI", os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687"))
_USER = os.getenv("GF_IT_NEO4J_USER", os.getenv("NEO4J_USER", "neo4j"))
_PASSWORD = os.getenv(
    "GF_IT_NEO4J_PASSWORD", os.getenv("NEO4J_PASSWORD", "graphforge-playground"))
_DB = os.getenv("GF_IT_NEO4J_DATABASE", os.getenv("NEO4J_DATABASE", "neo4j"))
_PG = os.getenv(
    "GF_IT_PG_URL",
    "postgresql://graphforge:graphforge-playground@127.0.0.1:5432/shopdb",
)


def _neo():
    return Neo4jSettings(uri=_URI, user=_USER, password=_PASSWORD, database=_DB)


def _flags():
    return ["--neo4j-uri", _URI, "--neo4j-user", _USER,
            "--neo4j-password", _PASSWORD, "--neo4j-database", _DB]


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def test_parser_fixtures_do_not_abort():
    java.extract(["class ((((("])
    python.extract(["def )(:"])
    typescript.extract(["export class {"])
    golang.extract(["func ("])


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_incremental_and_degrades(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    _git(tmp_path, "init", "-q", "-b", "main", str(d))
    _git(d, "config", "user.email", "e2e@example.com")
    _git(d, "config", "user.name", "E2E")
    (d / "a.py").write_text("class A:\n    pass\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "one")
    name = "e2e-inc"
    assert main(["git", str(d), "--name", name, "--replace", *_flags()]) == 0
    (d / "b.py").write_text("class B:\n    pass\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "two")
    assert main(["git", str(d), "--name", name, "--since-commit", "auto", *_flags()]) == 0
    gq = GraphQuery.connect(_neo())
    try:
        files = gq.read_cypher(
            "MATCH (f:File) WHERE f.repo = $r RETURN f.path AS p",
            {"r": name}, limit=50)
        paths = " ".join(r.get("p") or "" for r in files)
        assert "a.py" in paths and "b.py" in paths
        assert main(["git", str(d), "--name", name, "--since-commit", "deadbeef" * 5,
                     *_flags()]) == 0
    finally:
        gq.close()


def test_sample_rows_default_issues_no_count(monkeypatch):
    seen: list[str] = []
    orig = SchemaExtractor._scalar

    def wrapped(self, cursor, sql, *args, **kwargs):
        if "COUNT(*)" in sql.upper():
            seen.append(sql)
        return orig(self, cursor, sql, *args, **kwargs)

    monkeypatch.setattr(SchemaExtractor, "_scalar", wrapped)
    assert main(["db", "--url", _PG, "--replace", *_flags()]) == 0
    assert seen == []


def test_sample_rows_and_link():
    assert main(["db", "--url", _PG, "--sample-rows", "100000", *_flags()]) == 0
    gq = GraphQuery.connect(_neo())
    try:
        rows = gq.read_cypher(
            "MATCH (t:Table) WHERE t.approxRows IS NOT NULL RETURN t.name AS n LIMIT 5")
        assert rows, "approxRows not populated"
        assert main(["link", "--maps-to", *_flags()]) == 0
        assert main(["link", "--based-on", "--min-table-name-len", "4", *_flags()]) == 0
        assert main(["link", "--uses-table", *_flags()]) == 0
        assert main(["link", "--cross-db", *_flags()]) == 0
        assert main(["link", *_flags()]) == 0
        edges = gq.read_cypher(
            "MATCH ()-[r:MAPS_TO|BASED_ON|USES_TABLE|CROSS_DB_REFERENCE]->() "
            "RETURN type(r) AS t, count(*) AS c")
        counts = {r["t"]: r["c"] for r in edges}
        assert sum(counts.values()) >= 0
        assert counts  # at least one link type produced edges on the shop schema
    finally:
        gq.close()


def _tool_text(result) -> str:
    parts = []
    for item in getattr(result, "content", None) or []:
        parts.append(getattr(item, "text", None) or str(item))
    return "\n".join(parts)


def _tool_json(result):
    text = _tool_text(result).strip() or "{}"
    return json.loads(text)


async def _walk_pages(session, name: str, args: dict, limit: int = 2) -> list:
    offset = 0
    seen: list = []
    keys: set[str] = set()
    total = None
    while True:
        payload = _tool_json(await session.call_tool(
            name, {**args, "limit": limit, "offset": offset}))
        assert set(payload) >= {"rows", "total", "limit", "offset", "hasMore"}
        total = payload["total"]
        for row in payload["rows"]:
            key = json.dumps(row, sort_keys=True, default=str)
            assert key not in keys, f"duplicate row in {name}"
            keys.add(key)
            seen.append(row)
        if not payload["hasMore"]:
            break
        offset += limit
        assert offset <= total + limit
    assert len(seen) == total
    return seen


def test_mcp_stdio_tools_pagination_and_caveat():
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = os.environ.copy()
    env.update({
        "NEO4J_URI": _URI, "NEO4J_USER": _USER,
        "NEO4J_PASSWORD": _PASSWORD, "NEO4J_DATABASE": _DB,
    })
    params = StdioServerParameters(command="graphforge", args=["mcp"], env=env)

    async def _run():
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            expected = {
                "get_schema", "read_cypher", "search_nodes", "node_neighbors",
                "find_code", "search_codebase", "find_table", "find_procedure",
                "impact_of_column", "explain_impact", "find_dead_code",
                "blast_radius_of_file",
            }
            assert expected <= tools
            for name in expected:
                if name == "read_cypher":
                    result = await session.call_tool(
                        name, {"query": "MATCH (n) RETURN count(n) AS c", "limit": 1})
                elif name == "search_nodes":
                    result = await session.call_tool(
                        name, {"label": "File", "prop": "name", "value": "a",
                               "limit": 2, "offset": 0})
                elif name in ("find_code", "find_table", "find_procedure",
                              "search_codebase"):
                    args = {"text": "a", "limit": 2}
                    if name == "search_codebase":
                        args["kind"] = "all"
                    result = await session.call_tool(name, args)
                elif name == "node_neighbors":
                    result = await session.call_tool(
                        name, {"node_id": "missing", "limit": 1})
                elif name == "impact_of_column":
                    result = await session.call_tool(name, {"column": "id"})
                elif name == "explain_impact":
                    result = await session.call_tool(
                        name, {"target": "id", "kind": "auto"})
                elif name == "find_dead_code":
                    result = await session.call_tool(
                        name, {"repo": "e2e-inc", "days": 180, "limit": 5})
                elif name == "blast_radius_of_file":
                    result = await session.call_tool(
                        name, {"path": "Hello.py"})
                else:
                    result = await session.call_tool(name, {})
                assert result.isError is not True, (name, _tool_text(result))

            for cypher in MUST_REJECT:
                denied = await session.call_tool("read_cypher", {"query": cypher})
                blob = _tool_text(denied).lower()
                assert denied.isError or "read-only" in blob, cypher

            dead = await session.call_tool(
                "find_dead_code", {"repo": "e2e-inc", "days": 1, "limit": 5})
            body = _tool_text(dead)
            assert DEAD_CODE_CAVEAT in body

            first = _tool_json(await session.call_tool("get_schema", {}))
            second = _tool_json(await session.call_tool("get_schema", {}))
            assert first == second
            refreshed = await session.call_tool("get_schema", {"refresh": True})
            assert refreshed.isError is not True
            bypass = await session.call_tool("get_schema", {"ttl": 0})
            assert bypass.isError is not True

            await _walk_pages(
                session, "search_nodes",
                {"label": "File", "prop": "name", "value": "a"})
            await _walk_pages(session, "find_code", {"text": "a"})
            await _walk_pages(session, "find_table", {"text": "a"})
            await _walk_pages(
                session, "search_codebase", {"text": "a", "kind": "all"})

    asyncio.run(_run())
