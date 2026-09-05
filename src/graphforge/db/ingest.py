"""Map extracted relational metadata into the graph.

    Database-HAS_SCHEMA->Schema-HAS_TABLE->Table-HAS_COLUMN->Column
    Table-HAS_INDEX->Index   Schema-HAS_VIEW->View   Schema-HAS_PROCEDURE->StoredProcedure
    Column-FOREIGN_KEY->Column   Table-REFERENCES->Table

Kept intentionally separate from the Git subgraph (no cross edges).
"""

from __future__ import annotations

import logging

from ..core.cypher import NodeRef, Operation, lit, merge_node, merge_rel
from ..core.neo4j_writer import Neo4jWriter, load_schema
from .base import DatabaseMeta

log = logging.getLogger("graphforge.db.ingest")

try:  # progress bar is optional
    from tqdm import tqdm

    def _progress(it, **kw):
        return tqdm(it, **kw)
except ImportError:  # pragma: no cover

    def _progress(it, **kw):
        return it


# ---- deterministic id builders -------------------------------------------
def _db_id(engine: str, host: str, name: str) -> str:
    return f"{engine}://{host}/{name}"


def _schema_id(db_id: str, schema: str) -> str:
    return f"{db_id}/{schema}"


def _table_id(schema_id: str, table: str) -> str:
    return f"{schema_id}/{table}"


def _column_id(table_id: str, column: str) -> str:
    return f"{table_id}#{column}"


def _index_id(table_id: str, index: str) -> str:
    return f"{table_id}!{index}"


def _view_id(schema_id: str, view: str) -> str:
    return f"{schema_id}/view/{view}"


def _proc_id(schema_id: str, proc: str) -> str:
    return f"{schema_id}/proc/{proc}"


