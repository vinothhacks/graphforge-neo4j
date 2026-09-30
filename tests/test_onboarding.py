"""A first run must fail in one readable line, not 35 lines of driver internals.

`cli.main` used to catch only ValueError/RuntimeError/FileNotFoundError. Every
`neo4j.exceptions` type descends from Exception, so an unreachable database --
the single most common first-run failure -- dumped a traceback and exited 1
rather than the documented 2.
"""

from __future__ import annotations

import errno
import io
import json
import os
import sys
from pathlib import Path

import pytest

from graphforge.cli import build_parser, main, neo4j_advice
from graphforge.core.config import DbSettings, Neo4jSettings, Settings, allow_empty_password
from graphforge.mcp import clients, install
from graphforge.onboarding import (
    FAIL,
    INFO,
    MIN_MCP_MAJOR,
    OK,
    WARN,
    Check,
    quickstart,
    report,
    run_checks,
)


@pytest.fixture(autouse=True)
def _no_empty_password_opt_in(monkeypatch):
    """A developer's exported GF_ALLOW_EMPTY_PASSWORD must not reach these tests.

    With it set, every run_checks(_settings(password="")) here would try to
    connect to a real server. Tests that need the opt-in set it themselves.
    """
    monkeypatch.delenv("GF_ALLOW_EMPTY_PASSWORD", raising=False)


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


def _record_connects(monkeypatch) -> list[int]:
    """Count connection attempts; each one fails the way an unreachable server does."""
    attempts: list[int] = []

    def connect(_settings):
        attempts.append(1)
        raise _named("ServiceUnavailable", "Couldn't connect")

    monkeypatch.setattr("graphforge.mcp.server.GraphQuery.connect", staticmethod(connect))
    return attempts


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "", "  FALSE  "])
def test_a_false_opt_in_is_refused_everywhere_not_just_before_connecting(value, monkeypatch):
    """The doctor tested the raw string, so GF_ALLOW_EMPTY_PASSWORD=false meant "allowed".

    It then reported the password as set and tried to connect without one, while
    `check_connectable` refused the same value. All readers now share one helper.
    """
    attempts = _record_connects(monkeypatch)
    monkeypatch.setattr("graphforge.onboarding._client_checks", list)
    monkeypatch.setenv("GF_ALLOW_EMPTY_PASSWORD", value)

    assert allow_empty_password() is False
    with pytest.raises(ValueError, match="NEO4J_PASSWORD is not set"):
        Neo4jSettings(password="").check_connectable()
    labels = {c.label: c for c in run_checks(_settings(password=""))}
    assert labels["NEO4J_PASSWORD"].status == FAIL, f"{value!r} was taken as an opt-in"
    assert labels["neo4j"].status == INFO and "skipped" in labels["neo4j"].detail
    assert attempts == [], "the doctor connected with no password"


@pytest.mark.parametrize("value", ["true", "1", "yes", " On "])
def test_a_true_opt_in_is_honoured_everywhere(value, monkeypatch):
    monkeypatch.setattr(
        "graphforge.onboarding._graph_checks", lambda _s: [Check(OK, "neo4j", "stubbed")]
    )
    monkeypatch.setattr("graphforge.onboarding._client_checks", list)
    monkeypatch.setenv("GF_ALLOW_EMPTY_PASSWORD", value)

    assert allow_empty_password() is True
    Neo4jSettings(password="").check_connectable()  # must not raise
    labels = {c.label: c for c in run_checks(_settings(password=""))}
    assert labels["NEO4J_PASSWORD"].status == OK
    assert "GF_ALLOW_EMPTY_PASSWORD" in labels["NEO4J_PASSWORD"].detail, "claimed a password"


def test_quickstart_does_not_take_a_false_opt_in_as_permission(tmp_path, monkeypatch, capsys):
    """quickstart had the same raw-string test, so it went on to connect with no password."""
    attempts = _record_connects(monkeypatch)
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    monkeypatch.setenv("GF_ALLOW_EMPTY_PASSWORD", "false")
    env = tmp_path / "empty.env"
    env.write_text("", encoding="utf-8")

    code = quickstart(build_parser().parse_args(["quickstart", "--yes", "--env", str(env)]))
    assert code == 2
    assert "NEO4J_PASSWORD is not set" in capsys.readouterr().err
    assert attempts == [], "quickstart tried to connect with no password"


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


def _mcp_check(monkeypatch, installed: str | None) -> Check:
    """The doctor's mcp line, with the installed distribution version faked."""
    from importlib import metadata

    real_version = metadata.version

    def version(name):
        if name != "mcp":
            return real_version(name)
        if installed is None:
            raise metadata.PackageNotFoundError(name)
        return installed

    monkeypatch.setattr(metadata, "version", version)
    monkeypatch.setattr("graphforge.onboarding._client_checks", list)
    return {c.label: c for c in run_checks(_settings(password=""))}["mcp extra"]


