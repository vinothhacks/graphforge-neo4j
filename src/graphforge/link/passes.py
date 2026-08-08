"""Cypher passes that link the code subgraph to the database subgraph.

Each pass is a single bulk statement. They are idempotent (MERGE) and safe to
re-run. Text passes require that stored-procedure / view definitions were
captured at ingest time (they are, by default).
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import List

from ..core.cypher import Operation
from ..core.neo4j_writer import Neo4jWriter

# Ignore very short identifiers when doing substring matches on SQL text, so a
# table called "a" doesn't match half the corpus.
_MIN_NAME = 3


def maps_to() -> Operation:
    """(:Class:Entity)-[:MAPS_TO]->(:Table) by exact JPA table name."""
    return Operation(
        "MATCH (e:Class) WHERE e.mappedTable IS NOT NULL AND e.mappedTable <> '' "
        "MATCH (t:Table) WHERE toLower(t.name) = toLower(e.mappedTable) "
        "MERGE (e)-[:MAPS_TO]->(t)",
        comment="MAPS_TO: JPA entity -> table (exact name match)",
    )


def based_on() -> Operation:
    """(:View)-[:BASED_ON]->(:Table) named in the view's SQL (same database)."""
    return Operation(
        "MATCH (v:View) WHERE v.definition IS NOT NULL AND v.definition <> '' "
        "MATCH (t:Table) WHERE t.database = v.database AND size(t.name) >= $min "
        "AND toLower(v.definition) CONTAINS toLower(t.name) "
        "MERGE (v)-[:BASED_ON]->(t)",
        {"min": _MIN_NAME},
        comment="BASED_ON: view -> tables referenced in its definition (same DB)",
    )


def uses_table() -> Operation:
    """(:StoredProcedure)-[:USES_TABLE]->(:Table) named in its body (same database)."""
    return Operation(
        "MATCH (p:StoredProcedure) WHERE p.definition IS NOT NULL AND p.definition <> '' "
        "MATCH (t:Table) WHERE t.database = p.database AND size(t.name) >= $min "
        "AND toLower(p.definition) CONTAINS toLower(t.name) "
        "MERGE (p)-[:USES_TABLE]->(t)",
        {"min": _MIN_NAME},
        comment="USES_TABLE: stored procedure -> tables referenced in its body (same DB)",
    )


def cross_db_reference() -> Operation:
    """(:View)-[:CROSS_DB_REFERENCE]->(:Table) in a *different* database."""
    return Operation(
        "MATCH (v:View) WHERE v.definition IS NOT NULL AND v.definition <> '' "
        "MATCH (t:Table) WHERE t.database <> v.database AND size(t.name) >= $min "
        "AND toLower(v.definition) CONTAINS toLower(t.name) "
        "MERGE (v)-[:CROSS_DB_REFERENCE]->(t)",
        {"min": _MIN_NAME},
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

    def run(self, names: Iterable[str]) -> int:
        ops: List[Operation] = []
        for name in names:
            if name not in PASSES:
                raise ValueError(f"unknown link pass {name!r}; choose from {sorted(PASSES)}")
            ops.append(PASSES[name]())
        self.writer.write(ops, desc="linking")
        return len(ops)
