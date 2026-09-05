"""MySQL / MariaDB schema extractor."""

from __future__ import annotations

from typing import ClassVar

from .base import SchemaExtractor


class MySQLExtractor(SchemaExtractor):
    engine = "mysql"
    placeholder = "%s"
    default_schema_is_database = True  # one schema, named after the database

    _SYSTEM_DBS: ClassVar[set[str]] = {"information_schema", "mysql", "performance_schema", "sys"}

    def connect(self, database: str):
        try:
            import mysql.connector
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "MySQL support is not installed. Run: pip install 'graphforge-neo4j[mysql]'"
            ) from exc
        kwargs = {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
        }
        if database:  # omit to connect at server level (for discovery)
            kwargs["database"] = database
        return mysql.connector.connect(**kwargs)

    def list_databases(self) -> list:
        conn = self.connect("")
        try:
            cur = conn.cursor()
            cur.execute("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA")
            return [r[0] for r in cur.fetchall() if r[0] not in self._SYSTEM_DBS]
        finally:
            conn.close()

    def quote_ident(self, name: str) -> str:
        return "`" + name.replace("`", "``") + "`"

    def columns_sql(self, schema: str) -> tuple[str, tuple]:
        # MySQL exposes COLUMN_KEY (PRI/UNI/MUL) and EXTRA (auto_increment, ...)
        return (
            "SELECT TABLE_NAME AS table_name, COLUMN_NAME AS name, ORDINAL_POSITION AS ordinal, "
            "DATA_TYPE AS data_type, IS_NULLABLE AS is_nullable, COLUMN_DEFAULT AS column_default, "
            "COLUMN_KEY AS column_key, EXTRA AS extra "
            f"FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = {self.placeholder} "
            "ORDER BY TABLE_NAME, ORDINAL_POSITION",
            (schema,),
        )

    def indexes_sql(self, schema: str) -> tuple[str, tuple]:
        return (
            "SELECT TABLE_NAME AS table_name, INDEX_NAME AS name, NON_UNIQUE AS non_unique, "
            "INDEX_TYPE AS index_type, "
            "GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) AS columns "
            f"FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = {self.placeholder} "
            "GROUP BY TABLE_NAME, INDEX_NAME, NON_UNIQUE, INDEX_TYPE "
            "ORDER BY TABLE_NAME, INDEX_NAME",
            (schema,),
        )

    def map_index(self, row: dict) -> dict:
        cols = row.get("columns") or ""
        return {
            "name": row["name"],
            "table": row["table_name"],
            "isUnique": not bool(int(row.get("non_unique") or 0)),
            "indexType": row.get("index_type", ""),
            "columns": [c for c in cols.split(",") if c],
        }

    def foreign_keys_sql(self, schema: str) -> tuple[str, tuple]:
        return (
            "SELECT CONSTRAINT_NAME AS constraint_name, TABLE_NAME AS from_table, "
            "COLUMN_NAME AS from_column, REFERENCED_TABLE_NAME AS to_table, "
            "REFERENCED_COLUMN_NAME AS to_column "
            "FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE "
            f"WHERE TABLE_SCHEMA = {self.placeholder} AND REFERENCED_TABLE_NAME IS NOT NULL "
            "ORDER BY TABLE_NAME",
            (schema,),
        )
