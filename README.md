# graphforge

**Turn Git repositories and relational databases into a Neo4j knowledge graph — then query it from any MCP client.**

> Like Sourcegraph for your schema *and* your history — except the index is a graph you query with Cypher, and it is served to your agent over MCP.

`graphforge` reads two kinds of source and writes them into one graph:

- **Git** — code structure (modules, packages, files, classes, methods, dependencies) **and** commit history (commits, authors, branches, tags, and which files each commit changed). The two share `:File` nodes, so you can pivot from "who last touched this file" to "what classes it declares" in one query. Structure is parsed for **Java, Python, TypeScript/JavaScript, and Go**.
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
- [Language coverage](#language-coverage)
- [The graph model](#the-graph-model)
- [Recipes](#recipes)
- [Query it from an MCP client](#query-it-from-an-mcp-client)
- [Dashboard](#dashboard)
- [Configuration](#configuration)
- [Re-ingesting & incremental updates](#re-ingesting--incremental-updates)
- [Examples](#examples)
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

Requires **Python 3.10+** and the `git` CLI. SQL Server additionally needs a system ODBC driver installed.

---

## Quickstart

```bash
graphforge init                                   # create constraints + indexes

# ---- Git ----
graphforge git /path/to/repo --name my-service    # a local checkout
graphforge git https://github.com/octocat/Hello-World.git
graphforge git -c config/repositories.json        # many repos at once
graphforge git /path/to/repo --since-commit auto  # only what's new since the last run

# ---- Databases ----
# Point at one database with a connection URL:
graphforge db --url postgresql://ro:secret@127.0.0.1:5432/analytics
graphforge db --url mysql://ro:secret@127.0.0.1:3306/shop

# ...or omit the database and it graphs EVERY non-system database on the server:
graphforge db --url postgresql://ro:secret@127.0.0.1:5432/

# ...or use explicit flags:
graphforge db --engine mssql --host 127.0.0.1 --port 1433 --user ro --password '***' --databases Sales

# ...opt in to lightweight size profiling (COUNT(*) per table):
graphforge db --url postgresql://ro:secret@127.0.0.1:5432/analytics --sample-rows 100000

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
| `graphforge ui` | Serve the local web dashboard (graph canvas, Cypher console, masked config + live load status). |
| `graphforge mcp` | Serve the graph over MCP (stdio). |

Flags shared by the ingest commands: `--emit FILE`, `--dry-run`, `--no-schema`, `--replace`, and Neo4j overrides `--neo4j-uri/-user/-password/-database`, plus `--env FILE` to load a specific `.env`. Run any command with `--help` for its full list.

**`graphforge git`**

| Flag | What it does |
|------|--------------|
| `--name NAME` / `--branch BRANCH` | Name / branch for a single repo passed positionally. |
| `--since-commit SHA` | Incremental ingest: only commits after `SHA`. Pass `auto` to continue from `:Repository.lastCommit`. |
| `--since DAYS` | GitLab discovery: only repos active in the last N days. |
| `--lines` | Store per-line `:Line` nodes — heavy, opt-in. |
| `--no-structure` / `--no-history` | Ingest only one facet. |

**`graphforge db`**

| Flag | What it does |
|------|--------------|
| `--url URL` | `postgresql://…`, `mysql://…`, `mssql://…`. Omit the database name to graph every non-system database on the server. |
| `--engine/--host/--port/--user/--password/--databases` | The same thing spelled out. |
| `--schemas` | Comma-separated schema filter (PostgreSQL / SQL Server). |
| `--driver` | SQL Server ODBC driver name. |
| `--sample-rows N` | Opt-in profiling: `COUNT(*)` every table into `:Table.approxRows`, and for tables of at most N rows also `COUNT(DISTINCT col)` into `:Column.approxCardinality`. Default `0` = off. |

**`graphforge link`**

| Flag | What it does |
|------|--------------|
| `--maps-to` / `--based-on` / `--uses-table` / `--cross-db` | Run one pass instead of all four. |
| `--min-table-name-len N` | Ignore table names shorter than N characters in the SQL-text passes (default `4`) — `log`, `seq` and friends are aliases far too often to count as evidence. |

---

## Language coverage

Code structure (`:Class`, `:Method`, `IMPORTS` / `EXTENDS` / `IMPLEMENTS`) is extracted by four parsers, all regex/line-based so a file that does not compile still yields partial structure instead of aborting the scan:

| Language | Extensions | Notes |
|----------|-----------|-------|
| Java | `.java` | Annotations with arguments, framework stereotypes (`Entity` / `ManagedBean` / `EJBBean`), JPA `mappedTable`. Maven modules and dependencies come from `pom.xml`. |
| Python | `.py` | Classes with bases, `def` / `async def`, decorators (stored in the same `annotations` field as Java). `class X(Enum)` → enums, `Protocol` / `ABC` → interfaces. |
| TypeScript / JavaScript | `.ts` `.tsx` `.js` `.jsx` | Classes, `interface` / `type`, `enum`, imports and re-exports, decorators. |
| Go | `.go` | `type … struct` → classes, `type … interface` → interfaces, `const ( … iota )` → enums, funcs including methods with a receiver. |

Every parser returns the same dict shape, so `git.ingest` maps all four through one code path; `:Class.language` and `:Method.language` record which one produced a node. Beyond these four, the scanner still records `:File` nodes with a detected type for **41 extensions / 32 logical types** (Kotlin, Scala, Ruby, PHP, Rust, C/C++, C#, SQL, YAML, …) — files, hashes, line counts and history, just no class-level structure.

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
  File -->|CONTAINS_METHOD| Method
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

`:Commit -[:CHANGED]-> :File` and `:Package -[:CONTAINS_FILE]-> :File` share the same `:File` nodes — the spine linking history to structure. Java classes annotated `@Entity` / `@ManagedBean` / `@Stateless` also get a semantic label (e.g. `:Class:Entity`) and an entity records its `mappedTable`. Free functions that belong to no class (Go funcs, module-level Python `def`s) hang off the file via `CONTAINS_METHOD` instead of `HAS_METHOD`.

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

With `--sample-rows N`, tables additionally carry `approxRows` and columns of small-enough tables carry `approxCardinality` — enough to tell a lookup table from a fact table without reading any rows into the graph.

### Optional code ↔ database links (`graphforge link`)

```mermaid
graph LR
  E["Class:Entity"] -->|MAPS_TO| T[Table]
  V[View] -->|BASED_ON| T
  P[StoredProcedure] -->|USES_TABLE| T
  V -->|CROSS_DB_REFERENCE| T2["Table (other DB)"]
```

`MAPS_TO` is an exact JPA table-name match. `BASED_ON` / `USES_TABLE` / `CROSS_DB_REFERENCE` match table names as **whole tokens** in stored view/procedure definitions, scoped by database: every SQL separator is replaced with a space and both sides padded, so `os` matches a standalone `os` but never the `os` inside `os_config` or `position`. It is plain Cypher — no APOC, and no regex-escaping of table names (which routinely contain `$`, `#` and other metacharacters). Names shorter than `--min-table-name-len` (default 4) are skipped entirely. Run all passes, or select with `--maps-to` / `--based-on` / `--uses-table` / `--cross-db`.

Every node is `MERGE`-keyed on a deterministic `id`, so **re-running an import updates nodes in place instead of duplicating them** — and it works on Neo4j Community Edition.

---

## Recipes

Copy-paste straight into the Neo4j Browser, `cypher-shell`, the built-in [dashboard](#dashboard) console, or the MCP `read_cypher` tool. Each one is also a standalone file under [`examples/queries/`](examples/queries/).

**Who knows this code?** — top authors by the number of distinct files they have touched. The fastest way to find a reviewer for an unfamiliar area. ([`top-authors-by-files.cypher`](examples/queries/top-authors-by-files.cypher))

```cypher
MATCH (a:Author)-[:AUTHORED]->(:Commit)-[:CHANGED]->(f:File)
RETURN a.name AS author, a.email AS email, count(DISTINCT f) AS files
ORDER BY files DESC LIMIT 20;
```

**What keeps changing?** — highest-churn files by total lines added + removed. Churn concentrated in a few files is where refactoring and test effort pay off first. ([`highest-churn-files.cypher`](examples/queries/highest-churn-files.cypher))

```cypher
MATCH (:Commit)-[ch:CHANGED]->(f:File)
RETURN f.repo AS repo, f.path AS path,
       sum(ch.insertions + ch.deletions) AS churn,
       count(*) AS commits
ORDER BY churn DESC LIMIT 20;
```

**Which tables is the schema built around?** — foreign-key hotspots: the tables the most other tables point at. These are the ones you cannot change cheaply. ([`fk-hotspots.cypher`](examples/queries/fk-hotspots.cypher))

```cypher
MATCH (t:Table)<-[:REFERENCES]-(other:Table)
RETURN t.database AS database, t.schema AS schema, t.name AS table,
       count(DISTINCT other) AS incomingTables,
       collect(DISTINCT other.name)[0..10] AS referencedBy
ORDER BY incomingTables DESC LIMIT 20;
```

**Where does the code meet the database?** — the entity→table map produced by `graphforge link`, with the file each entity lives in so you can jump straight to it. ([`entity-to-table-map.cypher`](examples/queries/entity-to-table-map.cypher))

```cypher
MATCH (e:Entity)-[:MAPS_TO]->(t:Table)
OPTIONAL MATCH (f:File)-[:CONTAINS_CLASS]->(e)
RETURN e.fqn AS entity, f.path AS file, t.database AS database, t.name AS table
ORDER BY table, entity;
```

**What can we probably delete?** — files nobody has committed to in a year that no other file in the repo imports. **Candidates, not proof** — reflection, DI and cross-repo callers are invisible here. ([`dead-code-candidates.cypher`](examples/queries/dead-code-candidates.cypher))

```cypher
MATCH (f:File) WHERE f.repo = $repo
OPTIONAL MATCH (c:Commit)-[:CHANGED]->(f)
WITH f, max(substring(coalesce(c.authoredAt, c.committedAt, ''), 0, 10)) AS lastChanged
WHERE lastChanged IS NULL OR lastChanged < '2025-01-01'
OPTIONAL MATCH (f)-[:CONTAINS_CLASS]->(cls:Class)
OPTIONAL MATCH (importer:File)-[:CONTAINS_CLASS]->(:Class)-[:IMPORTS]->(ref:Class)
  WHERE ref.fqn = cls.fqn AND importer.repo = f.repo AND importer <> f
WITH f, lastChanged, count(DISTINCT importer) AS importers
WHERE importers = 0
RETURN f.path AS path, lastChanged ORDER BY lastChanged, path LIMIT 50;
```

**What breaks if I change this column?** — one traversal over every FK, index, view and stored procedure that touches a column name, plus the JPA entities mapped to its table. ([`column-blast-radius.cypher`](examples/queries/column-blast-radius.cypher))

```cypher
MATCH (c:Column) WHERE toLower(c.name) = toLower($column)
OPTIONAL MATCH (c)-[:FOREIGN_KEY]-(fk:Column)
OPTIONAL MATCH (t:Table)-[:HAS_COLUMN]->(c)
OPTIONAL MATCH (t)-[:HAS_INDEX]->(ix:Index) WHERE c.name IN ix.columns
OPTIONAL MATCH (t)<-[:USES_TABLE|BASED_ON]-(user)
RETURN t.database AS database, t.name AS table, c.dataType AS dataType,
       collect(DISTINCT fk.table)     AS foreignKeyPartners,
       collect(DISTINCT ix.name)      AS indexes,
       collect(DISTINCT user.name)    AS viewsAndProcedures;
```

> The last two recipes are also available as MCP tools that add caveats and summaries for you: `find_dead_code` and `explain_impact`.

---

## Query it from an MCP client

```bash
graphforge mcp          # starts a read-only stdio MCP server
```

Merge [`examples/claude_desktop_config.json`](examples/claude_desktop_config.json) into your MCP client's config (fill in the Neo4j password) and restart it. **Eleven** tools are exposed, all read-only:

| Tool | Arguments | Purpose |
|------|-----------|---------|
| `get_schema` | `ttl`, `refresh` | Labels, relationship types, node counts per label. Cached for 60s by default; `refresh=true` forces a fresh read, `ttl=0` bypasses the cache. |
| `read_cypher` | `query`, `limit` | Run a **read-only** Cypher query. Writes are rejected. |
| `search_nodes` | `label`, `prop`, `value`, `limit`, `offset` | Substring search on a property of a label. **Paged.** |
| `node_neighbors` | `node_id`, `limit` | The immediate neighbourhood of a node `id`. |
| `find_code` | `text`, `limit`, `offset` | File / Class / Method nodes whose name, fqn or path contains `text`. **Paged.** |
| `find_table` | `text`, `limit`, `offset` | Table / Column nodes whose name contains `text`. **Paged.** |
| `find_procedure` | `text`, `limit` | Stored procedures by name or SQL body. |
| `impact_of_column` | `column` | Blast radius of a column: matching columns across schemas, FKs, indexes, and procedures/views that reference it. |
| `explain_impact` | `target`, `kind` | Impact report for a column *or* table, split into `direct` (breaks by definition) and `transitive` (references it, needs review), with a one-line `summary`. `kind` is `auto` / `column` / `table`. |
| `find_dead_code` | `repo`, `days`, `limit` | Files and classes with no commit in `days` days and no in-repo importer. **Candidates only** — the answer carries its own caveat. |
| `blast_radius_of_file` | `path` | What a change to `path` can reach: the classes it declares, their methods, and the files whose classes import them. |

**Pagination.** `search_nodes`, `find_code` and `find_table` return `{rows, total, limit, offset, hasMore}`. Keep asking with `offset = offset + limit` while `hasMore` is true, instead of blowing the context on one giant result.

**Caching.** `get_schema` is memoised process-wide per connection for 60 seconds, so an agent that reaches for the schema on every turn does not re-count the whole graph each time.

---

## Dashboard

```bash
graphforge ui        # then open http://localhost:8000
```

A **zero-dependency** local page — Python's stdlib HTTP server, one hand-written HTML file, no npm, no build step, and no CDN, so it works on an air-gapped box. It gives you:

- **Force-directed graph canvas** — a live sample of the graph rendered in a `<canvas>`; click a node to expand its neighbours.
- **Cypher console** — run read-only queries in the browser, with query history. Writes are refused **server-side** (HTTP 400) before a connection is even opened; the client is never trusted.
- **Label explorer** — click any label in the counts table for a modal of sample nodes and their properties.
- **Search** — find nodes by substring, across all labels or scoped to one.
- **Light / dark theme**, remembered across visits.
- **Keyboard shortcuts** — `r` refresh, `/` focus search, `Esc` close modal / clear results (`Ctrl`/`Cmd`+`Enter` runs the query from inside the Cypher box).
- The original status view: your resolved configuration with **passwords masked**, Neo4j connection health, node counts by label, and per-repository / per-database load status.

Behind it are seven read-only JSON endpoints: `GET /api/status`, `/api/schema`, `/api/graph/sample`, `/api/search`, `/api/labels/<label>/sample`, `/api/node/<id>/neighbors`, and `POST /api/query`. Every one validates its input *before* opening a connection, so a rejected request provably never reaches the database.

### Screenshots

There is no dashboard screenshot checked in yet — a real one has to come from a real browser against a real graph, and a placeholder would be worse than nothing. If you have graphforge running, capturing one is a ten-minute contribution: see [`docs/img/README.md`](docs/img/README.md) for the exact recipe (what to load, what to frame, where to save it, and what to check for before publishing a picture of your own configuration). Once `docs/img/dashboard.png` exists, this section gets the image.

---

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

**Incremental git ingest.** After every successful run graphforge stores the HEAD it reached on `:Repository.lastCommit`. Pass `--since-commit auto` to pick up from there and ingest only the commits — and re-scan only the files — that changed since:

```bash
graphforge git /path/to/repo --name my-service                      # first run: everything
graphforge git /path/to/repo --name my-service --since-commit auto  # nightly: only what's new
graphforge git /path/to/repo --name my-service --since-commit a1b2c3d   # or an explicit sha
```

It degrades safely: if nothing is stored yet, if the stored sha no longer exists in the repository (a force-push, a fresh clone), or if you are not in push mode, it logs why and falls back to a full ingest. Reading `lastCommit` needs a live connection, so `--since-commit auto` is a no-op under `--emit` / `--dry-run`.

For large GitLab groups, discover and ingest only recently-active repos:

```bash
export GITLAB_SERVER=... GITLAB_GROUP_ID=... GITLAB_TOKEN=...
graphforge git --since 20        # repos active in the last 20 days
```

---

## Examples

Everything under [`examples/`](examples/) is runnable:

| Path | What it is |
|------|-----------|
| [`examples/quickstart.md`](examples/quickstart.md) | The five-minute path from `pip install` to a queryable graph. |
| [`examples/queries/`](examples/queries/) | The six [recipes](#recipes) as standalone `.cypher` files, each with a header comment stating the question it answers. |
| [`examples/docker-compose.yml`](examples/docker-compose.yml) | A local playground: Neo4j 5 + PostgreSQL 16, with the Postgres side auto-seeded so `graphforge db` and `graphforge link` have something real to chew on. |
| [`examples/seed/01-shop-schema.sql`](examples/seed/01-shop-schema.sql) | The seed the playground loads: tables, foreign keys, a view and a function. |
| [`examples/claude_desktop_config.json`](examples/claude_desktop_config.json) | MCP client wiring — merge into Claude Desktop (or any stdio MCP client) and restart. |

See [`examples/README.md`](examples/README.md) for the full walkthrough. The root [`docker-compose.yml`](docker-compose.yml) is the minimal "just give me a Neo4j" stack; [`docker-compose.ci.yml`](docker-compose.ci.yml) is the throwaway stack CI drives through [`scripts/integration_test.sh`](scripts/integration_test.sh).

---

## Extending

**A new database engine** — subclass `graphforge.db.base.SchemaExtractor`, implement the dialect-specific `connect()` and `indexes_sql()` (tables/columns/views/procedures/foreign keys come free from ANSI `INFORMATION_SCHEMA`), and register it in `graphforge.db.get_extractor`.

**A new language parser** — add a module under `graphforge/git/parsers/` exposing `extract(lines) -> dict`, register it in `PARSERS` in `graphforge/git/scan.py`, and add the extension to `SUPPORTED_EXTENSIONS` in `graphforge/git/parsers/generic.py`. The four existing parsers (`java.py`, `python.py`, `typescript.py`, `golang.py`) all return the same dict shape and are the template to copy — a new one is roughly a file, a fixture, and a test.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`docs/DESIGN.md`](docs/DESIGN.md).

---

## Limitations

- All four language parsers are regex/line-based (fast, dependency-free, no AST). They capture declarations, imports, and annotations well; they are not compilers. Method-signature and generic-type resolution is best-effort.
- The SQL-text link passes match table names as whole tokens, not substrings — but they are still heuristic: a table name that also reads as an English word inside a comment will match. `--min-table-name-len` (default 4) filters the worst of it; `MAPS_TO` is exact.
- `find_dead_code` proves *absence of evidence*, never absence of use. Reflection, dependency injection, config-driven wiring, framework entry points and cross-repo callers are all invisible to it.
- `--sample-rows` issues one `COUNT(*)` per table (and `COUNT(DISTINCT …)` per column on small tables). It is opt-in for a reason — do not point it at a hot production replica without thinking.
- The graph indexes **schema and structure, not row data** — use your database for questions about actual records.
- The graph is a point-in-time snapshot; freshness depends on how often you re-ingest (see `--since-commit`).

---

## Contributing

Issues and PRs welcome. Run the test suite and linter before submitting:

```bash
pip install -e ".[dev]"
pytest
ruff check src tests
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full guidelines (including the no-secrets rule) and a **[good first issues](CONTRIBUTING.md#good-first-issues)** list. Design notes and the decision log live in [`docs/DESIGN.md`](docs/DESIGN.md); notable changes are recorded in [`CHANGELOG.md`](CHANGELOG.md).

## License

MIT — see [`LICENSE`](LICENSE).
