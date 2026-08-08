"""Microsoft SQL Server schema extractor (via pyodbc)."""
from __future__ import annotations

from .base import SchemaExtractor

_SYSTEM_SCHEMAS = {
    "sys", "INFORMATION_SCHEMA", "guest", "db_owner", "db_accessadmin",
    "db_securityadmin", "db_ddladmin", "db_backupoperator", "db_datareader",
    "db_datawriter", "db_denydatareader", "db_denydatawriter",
}


class MssqlExtractor(SchemaExtractor):
    engine = "mssql"
    placeholder = "?"                 # pyodbc paramstyle
    default_schema_is_database = False

    def connect(self, database: str):
        try:
            import pyodbc
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "pyodbc is not installed. Run: pip install 'graphforge[mssql]' "
                "(a system ODBC driver for SQL Server is also required)."
            ) from exc
        driver = self.driver or "ODBC Driver 18 for SQL Server"
        conn_str = (
            f"DRIVER={{{driver}}};SERVER={self.host},{self.port};DATABASE={database or 'master'};"
            f"UID={self.user};PWD={self.password};TrustServerCertificate=yes"
        )
        return pyodbc.connect(conn_str)

    def list_databases(self) -> list:
        conn = self.connect("master")
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sys.databases WHERE database_id > 4")
            return [r[0] for r in cur.fetchall()]
        finally:
            conn.close()

    def quote_ident(self, name: str) -> str:
        return "[" + name.replace("]", "]]") + "]"

    def schemas(self, database: str, cursor) -> list[str]:
        """Honour an explicit --schemas filter, exactly as PostgreSQL does."""
        if self.schema_filter:
            return list(self.schema_filter)
        cursor.execute("SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA")
        rows = [r[0] for r in cursor.fetchall() if r[0] not in _SYSTEM_SCHEMAS]
        return rows or ["dbo"]

    def indexes_sql(self, schema: str) -> tuple[str, tuple]:
        return (
            "SELECT t.name AS table_name, i.name AS name, i.is_unique AS is_unique, "
            "i.type_desc AS index_type, STRING_AGG(c.name, ',') AS columns "
            "FROM sys.indexes i "
            "JOIN sys.tables t ON i.object_id = t.object_id "
            "JOIN sys.schemas s ON t.schema_id = s.schema_id "
            "JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id "
            "JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id "
            f"WHERE i.name IS NOT NULL AND s.name = {self.placeholder} "
            "GROUP BY t.name, i.name, i.is_unique, i.type_desc",
            (schema,),
        )

    def map_index(self, row: dict) -> dict:
        cols = row.get("columns") or ""
        return {
            "name": row["name"], "table": row["table_name"],
            "isUnique": bool(row.get("is_unique")),
            "indexType": row.get("index_type", ""),
            "columns": [c for c in cols.split(",") if c],
        }

    def procedures_sql(self, schema: str) -> tuple[str, tuple]:
        # INFORMATION_SCHEMA.ROUTINE_DEFINITION is truncated at 4000 chars on
        # SQL Server; OBJECT_DEFINITION returns the full body.
        return (
            "SELECT ROUTINE_NAME AS name, ROUTINE_TYPE AS routine_type, "
            "OBJECT_DEFINITION(OBJECT_ID(QUOTENAME(ROUTINE_SCHEMA)+'.'+QUOTENAME(ROUTINE_NAME))) AS definition "
            f"FROM INFORMATION_SCHEMA.ROUTINES WHERE ROUTINE_SCHEMA = {self.placeholder} "
            "ORDER BY ROUTINE_NAME",
            (schema,),
        )
