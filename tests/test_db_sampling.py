"""Opt-in row/cardinality sampling (--sample-rows) and the schema filter."""
import os

from graphforge.cli import _resolve_db_sources, build_parser
from graphforge.core.config import DbSettings
from graphforge.core.neo4j_writer import Neo4jWriter
from graphforge.db import get_extractor
from graphforge.db.base import DatabaseMeta, SchemaMeta
from graphforge.db.ingest import DbIngestor


class _FakeCursor:
    """Answers COUNT(*) / COUNT(DISTINCT …) from a table; raises where told to."""

    def __init__(self, rows=None, distinct=None, fail=()):
        self.rows = rows or {}
        self.distinct = distinct or {}
        self.fail = set(fail)
        self.executed = []
        self._value = None

    def execute(self, sql, params=None):
        self.executed.append(sql)
        table = sql.split("FROM ")[-1].strip()
        if any(token in sql for token in self.fail):
            raise RuntimeError("permission denied")
        if "COUNT(DISTINCT" in sql:
            column = sql.split("COUNT(DISTINCT ")[1].split(")")[0]
            self._value = self.distinct.get((table, column))
        else:
            self._value = self.rows.get(table)

    def fetchone(self):
        return None if self._value is None else (self._value,)


def _schema():
    sm = SchemaMeta(name="shop")
    sm.tables = [{"name": "orders"}, {"name": "events"}]
    sm.columns = [
        {"table": "orders", "name": "id"},
        {"table": "orders", "name": "status"},
        {"table": "events", "name": "id"},
    ]
    return sm


def _mysql(**kw):
    return get_extractor("mysql", host="h", port=3306, user="u", password="", **kw)


def _sources(argv, settings=None):
    """Resolve CLI args to db source dicts, ignoring any ambient DB_URL."""
    previous = os.environ.pop("DB_URL", None)
    try:
        return _resolve_db_sources(build_parser().parse_args(argv),
                                   settings or DbSettings())
    finally:
        if previous is not None:
            os.environ["DB_URL"] = previous


# ---- off by default -------------------------------------------------------
def test_sampling_is_off_by_default():
    assert _mysql().sample_rows == 0
    cursor = _FakeCursor()
    sm = _schema()
    _mysql().sample_statistics(cursor, "shop", sm)
    assert cursor.executed == []                       # no queries at all
    assert "approxRows" not in sm.tables[0]
    assert "approxCardinality" not in sm.columns[0]


def test_cli_sample_rows_defaults_to_zero():
    assert build_parser().parse_args(["db", "--host", "h"]).sample_rows == 0
    assert _sources(["db", "--host", "h"])[0]["sampleRows"] == 0


def test_cli_sample_rows_flows_into_the_source():
    assert _sources(["db", "--host", "h", "--sample-rows", "500"])[0]["sampleRows"] == 500


def test_emit_has_no_approx_properties_when_off(tmp_path):
    meta = DatabaseMeta(name="shop", engine="mysql", host="db1")
    meta.schemas = [_schema()]
    out = tmp_path / "o.cypher"
    with Neo4jWriter(settings=None, emit_path=str(out)) as w:
        DbIngestor(w).write_database(meta)
    text = out.read_text()
    assert "approxRows" not in text and "approxCardinality" not in text


# ---- on -------------------------------------------------------------------
def test_sampling_sets_rows_and_cardinality():
    sm = _schema()
    cursor = _FakeCursor(
        rows={"`shop`.`orders`": 12, "`shop`.`events`": 4},
        distinct={("`shop`.`orders`", "`id`"): 12, ("`shop`.`orders`", "`status`"): 3,
                  ("`shop`.`events`", "`id`"): 4},
    )
    _mysql(sample_rows=1000).sample_statistics(cursor, "shop", sm)

    assert [t["approxRows"] for t in sm.tables] == [12, 4]
    by_col = {(c["table"], c["name"]): c.get("approxCardinality") for c in sm.columns}
    assert by_col == {("orders", "id"): 12, ("orders", "status"): 3, ("events", "id"): 4}


