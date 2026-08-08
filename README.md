# graphforge

**Turn Git repositories and relational databases into a Neo4j knowledge graph — then query it from any MCP client.**

`graphforge` reads two kinds of source and writes them into one graph:

- **Git** — code structure (modules, packages, files, classes, methods, dependencies) **and** commit history (commits, authors, branches, tags, and which files each commit changed). The two share `:File` nodes, so you can pivot from "who last touched this file" to "what classes it declares" in one query.
- **Databases** — the schema of **MySQL, PostgreSQL, and SQL Server**: databases, schemas, tables, columns, indexes, views, stored procedures (with their SQL body and parameters), and foreign keys.

By default the git and database subgraphs live side by side but stay separate. One command (`graphforge link`) optionally connects them — entities to tables, views/procedures to the tables they reference. A built-in **MCP server** then exposes the whole graph to Claude Desktop or any other MCP-speaking agent.

> **Scope:** graphforge indexes *structure and schema*, not table *rows*. It complements a live database — it doesn't replace one for questions about actual data.

---

## Contents

- [Why a graph](#why-a-graph)
- [Install](#install)
- [Quickstart](#quickstart)
- [Try it with no database](#try-it-with-no-database)
- [Commands](#commands)
- [The graph model](#the-graph-model)
- [Query it from an MCP client](#query-it-from-an-mcp-client)
- [Configuration](#configuration)
- [Re-ingesting & incremental updates](#re-ingesting--incremental-updates)
- [Extending](#extending)
- [Limitations](#limitations)
- [Contributing](#contributing) · [License](#license)

---

## Why a graph

Relational schemas and Git histories are both graphs wearing a disguise. Modelling them natively lets you ask questions that are awkward anywhere else:

- *Which classes map to the `orders` table, who last changed them, and which stored procedures also read it?*
- *What's the blast radius of widening one column — foreign keys, indexes, views, and procedures that touch it?*
- *Which files churn the most, and who owns them?*
- *Which classes implement a given interface across hundreds of modules?*

A single Cypher traversal answers each of these. Doing the same with two separate tools means fetching both sides and stitching them together by hand.

---

## Install

```bash
pip install graphforge-neo4j            # Neo4j + MySQL/PostgreSQL/SQL Server drivers bundled
pip install "graphforge-neo4j[mcp]"     # add the MCP server
cp .env.example .env                    # then set your Neo4j password in .env
```

The import package and CLI are both `graphforge` (only the PyPI distribution name is `graphforge-neo4j`). The Neo4j driver and all three database drivers install by default, so any source works out of the box — if a driver is already installed, pip leaves it alone.

From source:

```bash
git clone https://github.com/vinothhacks/graphforge-neo4j.git
cd graphforge-neo4j
pip install -e ".[dev]"
```

Need a graph to write into? `docker compose up -d` starts Neo4j 5 at `bolt://127.0.0.1:7687` (edit the password in `docker-compose.yml` and mirror it in `.env`).

Requires **Python 3.9+** and the `git` CLI. SQL Server additionally needs a system ODBC driver installed.

---

## Quickstart

```bash
graphforge init                                   # create constraints + indexes

# ---- Git ----
graphforge git /path/to/repo --name my-service    # a local checkout
graphforge git https://github.com/octocat/Hello-World.git
graphforge git -c config/repositories.json        # many repos at once

# ---- Databases ----
# Point at one database with a connection URL:
graphforge db --url postgresql://ro:secret@127.0.0.1:5432/analytics
graphforge db --url mysql://ro:secret@127.0.0.1:3306/shop

# ...or omit the database and it graphs EVERY non-system database on the server:
graphforge db --url postgresql://ro:secret@127.0.0.1:5432/

# ...or use explicit flags:
graphforge db --engine mssql --host 127.0.0.1 --port 1433 --user ro --password '***' --databases Sales

# ---- Connect the two (optional) ----
graphforge link                                   # MAPS_TO, BASED_ON, USES_TABLE, CROSS_DB_REFERENCE

# ---- Inspect ----
graphforge ui                                     # web dashboard at http://localhost:8000
graphforge status                                 # per-repository ingest status
graphforge verify                                 # node counts by label

# ---- Serve to an MCP client ----
graphforge mcp                                    # stdio MCP server
```

---

## Try it with no database

Every ingest command has two offline modes, so you can see exactly what would be written before touching Neo4j:

```bash
graphforge git /path/to/repo --emit repo.cypher   # write a replayable .cypher script
graphforge git /path/to/repo --dry-run            # just count the operations
```

`--emit` runs the full pipeline (clone → scan → history → generate) and serializes the result as literal Cypher you can review or replay in the Neo4j Browser / cypher-shell. Neither mode needs a running database or the Neo4j driver.

---

## Commands

| Command | What it does |
|---------|--------------|
| `graphforge init` | Create the graph's constraints and indexes. |
| `graphforge git [PATHS/URLS…]` | Ingest repositories: code structure + commit history. |
| `graphforge db` | Ingest relational schemas (MySQL / PostgreSQL / SQL Server). |
| `graphforge vds` | Optional: import a virtual-data-service / query catalog (see below). |
| `graphforge link` | Create code↔database edges over the loaded graph. |
| `graphforge status` | Show per-repository ingest status. |
| `graphforge verify` | Report node counts per label. |
| `graphforge ui` | Serve a local web dashboard (masked config + live load status). |
| `graphforge mcp` | Serve the graph over MCP (stdio). |

Flags shared by the ingest commands: `--emit FILE`, `--dry-run`, `--no-schema`, `--replace`, and Neo4j overrides `--neo4j-uri/-user/-password/-database`, plus `--env FILE` to load a specific `.env`. Run any command with `--help` for its full list.

Useful git flags: `--lines` (store per-line `:Line` nodes — heavy), `--no-history` / `--no-structure`, `--since DAYS` (limit GitLab discovery to recently-active repos), `--branch`, `--name`.

---

## The graph model

### Git subgraph

```mermaid
graph LR
  Repo[Repository] -->|HAS_MODULE| Mod[Module]
  Repo -->|HAS_COMMIT| Commit
  Repo -->|HAS_BRANCH| Branch
  Repo -->|HAS_TAG| Tag
  Mod -->|HAS_PACKAGE| Pkg[Package]
  Mod -->|DEPENDS_ON| Dep[Dependency]
  Pkg -->|CONTAINS_FILE| File
  File -->|CONTAINS_CLASS| Class
  File -->|CONTAINS_LINE| Line
  Line -->|NEXT_LINE| Line
  Class -->|HAS_METHOD| Method
  Class -->|EXTENDS / IMPLEMENTS / IMPORTS| Class
  Author -->|AUTHORED| Commit
  Commit -->|PARENT| Commit
  Commit -->|CHANGED| File
  Branch -->|POINTS_TO| Commit
  Tag -->|TAGS| Commit
```

`:Commit -[:CHANGED]-> :File` and `:Package -[:CONTAINS_FILE]-> :File` share the same `:File` nodes — the spine linking history to structure. Classes annotated `@Entity` / `@ManagedBean` / `@Stateless` also get a semantic label (e.g. `:Class:Entity`) and an entity records its `mappedTable`.

### Database subgraph

```mermaid
graph LR
  DB[Database] -->|HAS_SCHEMA| S[Schema]
  S -->|HAS_TABLE| T[Table]
  S -->|HAS_VIEW| V[View]
  S -->|HAS_PROCEDURE| P[StoredProcedure]
  T -->|HAS_COLUMN| C[Column]
  T -->|HAS_INDEX| I[Index]
  C -->|FOREIGN_KEY| C2[Column]
  T -->|REFERENCES| T2[Table]
```

### Optional code ↔ database links (`graphforge link`)

```mermaid
graph LR
  E["Class:Entity"] -->|MAPS_TO| T[Table]
  V[View] -->|BASED_ON| T
  P[StoredProcedure] -->|USES_TABLE| T
  V -->|CROSS_DB_REFERENCE| T2["Table (other DB)"]
```

`MAPS_TO` is an exact JPA table-name match. `BASED_ON` / `USES_TABLE` / `CROSS_DB_REFERENCE` are substring matches over stored view/procedure definitions, scoped by database — run all passes, or select with `--maps-to` / `--based-on` / `--uses-table` / `--cross-db`.

Every node is `MERGE`-keyed on a deterministic `id`, so **re-running an import updates nodes in place instead of duplicating them** — and it works on Neo4j Community Edition.

### Example queries

```cypher
// Top authors by distinct files touched
MATCH (a:Author)-[:AUTHORED]->(:Commit)-[:CHANGED]->(f:File)
RETURN a.name, count(DISTINCT f) AS files ORDER BY files DESC LIMIT 10;

// Highest-churn files
MATCH (:Commit)-[ch:CHANGED]->(f:File)
RETURN f.path, sum(ch.insertions + ch.deletions) AS churn ORDER BY churn DESC LIMIT 10;

// Foreign-key hotspots
MATCH (t:Table)<-[:REFERENCES]-(other:Table)
RETURN t.name, count(other) AS incoming ORDER BY incoming DESC LIMIT 10;

// After `graphforge link`: entities and the tables they map to
MATCH (e:Entity)-[:MAPS_TO]->(t:Table)
RETURN e.fqn, t.name ORDER BY t.name;
```

---

## Query it from an MCP client

```bash
graphforge mcp          # starts a read-only stdio MCP server
```

Merge [`examples/claude_desktop_config.json`](examples/claude_desktop_config.json) into your MCP client's config (fill in the Neo4j password) and restart it. Available tools:

| Tool | Purpose |
|------|---------|
| `get_schema` | labels, relationship types, node counts |
| `read_cypher` | run a **read-only** Cypher query (writes are rejected) |
| `search_nodes` | substring search on a property of a label |
| `node_neighbors` | the immediate neighbourhood of a node `id` |
| `find_code` | locate files / classes / methods by name |
| `find_table` | locate tables / columns by name |
| `find_procedure` | locate stored procedures by name or SQL body |
| `impact_of_column` | blast radius of a column: matching columns, FKs, indexes, and procedures/views that reference it |

---

## Dashboard

```bash
graphforge ui        # then open http://localhost:8000
```

A dependency-free local page (stdlib web server) showing your resolved configuration with **passwords masked**, Neo4j connection health, node counts by label, and per-repository / per-database load status — refreshed on demand. Handy for confirming what's loaded and that your `.env` points where you expect.

## Configuration

Everything is read from the environment (optionally seeded from a local `.env`), and CLI flags override individual values. See [`.env.example`](.env.example) for the full list. Essentials:

```bash
NEO4J_URI=bolt://127.0.0.1:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=...
NEO4J_DATABASE=neo4j          # Community Edition supports only "neo4j"
GF_BATCH_SIZE=500             # statements per write transaction
GF_INCLUDE_LINES=false        # store per-line :Line nodes (heavy; opt-in)
```

For repeatable runs, describe your sources in gitignored config files — copy the `config/*.example.json` templates. Database passwords are pulled from environment variables named by each source's `passwordEnv`, never stored in the JSON.

**No secrets in the repo:** `.gitignore` blocks `.env` and `config/*.json`, and CI scans every PR for accidental tokens. Point graphforge at a **read-only** database account.

---

## Re-ingesting & incremental updates

Re-running an ingest is safe and idempotent — `MERGE` updates existing nodes rather than duplicating them. To also remove things that disappeared upstream, add `--replace`, which deletes a repository's or database's existing subgraph before re-ingesting it:

```bash
graphforge git /path/to/repo --name my-service --replace
graphforge db  --engine mysql --databases shop --replace
```

For large GitLab groups, discover and ingest only recently-active repos:

```bash
export GITLAB_SERVER=... GITLAB_GROUP_ID=... GITLAB_TOKEN=...
graphforge git --since 20        # repos active in the last 20 days
```

---

## Extending

**A new database engine** — subclass `graphforge.db.base.SchemaExtractor`, implement the dialect-specific `connect()` and `indexes_sql()` (tables/columns/views/procedures/foreign keys come free from ANSI `INFORMATION_SCHEMA`), and register it in `graphforge.db.get_extractor`.

**A new language parser** — add a module under `graphforge/git/parsers/` exposing `extract(lines) -> dict` and wire its extensions into `graphforge/git/scan.py`. Line classification already covers ~30 file types.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`docs/DESIGN.md`](docs/DESIGN.md).

---

## Limitations

- The Java parser is regex-based (fast, no AST). It captures declarations, imports, and annotations well; it is not a compiler.
- The text-based link passes are heuristic substring matches — expect occasional false positives on short table names. `MAPS_TO` is exact.
- The graph indexes **schema and structure, not row data** — use your database for questions about actual records.
- The graph is a point-in-time snapshot; freshness depends on how often you re-ingest.

---

## Contributing

Issues and PRs welcome. Run the test suite and linter before submitting:

```bash
pip install -e ".[dev]"
pytest
ruff check src tests
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full guidelines (including the no-secrets rule).

## License

MIT — see [`LICENSE`](LICENSE).
