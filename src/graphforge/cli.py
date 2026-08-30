"""graphforge command-line interface.

    graphforge init            # create graph constraints & indexes
    graphforge git  [...]      # ingest git repositories (structure + history)
    graphforge db   [...]      # ingest relational database schemas
    graphforge mcp             # serve the graph over MCP
    graphforge search QUERY    # search code / schema nodes in the graph
    graphforge verify          # connect and report node counts per label
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .core.config import Settings, load_settings
from .core.errors import neo4j_advice
from .core.neo4j_writer import Neo4jWriter, load_schema

log = logging.getLogger("graphforge.cli")


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--env", metavar="FILE", help="path to a .env file to load")
    p.add_argument("--neo4j-uri", dest="neo4j_uri")
    p.add_argument("--neo4j-user", dest="neo4j_user")
    p.add_argument("--neo4j-password", dest="neo4j_password")
    p.add_argument("--neo4j-database", dest="neo4j_database")


def _add_writer_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--emit", metavar="FILE",
                   help="write a replayable .cypher script instead of pushing to Neo4j")
    p.add_argument("--dry-run", action="store_true",
                   help="build operations but neither connect nor write (just count)")
    p.add_argument("--no-schema", action="store_true",
                   help="skip creating constraints/indexes before ingesting")


#: The settings the current command resolved, so the top-level error handler can
#: name the URI and database a failure was actually about. Set by :func:`_settings`.
_RESOLVED: Settings | None = None


def _settings(args) -> Settings:
    global _RESOLVED
    s = load_settings(getattr(args, "env", None))
    if getattr(args, "neo4j_uri", None):
        s.neo4j.uri = args.neo4j_uri
    if getattr(args, "neo4j_user", None):
        s.neo4j.user = args.neo4j_user
    if getattr(args, "neo4j_password", None):
        s.neo4j.password = args.neo4j_password
    if getattr(args, "neo4j_database", None):
        s.neo4j.database = args.neo4j_database
    _RESOLVED = s
    return s


def _writer(s: Settings, args) -> Neo4jWriter:
    return Neo4jWriter(s.neo4j, emit_path=getattr(args, "emit", None),
                       dry_run=getattr(args, "dry_run", False))


def _report(writer: Neo4jWriter, extra: str = "") -> None:
    where = {
        "emit": f"wrote {writer.ops_written} operations -> {writer.emit_path}",
        "dry-run": f"dry run: {writer.ops_written} operations (nothing written)",
        "push": f"pushed {writer.ops_written} operations to Neo4j",
    }[writer.mode]
    print(f"[graphforge] {where}{(' | ' + extra) if extra else ''}")


# ----------------------------------------------------------------------------
# git
# ----------------------------------------------------------------------------
def _resolve_git_sources(args, git_settings) -> list[dict]:
    specs: list[dict] = []
    if args.config:
        with open(args.config, encoding="utf-8") as fh:
            data = json.load(fh)
        default_branch = data.get("defaultBranch", git_settings.default_branch)
        for repo in data.get("repositories", []):
            repo.setdefault("branch", default_branch)
            specs.append(repo)
    for item in args.sources:
        if "://" in item or item.endswith(".git"):
            specs.append({"url": item, "name": args.name, "branch": args.branch})
        else:
            specs.append({"path": item, "name": args.name})
    if not specs and git_settings.gitlab_server and git_settings.gitlab_group_id:
        from .git.discover import gitlab_group_repos
        specs = gitlab_group_repos(
            git_settings.gitlab_server, git_settings.gitlab_group_id,
            git_settings.gitlab_token, git_settings.default_branch,
            since_days=getattr(args, "since", 0) or 0,
        )
    return specs


def cmd_git(args) -> int:
    from .git.ingest import GitIngestor

    s = _settings(args)
    specs = _resolve_git_sources(args, s.git)
    if not specs:
        print("nothing to ingest: pass a path/URL, -c config.json, or set GITLAB_* env", file=sys.stderr)
        return 2
    with _writer(s, args) as w:
        gi = GitIngestor(w, s.git)
        if not args.no_schema:
            gi.apply_schema()
        stats = gi.ingest(
            specs,
            include_lines=args.lines or s.neo4j.include_lines,
            with_structure=not args.no_structure,
            with_history=not args.no_history,
            replace=args.replace,
            since_commit=getattr(args, "since_commit", "") or "",
        )
        extra = f"{stats['repos']} repos, {stats['files']} files, {stats['commits']} commits"
        if stats.get("failed"):
            extra += f", {stats['failed']} failed (see log with -v)"
        _report(w, extra)
    return 0


# ----------------------------------------------------------------------------
# db
# ----------------------------------------------------------------------------
def _default_schemas(engine: str, db_settings) -> list[str]:
    """PG_SCHEMAS is a PostgreSQL default; other engines discover their own."""
    return list(db_settings.pg_schemas) if (engine or "").lower().startswith(("pg", "postgres")) else []


def _resolve_db_sources(args, db_settings) -> list[dict]:
    sample_rows = getattr(args, "sample_rows", 0) or 0
    if args.config:
        with open(args.config, encoding="utf-8") as fh:
            data = json.load(fh)
        sources = data.get("sources", [])
        for src in sources:
            if "password" not in src and src.get("passwordEnv"):
                src["password"] = os.getenv(src["passwordEnv"], "")
            if sample_rows:
                src["sampleRows"] = sample_rows
        return sources

    url = getattr(args, "url", None) or os.getenv("DB_URL")
    if url:
        from .db import parse_db_url
        src = parse_db_url(url)
        if args.databases:
            src["databases"] = [d.strip() for d in args.databases.split(",") if d.strip()]
        if args.driver:
            src["driver"] = args.driver
        if args.schemas:
            src["schemas"] = [s.strip() for s in args.schemas.split(",") if s.strip()]
        src["sampleRows"] = sample_rows
        return [src]

    engine = args.engine or db_settings.engine
    databases = (args.databases.split(",") if args.databases else db_settings.names)
    return [{
        "engine": engine,
        "host": args.host or db_settings.host,
        "port": args.port or db_settings.port,
        "user": args.user or db_settings.user,
        "password": args.password or db_settings.password,
        # empty -> auto-discover every non-system database on the server
        "databases": [d.strip() for d in databases if d.strip()],
        "driver": args.driver or db_settings.mssql_driver,
        "schemas": (args.schemas.split(",") if args.schemas
                    else _default_schemas(engine, db_settings)),
        "sampleRows": sample_rows,
    }]


def cmd_db(args) -> int:
    from .db import DbIngestor

    have_conn = bool(getattr(args, "url", None) or args.config or args.host
                     or os.getenv("DB_URL") or os.getenv("DB_HOST"))
    if not have_conn:
        print("provide a connection: --url <db-url>, or --host …, or -c config.json "
              "(or set DB_URL / DB_HOST)", file=sys.stderr)
        return 2
    s = _settings(args)
    sources = _resolve_db_sources(args, s.db)
    with _writer(s, args) as w:
        di = DbIngestor(w)
        if not args.no_schema:
            di.apply_schema()
        stats = di.ingest_sources(sources, replace=args.replace)
        extra = f"{stats['databases']} databases, {stats['tables']} tables, {stats['columns']} columns"
        if stats.get("failed"):
            extra += f", {stats['failed']} failed (see log with -v)"
        _report(w, extra)
    return 0


# ----------------------------------------------------------------------------
# init / verify / mcp
# ----------------------------------------------------------------------------
def cmd_init(args) -> int:
    s = _settings(args)
    with _writer(s, args) as w:
        n = w.apply_schema(load_schema("git_schema.cypher"))
        n += w.apply_schema(load_schema("db_schema.cypher"))
        print(f"[graphforge] applied {n} schema statements ({w.mode})")
    return 0


def cmd_verify(args) -> int:
    s = _settings(args)
    with Neo4jWriter(s.neo4j) as w:
        counts = w.label_counts()
    if not counts:
        print("[graphforge] no nodes found (is the graph empty, or wrong database?)")
        return 0
    print("[graphforge] node counts by label:")
    for label, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {label:20s} {count}")
    return 0


def cmd_link(args) -> int:
    from .link import PASSES, LinkRunner

    selected = [name for name in PASSES if getattr(args, name.replace("-", "_"))]
    if not selected:
        selected = list(PASSES)  # default: run every pass
    s = _settings(args)
    with _writer(s, args) as w:
        n = LinkRunner(w).run(selected, min_name_len=args.min_table_name_len)
        _report(w, f"{n} link passes: {', '.join(selected)}")
    return 0


def cmd_vds(args) -> int:
    from .db.vds import VdsIngestor

    s = _settings(args)
    database = args.database or (s.db.names[0] if s.db.names else "")
    if not database:
        print("VDS import needs --database (or DB_NAMES)", file=sys.stderr)
        return 2
    with _writer(s, args) as w:
        vi = VdsIngestor(w)
        if not args.no_schema:
            vi.apply_schema()
        stats = vi.ingest(
            engine=args.engine or s.db.engine, host=args.host or s.db.host,
            port=args.port or s.db.port, user=args.user or s.db.user,
            password=args.password or s.db.password, database=database,
            driver=args.driver or s.db.mssql_driver,
        )
        _report(w, f"{stats['services']} VDS services, {stats['queries']} queries, "
                   f"{stats['whereFields']} where-fields")
    return 0


def cmd_status(args) -> int:
    s = _settings(args)
    with Neo4jWriter(s.neo4j) as w:
        rows = w.repository_status()
    if not rows:
        print("[graphforge] no repositories ingested yet")
        return 0
    print(f"{'repository':30s} {'status':12s} {'files':>7s} {'commits':>8s}  lastIngestedAt")
    for r in rows:
        print(f"{(r.get('name') or ''):30s} {(r.get('status') or ''):12s} "
              f"{(r.get('files') or 0):>7} {(r.get('commits') or 0):>8}  {r.get('lastIngestedAt') or ''}")
    return 0


def cmd_ui(args) -> int:
    from .ui import serve

    s = _settings(args)
    try:
        serve(s, host=args.host, port=args.port)
    except OSError as exc:
        if getattr(exc, "errno", None) in (48, 98, 10048):  # EADDRINUSE, incl. WSAEADDRINUSE
            raise RuntimeError(
                f"port {args.port} is already in use — "
                f"try `graphforge ui --port {args.port + 1}`") from exc
        raise
    return 0


def cmd_doctor(args) -> int:
    from .onboarding import report, run_checks

    checks = run_checks(_settings(args), getattr(args, "env", None))
    return report(checks)


def cmd_quickstart(args) -> int:
    from .onboarding import quickstart

    return quickstart(args)


def cmd_mcp_install(args) -> int:
    from .mcp.install import install, uninstall

    clients = [args.client] if args.client else None
    if args.remove:
        lines = uninstall(clients)
    else:
        env_file = Path(args.env).resolve() if args.env else _default_env_file()
        lines = install(clients, env_file, dry_run=args.dry_run)
    for line in lines:
        print(line)
    return 0


def _default_env_file() -> Path | None:
    """The .env to point the MCP entry at, so no password lands in a client config."""
    candidate = Path.cwd() / ".env"
    return candidate if candidate.exists() else None


def cmd_mcp(args) -> int:
    from .mcp.server import run

    s = _settings(args)
    run(s.neo4j)
    return 0


def cmd_search(args) -> int:
    query = (args.query or "").strip()
    if not query:
        print("error: empty query", file=sys.stderr)
        return 2
    from .mcp.server import GraphQuery

    s = _settings(args)
    gq = GraphQuery.connect(s.neo4j)
    try:
        page = gq.search_codebase(
            query, kind=args.kind, repo=args.repo or "", limit=args.limit)
    finally:
        gq.close()
    rows = page["rows"]
    if not rows:
        print("[graphforge] no matches")
        return 0
    if args.kind == "schema":
        print(f"{'labels':24s} {'name':32s} {'database':20s} table")
        for row in rows:
            labels = ",".join(row.get("labels") or [])
            print(f"{labels:24s} {(row.get('name') or ''):32s} "
                  f"{(row.get('database') or ''):20s} {row.get('table') or ''}")
    else:
        print(f"{'labels':24s} {'name':32s} {'ref':44s} repo")
        for row in rows:
            labels = ",".join(row.get("labels") or [])
            ref = row.get("ref") or row.get("database") or ""
            print(f"{labels:24s} {(row.get('name') or ''):32s} "
                  f"{ref:44s} {row.get('repo') or ''}")
    more = f", {page['total']} total — pass a higher --limit" if page.get("hasMore") else ""
    print(f"[graphforge] {len(rows)} match(es){more}")
    return 0


# ----------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    from .link import passes as link_passes  # for the --min-table-name-len default

    parser = argparse.ArgumentParser(prog="graphforge", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"graphforge {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create graph constraints and indexes")
    _add_common(p_init)
    _add_writer_opts(p_init)
    p_init.set_defaults(func=cmd_init)

    p_git = sub.add_parser("git", help="ingest git repositories (code structure + history)")
    p_git.add_argument("sources", nargs="*", help="local repo paths and/or git URLs")
    p_git.add_argument("-c", "--config", help="repositories.json")
    p_git.add_argument("--name", help="name for a single repo passed positionally")
    p_git.add_argument("--branch", help="branch for a single URL")
    p_git.add_argument("--lines", action="store_true", help="store per-line :Line nodes (heavy)")
    p_git.add_argument("--no-structure", action="store_true", help="skip code structure")
    p_git.add_argument("--no-history", action="store_true", help="skip commit history")
    p_git.add_argument("--replace", action="store_true", help="delete each repo's existing subgraph first")
    p_git.add_argument("--since", type=int, default=0, metavar="DAYS",
                       help="GitLab discovery: only repos active in the last N days")
    p_git.add_argument("--since-commit", dest="since_commit", metavar="SHA", default="",
                       help="incremental ingest: only commits after SHA (or 'auto' to "
                            "continue from :Repository.lastCommit)")
    _add_common(p_git)
    _add_writer_opts(p_git)
    p_git.set_defaults(func=cmd_git)

    p_db = sub.add_parser("db", help="ingest relational database schemas")
    p_db.add_argument("-c", "--config", help="databases.json")
    p_db.add_argument("--url", help="connection URL, e.g. postgresql://user:pass@host:5432/dbname "
                                     "(omit the database to graph every non-system database)")
    p_db.add_argument("--engine", help="mysql | postgres | mssql")
    p_db.add_argument("--host")
    p_db.add_argument("--port", type=int)
    p_db.add_argument("--user")
    p_db.add_argument("--password")
    p_db.add_argument("--databases", help="comma-separated database names")
    p_db.add_argument("--driver", help="MSSQL ODBC driver name")
    p_db.add_argument("--schemas", help="comma-separated schema filter (postgres/mssql)")
    p_db.add_argument("--sample-rows", dest="sample_rows", type=int, default=0, metavar="N",
                      help="opt-in profiling: COUNT(*) every table into :Table.approxRows, "
                           "and for tables of at most N rows also COUNT(DISTINCT col) into "
                           ":Column.approxCardinality (default 0 = off)")
    p_db.add_argument("--replace", action="store_true", help="delete each database's existing subgraph first")
    _add_common(p_db)
    _add_writer_opts(p_db)
    p_db.set_defaults(func=cmd_db)

    p_vds = sub.add_parser("vds", help="import a VDS (virtual data service) catalog")
    p_vds.add_argument("--engine", help="mysql | postgres | mssql")
    p_vds.add_argument("--host")
    p_vds.add_argument("--port", type=int)
    p_vds.add_argument("--user")
    p_vds.add_argument("--password")
    p_vds.add_argument("--database", help="database holding the VDS catalog tables")
    p_vds.add_argument("--driver", help="MSSQL ODBC driver name")
    _add_common(p_vds)
    _add_writer_opts(p_vds)
    p_vds.set_defaults(func=cmd_vds)

    p_link = sub.add_parser("link", help="create code<->database links over the loaded graph")
    p_link.add_argument("--maps-to", action="store_true", help="JPA entity -> Table")
    p_link.add_argument("--based-on", action="store_true", help="View -> Table (same DB)")
    p_link.add_argument("--uses-table", action="store_true", help="StoredProcedure -> Table (same DB)")
    p_link.add_argument("--cross-db", action="store_true", help="View -> Table (different DB)")
    p_link.add_argument("--min-table-name-len", dest="min_table_name_len", type=int,
                        default=link_passes.DEFAULT_MIN_NAME_LEN, metavar="N",
                        help="ignore table names shorter than N characters in the SQL-text "
                             f"passes (default {link_passes.DEFAULT_MIN_NAME_LEN})")
    _add_common(p_link)
    _add_writer_opts(p_link)
    p_link.set_defaults(func=cmd_link)

    p_verify = sub.add_parser("verify", help="report node counts per label")
    _add_common(p_verify)
    p_verify.set_defaults(func=cmd_verify)

    p_status = sub.add_parser("status", help="show per-repository ingest status")
    _add_common(p_status)
    p_status.set_defaults(func=cmd_status)

    p_ui = sub.add_parser("ui", help="serve a local web dashboard (config + load status)")
    p_ui.add_argument("--host", default="127.0.0.1")
    p_ui.add_argument("--port", type=int, default=8000)
    _add_common(p_ui)
    p_ui.set_defaults(func=cmd_ui)

    p_mcp = sub.add_parser("mcp", help="serve the knowledge graph over MCP (stdio)")
    mcp_sub = p_mcp.add_subparsers(dest="mcp_command")
    p_mcp_install = mcp_sub.add_parser(
        "install", help="register graphforge with an MCP client (no password is written)")
    p_mcp_install.add_argument(
        "--client", choices=["claude-code", "claude-desktop", "cursor", "all"],
        help="which client to write to (default: every client we know about)")
    p_mcp_install.add_argument("--remove", action="store_true",
                               help="remove the entry instead of adding it")
    p_mcp_install.add_argument("--dry-run", action="store_true",
                               help="print what would be written and exit")
    _add_common(p_mcp_install)
    p_mcp_install.set_defaults(func=cmd_mcp_install)
    _add_common(p_mcp)
    p_mcp.set_defaults(func=cmd_mcp)

    p_doctor = sub.add_parser(
        "doctor", help="check the install, the connection, and the graph, and say how to fix it")
    _add_common(p_doctor)
    p_doctor.set_defaults(func=cmd_doctor)

    p_quick = sub.add_parser(
        "quickstart", help="guided first run: configure, connect, ingest, and wire up MCP")
    p_quick.add_argument("--yes", action="store_true",
                         help="take defaults and never prompt (for scripts and CI)")
    p_quick.add_argument("--repo", metavar="PATH|URL", default="",
                         help="repository to ingest, instead of being asked")
    p_quick.add_argument("--db-url", dest="db_url", metavar="URL", default="",
                         help="database URL to ingest, instead of being asked")
    _add_common(p_quick)
    p_quick.set_defaults(func=cmd_quickstart)

    p_search = sub.add_parser("search", help="search code and/or schema nodes in the graph")
    p_search.add_argument("query", nargs="?", default="",
                          help="substring to match (case-insensitive)")
    p_search.add_argument("--kind", choices=["code", "schema", "all"], default="code",
                          help="code = File/Class/Method (default); schema = Table/Column/StoredProcedure")
    p_search.add_argument("--repo", default="", metavar="NAME",
                          help="limit hits to one repository name")
    p_search.add_argument("--limit", type=int, default=25)
    _add_common(p_search)
    p_search.set_defaults(func=cmd_search)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        # Expected, user-facing failures (bad engine, missing driver, missing
        # config file) exit cleanly instead of dumping a traceback.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except OSError as exc:
        # Port already in use, unreadable path, no route to host.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — driver errors are user errors, not crashes
        advice = neo4j_advice(exc, _RESOLVED)
        if advice is None:
            raise
        # Every neo4j.exceptions type descends from Exception, so none of them
        # were caught above: an unreachable database used to print 35 lines of
        # driver internals and exit 1 instead of the documented 2.
        print(f"error: {advice}", file=sys.stderr)
        print("  run `graphforge doctor` for a full check.", file=sys.stderr)
        if args.verbose:
            log.exception("underlying driver error")
        return 2


if __name__ == "__main__":
    sys.exit(main())
