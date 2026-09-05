"""Turn driver exceptions into advice a person can act on.

Shared by the CLI, `doctor`, and the MCP server so the same failure reads the
same way wherever you meet it. The neo4j package is deliberately never imported
here: knowing which module an exception came from is enough to classify it, and
importing the driver would defeat `--emit` / `--dry-run` staying driver-free.
"""

from __future__ import annotations

from typing import Any


class MissingExtra(RuntimeError):
    """A required optional dependency is not installed.

    Deliberately distinct from a per-item failure. `graphforge db` skips one bad
    database so a single bad credential cannot abort a ten-database run — but a
    missing driver is categorical, not per-item: it will fail every database on
    that engine. Swallowed as an item failure it exited 0, reported "0 tables"
    as success, and hid the `pip install` line behind `-v`.

    Subclasses RuntimeError so the CLI's existing handler still turns it into
    `error: ...` and exit 2 with no traceback.
    """


def neo4j_advice(exc: BaseException, settings: Any = None) -> str | None:
    """One actionable line for a Neo4j failure, or ``None`` if it isn't one.

    ``settings`` may be a :class:`~graphforge.core.config.Settings` or a bare
    :class:`~graphforge.core.config.Neo4jSettings`; anything else is ignored.
    Returning ``None`` lets the caller re-raise, so a genuine bug is never
    disguised as a connection problem.
    """
    if not (type(exc).__module__ or "").startswith("neo4j"):
        return None
    name = type(exc).__name__
    text = str(exc).strip()
    detail = text.splitlines()[0] if text else name
    neo = getattr(settings, "neo4j", settings)
    uri = getattr(neo, "uri", None) or "the configured URI"

    if name in {"ServiceUnavailable", "SessionExpired"} or "Couldn't connect" in detail:
        return (
            f"cannot reach Neo4j at {uri}\n"
            "  start one with `docker compose up -d`, or point --neo4j-uri elsewhere"
        )
    if name == "AuthError" or "authentication failure" in detail.lower():
        user = getattr(neo, "user", None) or "neo4j"
        return (
            f"Neo4j rejected the credentials for user {user!r}\n"
            "  set NEO4J_PASSWORD in your .env, or pass --neo4j-password"
        )
    if "database does not exist" in detail.lower() or (
        name == "ClientError" and "DatabaseNotFound" in detail
    ):
        database = getattr(neo, "database", None) or "the configured database"
        return (
            f"Neo4j has no database named {database!r}\n"
            "  Community Edition only ever has 'neo4j' - check NEO4J_DATABASE"
        )
    if name == "ConfigurationError":
        return f"Neo4j connection is misconfigured: {detail}\n  check NEO4J_URI ({uri})"
    return f"Neo4j error ({name}): {detail}"
