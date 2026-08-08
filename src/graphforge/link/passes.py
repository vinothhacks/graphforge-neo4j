"""Cypher passes that link the code subgraph to the database subgraph.

Each pass is a single bulk statement. They are idempotent (MERGE) and safe to
re-run. Text passes require that stored-procedure / view definitions were
captured at ingest time (they are, by default).

Text matching is **whole-token**, not substring. Every SQL separator in a
definition is replaced with a space and both sides are padded, so ``' os '``
only matches a standalone ``os`` — never the ``os`` inside ``os_config`` or
``position``. This is plain Cypher: no APOC, and no regex escaping of table
names (which routinely contain ``$``, ``#`` and other metacharacters).
"""
from __future__ import annotations

from collections.abc import Iterable

from ..core.cypher import Operation
from ..core.neo4j_writer import Neo4jWriter

# Ignore very short identifiers even when they match as a token: a table called
# "log" or "seq" still appears as a column alias far too often to be evidence.
DEFAULT_MIN_NAME_LEN = 4

# Characters that terminate a SQL identifier. Replacing each with a space turns a
# definition into space-delimited tokens; `_` and `$` stay inside the token, so
# `os_config` never yields the token `os`.
SEPARATORS = [
    "\n", "\r", "\t", " ", "(", ")", ",", ";", ".", "=", "<", ">", "+", "-",
    "*", "/", "%", "!", "?", "[", "]", "{", "}", "|", "&", "^", "~", ":",
    "'", '"', "`", "@", "#", "\\",
]


def _tokens_of(var: str) -> str:
    """Cypher expression: `var`.definition as a lower-cased, space-delimited token run."""
    return (f"' ' + reduce(s = toLower({var}.definition), sep IN $seps | "
            f"replace(s, sep, ' ')) + ' '")


def _token_match(text_var: str, table_var: str) -> str:
    return f"{text_var} CONTAINS (' ' + toLower({table_var}.name) + ' ')"


def matches_table(definition: str, table_name: str,
                  min_name_len: int = DEFAULT_MIN_NAME_LEN) -> bool:
    """Python mirror of the Cypher token test — the executable spec for the passes.

    Kept in this module (and driven by the same ``SEPARATORS``) so the matching
    rule can be unit-tested without a live Neo4j.
    """
    if not definition or not table_name or len(table_name) < min_name_len:
        return False
    text = definition.lower()
    for sep in SEPARATORS:
        text = text.replace(sep, " ")
    return f" {table_name.lower()} " in f" {text} "


def maps_to(min_name_len: int = DEFAULT_MIN_NAME_LEN) -> Operation:
    """(:Class:Entity)-[:MAPS_TO]->(:Table) by exact JPA table name."""
    return Operation(
        "MATCH (e:Class) WHERE e.mappedTable IS NOT NULL AND e.mappedTable <> '' "
        "MATCH (t:Table) WHERE toLower(t.name) = toLower(e.mappedTable) "
        "MERGE (e)-[:MAPS_TO]->(t)",
        comment="MAPS_TO: JPA entity -> table (exact name match)",
    )


def based_on(min_name_len: int = DEFAULT_MIN_NAME_LEN) -> Operation:
    """(:View)-[:BASED_ON]->(:Table) named in the view's SQL (same database)."""
    return Operation(
        "MATCH (v:View) WHERE v.definition IS NOT NULL AND v.definition <> '' "
        f"WITH v, {_tokens_of('v')} AS tokens "
        "MATCH (t:Table) WHERE t.database = v.database AND size(t.name) >= $min "
        f"AND {_token_match('tokens', 't')} "
        "MERGE (v)-[:BASED_ON]->(t)",
        {"min": min_name_len, "seps": SEPARATORS},
        comment="BASED_ON: view -> tables named as whole tokens in its definition (same DB)",
    )


def uses_table(min_name_len: int = DEFAULT_MIN_NAME_LEN) -> Operation:
    """(:StoredProcedure)-[:USES_TABLE]->(:Table) named in its body (same database)."""
    return Operation(
        "MATCH (p:StoredProcedure) WHERE p.definition IS NOT NULL AND p.definition <> '' "
        f"WITH p, {_tokens_of('p')} AS tokens "
        "MATCH (t:Table) WHERE t.database = p.database AND size(t.name) >= $min "
        f"AND {_token_match('tokens', 't')} "
        "MERGE (p)-[:USES_TABLE]->(t)",
        {"min": min_name_len, "seps": SEPARATORS},
        comment="USES_TABLE: stored procedure -> tables named as whole tokens in its body (same DB)",
    )


def cross_db_reference(min_name_len: int = DEFAULT_MIN_NAME_LEN) -> Operation:
    """(:View)-[:CROSS_DB_REFERENCE]->(:Table) in a *different* database."""
    return Operation(
        "MATCH (v:View) WHERE v.definition IS NOT NULL AND v.definition <> '' "
        f"WITH v, {_tokens_of('v')} AS tokens "
        "MATCH (t:Table) WHERE t.database <> v.database AND size(t.name) >= $min "
        f"AND {_token_match('tokens', 't')} "
        "MERGE (v)-[:CROSS_DB_REFERENCE]->(t)",
        {"min": min_name_len, "seps": SEPARATORS},
        comment="CROSS_DB_REFERENCE: view -> table in a different database",
    )


PASSES = {
    "maps-to": maps_to,
    "based-on": based_on,
    "uses-table": uses_table,
    "cross-db": cross_db_reference,
}


class LinkRunner:
    def __init__(self, writer: Neo4jWriter):
        self.writer = writer

    def run(self, names: Iterable[str],
            min_name_len: int = DEFAULT_MIN_NAME_LEN) -> int:
        ops: list[Operation] = []
        for name in names:
            if name not in PASSES:
                raise ValueError(f"unknown link pass {name!r}; choose from {sorted(PASSES)}")
            ops.append(PASSES[name](min_name_len))
        self.writer.write(ops, desc="linking")
        return len(ops)