def test_tables_over_the_ceiling_skip_the_distinct_probes():
    sm = _schema()
    cursor = _FakeCursor(rows={"`shop`.`orders`": 5_000_000, "`shop`.`events`": 4},
                         distinct={("`shop`.`events`", "`id`"): 4})
    _mysql(sample_rows=100).sample_statistics(cursor, "shop", sm)

    assert sm.tables[0]["approxRows"] == 5_000_000     # COUNT(*) still recorded
    orders_cols = [c for c in sm.columns if c["table"] == "orders"]
    assert all("approxCardinality" not in c for c in orders_cols)
    assert sm.columns[2]["approxCardinality"] == 4     # small table still profiled


def test_a_failing_probe_degrades_gracefully():
    sm = _schema()
    cursor = _FakeCursor(rows={"`shop`.`events`": 4},
                         distinct={("`shop`.`events`", "`id`"): 4},
                         fail=("`shop`.`orders`",))
    _mysql(sample_rows=1000).sample_statistics(cursor, "shop", sm)   # must not raise

    assert "approxRows" not in sm.tables[0]            # the failing table is skipped
    assert sm.tables[1]["approxRows"] == 4             # the rest is still profiled


def test_sampled_values_reach_the_graph(tmp_path):
    sm = _schema()
    sm.tables[0]["approxRows"] = 12
    sm.columns[1]["approxCardinality"] = 3
    meta = DatabaseMeta(name="shop", engine="mysql", host="db1")
    meta.schemas = [sm]
    out = tmp_path / "o.cypher"
    with Neo4jWriter(settings=None, emit_path=str(out)) as w:
        DbIngestor(w).write_database(meta)
    text = out.read_text()
    assert "n.approxRows = 12" in text
    assert "n.approxCardinality = 3" in text


def test_identifiers_are_quoted_per_dialect_and_validated():
    assert _mysql().qualified_table("shop", "orders") == "`shop`.`orders`"
    pg = get_extractor("postgres", host="h", port=1, user="u", password="")
    assert pg.qualified_table("public", "orders") == '"public"."orders"'
    mssql = get_extractor("mssql", host="h", port=1, user="u", password="")
    assert mssql.qualified_table("dbo", "orders") == "[dbo].[orders]"

    # a table name that is not a plain identifier is skipped, never interpolated
    sm = SchemaMeta(name="shop")
    sm.tables = [{"name": "orders; DROP TABLE x"}]
    cursor = _FakeCursor()
    _mysql(sample_rows=10).sample_statistics(cursor, "shop", sm)
    assert cursor.executed == []


# ---- schema filter (mssql must behave like postgres) ----------------------
class _SchemaCursor:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def execute(self, sql, params=None):
        self.calls += 1

    def fetchall(self):
        return self.rows


def test_explicit_schema_filter_is_honoured():
    for engine in ("mssql", "postgres"):
        x = get_extractor(engine, host="h", port=1, user="u", password="",
                          schemas=["sales", " ops "])
        cursor = _SchemaCursor([("dbo",), ("other",)])
        assert x.schemas("db", cursor) == ["sales", "ops"]   # trimmed, not discovered
        assert cursor.calls == 0


def test_without_a_filter_each_engine_discovers_its_own_schemas():
    mssql = get_extractor("mssql", host="h", port=1, user="u", password="")
    assert mssql.schemas("db", _SchemaCursor([("sales",), ("sys",)])) == ["sales"]
    assert mssql.schemas("db", _SchemaCursor([("sys",)])) == ["dbo"]   # sane fallback
    pg = get_extractor("postgres", host="h", port=1, user="u", password="")
    assert pg.schemas("db", _SchemaCursor([("public",), ("app",)])) == ["public", "app"]


def test_pg_schemas_default_does_not_leak_into_other_engines():
    settings = DbSettings()
    assert settings.pg_schemas == ["public"]
    # PG_SCHEMAS used to be handed to every engine, so an MSSQL run silently
    # looked for a "public" schema and imported nothing.
    assert _sources(["db", "--host", "h", "--engine", "mssql"], settings)[0]["schemas"] == []
    assert _sources(["db", "--host", "h", "--engine", "postgres"], settings)[0]["schemas"] == ["public"]
    assert _sources(["db", "--host", "h", "--engine", "mssql", "--schemas", "sales,ops"],
                    settings)[0]["schemas"] == ["sales", "ops"]
