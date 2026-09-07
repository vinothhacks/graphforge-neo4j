"""`graphforge doctor` and `graphforge quickstart` - the two commands that make a
first run survivable.

`doctor` answers "why isn't this working?" in one screen. `quickstart` composes
the existing commands into the six-step sequence a new user otherwise has to
assemble from the README by hand. Neither contains any ingest logic of its own:
they call `cmd_init` / `cmd_git` / `cmd_db` / `cmd_link` like anybody else would.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .core.config import Settings

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"

_MARKS = {OK: ("v", "✓"), WARN: ("!", "!"), FAIL: ("x", "✗"), INFO: ("-", "·")}


def _mark(status: str) -> str:
    """A status glyph the current console can actually encode."""
    ascii_mark, pretty = _MARKS[status]
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        pretty.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return ascii_mark
    return pretty


@dataclass
class Check:
    """One line of the doctor report."""

    status: str
    label: str
    detail: str = ""
    fix: str = ""

    @property
    def failed(self) -> bool:
        return self.status == FAIL


def _driver_status(module: str, engine: str, extra: str) -> Check:
    try:
        __import__(module)
    except ImportError:
        return Check(
            INFO, f"{engine} driver", "not installed", f"pip install 'graphforge-neo4j[{extra}]'"
        )
    return Check(OK, f"{engine} driver", module)


def run_checks(settings: Settings, env_path: Path | None = None) -> list[Check]:
    """Everything `doctor` inspects, as data, so it is testable without a console."""
    checks: list[Check] = []

    # -- the machine -------------------------------------------------------
    version = ".".join(str(p) for p in sys.version_info[:3])
    checks.append(
        Check(
            OK if sys.version_info >= (3, 10) else FAIL,
            "python",
            version,
            "graphforge needs Python 3.10 or newer",
        )
    )

    git = shutil.which("git")
    checks.append(
        Check(OK, "git", git)
        if git
        else Check(FAIL, "git", "not on PATH", "install git - `graphforge git` shells out to it")
    )

    # -- configuration -----------------------------------------------------
    env_file = Path(env_path) if env_path else Path.cwd() / ".env"
    if env_file.exists():
        checks.append(Check(OK, ".env", str(env_file)))
    elif os.getenv("NEO4J_PASSWORD"):
        checks.append(Check(OK, ".env", "not present (NEO4J_* set in the environment)"))
    else:
        checks.append(
            Check(WARN, ".env", "not found", "cp .env.example .env, then set NEO4J_PASSWORD")
        )

    neo = settings.neo4j
    checks.append(Check(OK, "NEO4J_URI", neo.uri))
    if neo.password or os.getenv("GF_ALLOW_EMPTY_PASSWORD"):
        checks.append(Check(OK, "NEO4J_PASSWORD", "set"))
    else:
        checks.append(
            Check(FAIL, "NEO4J_PASSWORD", "not set", "set it in .env, or pass --neo4j-password")
        )

    # -- the graph ---------------------------------------------------------
    checks.extend(_graph_checks(settings))

    # -- optional pieces ---------------------------------------------------
    checks.append(_driver_status("mysql.connector", "MySQL", "mysql"))
    checks.append(_driver_status("psycopg2", "PostgreSQL", "postgres"))
    checks.append(_driver_status("pyodbc", "SQL Server", "mssql"))

    try:
        import mcp  # noqa: F401

        checks.append(Check(OK, "mcp extra", "installed"))
    except ImportError:
        checks.append(
            Check(
                WARN,
                "mcp extra",
                "not installed",
                "pip install 'graphforge-neo4j[mcp]' to serve the graph",
            )
        )

    checks.extend(_client_checks())
    return checks


def _graph_checks(settings: Settings) -> list[Check]:
    """Reach the database and report what is actually in it."""
    if not (settings.neo4j.password or os.getenv("GF_ALLOW_EMPTY_PASSWORD")):
        return [Check(INFO, "neo4j", "skipped (no password configured)")]
    from .mcp.server import GraphQuery

    try:
        graph = GraphQuery.connect(settings.neo4j)
    except Exception as exc:  # noqa: BLE001 — doctor reports failures, never raises
        from .cli import neo4j_advice

        advice = neo4j_advice(exc, settings) or str(exc)
        head, _, rest = advice.partition("\n")
        # One hint only: the report is a column, and a wrapped fix breaks it.
        fix = rest.strip().splitlines()[0].strip() if rest.strip() else ""
        return [Check(FAIL, "neo4j", head, fix)]

    try:
        counts = graph.get_schema(refresh=True).get("nodeCountsByLabel") or {}
    except Exception as exc:  # noqa: BLE001 — a reachable but unusable graph is a finding
        return [
            Check(OK, "neo4j", f"reachable at {settings.neo4j.uri}"),
            Check(FAIL, "graph", f"could not read the schema: {exc}"),
        ]
    finally:
        graph.close()

    checks = [
        Check(
            OK, "neo4j", f"reachable at {settings.neo4j.uri} (database={settings.neo4j.database})"
        )
    ]
    total = sum(int(v or 0) for v in counts.values())
    if total:
        top = sorted(counts.items(), key=lambda kv: -int(kv[1] or 0))[:4]
        summary = ", ".join(f"{label} {count}" for label, count in top)
        checks.append(Check(OK, "graph", f"{total} nodes - {summary}"))
    else:
        checks.append(
            Check(WARN, "graph", "empty", "run `graphforge quickstart`, or `graphforge git <path>`")
        )
    return checks


def _client_checks() -> list[Check]:
    from .mcp.clients import known_clients

    wired = [c for c in known_clients() if c.has_graphforge()]
    if wired:
        return [Check(OK, "mcp clients", ", ".join(c.label for c in wired))]
    present = [c for c in known_clients() if c.path.exists()]
    detail = "none wired up" + (f" ({len(present)} client config(s) found)" if present else "")
    return [Check(WARN, "mcp clients", detail, "graphforge mcp install")]


def report(checks: list[Check], out=None) -> int:
    """Print the checks. Returns the process exit code."""
    stream = out or sys.stdout
    width = max((len(c.label) for c in checks), default=0)
    for check in checks:
        detail = f"  {check.detail}" if check.detail else ""
        print(f"  {_mark(check.status)} {check.label.ljust(width)}{detail}", file=stream)
        if check.fix and check.status in (FAIL, WARN):
            print(f"    {'':>{width}}  -> {check.fix}", file=stream)

    failures = [c for c in checks if c.failed]
    print(file=stream)
    if failures:
        print(f"{len(failures)} check(s) failed - fix the arrows above and re-run.", file=stream)
        return 1
    print("All checks passed.", file=stream)
    return 0


# ------------------------------------------------------------------ wizard --
def _ask(prompt: str, default: str = "", *, assume_yes: bool = False) -> str:
    if assume_yes:
        return default
    try:
        answer = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    except EOFError:
        return default
    return answer or default


def _confirm(prompt: str, *, default: bool = True, assume_yes: bool = False) -> bool:
    if assume_yes:
        return default
    suffix = "Y/n" if default else "y/N"
    answer = _ask(f"{prompt} ({suffix})", "", assume_yes=False).lower()
    return default if not answer else answer.startswith("y")


def _step(number: int, total: int, text: str) -> None:
    print(f"\n[{number}/{total}] {text}")


def write_env_file(path: Path, password: str, uri: str, database: str) -> None:
    """Seed a .env from the shipped example, substituting what the user gave us."""
    example = Path(__file__).resolve().parent.parent.parent / ".env.example"
    lines: list[str]
    if example.exists():
        lines = example.read_text(encoding="utf-8").splitlines()
    else:  # installed from a wheel: the example is not packaged
        lines = ["NEO4J_URI=", "NEO4J_USER=neo4j", "NEO4J_PASSWORD=", "NEO4J_DATABASE=neo4j"]
    replacements = {
        "NEO4J_URI": uri,
        "NEO4J_PASSWORD": password,
        "NEO4J_DATABASE": database,
    }
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        out.append(f"{key}={replacements[key]}" if key in replacements else line)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _sub_args(argv: list[str], args):
    """Build a namespace for another subcommand through the real parser.

    Reusing the wizard's own namespace would mean guessing every default that
    `git` / `db` / `link` read; going back through `build_parser` means they get
    exactly what they would from the command line, and stay in step if a flag is
    added later. Neo4j settings are forwarded so a password typed at the prompt
    reaches the commands that need it.
    """
    from .cli import build_parser

    passthrough: list[str] = []
    for flag, attr in (
        ("--env", "env"),
        ("--neo4j-uri", "neo4j_uri"),
        ("--neo4j-user", "neo4j_user"),
        ("--neo4j-password", "neo4j_password"),
        ("--neo4j-database", "neo4j_database"),
    ):
        value = getattr(args, attr, None)
        if value:
            passthrough += [flag, str(value)]
    return build_parser().parse_args([*argv, *passthrough])


def quickstart(args) -> int:
    """Walk a new user from nothing to a populated, MCP-connected graph."""
    from .cli import _settings, cmd_db, cmd_git, cmd_init, cmd_link

    yes = bool(getattr(args, "yes", False))
    total = 6
    print(
        "graphforge quickstart - from an empty database to a connected graph.\n"
        "Nothing here is irreversible; every step is a command you can re-run."
    )

    # 1. configuration
    _step(1, total, "Configuration")
    env_file = Path(getattr(args, "env", None) or ".env")
    settings = _settings(args)
    if not settings.neo4j.password and not os.getenv("GF_ALLOW_EMPTY_PASSWORD"):
        if yes:
            print("error: NEO4J_PASSWORD is not set and --yes cannot invent one.", file=sys.stderr)
            return 2
        password = _ask("Neo4j password")
        if not password:
            print("error: a password is required.", file=sys.stderr)
            return 2
        settings.neo4j.password = password
        if not env_file.exists() and _confirm(f"Write {env_file}?"):
            write_env_file(env_file, password, settings.neo4j.uri, settings.neo4j.database)
            print(f"  wrote {env_file}")
    print(f"  Neo4j at {settings.neo4j.uri} (database={settings.neo4j.database})")

    # 2. reachability, before anything tries to write
    _step(2, total, "Connecting")
    args.neo4j_password = settings.neo4j.password
    from .mcp.server import GraphQuery

    try:
        GraphQuery.connect(settings.neo4j).close()
    except Exception as exc:  # noqa: BLE001 — turn this into advice, not a traceback
        from .cli import neo4j_advice

        print(f"error: {neo4j_advice(exc, settings) or exc}", file=sys.stderr)
        return 2
    print("  connected.")

    # 3. schema
    _step(3, total, "Creating constraints and indexes")
    cmd_init(_sub_args(["init"], args))

    # 4. sources
    _step(4, total, "Loading data")
    loaded = False
    repo = getattr(args, "repo", "") or (
        "" if yes else _ask("Path or URL of a git repo (blank to skip)")
    )
    if repo:
        cmd_git(_sub_args(["git", repo], args))
        loaded = True
    db_url = getattr(args, "db_url", "") or ("" if yes else _ask("Database URL (blank to skip)"))
    if db_url:
        cmd_db(_sub_args(["db", "--url", db_url], args))
        loaded = True
    if not loaded:
        print("  nothing loaded - add some later with `graphforge git` / `graphforge db`.")

    # 5. link the two subgraphs
    _step(5, total, "Linking code to schema")
    if loaded:
        cmd_link(_sub_args(["link"], args))
    else:
        print("  skipped (nothing to link yet).")

    # 6. MCP
    _step(6, total, "Connecting an MCP client")
    from .mcp.install import install

    installed = install(clients=["claude-code"], env_file=env_file if env_file.exists() else None)
    for line in installed:
        print(f"  {line}")

    print("\nDone. Next:")
    print("  graphforge ui        explore the graph in a browser")
    print("  graphforge doctor    re-check everything at any time")
    return 0