class DbIngestor:
    def __init__(self, writer: Neo4jWriter):
        self.writer = writer

    def apply_schema(self) -> None:
        self.writer.apply_schema(load_schema("db_schema.cypher"))

    def ingest_sources(self, sources: list[dict], replace: bool = False) -> dict[str, int]:
        """Each source: {engine, host, port, user, password, [databases], [driver],
        [schemas], [sampleRows]}.

        If a source lists no databases, every non-system database on the server is
        discovered and graphed. A failure on one database is logged and recorded,
        not fatal — the run continues. ``sampleRows`` (default 0/off) enables the
        COUNT(*) / COUNT(DISTINCT) probes behind :Table.approxRows.
        """
        from . import get_extractor  # local import avoids package init cycle

        stats: dict[str, int] = {"databases": 0, "tables": 0, "columns": 0}
        failures: list[dict] = []
        for src in sources:
            extractor = get_extractor(
                src["engine"],
                host=src.get("host", "127.0.0.1"),
                port=src.get("port", 0),
                user=src.get("user", ""),
                password=src.get("password", ""),
                driver=src.get("driver"),
                schemas=src.get("schemas"),
                sample_rows=int(src.get("sampleRows") or 0),
            )
            databases = [d for d in (src.get("databases") or []) if d]
            if not databases:
                try:
                    databases = extractor.list_databases()
                    log.info(
                        "auto-discovered %d database(s) on %s", len(databases), src.get("host")
                    )
                except Exception as exc:  # noqa: BLE001  # discovery failed — skip this source, keep going
                    log.error("could not list databases on %s: %s", src.get("host"), exc)
                    failures.append(
                        {"source": f"{src.get('engine')}://{src.get('host')}", "error": str(exc)}
                    )
                    continue

            for database in _progress(databases, desc="databases", unit="db"):
                try:
                    meta = extractor.extract_database(database)
                    if replace:
                        self.clear_database(meta.engine, meta.host, meta.name)
                    counts = self.write_database(meta)
                    stats["databases"] += 1
                    stats["tables"] += counts["tables"]
                    stats["columns"] += counts["columns"]
                except Exception as exc:  # noqa: BLE001  # one bad database must not abort the rest
                    log.error("failed to ingest database %r: %s", database, exc)
                    failures.append({"database": database, "error": str(exc)})

        if failures:
            stats["failed"] = len(failures)
            log.warning(
                "%d item(s) failed: %s",
                len(failures),
                "; ".join(f.get("database", f.get("source", "?")) for f in failures),
            )
        return stats

    def clear_database(self, engine: str, host: str, name: str) -> int:
        """Delete an existing database subgraph (idempotent re-ingest)."""
        prefix = lit(_db_id(engine, host, name) + "/")
        dbid = lit(_db_id(engine, host, name))
        statements = [
            f"MATCH (n:Column) WHERE n.id STARTS WITH {prefix} DETACH DELETE n",
            f"MATCH (n:Index)  WHERE n.id STARTS WITH {prefix} DETACH DELETE n",
            f"MATCH (n:Table)  WHERE n.id STARTS WITH {prefix} DETACH DELETE n",
            f"MATCH (n:View)   WHERE n.id STARTS WITH {prefix} DETACH DELETE n",
            f"MATCH (n:StoredProcedure) WHERE n.id STARTS WITH {prefix} DETACH DELETE n",
            f"MATCH (n:Schema) WHERE n.id STARTS WITH {prefix} DETACH DELETE n",
            f"MATCH (n:Database) WHERE n.id = {dbid} DETACH DELETE n",
        ]
        return self.writer.run_statements(statements, desc=f"{name}: clear")

    def write_database(self, meta: DatabaseMeta) -> dict[str, int]:
        db_id = _db_id(meta.engine, meta.host, meta.name)
        counts = {"tables": 0, "columns": 0}

        self.writer.write(
            [
                merge_node(
                    "Database",
                    {"id": db_id},
                    {"name": meta.name, "engine": meta.engine, "host": meta.host},
                    comment=f"Database {meta.name}",
                )
            ],
            desc=f"{meta.name}: database",
        )

        for sm in meta.schemas:
            schema_id = _schema_id(db_id, sm.name)
            ops: list[Operation] = [
                merge_node("Schema", {"id": schema_id}, {"name": sm.name, "database": meta.name}),
                merge_rel(
                    NodeRef("Database", {"id": db_id}),
                    "HAS_SCHEMA",
                    NodeRef("Schema", {"id": schema_id}),
                ),
            ]
            for t in sm.tables:
                tid = _table_id(schema_id, t["name"])
                props = {"name": t["name"], "schema": sm.name, "database": meta.name}
                if t.get("approxRows") is not None:  # only when --sample-rows ran
                    props["approxRows"] = t["approxRows"]
                ops.append(merge_node("Table", {"id": tid}, props))
                ops.append(
                    merge_rel(
                        NodeRef("Schema", {"id": schema_id}),
                        "HAS_TABLE",
                        NodeRef("Table", {"id": tid}),
                    )
                )
                counts["tables"] += 1

            for c in sm.columns:
                tid = _table_id(schema_id, c["table"])
                cid = _column_id(tid, c["name"])
                props = {
                    "name": c["name"],
                    "table": c["table"],
                    "schema": sm.name,
                    "database": meta.name,
                    "ordinal": c.get("ordinal"),
                    "dataType": c.get("dataType", ""),
                    "isNullable": c.get("isNullable", True),
                    "columnKey": c.get("columnKey", ""),
                    "default": c.get("default", ""),
                    "extra": c.get("extra", ""),
                }
                if c.get("approxCardinality") is not None:
                    props["approxCardinality"] = c["approxCardinality"]
                ops.append(merge_node("Column", {"id": cid}, props))
                ops.append(
                    merge_rel(
                        NodeRef("Table", {"id": tid}), "HAS_COLUMN", NodeRef("Column", {"id": cid})
                    )
                )
                counts["columns"] += 1

            for ix in sm.indexes:
                tid = _table_id(schema_id, ix["table"])
                iid = _index_id(tid, ix["name"])
                ops.append(
                    merge_node(
                        "Index",
                        {"id": iid},
                        {
                            "name": ix["name"],
                            "table": ix["table"],
                            "database": meta.name,
                            "isUnique": ix.get("isUnique", False),
                            "indexType": ix.get("indexType", ""),
                            "columns": ix.get("columns", []),
                        },
                    )
                )
                ops.append(
                    merge_rel(
                        NodeRef("Table", {"id": tid}), "HAS_INDEX", NodeRef("Index", {"id": iid})
                    )
                )

            for v in sm.views:
                vid = _view_id(schema_id, v["name"])
                ops.append(
                    merge_node(
                        "View",
                        {"id": vid},
                        {
                            "name": v["name"],
                            "database": meta.name,
                            "definition": v.get("definition", ""),
                        },
                    )
                )
                ops.append(
                    merge_rel(
                        NodeRef("Schema", {"id": schema_id}),
                        "HAS_VIEW",
                        NodeRef("View", {"id": vid}),
                    )
                )

            for p in sm.procedures:
                pid = _proc_id(schema_id, p["name"])
                ops.append(
                    merge_node(
                        "StoredProcedure",
                        {"id": pid},
                        {
                            "name": p["name"],
                            "database": meta.name,
                            "type": p.get("type", "PROCEDURE"),
                            "definition": p.get("definition", ""),
                            "parameters": p.get("parameters", []),
                        },
                    )
                )
                ops.append(
                    merge_rel(
                        NodeRef("Schema", {"id": schema_id}),
                        "HAS_PROCEDURE",
                        NodeRef("StoredProcedure", {"id": pid}),
                    )
                )

            for fk in sm.foreign_keys:
                from_tid = _table_id(schema_id, fk["fromTable"])
                to_tid = _table_id(schema_id, fk["toTable"])
                from_cid = _column_id(from_tid, fk["fromColumn"])
                to_cid = _column_id(to_tid, fk["toColumn"])
                # Ensure endpoints exist (target may be outside imported scope).
                ops.append(
                    merge_node(
                        "Table",
                        {"id": to_tid},
                        {"name": fk["toTable"], "schema": sm.name, "database": meta.name},
                    )
                )
                ops.append(
                    merge_node(
                        "Column",
                        {"id": to_cid},
                        {"name": fk["toColumn"], "table": fk["toTable"], "schema": sm.name},
                    )
                )
                ops.append(
                    merge_rel(
                        NodeRef("Column", {"id": from_cid}),
                        "FOREIGN_KEY",
                        NodeRef("Column", {"id": to_cid}),
                        {"constraintName": fk.get("constraintName", "")},
                    )
                )
                ops.append(
                    merge_rel(
                        NodeRef("Table", {"id": from_tid}),
                        "REFERENCES",
                        NodeRef("Table", {"id": to_tid}),
                    )
                )

            self.writer.write(ops, desc=f"{meta.name}.{sm.name}")
        return counts