def test_doctor_does_not_pass_an_mcp_the_server_cannot_run(monkeypatch):
    """`import mcp` succeeds on 1.x, so the doctor said "installed" for a server
    that cannot start: 2.0 is where FastMCP became MCPServer."""
    check = _mcp_check(monkeypatch, "1.9.4")
    assert check.status == WARN, "mcp 1.x was reported as fine"
    assert "1.9.4" in check.detail and f"mcp>={MIN_MCP_MAJOR}" in check.detail
    assert "pip install -U" in check.fix


def test_doctor_accepts_a_current_mcp_and_names_its_version(monkeypatch):
    check = _mcp_check(monkeypatch, "2.1.1")
    assert check.status == OK
    assert "2.1.1" in check.detail


def test_doctor_says_how_to_install_a_missing_mcp(monkeypatch):
    check = _mcp_check(monkeypatch, None)
    assert check.status == WARN
    assert check.detail == "not installed"
    assert "graphforge-neo4j[mcp]" in check.fix


def test_the_doctor_mcp_floor_matches_the_declared_pin():
    tomllib = pytest.importorskip("tomllib")  # stdlib from 3.11; CI also runs 3.10

    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert pyproject["project"]["optional-dependencies"]["mcp"] == [f"mcp>={MIN_MCP_MAJOR}"]


@pytest.fixture
def isolated_clients(tmp_path, monkeypatch):
    """Point every known client at tmp_path, never at this machine's real configs."""
    monkeypatch.chdir(tmp_path)
    desktop = tmp_path / "desktop" / "claude_desktop_config.json"
    monkeypatch.setattr(clients, "_claude_desktop_path", lambda: desktop)
    return tmp_path


def test_doctor_reports_a_client_config_it_cannot_parse(isolated_clients):
    """It used to read as "no config", so the doctor said "none wired up" and
    pointed at `mcp install` -- the command that then erased the file."""
    target = clients.client_by_key("claude-code").path
    target.write_text('{"mcpServers": {"graphforge": {"command": "g"},}}', encoding="utf-8")

    checks = run_checks(_settings(password=""))
    broken = [c for c in checks if c.label == "mcp config"]
    assert broken, "an unparseable client config was not reported"
    assert broken[0].status == WARN
    assert str(target) in broken[0].detail
    assert "line 1" in broken[0].detail, "the parse error was not passed on"


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


def test_uninstall_dry_run_changes_nothing(tmp_path):
    """`mcp install --remove --dry-run` really removed the entry: uninstall had no dry run."""
    target = tmp_path / ".mcp.json"
    install.install(["claude-code"], None, project_dir=tmp_path)
    before = target.read_bytes()

    lines = install.uninstall(["claude-code"], project_dir=tmp_path, dry_run=True)
    assert target.read_bytes() == before, "a dry run changed the config"
    assert any("would be removed" in ln for ln in lines), lines


def test_install_reads_a_config_saved_with_a_bom(tmp_path):
    """Notepad and PowerShell 5.1's Out-File write a UTF-8 BOM, which plain utf-8
    rejects; the failed read came back as {} and the merge erased everything."""
    target = tmp_path / ".mcp.json"
    config = {"mcpServers": {"other": {"command": "keepme"}}, "somethingElse": True}
    target.write_bytes(b"\xef\xbb\xbf" + json.dumps(config).encode("utf-8"))

    install.install(["claude-code"], None, project_dir=tmp_path)
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["mcpServers"]["other"] == {"command": "keepme"}, "a BOM erased the config"
    assert written["somethingElse"] is True
    assert "graphforge" in written["mcpServers"]


_UNPARSEABLE = {
    "trailing comma": '{"mcpServers": {"other": {"command": "keepme"},}}',
    "top-level array": '[{"mcpServers": {"other": {"command": "keepme"}}}]',
    "servers not an object": '{"mcpServers": ["other"], "keep": 1}',
}


@pytest.mark.parametrize("content", _UNPARSEABLE.values(), ids=list(_UNPARSEABLE))
def test_install_never_overwrites_a_config_it_cannot_parse(content, tmp_path):
    target = tmp_path / ".mcp.json"
    target.write_text(content, encoding="utf-8")
    before = target.read_bytes()

    lines = install.install(["claude-code", "cursor"], None, project_dir=tmp_path)
    assert target.read_bytes() == before, "an unparseable config was overwritten"
    refused = [ln for ln in lines if ln.startswith("Claude Code")]
    assert refused and "left untouched" in refused[0], lines
    assert str(target) in refused[0], "the message does not name the file"
    # The other client is independent of this one and still gets its entry.
    assert clients.client_by_key("cursor", tmp_path).has_graphforge()


def test_the_refusal_passes_on_the_parse_error(tmp_path):
    (tmp_path / ".mcp.json").write_text(_UNPARSEABLE["trailing comma"], encoding="utf-8")
    [line] = install.install(["claude-code"], None, project_dir=tmp_path, dry_run=True)
    assert "cannot parse" in line and "line 1 column" in line, line


