"""Abstract relational-schema extractor built on INFORMATION_SCHEMA.

A concrete dialect subclass provides a live database connection plus the few
queries that are not portable (indexes, and — for MySQL — foreign keys and
column keys). Everything else (tables, columns, views, procedures, standard
foreign keys) comes from ANSI INFORMATION_SCHEMA in this base class.

Extractors return plain normalized dicts so the graph mapping in
``db.ingest`` can be unit-tested with a fake extractor and no live database.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("graphforge.db")


@dataclass
class SchemaMeta:
    name: str
    tables: list[dict] = field(default_factory=list)
    columns: list[dict] = field(default_factory=list)
    indexes: list[dict] = field(default_factory=list)
    views: list[dict] = field(default_factory=list)
    procedures: list[dict] = field(default_factory=list)
    foreign_keys: list[dict] = field(default_factory=list)


@dataclass
class DatabaseMeta:
    name: str
    engine: str
    host: str
    schemas: list[SchemaMeta] = field(default_factory=list)


class SchemaExtractor:
    engine: str = "generic"
    placeholder: str = "%s"  # DB-API paramstyle marker
    default_schema_is_database: bool = True  # MySQL-style: one schema == the database

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        driver: str | None = None,
        schemas: list[str] | None = None,
        sample_rows: int = 0,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.driver = driver
        self.schema_filter = [s.strip() for s in (schemas or []) if s and s.strip()]
        # 0 == off. When > 0 it doubles as the row ceiling under which the
        # (more expensive) per-column COUNT(DISTINCT) probes are also run.
        self.sample_rows = int(sample_rows or 0)

    # -- connection (dialect-specific) ------------------------------------
    def connect(self, database: str):
        raise NotImplementedError

    def list_databases(self) -> list[str]:
        """Return non-system database names on the server (for auto-discovery)."""
        raise NotImplementedError(f"{self.engine} does not support auto-discovery")

    # -- schema discovery --------------------------------------------------
    def schemas(self, database: str, cursor) -> list[str]:
        """Which schema names to import within a connected database."""
        if self.default_schema_is_database:
            return [database]
        return list(self.schema_filter) if self.schema_filter else self._all_schemas(cursor)

    def _all_schemas(self, cursor) -> list[str]:
        cursor.execute(
            "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA "
            "WHERE SCHEMA_NAME NOT IN ('information_schema','pg_catalog',"
            "'sys','mysql','performance_schema')"
        )
        return [r[0] for r in cursor.fetchall()]

    # -- portable INFORMATION_SCHEMA queries ------------------------------
    def tables_sql(self, schema: str) -> tuple[str, tuple]:
        return (
            "SELECT TABLE_NAME AS name FROM INFORMATION_SCHEMA.TABLES "
            f"WHERE TABLE_SCHEMA = {self.placeholder} AND TABLE_TYPE = 'BASE TABLE' "
            "ORDER BY TABLE_NAME",
            (schema,),
        )

    def columns_sql(self, schema: str) -> tuple[str, tuple]:
        return (
            "SELECT TABLE_NAME AS table_name, COLUMN_NAME AS name, ORDINAL_POSITION AS ordinal, "
            "DATA_TYPE AS data_type, IS_NULLABLE AS is_nullable, COLUMN_DEFAULT AS column_default "
            f"FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = {self.placeholder} "
            "ORDER BY TABLE_NAME, ORDINAL_POSITION",
            (schema,),
        )

    def views_sql(self, schema: str) -> tuple[str, tuple]:
        return (
            "SELECT TABLE_NAME AS name, VIEW_DEFINITION AS definition "
            f"FROM INFORMATION_SCHEMA.VIEWS WHERE TABLE_SCHEMA = {self.placeholder} "
            "ORDER BY TABLE_NAME",
            (schema,),
        )

    def procedures_sql(self, schema: str) -> tuple[str, tuple]:
        # ROUTINE_DEFINITION carries the SQL body (needed for impact analysis).
        return (
            "SELECT ROUTINE_NAME AS name, ROUTINE_TYPE AS routine_type, "
            "ROUTINE_DEFINITION AS definition "
            f"FROM INFORMATION_SCHEMA.ROUTINES WHERE ROUTINE_SCHEMA = {self.placeholder} "
            "ORDER BY ROUTINE_NAME",
            (schema,),
        )

    def parameters_sql(self, schema: str) -> tuple[str, tuple]:
        return (
            "SELECT SPECIFIC_NAME AS routine, PARAMETER_NAME AS name, "
            "DATA_TYPE AS data_type, PARAMETER_MODE AS mode, ORDINAL_POSITION AS ordinal "
            f"FROM INFORMATION_SCHEMA.PARAMETERS WHERE SPECIFIC_SCHEMA = {self.placeholder} "
            "ORDER BY SPECIFIC_NAME, ORDINAL_POSITION",
            (schema,),
        )

    def indexes_sql(self, schema: str) -> tuple[str, tuple]:
        raise NotImplementedError  # dialect-specific

    def foreign_keys_sql(self, schema: str) -> tuple[str, tuple]:
        # ANSI standard join; MySQL overrides with its simpler REFERENCED_* columns.
        return (
            "SELECT rc.CONSTRAINT_NAME AS constraint_name, "
            "kcu.TABLE_NAME AS from_table, kcu.COLUMN_NAME AS from_column, "
            "kcu2.TABLE_NAME AS to_table, kcu2.COLUMN_NAME AS to_column "
            "FROM INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc "
            "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu "
            "  ON rc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME "
            "  AND rc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA "
            "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu2 "
            "  ON rc.UNIQUE_CONSTRAINT_NAME = kcu2.CONSTRAINT_NAME "
            "  AND rc.UNIQUE_CONSTRAINT_SCHEMA = kcu2.CONSTRAINT_SCHEMA "
            "  AND kcu.ORDINAL_POSITION = kcu2.ORDINAL_POSITION "
            f"WHERE kcu.TABLE_SCHEMA = {self.placeholder}",
            (schema,),
        )

    # -- row mappers (override to normalise dialect quirks) ----------------
    def map_column(self, row: dict) -> dict:
        return {
            "table": row["table_name"],
            "name": row["name"],
            "ordinal": row.get("ordinal"),
            "dataType": row.get("data_type", ""),
            "isNullable": str(row.get("is_nullable", "")).upper() == "YES",
            "default": _s(row.get("column_default")),
            "columnKey": row.get("column_key", "") or "",
            "extra": row.get("extra", "") or "",
        }

    def map_index(self, row: dict) -> dict:
        raise NotImplementedError

    def map_view(self, row: dict) -> dict:
        return {"name": row["name"], "definition": _s(row.get("definition"))[:10000]}

    def map_procedure(self, row: dict) -> dict:
        return {
            "name": row["name"],
            "type": row.get("routine_type", "PROCEDURE"),
            "definition": _s(row.get("definition"))[:50000],
            "parameters": [],
        }

    def map_foreign_key(self, row: dict) -> dict:
        return {
            "constraintName": row.get("constraint_name", ""),
            "fromTable": row["from_table"],
            "fromColumn": row["from_column"],
            "toTable": row["to_table"],
            "toColumn": row["to_column"],
        }

    # -- driver loop -------------------------------------------------------
    @staticmethod
    def _rows(cursor, sql_params: tuple[str, tuple]) -> list[dict]:
        sql, params = sql_params
        cursor.execute(sql, params)
        cols = [d[0].lower() for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def _attach_parameters(self, cursor, schema: str, procedures: list[dict]) -> None:
        """Group INFORMATION_SCHEMA.PARAMETERS rows onto their procedures."""
        if not procedures:
            return
        try:
            rows = self._rows(cursor, self.parameters_sql(schema))
        except Exception as exc:  # noqa: BLE001  # PARAMETERS may be restricted; DB-API drivers raise dialect-specific errors
            log.warning("parameter extraction failed for %s: %s", schema, exc)
            return
        by_routine: dict[str, list[str]] = {}
        for r in rows:
            if not r.get("name"):  # skip function return rows
                continue
            sig = f"{(r.get('mode') or '').strip()} {r['name']} {r.get('data_type', '')}".strip()
            by_routine.setdefault(r["routine"], []).append(sig)
        for proc in procedures:
            proc["parameters"] = by_routine.get(proc["name"], [])

    # -- optional row / cardinality sampling -------------------------------
    def quote_ident(self, name: str) -> str:
        """Quote an identifier for this dialect (ANSI double quotes by default)."""
        return '"' + name.replace('"', '""') + '"'

    def qualified_table(self, schema: str, table: str) -> str:
        return f"{self.quote_ident(schema)}.{self.quote_ident(table)}"

    def _scalar(self, cursor, sql: str):
        """Run a single-value query, returning None if the database refuses it."""
        try:
            cursor.execute(sql)
            row = cursor.fetchone()
        except Exception as exc:  # noqa: BLE001  # sampling is best-effort by design
            log.warning("sampling query failed (%s): %s", sql, exc)
            return None
        if not row:
            return None
        value = row[0] if not isinstance(row, dict) else next(iter(row.values()))
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def sample_statistics(self, cursor, schema: str, meta: SchemaMeta) -> None:
        """Annotate tables with ``approxRows`` and columns with ``approxCardinality``.

        Opt-in (``sample_rows`` > 0) because it issues one COUNT(*) per table and,
        for tables at or below that ceiling, one COUNT(DISTINCT col) per column.
        Any failing probe is logged and skipped — never fatal.
        """
        if self.sample_rows <= 0 or not _safe_ident(schema):
            return
        columns_by_table: dict[str, list[dict]] = {}
        for col in meta.columns:
            columns_by_table.setdefault(col.get("table", ""), []).append(col)

        for table in meta.tables:
            name = table.get("name", "")
            if not _safe_ident(name):
                continue
            target = self.qualified_table(schema, name)
            rows = self._scalar(cursor, f"SELECT COUNT(*) FROM {target}")
            if rows is None:
                continue
            table["approxRows"] = rows
            if rows > self.sample_rows:
                log.debug(
                    "%s.%s has %d rows (> %d); skipping cardinality probes",
                    schema,
                    name,
                    rows,
                    self.sample_rows,
                )
                continue
            for col in columns_by_table.get(name, []):
                cname = col.get("name", "")
                if not _safe_ident(cname):
                    continue
                distinct = self._scalar(
                    cursor, f"SELECT COUNT(DISTINCT {self.quote_ident(cname)}) FROM {target}"
                )
                if distinct is not None:
                    col["approxCardinality"] = distinct

    def extract_database(self, database: str) -> DatabaseMeta:
        conn = self.connect(database)
        meta = DatabaseMeta(name=database, engine=self.engine, host=self.host)
        try:
            cursor = conn.cursor()
            for schema in self.schemas(database, cursor):
                sm = SchemaMeta(name=schema)
                sm.tables = [
                    {"name": r["name"]} for r in self._rows(cursor, self.tables_sql(schema))
                ]
                sm.columns = [
                    self.map_column(r) for r in self._rows(cursor, self.columns_sql(schema))
                ]
                sm.views = [self.map_view(r) for r in self._rows(cursor, self.views_sql(schema))]
                sm.procedures = [
                    self.map_procedure(r) for r in self._rows(cursor, self.procedures_sql(schema))
                ]
                self._attach_parameters(cursor, schema, sm.procedures)
                try:
                    sm.indexes = [
                        self.map_index(r) for r in self._rows(cursor, self.indexes_sql(schema))
                    ]
                except Exception as exc:  # noqa: BLE001  # indexes are best-effort; keep the rest of the schema
                    log.warning("index extraction failed for %s.%s: %s", database, schema, exc)
                try:
                    sm.foreign_keys = [
                        self.map_foreign_key(r)
                        for r in self._rows(cursor, self.foreign_keys_sql(schema))
                    ]
                except Exception as exc:  # noqa: BLE001  # FKs are best-effort; keep the rest of the schema
                    log.warning("FK extraction failed for %s.%s: %s", database, schema, exc)
                self.sample_statistics(cursor, schema, sm)
                meta.schemas.append(sm)
        finally:
            conn.close()
        return meta


def _s(value: Any) -> str:
    return "" if value is None else str(value)


def _safe_ident(name: str) -> bool:
    """Only plain identifiers may be interpolated into a sampling query."""
    return bool(name) and bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name))


def parse_index_columns(indexdef: str) -> list[str]:
    """Extract column names from a PostgreSQL CREATE INDEX definition."""
    match = re.search(r"\((.*)\)", indexdef or "")
    if not match:
        return []
    cols = []
    for part in match.group(1).split(","):
        token = part.strip().strip('"').split(" ")[0]
        if token:
            cols.append(token)
    return cols
