"""A first run must fail in one readable line, not 35 lines of driver internals.

`cli.main` used to catch only ValueError/RuntimeError/FileNotFoundError. Every
`neo4j.exceptions` type descends from Exception, so an unreachable database --
the single most common first-run failure -- dumped a traceback and exited 1
rather than the documented 2.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from graphforge.cli import main, neo4j_advice
from graphforge.core.config import DbSettings, Neo4jSettings, Settings
from graphforge.mcp import clients, install
from graphforge.onboarding import FAIL, OK, Check, report, run_checks


class _FakeNeo4jError(Exception):
    """Stands in for a driver exception: what matters is the module it comes from."""

    __module__ = "neo4j.exceptions"


def _named(name: str, message: str) -> Exception:
    return type(name, (_FakeNeo4jError,), {"__module__": "neo4j.exceptions"})(message)


def _settings(password: str = "pw", uri: str = "bolt://127.0.0.1:7687") -> Settings:
    return Settings(
        neo4j=Neo4jSettings(uri=uri, user="neo4j", password=password, database="neo4j"),
        db=DbSettings(engine="mysql", host="h"),
    )


# ------------------------------------------------------------ error advice --
def test_a_driver_error_becomes_one_actionable_line():
    advice = neo4j_advice(
        _named("ServiceUnavailable", "Couldn't connect to 127.0.0.1:9999"), _settings()
    )
    assert advice is not None
    assert "cannot reach Neo4j at bolt://127.0.0.1:7687" in advice
    assert "docker compose up -d" in advice
    assert "Traceback" not in advice


def test_auth_and_database_failures_name_the_thing_to_change():
    auth = neo4j_advice(_named("AuthError", "authentication failure"), _settings())
    assert "NEO4J_PASSWORD" in auth

    missing = neo4j_advice(_named("ClientError", "database does not exist"), _settings())
    assert "NEO4J_DATABASE" in missing


def test_a_non_driver_exception_is_left_alone_to_be_raised():
    assert neo4j_advice(ValueError("nope"), _settings()) is None
    assert neo4j_advice(KeyError("k"), _settings()) is None


def test_main_turns_a_driver_error_into_exit_2(monkeypatch, capsys):
    """The contract is exit 2 for a user error; an uncaught exception exits 1."""

    def boom(_args):
        raise _named("ServiceUnavailable", "Couldn't connect to 127.0.0.1:9999")

    monkeypatch.setattr("graphforge.cli.cmd_verify", boom)
    code = main(["verify"])
    err = capsys.readouterr().err
    assert code == 2, "a driver failure must exit 2, not 1"
    assert err.startswith("error: cannot reach Neo4j")
    assert "Traceback" not in err
    assert "neo4j._sync" not in err, "driver internals leaked to the user"


def test_an_unexpected_error_still_propagates(monkeypatch):
    """Only driver errors are translated; a real bug must not be swallowed."""

    def boom(_args):
        raise ZeroDivisionError("a genuine bug")

    monkeypatch.setattr("graphforge.cli.cmd_verify", boom)
    with pytest.raises(ZeroDivisionError):
        main(["verify"])


# ------------------------------------------------------- password up front --
def test_an_empty_password_fails_before_the_driver_is_reached():
    with pytest.raises(ValueError, match="NEO4J_PASSWORD is not set"):
        Neo4jSettings(password="").check_connectable()


def test_auth_disabled_servers_have_an_escape_hatch(monkeypatch):
    monkeypatch.setenv("GF_ALLOW_EMPTY_PASSWORD", "true")
    Neo4jSettings(password="").check_connectable()  # must not raise


def test_a_configured_password_is_never_questioned():
    Neo4jSettings(password="pw").check_connectable()


# -------------------------------------------------------------- the doctor --
def test_doctor_reports_a_failure_without_raising(monkeypatch):
    """An unreachable graph is a finding on the report, never an exception."""

    def boom(_settings):
        raise _named("ServiceUnavailable", "Couldn't connect")

    monkeypatch.setattr("graphforge.mcp.server.GraphQuery.connect", staticmethod(boom))
    checks = run_checks(_settings())
    neo = [c for c in checks if c.label == "neo4j"]
    assert neo and neo[0].status == FAIL
    assert "cannot reach Neo4j" in neo[0].detail
    assert "\n" not in neo[0].fix, "a wrapped fix breaks the report's alignment"


def test_doctor_skips_the_graph_when_there_is_no_password():
    checks = run_checks(_settings(password=""))
    labels = {c.label: c for c in checks}
    assert labels["NEO4J_PASSWORD"].status == FAIL
    assert "skipped" in labels["neo4j"].detail


def test_report_exit_code_follows_the_failures():
    out = io.StringIO()
    assert report([Check(OK, "a", "fine")], out) == 0
    assert "All checks passed" in out.getvalue()
    assert report([Check(FAIL, "b", "broken", "do the thing")], io.StringIO()) == 1


def test_report_prints_the_fix_for_a_failing_check():
    out = io.StringIO()
    report([Check(FAIL, "neo4j", "unreachable", "docker compose up -d")], out)
    assert "-> docker compose up -d" in out.getvalue()


# --------------------------------------------------------------- mcp install --
def test_install_writes_no_password_and_points_at_the_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("NEO4J_PASSWORD=hunter2\n", encoding="utf-8")
    install.install(["claude-code"], env, project_dir=tmp_path)

    written = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    entry = written["mcpServers"]["graphforge"]
    assert entry["args"][:1] == ["mcp"]
    assert "--env" in entry["args"]
    assert "hunter2" not in json.dumps(written), "a password reached the client config"


def test_install_merges_and_leaves_other_servers_alone(tmp_path):
    target = tmp_path / ".mcp.json"
    target.write_text(
        json.dumps(
            {
                "mcpServers": {"other": {"command": "keepme"}},
                "somethingElse": True,
            }
        ),
        encoding="utf-8",
    )

    install.install(["claude-code"], None, project_dir=tmp_path)
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["mcpServers"]["other"] == {"command": "keepme"}, "clobbered another server"
    assert written["somethingElse"] is True, "dropped an unrelated key"
    assert "graphforge" in written["mcpServers"]


def test_install_is_idempotent(tmp_path):
    install.install(["claude-code"], None, project_dir=tmp_path)
    lines = install.install(["claude-code"], None, project_dir=tmp_path)
    assert any("already registered" in ln for ln in lines)


def test_dry_run_writes_nothing(tmp_path):
    lines = install.install(["claude-code"], None, project_dir=tmp_path, dry_run=True)
    assert not (tmp_path / ".mcp.json").exists()
    assert any("would be added" in ln for ln in lines)


def test_uninstall_removes_only_graphforge(tmp_path):
    target = tmp_path / ".mcp.json"
    target.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")
    install.install(["claude-code"], None, project_dir=tmp_path)
    install.uninstall(["claude-code"], project_dir=tmp_path)

    written = json.loads(target.read_text(encoding="utf-8"))
    assert "graphforge" not in written["mcpServers"]
    assert written["mcpServers"]["other"] == {"command": "x"}


# ------------------------------------------------------ mcp starts anyway --
def test_the_mcp_server_does_not_connect_until_a_tool_is_used(monkeypatch):
    """An MCP server that exits at startup shows up as "server exited", nothing more.

    Connecting lazily means the client still starts and still lists the tools, so
    the reason reaches whoever asks a question -- and starting Neo4j afterwards
    is enough, with no client restart.
    """
    from graphforge.mcp.server import LazyGraph

    attempts = []

    def boom(_settings):
        attempts.append(1)
        raise _named("ServiceUnavailable", "Couldn't connect to 127.0.0.1:7687")

    monkeypatch.setattr("graphforge.mcp.server.GraphQuery.connect", staticmethod(boom))
    graph = LazyGraph(Neo4jSettings(password="pw"))
    assert attempts == [], "constructing the server already opened a connection"

    with pytest.raises(RuntimeError, match="cannot reach Neo4j"):
        graph.get_schema()
    assert attempts == [1]

    # A failed attempt must not be cached, or the user has to restart the client
    # after starting the database.
    with pytest.raises(RuntimeError):
        graph.get_schema()
    assert attempts == [1, 1], "a failed connection was cached"


def test_an_unknown_client_is_named_along_with_the_valid_ones():
    with pytest.raises(ValueError, match="unknown MCP client"):
        clients.client_by_key("emacs")


def test_the_command_is_resolved_not_guessed():
    """A PATH lookup is what put a stale hermes-agent venv path in .cursor/mcp.json."""
    command = clients.default_command()
    assert command, "no command resolved"
    assert "graphforge" in Path(command).name


def test_the_driver_is_imported_when_the_lazy_graph_is_built_not_when_it_connects():
    """Deferring the *import* as well as the connection deadlocks the MCP server.

    The first `import neo4j` executed inside a running MCP server never returns:
    the tool call hangs forever instead of erroring, which is strictly worse than
    the eager connect that lazy connection replaced. The import is cheap and
    cannot fail for anything the user can act on, so it belongs at build time;
    only the connection is worth deferring.

    Run in a subprocess because `neo4j` is already imported in this one.
    """
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent("""
        import sys
        assert "neo4j" not in sys.modules, "precondition: neo4j already imported"

        from graphforge.core.config import Neo4jSettings
        from graphforge.mcp.server import LazyGraph
        assert "neo4j" not in sys.modules, "importing the module should not import the driver"

        graph = LazyGraph(Neo4jSettings(password="pw"))
        assert "neo4j" in sys.modules, "the driver must be imported when LazyGraph is built"
        assert graph._graph is None, "building it must not open a connection"
        print("OK")
    """)
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "OK" in done.stdout
