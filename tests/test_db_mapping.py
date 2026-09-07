from graphforge.core.neo4j_writer import Neo4jWriter
from graphforge.db import get_extractor
from graphforge.db.base import DatabaseMeta, SchemaMeta, parse_index_columns
from graphforge.db.ingest import DbIngestor


def test_engine_factory_and_sql():
    mx = get_extractor("mysql", host="h", port=3306, user="u", password="")
    assert mx.engine == "mysql"
    assert "INFORMATION_SCHEMA.STATISTICS" in mx.indexes_sql("s")[0]
    assert mx.foreign_keys_sql("s")[0].count("%s") == 1

    px = get_extractor("postgresql", host="h", port=5432, user="u", password="")
    assert px.engine == "postgres"
    assert "pg_indexes" in px.indexes_sql("s")[0]

    sx = get_extractor("sqlserver", host="h", port=1433, user="u", password="")
    assert sx.engine == "mssql"
    assert "sys.indexes" in sx.indexes_sql("s")[0]
    assert sx.placeholder == "?"


def test_unknown_engine():
    import pytest

    with pytest.raises(ValueError):
        get_extractor("oracle", host="h", port=1, user="u", password="")


def test_parse_index_columns():
    assert parse_index_columns("CREATE UNIQUE INDEX x ON t USING btree (a, b)") == ["a", "b"]
    assert parse_index_columns("no parens") == []


def test_column_mapping_bool():
    mx = get_extractor("mysql", host="h", port=3306, user="u", password="")
    col = mx.map_column(
        {
            "table_name": "t",
            "name": "c",
            "ordinal": 1,
            "data_type": "int",
            "is_nullable": "NO",
            "column_default": None,
            "column_key": "PRI",
            "extra": "",
        }
    )
    assert col["isNullable"] is False
    assert col["columnKey"] == "PRI"
    assert col["default"] == ""


def test_write_database_emit(tmp_path):
    meta = DatabaseMeta(name="shop", engine="mysql", host="db1")
    sm = SchemaMeta(name="shop")
    sm.tables = [{"name": "orders"}, {"name": "cust"}]
    sm.columns = [
        {
            "table": "orders",
            "name": "id",
            "ordinal": 1,
            "dataType": "int",
            "isNullable": False,
            "columnKey": "PRI",
            "default": "",
            "extra": "",
        },
        {
            "table": "orders",
            "name": "cid",
            "ordinal": 2,
            "dataType": "int",
            "isNullable": False,
            "columnKey": "MUL",
            "default": "",
            "extra": "",
        },
    ]
    sm.indexes = [
        {
            "name": "PRIMARY",
            "table": "orders",
            "isUnique": True,
            "indexType": "BTREE",
            "columns": ["id"],
        }
    ]
    sm.procedures = [
        {
            "name": "sp_recalc",
            "type": "PROCEDURE",
            "definition": "BEGIN UPDATE orders SET total=0; END",
            "parameters": ["IN cid int"],
        }
    ]
    sm.foreign_keys = [
        {
            "constraintName": "fk",
            "fromTable": "orders",
            "fromColumn": "cid",
            "toTable": "cust",
            "toColumn": "id",
        }
    ]
    meta.schemas = [sm]

    out = tmp_path / "o.cypher"
    with Neo4jWriter(settings=None, emit_path=str(out)) as w:
        counts = DbIngestor(w).write_database(meta)
    text = out.read_text()

    assert counts == {"tables": 2, "columns": 2}
    assert "MERGE (n:Database {id: 'mysql://db1/shop'})" in text
    assert "MERGE (n:Table {id: 'mysql://db1/shop/shop/orders'})" in text
    assert "MERGE (n:Column {id: 'mysql://db1/shop/shop/orders#cid'})" in text
    assert "r:FOREIGN_KEY" in text
    assert "r:REFERENCES" in text
    # stored-procedure body + parameters are captured (needed for impact analysis)
    assert "BEGIN UPDATE orders SET total=0; END" in text
    assert "IN cid int" in text


def test_procedure_mapping_includes_definition():
    mx = get_extractor("mysql", host="h", port=3306, user="u", password="")
    proc = mx.map_procedure({"name": "sp_x", "routine_type": "PROCEDURE", "definition": "SELECT 1"})
    assert proc["definition"] == "SELECT 1"
    assert proc["parameters"] == []
    # base procedures_sql must request the body
    assert "ROUTINE_DEFINITION" in mx.procedures_sql("s")[0]
    # MSSQL must use OBJECT_DEFINITION for the full (untruncated) body
    sx = get_extractor("mssql", host="h", port=1, user="u", password="")
    assert "OBJECT_DEFINITION" in sx.procedures_sql("s")[0]
