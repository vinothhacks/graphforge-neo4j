"""PostgreSQL schema extractor."""
from __future__ import annotations

from typing import List, Tuple

from .base import SchemaExtractor, parse_index_columns


class PostgresExtractor(SchemaExtractor):
    engine = "postgres"
    placeholder = "%s"
    default_schema_is_database = False  # real schemas within a database

    def connect(self, database: str):
        try:
            import psycopg2
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "psycopg2 is not installed. Run: pip install psycopg2-binary"
            ) from exc
        return psycopg2.connect(
            host=self.host, port=self.port, user=self.user,
            password=self.password, dbname=database or "postgres",
        )

    def list_databases(self) -> list:
        # PostgreSQL requires connecting to an existing database to enumerate the rest.
        conn = self.connect("postgres")
        try:
            cur = conn.cursor()
            cur.execute("SELECT datname FROM pg_database WHERE datistemplate = false")
            return [r[0] for r in cur.fetchall()]
        finally:
            conn.close()

    def schemas(self, database: str, cursor) -> List[str]:
        if self.schema_filter:
            return self.schema_filter
        cursor.execute(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name NOT IN ('information_schema','pg_catalog','pg_toast')"
        )
        return [r[0] for r in cursor.fetchall()]

    def indexes_sql(self, schema: str) -> Tuple[str, tuple]:
        return (
            "SELECT tablename AS table_name, indexname AS name, indexdef AS indexdef "
            f"FROM pg_indexes WHERE schemaname = {self.placeholder} "
            "ORDER BY tablename, indexname",
            (schema,),
        )

    def map_index(self, row: dict) -> dict:
        indexdef = row.get("indexdef", "") or ""
        return {
            "name": row["name"], "table": row["table_name"],
            "isUnique": "UNIQUE INDEX" in indexdef.upper(),
            "indexType": "btree",
            "columns": parse_index_columns(indexdef),
        }