def test_uninstall_never_touches_a_config_it_cannot_parse(tmp_path):
    """It used to read the broken file as {} and report "not registered"."""
    target = tmp_path / ".mcp.json"
    target.write_text(
        '{"mcpServers": {"graphforge": {"command": "g"}, "other": {}},}', encoding="utf-8"
    )
    before = target.read_bytes()

    [line] = install.uninstall(["claude-code"], project_dir=tmp_path)
    assert target.read_bytes() == before
    assert "left untouched" in line and "cannot parse" in line, line


def test_an_unreadable_config_is_refused_not_mistaken_for_a_missing_one(tmp_path):
    (tmp_path / ".mcp.json").mkdir()  # exists, but reading it fails
    [line] = install.install(["claude-code"], None, project_dir=tmp_path)
    assert "left untouched" in line and "cannot read" in line, line


def test_an_empty_config_file_still_counts_as_no_config(tmp_path):
    """Strict parsing is about not losing data; a zero-byte file holds none."""
    target = tmp_path / ".mcp.json"
    target.write_text("", encoding="utf-8")
    install.install(["claude-code"], None, project_dir=tmp_path)
    assert "graphforge" in json.loads(target.read_text(encoding="utf-8"))["mcpServers"]


def test_a_failed_write_leaves_the_existing_config_whole(tmp_path, monkeypatch):
    """A write that dies halfway (disk full, crash) truncated the file in place.

    Simulated by letting every text file opened for writing take half of what
    it is given and then fail. Writing to a temporary file and renaming it over
    the original means the original is either fully replaced or not touched.
    """
    target = tmp_path / ".mcp.json"
    original = json.dumps({"mcpServers": {"other": {"command": "keepme"}}})
    target.write_text(original, encoding="utf-8")
    real_open = io.open

    class _DiskFull:
        def __init__(self, fh):
            self._fh = fh

        def write(self, text):
            self._fh.write(text[: len(text) // 2])
            self._fh.flush()
            raise OSError(errno.ENOSPC, "No space left on device")

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self._fh.close()
            return False

        def __getattr__(self, name):
            return getattr(self._fh, name)

    def half_open(file, mode="r", *args, **kwargs):
        fh = real_open(file, mode, *args, **kwargs)
        return _DiskFull(fh) if "w" in mode else fh

    monkeypatch.setattr(io, "open", half_open)
    lines = install.install(["claude-code"], None, project_dir=tmp_path)
    monkeypatch.undo()

    assert target.read_text(encoding="utf-8") == original, "a failed write truncated the file"
    assert any("could not write" in ln for ln in lines), lines
    assert [p.name for p in tmp_path.iterdir()] == [".mcp.json"], "a temp file was left behind"


def test_a_symlinked_config_is_written_through_not_replaced(tmp_path):
    """A rename would swap a dotfiles-managed symlink for a plain file."""
    real = tmp_path / "dotfiles" / "mcp.json"
    real.parent.mkdir()
    real.write_text(json.dumps({"mcpServers": {"other": {"command": "k"}}}), encoding="utf-8")
    link = tmp_path / ".mcp.json"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("this platform or account cannot create symlinks")

    install.install(["claude-code"], None, project_dir=tmp_path)
    assert link.is_symlink(), "the symlink was replaced by a regular file"
    written = json.loads(real.read_text(encoding="utf-8"))
    assert set(written["mcpServers"]) == {"other", "graphforge"}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_rewriting_a_config_keeps_its_permissions(tmp_path):
    target = tmp_path / ".mcp.json"
    target.write_text("{}", encoding="utf-8")
    os.chmod(target, 0o640)
    install.install(["claude-code"], None, project_dir=tmp_path)
    assert (target.stat().st_mode & 0o777) == 0o640


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_a_new_config_gets_the_usual_mode_not_mkstemps_0600(tmp_path):
    previous = os.umask(0o022)
    try:
        install.install(["claude-code"], None, project_dir=tmp_path)
    finally:
        os.umask(previous)
    assert ((tmp_path / ".mcp.json").stat().st_mode & 0o777) == 0o644


def test_a_config_held_open_on_windows_is_still_written(tmp_path, monkeypatch):
    """Windows refuses os.replace over a file another process holds open.

    The old in-place write worked there, so the atomic rename falls back to it
    on Windows; elsewhere the error is real and the config is left alone.
    """
    target = tmp_path / ".mcp.json"
    target.write_text(json.dumps({"mcpServers": {"other": {"command": "k"}}}), encoding="utf-8")

    def locked(_src, _dst):
        raise PermissionError(13, "The process cannot access the file")

    monkeypatch.setattr(clients.os, "replace", locked)
    client = clients.McpClient("claude-code", "Claude Code", target, project_scoped=True)
    merged = {"mcpServers": {"other": {"command": "k"}, "graphforge": {"command": "g"}}}
    if os.name == "nt":
        client.write(merged)
        assert json.loads(target.read_text(encoding="utf-8")) == merged
    else:
        with pytest.raises(PermissionError):
            client.write(merged)
        assert set(json.loads(target.read_text(encoding="utf-8"))["mcpServers"]) == {"other"}
    assert [p.name for p in tmp_path.iterdir()] == [".mcp.json"], "a temp file was left behind"


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
