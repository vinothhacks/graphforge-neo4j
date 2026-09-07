"""Read-only Cypher gate used by MCP ``read_cypher`` and ``POST /api/query``.

Security is **not** a substring match on the raw query text (that false-positives
on ``n.name = 'CREATE'`` and ``:Create``). Layers:

1. Procedure allowlist — deny-by-default, including every ``apoc.*``. Every
   ``CALL`` must resolve to an allowlisted name, so a quoted target cannot slip
   past the list by being unparseable.
2. Clause denies — ``LOAD CSV``, ``USE``, ``SHOW``, multi-statement, write clauses
   after strings, comments and backtick-quoted identifiers are stripped.
3. Hard cap on query length and ``LIMIT``.
4. Callers then run the query in a Neo4j **read** transaction (timeout).

A denied query must not open a connection (layers 1–3 run first).
"""

from __future__ import annotations

import re

from .core.neo4j_writer import _split_statements

#: Procedures ``read_cypher`` / ``POST /api/query`` may invoke. Unknown = denied.
READ_PROCEDURES = frozenset(
    {
        "db.labels",
        "db.relationshiptypes",
        "db.propertykeys",
    }
)

MAX_QUERY_CHARS = 8000
MAX_READ_LIMIT = 500
READ_TX_TIMEOUT_SECONDS = 30.0

_DENIED_PREFIX = "read-only query: "

_WRITE_CLAUSE = re.compile(
    r"(?<![:.`])\b(CREATE|MERGE|SET|REMOVE|DELETE|DETACH|DROP)\b",
    re.IGNORECASE,
)
#: Every ``CALL`` site. Deny-by-default walks these rather than only the ones
#: that happen to parse as a dotted name — an unparseable target is a denial.
_CALL_KEYWORD = re.compile(r"\bCALL\b", re.IGNORECASE)
#: ``CALL { ... }`` is a subquery, not a procedure; its body is checked normally.
_CALL_SUBQUERY = re.compile(r"\bCALL\s*\{", re.IGNORECASE)
_CALL_TARGET = re.compile(
    r"\bCALL\s+((?:[A-Za-z_]\w*)(?:\s*\.\s*[A-Za-z_]\w*)*)",
    re.IGNORECASE,
)
#: A CALL target whose segments may be backtick-quoted. Applied *before* quoted
#: identifiers are blanked, so the real procedure name survives to the allowlist
#: check instead of vanishing into whitespace.
_CALL_QUOTED_TARGET = re.compile(
    r"(\bCALL\s+)((?:`[^`]*`|[A-Za-z_]\w*)(?:\s*\.\s*(?:`[^`]*`|[A-Za-z_]\w*))*)",
    re.IGNORECASE,
)
_LIMIT_N = re.compile(r"\bLIMIT\s+(\d+)\b", re.IGNORECASE)
_LEADING_EXPLAIN = re.compile(r"^\s*EXPLAIN\s+", re.IGNORECASE)
_LEADING_LOAD_CSV = re.compile(r"^\s*LOAD\s+CSV\b", re.IGNORECASE)
_LEADING_USE = re.compile(r"^\s*USE\b", re.IGNORECASE)
_LEADING_SHOW = re.compile(r"^\s*SHOW\b", re.IGNORECASE)


def check_read_query(cypher: str, limit: int | None = None) -> str | None:
    """Return a denial reason, or ``None`` if layers 1–3 accept the query."""
    text = cypher if isinstance(cypher, str) else ""
    if not text.strip():
        return _DENIED_PREFIX + "empty query"
    if len(text) > MAX_QUERY_CHARS:
        return _DENIED_PREFIX + f"query too long (max {MAX_QUERY_CHARS} characters)"

    statements = [s.strip() for s in _split_statements(text) if s.strip()]
    if not statements:
        return _DENIED_PREFIX + "empty query"
    if len(statements) > 1:
        return _DENIED_PREFIX + "multi-statement queries are not allowed"

    stmt = _LEADING_EXPLAIN.sub("", statements[0], count=1).strip()
    masked = mask_query(stmt)

    if _LEADING_LOAD_CSV.match(masked):
        return _DENIED_PREFIX + "LOAD CSV is not allowed"
    if _LEADING_USE.match(masked):
        return _DENIED_PREFIX + "USE <database> is not allowed"
    if _LEADING_SHOW.match(masked):
        return _DENIED_PREFIX + "SHOW is not allowed"

    write = _WRITE_CLAUSE.search(masked)
    if write:
        return _DENIED_PREFIX + f"{write.group(1).upper()} is not allowed"

    denied = _denied_procedure(masked)
    if denied:
        return denied

    if limit is not None:
        try:
            n = int(limit)
        except (TypeError, ValueError):
            n = MAX_READ_LIMIT
        if n > MAX_READ_LIMIT:
            return _DENIED_PREFIX + f"limit exceeds {MAX_READ_LIMIT}"

    for hit in _LIMIT_N.finditer(masked):
        if int(hit.group(1)) > MAX_READ_LIMIT:
            return _DENIED_PREFIX + f"LIMIT exceeds {MAX_READ_LIMIT}"
    return None


