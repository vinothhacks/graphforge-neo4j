"""README numbers must match the code. If this fails, fix the README (code wins)."""

from __future__ import annotations

from pathlib import Path

from test_mcp_tools import FakeDriver, _build_server_with_fakes

from graphforge.cli import build_parser
from graphforge.git.parsers.generic import SUPPORTED_EXTENSIONS
from graphforge.git.scan import PARSERS
from graphforge.ui import server as ui

ROOT = Path(__file__).resolve().parents[1]


def test_readme_command_count():
    parser = build_parser()
    sub = parser._subparsers._group_actions[0]
    names = set(sub.choices)
    assert names == {
        "init",
        "git",
        "db",
        "vds",
        "link",
        "status",
        "verify",
        "search",
        "ui",
        "mcp",
        "doctor",
        "quickstart",
    }
    assert len(names) == 12


def test_readme_mcp_tool_count():
    server = _build_server_with_fakes(FakeDriver())
    assert len(server.tools) == 12
    assert "search_codebase" in server.tools
    assert "find_code" in server.tools


def test_readme_dashboard_endpoint_count():
    """Seven JSON endpoints are routed (status/schema/graph/search/labels/node/query)."""
    from graphforge.core.config import DbSettings, Neo4jSettings, Settings

    settings = Settings(
        neo4j=Neo4jSettings(uri="bolt://x:7687", user="neo4j", password="x", database="neo4j"),
        db=DbSettings(engine="mysql", host="h", user="u", password="p"),
    )

    def _boom(_s):
        raise RuntimeError("offline")

    specs = (
        ("GET", "/api/status", "", None),
        ("GET", "/api/schema", "", None),
        ("GET", "/api/graph/sample", "", None),
        ("GET", "/api/search", "q=x", None),
        ("GET", "/api/labels/Class/sample", "", None),
        ("GET", "/api/node/n1/neighbors", "", None),
        ("POST", "/api/query", "", {"cypher": "MATCH (n) RETURN n LIMIT 1"}),
    )
    for method, path, query, body in specs:
        code, _ = ui.route(path, query, body, settings, method=method, connect=_boom)
        assert code != 404, path
    assert len(specs) == 7


def test_readme_parser_and_extension_counts():
    modules = {id(mod) for mod in PARSERS.values()}
    assert len(modules) == 4
    assert set(PARSERS) >= {"java", "python", "typescript", "javascript", "go"}
    assert len(SUPPORTED_EXTENSIONS) == 41
    assert len(set(SUPPORTED_EXTENSIONS.values())) == 32


def test_readme_example_query_file_count():
    files = list((ROOT / "examples" / "queries").glob("*.cypher"))
    assert len(files) == 6
