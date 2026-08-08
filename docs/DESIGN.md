# graphforge — design & roadmap

## What it is

graphforge ingests two kinds of source into a single Neo4j knowledge graph and
exposes that graph to any MCP client:

- **Git** — code structure (modules, packages, files, classes, methods,
  dependencies) and commit history (commits, authors, branches, tags, file
  changes), sharing `:File` nodes.
- **Databases** — relational schema (databases, schemas, tables, columns,
  indexes, views, stored procedures, foreign keys) for MySQL, PostgreSQL, and
  SQL Server.

The two subgraphs are kept separate by default; `graphforge link` optionally
connects them.

## Architecture

```
source ──► extract ──────────► graph operations ──► Neo4jWriter ──► Neo4j
           (git / db / vds)    (parameterized       │  push  ├─► live database
                                 MERGE ops)          │  emit  ├─► .cypher script
                                                     │  dry   └─► count only
```

- `core/` — env config, Cypher operation builders, the three-mode Neo4j writer,
  bundled graph schema.
- `git/` — clone/pull, history extraction, file scanner, language parsers,
  ingestor.
- `db/` — a base `SchemaExtractor` over ANSI `INFORMATION_SCHEMA` plus per-dialect
  subclasses; ingestor; optional VDS/service-catalog importer.
- `link/` — Cypher passes that connect the code and database subgraphs.
- `mcp/` — a read-only MCP server and its query helper.
- `cli.py` — the `graphforge` command.

## Design decisions

- **Deterministic `id` keys + MERGE everywhere.** Every node is keyed on a
  computed `id`, so re-ingesting updates in place instead of duplicating. This
  also keeps the schema Community-Edition compatible (single-property
  uniqueness, no enterprise node keys).
- **Three writer modes.** `push` (live Neo4j), `emit` (a replayable `.cypher`
  script, no DB needed), and `dry-run` (count only) — so the whole pipeline can
  be exercised and reviewed without a running database.
- **Subgraphs stay separate until asked.** Git and DB graphs are independent;
  linking is an explicit, opt-in pass because the cross-domain edges are partly
  heuristic.
- **Env-based config, no secrets in the repo.** All hosts/credentials come from
  the environment or gitignored config files.

## What's implemented

- Git: structure + full history; semantic labels (`Entity` / `ManagedBean` /
  `EJBBean`) from annotations; `mappedTable` for JPA entities.
- DB: MySQL / PostgreSQL / SQL Server; tables, columns, indexes, views, stored
  procedures (with body + parameters), foreign keys.
- Linking: `MAPS_TO`, `BASED_ON`, `USES_TABLE`, `CROSS_DB_REFERENCE`.
- Orchestration: `--replace` (prune-then-reingest), per-repository status,
  `graphforge status`, incremental `--since` GitLab discovery.
- MCP: `get_schema`, `read_cypher` (read-only), `search_nodes`, `node_neighbors`,
  `find_code`, `find_table`, `find_procedure`, `impact_of_column`.
- Optional VDS / service-catalog importer.

## Roadmap

- Per-file incremental diffing (finer than whole-subgraph `--replace`).
- More first-class language parsers (the current Java parser is regex-based).
- Optional quality/coverage overlays attached to `File` / `Class` nodes.
- Multi-database topologies (per-domain graphs, per-domain MCP aliases).
- Optional AST-based parsing for higher-fidelity code structure.

## Known limitations

- The Java parser is regex-based, not a compiler; method/argument resolution is
  best-effort.
- Text-based link passes (`BASED_ON` / `USES_TABLE` / `CROSS_DB_REFERENCE`) are
  substring matches over stored SQL definitions — expect occasional false
  positives on short table names. `MAPS_TO` is exact.
- The graph indexes **schema and structure, not row data** — it is not a
  substitute for querying the live database for actual records.
- The graph is a point-in-time snapshot; freshness depends on how often you
  re-ingest.
