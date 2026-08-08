"""DB connection-URL parsing, auto-discovery, and continue-on-error ingestion."""
import pytest

import graphforge.db as dbmod
from graphforge.core.neo4j_writer import Neo4jWriter
from graphforge.db import parse_db_url
from graphforge.db.base import DatabaseMeta, SchemaMeta
from graphforge.db.ingest import DbIngestor


def test_parse_db_url_with_database():
    s = parse_db_url("postgresql://ro:sec@dbhost:5432/analytics")
    assert s["engine"] == "postgresql"
    assert s["host"] == "dbhost" and s["port"] == 5432
    assert s["user"] == "ro" and s["password"] == "sec"
    assert s["databases"] == ["analytics"]


def test_parse_db_url_without_database_triggers_autodiscover():
    s = parse_db_url("mysql://root@127.0.0.1/")
    assert s["port"] == 3306               # default port filled in
    assert s["databases"] == []            # empty -> auto-discover


def test_parse_db_url_rejects_unknown_scheme():
    with pytest.raises(ValueError):
        parse_db_url("oracle://x/y")


class _FakeExtractor:
    """Stands in for a live DB: lists two databases; 'boom' fails to connect."""
    engine = "mysql"

    def __init__(self, **_kw):
        pass

    def list_databases(self):
        return ["alpha", "beta"]

    def extract_database(self, database):
        if database == "boom":
            raise RuntimeError("cannot connect to boom")
        meta = DatabaseMeta(name=database, engine="mysql", host="h")
        sm = SchemaMeta(name=database)
        sm.tables = [{"name": "t1"}]
        sm.columns = [{"table": "t1", "name": "c1", "ordinal": 1,
                       "dataType": "int", "isNullable": False}]
        meta.schemas = [sm]
        return meta


def _run(sources, tmp_path):
    original = dbmod.get_extractor
    dbmod.get_extractor = lambda engine, **kw: _FakeExtractor(**kw)
    try:
        out = tmp_path / "o.cypher"
        with Neo4jWriter(settings=None, emit_path=str(out)) as w:
            stats = DbIngestor(w).ingest_sources(sources)
        return stats, out.read_text()
    finally:
        dbmod.get_extractor = original


def test_autodiscovers_all_databases(tmp_path):
    stats, text = _run([{"engine": "mysql", "host": "h", "databases": []}], tmp_path)
    assert stats["databases"] == 2
    assert "mysql://h/alpha" in text and "mysql://h/beta" in text


def test_continue_on_error(tmp_path):
    stats, text = _run(
        [{"engine": "mysql", "host": "h", "databases": ["alpha", "boom", "beta"]}], tmp_path)
    assert stats["databases"] == 2     # alpha + beta succeeded
    assert stats.get("failed") == 1    # boom failed but did not abort the run
    assert "mysql://h/beta" in text    # run continued past the failure
