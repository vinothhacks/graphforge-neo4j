# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Published to PyPI as **`graphforge-neo4j`** (`pip install graphforge-neo4j`).
- Database **connection-URL** input: `graphforge db --url postgresql://user:pass@host:5432/dbname`
  (also `mysql://`, `mssql://`).
- **Auto-discovery**: point at a server without naming a database and graphforge
  graphs every non-system database it finds, reporting tables loaded for each.
- **Local web dashboard**: `graphforge ui` serves a status page (masked config,
  connection health, node counts, per-database/per-repo load status).
- Bundled MySQL / PostgreSQL / SQL Server drivers by default (SQL Server still
  needs a system ODBC driver at run time).
- **Continue-on-error** ingestion: a single failing database/repo/file no longer
  aborts the whole run; failures are collected and summarised at the end.
- GitHub Actions: CI (pytest + ruff) and a Trusted-Publishing release workflow.

### Notes
- Import package and CLI remain `graphforge`; only the PyPI distribution name is
  `graphforge-neo4j` (the name `graphforge` was already taken).

## [0.1.0]

### Added
- Initial release: Git (structure + history) and relational-schema (MySQL,
  PostgreSQL, SQL Server) ingestion into Neo4j; optional code↔DB linking; a
  read-only MCP server; emit / dry-run / push writer modes.