def is_denied(cypher: str, limit: int | None = None) -> bool:
    """True when layers 1–3 reject the query (no connection should be opened)."""
    return check_read_query(cypher, limit) is not None


def clamp_read_limit(limit: int | None, default: int = 200) -> int:
    try:
        n = int(limit) if limit is not None else default
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, MAX_READ_LIMIT))


def mask_query(cypher: str) -> str:
    """Blank strings, comments and quoted identifiers; bare-name any CALL target.

    The result is safe to run keyword regexes against: nothing the user controls
    as *data* survives, but the structure they control as *code* does. Callers
    outside the guard use this to reason about a query without re-parsing it.
    """
    kept = _mask_literals(cypher, backticks="keep")
    unquoted = _CALL_QUOTED_TARGET.sub(lambda m: m.group(1) + m.group(2).replace("`", " "), kept)
    return _mask_literals(unquoted, backticks="blank")


def _denied_procedure(masked: str) -> str | None:
    """Deny by default: every ``CALL`` must name an allowlisted procedure."""
    for hit in _CALL_KEYWORD.finditer(masked):
        if _CALL_SUBQUERY.match(masked, hit.start()):
            continue
        target = _CALL_TARGET.match(masked, hit.start())
        if target is None:
            return _DENIED_PREFIX + "CALL target is not a plain procedure name"
        name = re.sub(r"\s+", "", target.group(1)).lower()
        if name not in READ_PROCEDURES:
            return _DENIED_PREFIX + f"procedure {name} is not allowlisted"
    return None


def _mask_literals(cypher: str, *, backticks: str = "blank") -> str:
    """Replace string contents and comments with spaces so keywords inside them vanish.

    ``backticks="blank"`` also blanks quoted identifiers; ``"keep"`` leaves them
    intact so :func:`mask_query` can lift a quoted procedure name out first.
    """
    out: list[str] = []
    i = 0
    n = len(cypher)
    while i < n:
        ch = cypher[i]
        nxt = cypher[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            i = _skip_line_comment(cypher, i, out)
            continue
        if ch == "/" and nxt == "*":
            i = _skip_block_comment(cypher, i, out)
            continue
        if ch == "'":
            i = _skip_string(cypher, i, "'", out)
            continue
        if ch == '"':
            i = _skip_string(cypher, i, '"', out)
            continue
        if ch == "`":
            i = _skip_backtick(cypher, i, out, keep=backticks == "keep")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _skip_line_comment(text: str, i: int, out: list[str]) -> int:
    while i < len(text) and text[i] != "\n":
        out.append(" ")
        i += 1
    return i


def _skip_block_comment(text: str, i: int, out: list[str]) -> int:
    out.extend("  ")
    i += 2
    while i < len(text) - 1:
        if text[i] == "*" and text[i + 1] == "/":
            out.extend("  ")
            return i + 2
        out.append("\n" if text[i] == "\n" else " ")
        i += 1
    while i < len(text):
        out.append(" ")
        i += 1
    return i


def _skip_string(text: str, i: int, quote: str, out: list[str]) -> int:
    out.append(" ")
    i += 1
    while i < len(text):
        if quote == "'" and text[i] == "'" and i + 1 < len(text) and text[i + 1] == "'":
            out.extend("  ")
            i += 2
            continue
        if text[i] == quote:
            out.append(" ")
            return i + 1
        out.append("\n" if text[i] == "\n" else " ")
        i += 1
    return i


def _skip_backtick(text: str, i: int, out: list[str], *, keep: bool) -> int:
    """Consume a backtick-quoted identifier. A doubled backtick is an escape."""
    out.append(text[i] if keep else " ")
    i += 1
    while i < len(text):
        if text[i] == "`" and i + 1 < len(text) and text[i + 1] == "`":
            out.extend(text[i : i + 2] if keep else "  ")
            i += 2
            continue
        if text[i] == "`":
            out.append(text[i] if keep else " ")
            return i + 1
        if keep:
            out.append(text[i])
        else:
            out.append("\n" if text[i] == "\n" else " ")
        i += 1
    return i
