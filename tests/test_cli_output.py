"""What the CLI actually prints.

`status` and `search` built their tables with fixed widths like `f"{ref:44s}"`.
That is a *minimum* width, so one path longer than 44 characters -- which is most
real paths -- pushed every later column out of line for that row only, and the
table stopped being a table exactly when there was enough data to need one.
"""

from __future__ import annotations

import pytest

from graphforge import __version__
from graphforge.cli import build_parser, print_table


def _lines(capsys) -> list[str]:
    return capsys.readouterr().out.rstrip("\n").split("\n")


def test_columns_are_measured_from_the_widest_value(capsys):
    print_table(["a", "b"], [["x", "1"], ["a much longer value", "2"]])
    header, first, second = _lines(capsys)
    assert header.index("b") == first.index("1") == second.index("2"), "columns did not line up"


def test_an_overlong_value_is_truncated_rather_than_shunting_the_row(capsys):
    long_path = "src/graphforge/" + "very_long_directory_name/" * 6 + "module.py"
    assert len(long_path) > 46
    print_table(["path", "repo"], [["short.py", "a"], [long_path, "b"]], max_width=46)
    header, first, second = _lines(capsys)
    assert header.index("repo") == first.index("a") == second.index("b"), (
        "a long path broke the alignment of its own row"
    )
    assert second.split()[0].endswith("...")
    assert len(second.split()[0]) <= 46


def test_numeric_columns_are_right_aligned(capsys):
    print_table(["repo", "files"], [["a", "7"], ["b", "1234"]], right=(1,))
    _, first, second = _lines(capsys)
    assert first.rstrip().endswith("7")
    assert second.rstrip().endswith("1234")
    assert first.index("7") == second.index("4"), "digits are not aligned on the right"


def test_no_trailing_whitespace_on_any_line(capsys):
    print_table(["a", "b"], [["x", "y"]])
    for line in _lines(capsys):
        assert line == line.rstrip(), f"trailing whitespace: {line!r}"


def test_missing_values_render_as_empty_not_none(capsys):
    print_table(["a", "b"], [[None, "y"]])
    assert "None" not in capsys.readouterr().out


# --------------------------------------------------------------------- help --
@pytest.mark.parametrize(
    "command,flags",
    [
        ("ui", ["--host", "--port"]),
        ("search", ["--limit", "--kind", "--repo"]),
    ],
)
def test_every_flag_a_command_exists_for_is_documented(command, flags):
    """`--host` and `--port` had no help at all on the command that exists for them."""
    parser = build_parser()
    sub = parser._subparsers._group_actions[0].choices[command]
    described = {opt: action.help for action in sub._actions for opt in action.option_strings}
    for flag in flags:
        assert described.get(flag), f"{command} {flag} has no help text"


def test_the_shared_neo4j_flags_are_documented_everywhere_they_appear():
    parser = build_parser()
    for name, sub in parser._subparsers._group_actions[0].choices.items():
        described = {opt: action.help for action in sub._actions for opt in action.option_strings}
        for flag in ("--neo4j-uri", "--neo4j-user", "--neo4j-password", "--neo4j-database"):
            if flag in described:
                assert described[flag], f"{name} {flag} has no help text"


def test_verbose_is_documented():
    parser = build_parser()
    described = {opt: action.help for action in parser._actions for opt in action.option_strings}
    assert described.get("--verbose")


# ------------------------------------------------------------------ version --
def test_the_version_fallback_cannot_be_mistaken_for_a_release():
    """A hardcoded release number drifts: it said 0.1.0 while pyproject said 0.2.0."""
    import graphforge

    source = graphforge.__file__
    from pathlib import Path

    text = Path(source).read_text(encoding="utf-8")
    assert '"0+unknown"' in text, "the fallback is a literal release number again"
    assert __version__, "no version resolved at all"


