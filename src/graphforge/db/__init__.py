"""Database ingestion: relational schema -> graph for MySQL, PostgreSQL, MSSQL."""
from __future__ import annotations

from urllib.parse import unquote, urlparse

from .base import DatabaseMeta, SchemaExtractor, SchemaMeta
from .ingest import DbIngestor
from .mssql import MssqlExtractor
from .mysql import MySQLExtractor
from .postgres import PostgresExtractor

_ENGINES = {
    "mysql": MySQLExtractor, "mariadb": MySQLExtractor,
    "postgres": PostgresExtractor, "postgresql": PostgresExtractor, "pg": PostgresExtractor,
    "mssql": MssqlExtractor, "sqlserver": MssqlExtractor, "sql-server": MssqlExtractor,
}

_DEFAULT_PORTS = {"mysql": 3306, "mariadb": 3306, "postgres": 5432, "postgresql": 5432,
                  "pg": 5432, "mssql": 1433, "sqlserver": 1433, "sql-server": 1433}


def get_extractor(engine: str, **kwargs) -> SchemaExtractor:
    """Construct a dialect extractor by engine name."""
    key = (engine or "").lower()
    if key not in _ENGINES:
        raise ValueError(
            f"unsupported db engine {engine!r}; choose from {sorted(set(_ENGINES))}"
        )
    return _ENGINES[key](**kwargs)


def parse_db_url(url: str) -> dict:
    """Parse a database URL into a source dict.

    Examples:
        postgresql://user:pass@host:5432/analytics
        mysql://root@127.0.0.1/          (no database -> auto-discover)
        mssql://sa:pw@host:1433/Sales
    """
    p = urlparse(url)
    engine = (p.scheme or "").lower()
    if engine not in _ENGINES:
        raise ValueError(f"unsupported db url scheme {p.scheme!r}; "
                         f"use one of {sorted(set(_ENGINES))}")
    database = (p.path or "").lstrip("/") or None
    return {
        "engine": engine,
        "host": p.hostname or "127.0.0.1",
        "port": p.port or _DEFAULT_PORTS.get(engine, 0),
        "user": unquote(p.username) if p.username else "",
        "password": unquote(p.password) if p.password else "",
        "databases": [database] if database else [],
    }


__all__ = [
    "DatabaseMeta",
    "DbIngestor",
    "MssqlExtractor",
    "MySQLExtractor",
    "PostgresExtractor",
    "SchemaExtractor",
    "SchemaMeta",
    "get_extractor",
    "parse_db_url",
]
