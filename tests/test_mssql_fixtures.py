"""MSSQL extractor against recorded INFORMATION_SCHEMA rows — no live server."""
from __future__ import annotations

import json
from pathlib import Path

from graphforge.core.neo4j_writer import Neo4jWriter
from graphforge.db import get_extractor
from graphforge.db.base import DatabaseMeta, SchemaMeta
from graphforge.db.ingest import DbIngestor

_FIXTURE = Path(__file__).parent / "fixtures" / "mssql" / "information_schema.json"


def test_mssql_maps_recorded_information_schema_rows(tmp_path):
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    ex = get_extractor("mssql", host="sql.example", port=1433, user="u", password="")
    sm = SchemaMeta(name="dbo")
    sm.tables = [{"name": r["name"]} for r in data["tables"]]
    sm.columns = [ex.map_column(r) for r in data["columns"]]
    sm.indexes = [ex.map_index(r) for r in data["indexes"]]
    sm.procedures = [ex.map_procedure(r) for r in data["procedures"]]
    assert sm.columns[0]["name"] == "id" and sm.columns[0]["isNullable"] is False
    assert sm.indexes[0]["columns"] == ["id"] and sm.indexes[0]["isUnique"] is True
    assert "UPDATE dbo.orders" in sm.procedures[0]["definition"]
    meta = DatabaseMeta(name="Sales", engine="mssql", host="sql.example")
    meta.schemas = [sm]
    out = tmp_path / "mssql.cypher"
    with Neo4jWriter(settings=None, emit_path=str(out)) as w:
        DbIngestor(w).write_database(meta)
    text = out.read_text()
    assert "mssql://sql.example/Sales" in text
    assert "PK_orders" in text
    assert "usp_recalc" in text
    sql, params = ex.procedures_sql("dbo")
    assert "OBJECT_DEFINITION" in sql and params == ("dbo",)
    sql, params = ex.indexes_sql("dbo")
    assert "sys.indexes" in sql and params == ("dbo",)