# ------------------------------------------------------------------ extras --
@pytest.mark.parametrize(
    "engine,module,extra",
    [
        ("mssql", "pyodbc", "mssql"),
        ("mysql", "mysql.connector", "mysql"),
        ("postgres", "psycopg2", "postgres"),
    ],
)
def test_a_missing_driver_names_an_extra_that_exists(engine, module, extra, monkeypatch):
    """The old advice was `pip install 'graphforge[mssql]'` — wrong extra, wrong dist.

    Drivers are optional as of 0.3, so this message is now the whole of the
    recovery path for anyone who installed the core package.
    """
    import builtins
    from pathlib import Path

    import tomllib

    from graphforge.db import get_extractor

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == module or name.startswith(module + "."):
            raise ImportError("simulated missing driver")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    extractor = get_extractor(engine, host="h", port=1, user="u", password="p")
    with pytest.raises(RuntimeError) as err:
        extractor.connect("db")

    message = str(err.value)
    assert f"graphforge-neo4j[{extra}]" in message, message

    # And the extra it names is really declared.
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert extra in pyproject["project"]["optional-dependencies"], (
        f"{extra} is advertised but not declared"
    )


def test_all_extra_restores_every_optional_dependency():
    """`[all]` is the documented one-line way back to the pre-0.3 install."""
    from pathlib import Path

    import tomllib

    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    extras = pyproject["project"]["optional-dependencies"]
    everything = set()
    for name, pins in extras.items():
        if name not in ("dev", "all"):
            everything.update(pins)
    assert everything <= set(extras["all"]), f"[all] is missing: {everything - set(extras['all'])}"


# ------------------------------------------------- status / verify commands --
class _FakeWriter:
    """Stands in for Neo4jWriter as a context manager over canned results."""

    def __init__(self, counts=None, repos=None):
        self._counts = counts if counts is not None else {}
        self._repos = repos if repos is not None else []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def label_counts(self):
        return self._counts

    def repository_status(self):
        return self._repos


def _run(monkeypatch, command, writer):
    from graphforge import cli
    from graphforge.core.config import DbSettings, Neo4jSettings, Settings

    settings = Settings(neo4j=Neo4jSettings(password="pw"), db=DbSettings())
    monkeypatch.setattr(cli, "Neo4jWriter", lambda *a, **k: writer)
    monkeypatch.setattr(cli, "_settings", lambda _args: settings)
    return getattr(cli, command)(build_parser().parse_args([command.removeprefix("cmd_")]))


def test_verify_reports_counts_highest_first(monkeypatch, capsys):
    code = _run(
        monkeypatch, "cmd_verify", _FakeWriter(counts={"Method": 721, "Class": 280, "File": 114})
    )
    out = capsys.readouterr().out
    assert code == 0
    order = [out.index(label) for label in ("Method", "Class", "File")]
    assert order == sorted(order), "labels were not ordered by count"
    assert "721" in out


def test_verify_on_an_empty_graph_says_so_instead_of_printing_nothing(monkeypatch, capsys):
    code = _run(monkeypatch, "cmd_verify", _FakeWriter(counts={}))
    out = capsys.readouterr().out
    assert code == 0
    assert "no nodes found" in out
    assert "empty" in out


def test_status_prints_an_aligned_table(monkeypatch, capsys):
    rows = [
        {
            "name": "short",
            "status": "completed",
            "files": 7,
            "commits": 2,
            "lastIngestedAt": "2026-08-30T17:04:21+00:00",
        },
        {
            "name": "a-considerably-longer-repository-name",
            "status": "ingesting",
            "files": 12345,
            "commits": 678,
            "lastIngestedAt": "2026-08-30T17:05:00+00:00",
        },
    ]
    code = _run(monkeypatch, "cmd_status", _FakeWriter(repos=rows))
    header, first, second = capsys.readouterr().out.rstrip("\n").split("\n")
    assert code == 0
    assert header.index("lastIngestedAt") == first.index("2026") == second.index("2026"), (
        "a long repository name pushed the timestamp column out of line"
    )


def test_status_with_nothing_ingested_says_so(monkeypatch, capsys):
    code = _run(monkeypatch, "cmd_status", _FakeWriter(repos=[]))
    assert code == 0
    assert "no repositories ingested yet" in capsys.readouterr().out
