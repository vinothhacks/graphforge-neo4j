# graphforge — design, decisions & roadmap

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
- `db/` — a base `SchemaExtractor` over ANSI `INFORMATION_SCHEMA` plus
  per-dialect subclasses; ingestor; optional VDS/service-catalog importer.
- `link/` — Cypher passes that connect the code and database subgraphs.
- `mcp/` — a read-only MCP server and its query helper (`GraphQuery`).
- `ui/` — a stdlib HTTP server plus one hand-written HTML file: the dashboard.
- `cli.py` — the `graphforge` command.

### Language parsers

`git/parsers/` holds four structural parsers plus two helpers:

| Module | Handles | Produces |
|--------|---------|----------|
| `java.py` | `.java` | classes/interfaces/enums, methods, imports, annotations **with arguments**, framework stereotype (`Entity` / `ManagedBean` / `EJBBean`), JPA `mappedTable` |
| `python.py` | `.py` | classes with bases, `def` / `async def`, decorators → `annotations`; `Enum` bases → enums, `Protocol` / `ABC` → interfaces |
| `typescript.py` | `.ts` `.tsx` `.js` `.jsx` | classes, `interface` / `type` → interfaces, `enum`, imports, re-exports, decorators, plus an extra `exports` key |
| `golang.py` | `.go` | `type … struct` → classes, `type … interface` → interfaces, `const ( … iota )` → enums, funcs including receiver methods |
| `pom.py` | `pom.xml` | Maven module identity + dependencies |
| `generic.py` | everything | extension → file-type table (41 extensions, 32 types), line classification, skip lists |

Every language module exposes exactly `extract(lines: list[str]) -> dict` and
returns the same keys (`package`, `imports`, `classes`, `interfaces`, `enums`,
`methods`, `annotations`), so `git/scan.py` stores the result in one
language-agnostic field (`ScannedFile.structure`) and `git/ingest.py` maps all
four through a single code path. `:Class.language` / `:Method.language` record
which parser produced a node.

FQN construction is the one place the languages genuinely differ, and it is
isolated in `scan.namespace_for()`: Java uses the declared `package` (so
pre-existing graph ids are unchanged), Go uses the directory holding the
package, Python and TypeScript use the dotted module path — which is exactly how
those languages address a file. Free functions with no owning class (Go funcs,
module-level Python `def`s) attach to the file via `CONTAINS_METHOD` rather than
`HAS_METHOD`.

All four are regex/line-based rather than AST-based. See
[Decisions](#adr-3-regexline-parsers-not-asts).

### Link passes

`link/passes.py` emits one bulk, idempotent (`MERGE`) Cypher statement per pass:

| Pass | Edge | Matching |
|------|------|----------|
| `maps-to` | `(:Class:Entity)-[:MAPS_TO]->(:Table)` | **Exact** — `mappedTable` == `Table.name` |
| `based-on` | `(:View)-[:BASED_ON]->(:Table)` | Whole-token, same database |
| `uses-table` | `(:StoredProcedure)-[:USES_TABLE]->(:Table)` | Whole-token, same database |
| `cross-db` | `(:View)-[:CROSS_DB_REFERENCE]->(:Table)` | Whole-token, *different* database |

The three text passes match **whole tokens, not substrings**. Every SQL
separator in the stored definition is replaced with a space and both sides are
padded, so `' os '` matches a standalone `os` and never the `os` inside
`os_config` or `position`. `matches_table()` in the same module is a Python
mirror of the Cypher test — the executable spec, unit-tested without a live
Neo4j. Identifiers shorter than `--min-table-name-len` (default 4) are skipped
regardless: a table called `log` or `seq` appears as a column alias far too
often to count as evidence.

This is plain Cypher — no APOC, and no regex-escaping of table names, which
routinely contain `$`, `#` and other metacharacters.

### Incremental git ingest

After a successful run the ingestor records the HEAD it reached on
`:Repository.lastCommit`, alongside `status`, `lastIngestedAt`, `files` and
`commits`. `--since-commit <sha>` then limits history extraction to commits
after that sha and restricts the structure re-scan to the paths those commits
touched.

`--since-commit auto` reads `lastCommit` back from the graph. It degrades to a
full ingest, with a log line saying why, when: nothing is stored yet; the stored
sha no longer exists in the repository (force-push, fresh clone); or the writer
is not in `push` mode (`--emit` / `--dry-run` have no connection to read from).
Never silently doing less than asked is the point — a half-ingested history is
worse than a slow one.

### Schema sampling

`--sample-rows N` (default `0`, off) issues one `COUNT(*)` per table into
`:Table.approxRows`, and for tables of at most N rows also `COUNT(DISTINCT col)`
per column into `:Column.approxCardinality`. It is opt-in because it is the only
part of graphforge that reads anything about the *contents* of a database, and
the cost scales with table size. Schema names are identifier-checked before
interpolation. See [ADR 6](#adr-6-schema-not-rows).

### MCP surface

`mcp/server.py` builds a FastMCP server over a `GraphQuery` helper. Twelve
tools, all read-only:

| Tool | Notes |
|------|-------|
| `get_schema` | TTL cache, 60s default; `refresh` / `ttl=0` bypass it |
| `read_cypher` | write statements rejected |
| `search_nodes` | paged |
| `node_neighbors` | |
| `search_codebase` | paged; `kind` = code / schema / all; case-insensitive |
| `find_code` | paged; case-insensitive; wraps `search_codebase` |
| `find_table` | paged |
| `find_procedure` | |
| `impact_of_column` | |
| `explain_impact` | column *or* table, split `direct` / `transitive`, plus a `summary` |
| `find_dead_code` | candidates only; ships its own caveat in every response |
| `blast_radius_of_file` | |

**Pagination** returns `{rows, total, limit, offset, hasMore}` rather than a
bare list, so an agent can walk a large result set deliberately instead of
having one dumped into its context. **Caching** is process-wide and keyed on the
connection, not the driver object — the dashboard opens a fresh connection per
request, so a per-instance cache would never register a hit.

`find_dead_code` deserves its caveat. It proves absence of evidence, never
absence of use: reflection, dependency injection, name-based wiring, service
loaders, dynamic imports, template/config references, framework entry points and
cross-repo callers are all invisible to it, and a class used without an import
(same package in Java, a wildcard import) looks dead. The tool's own docstring
and its `caveat` field say so on every call, because an agent that reads only
the result must still see the warning.

### Dashboard

`ui/server.py` routes `/api/...` through one pure `route()` function returning
`(status, payload)`; network plumbing is a thin `BaseHTTPRequestHandler` around
it, and tests drive `route()` directly with an injected fake graph. Input
validation happens **before** any connection is opened, so a rejected request
provably never reaches the database.

| Endpoint | Method | Returns |
|----------|--------|---------|
| `/api/status` | GET | masked config, connection health, node counts, per-repo/per-db load status |
| `/api/schema` | GET | labels, relationship types, counts |
| `/api/graph/sample` | GET | a bounded sample of nodes + edges for the canvas |
| `/api/search` | GET | substring search, all labels or scoped by `label`/`prop` |
| `/api/labels/<label>/sample` | GET | sample nodes of one label with properties |
| `/api/node/<id>/neighbors` | GET | immediate neighbourhood |
| `/api/query` | **POST** | run a read-only Cypher query |

`POST /api/query` is guarded twice: `is_write_query()` rejects writes with HTTP
400 before connecting, and `GraphQuery.read_cypher` re-checks server-side. The
client is never trusted. Labels and property names are identifier-checked before
they are interpolated into Cypher; limits are clamped; the query body is length-
capped.

The front end is one HTML file with inline CSS and JS: a force-directed
`<canvas>` sample of the graph, a Cypher console with history, a label-explorer
modal, search, a light/dark toggle persisted to `localStorage`, and keyboard
shortcuts (`r` refresh, `/` focus search, `Esc` close, `Ctrl`/`Cmd`+`Enter` run).
No npm, no build step, no CDN — see [ADR 2](#adr-2-a-zero-dependency-dashboard).

---

## Decisions

Short ADRs. Each records the decision, why, and what it costs — the cost matters
most, because that is what a future reader needs in order to revisit it.

### ADR 1: Python 3.10+

**Decision.** `requires-python = ">=3.10"`. The floor was 3.9.

**Why.** 3.9 reached end of life in October 2025, so shipping support for it
meant supporting an unpatched runtime. 3.10 buys PEP 604 unions (`int | None`)
and PEP 585 builtin generics in *runtime* positions, not just under
`from __future__ import annotations`; structural pattern matching; and
`dataclasses(slots=True)`. Concretely it removed a pile of `typing.Optional` /
`typing.List` noise from a codebase whose main job is shuffling nested dicts.
The CI matrix runs 3.10–3.13, with mypy pinned to 3.10 so the floor is actually
enforced rather than assumed.

**Cost.** Anyone on a distro python of 3.9 or older needs a newer interpreter.
Judged acceptable: this is a developer tool installed into a virtualenv, not a
system library. `typing.Self` is still out of reach (3.11+), which is why
`PYI034` is in the ruff ignore list with a note to revisit.

### ADR 2: A zero-dependency dashboard

**Decision.** `graphforge ui` is `http.server` plus a single hand-written
`dashboard.html` with inline CSS and JS. No framework, no bundler, no CDN, no
`npm`, no extra Python dependency — not even an optional extra.

**Why.** The dashboard's audience is someone confirming that an ingest landed,
often on a locked-down build box or a VM with no outbound internet. A CDN
`<script>` tag would make the tool silently useless exactly there, and a build
step would mean the published wheel could not simply carry the file
(`package-data` ships `ui/*.html` verbatim). It also keeps the security story
small: the entire client is auditable in one file, and every capability it has
is a documented server endpoint.

**Cost.** The HTML file is ~730 lines and the force-directed layout is
hand-rolled (a few dozen lines of Verlet-ish integration on a `<canvas>`) rather
than d3. Adding a substantial new view means writing DOM by hand. That trade is
worth re-examining only if the dashboard grows into a product; as a status page
with a graph preview and a query box, it does not.

### ADR 3: Regex/line parsers, not ASTs

**Decision.** All four language parsers are regex- and line-based.

**Why.** Three reasons, in order of weight. (1) *No dependencies*: a real parser
per language means `javalang`, a TypeScript toolchain, `go/ast` — cross-language
build tooling for a Python package. (2) *Robustness over precision*: a scanner
walking someone's 15-year-old monorepo meets files that do not compile, use
dialects the parser has never seen, or are simply truncated. A regex parser
returns partial structure; a strict parser raises and takes the file — or the
scan — with it. (3) *The graph does not need a compiler*: the questions being
asked are "which classes live here", "who imports this", "what maps to that
table". Declarations and imports are enough.

**Cost.** Method signatures, generics and overload resolution are best-effort;
there is no call graph and no type resolution. `find_dead_code` is blind to
same-package usage without an import for exactly this reason. Genuine
call-graph work needs an AST, which is on the roadmap as an *optional* higher-
fidelity path rather than a replacement.

### ADR 4: The distribution is `graphforge-neo4j`, the package is `graphforge`

**Decision.** PyPI name `graphforge-neo4j`; import package and console script
both stay `graphforge`.

**Why.** The name `graphforge` was already taken on PyPI. Renaming the import
package to match the distribution would have broken every existing import and
every `graphforge …` command line for no benefit, and `-neo4j` is honest
disambiguation rather than decoration — it names the backend the tool actually
writes to.

**Cost.** The mismatch surprises people: you `pip install graphforge-neo4j` and
then `import graphforge`. It is called out in the README's Install section, in
CONTRIBUTING, and in the changelog. Python distributions and import names differ
routinely (`pillow`/`PIL`, `beautifulsoup4`/`bs4`), so the pattern is at least
familiar.

### ADR 5: Subgraphs stay separate until asked

**Decision.** `graphforge git` and `graphforge db` write independent subgraphs.
Cross-domain edges exist only after an explicit `graphforge link`.

**Why.** The cross-domain edges are the only *inferred* thing in the graph.
Everything else is a fact read out of git or `INFORMATION_SCHEMA`; `BASED_ON`,
`USES_TABLE` and `CROSS_DB_REFERENCE` are guesses from SQL text, and even the
exact `MAPS_TO` depends on an annotation being right. Mixing inference into
ingest would mean a user could never tell which edges were observed and which
were derived — and could not re-run the inference with different settings
(`--min-table-name-len`) without re-ingesting everything.

Keeping linking separate also keeps it cheap to iterate: the passes are four
bulk Cypher statements over data that is already loaded, so tuning the matcher
is seconds, not another full ingest.

**Cost.** One more command to run, and a graph that looks disconnected until you
run it. The README, the quickstart and `examples/README.md` all show `link` as
step 3c for that reason.

### ADR 6: Schema, not rows

**Decision.** graphforge indexes structure and schema. It never copies table
rows into the graph. `--sample-rows` is the single, opt-in exception, and it
stores only aggregate counts (`approxRows`, `approxCardinality`) — never values.

**Why.** Three arguments converge. *Correctness*: rows go stale between
ingests, and a knowledge graph that quietly serves month-old business data is a
liability, not a feature. *Security*: copying rows means copying PII into a
second datastore with a different access-control model — a compliance problem
the tool has no business creating. *Scope*: the live database already answers
"what is in the data" well; nothing answers "how is this wired together"
without a graph. Complementing the database beats badly duplicating it.

This is also why the recommended posture is a **read-only** database account:
graphforge only ever needs `INFORMATION_SCHEMA` and, with `--sample-rows`,
`COUNT`.

**Cost.** Any question about actual records has to go to the database. The
README says so in three places, and `--sample-rows` exists precisely because
"how big is this table" turned out to be a structural question people needed
answered while looking at structure.

### ADR 7: Deterministic `id` keys and MERGE everywhere

**Decision.** Every node is keyed on a computed, deterministic `id`, and every
write is a `MERGE`.

**Why.** Re-ingesting updates in place instead of duplicating, which makes the
whole tool safe to run on a cron. Single-property uniqueness constraints also
keep the schema **Neo4j Community Edition** compatible — enterprise node keys
(composite constraints) are not available there, and requiring Enterprise for a
developer tool would be a hard sell.

**Cost.** Ids must be stable across versions or old and new nodes diverge; the
id scheme is therefore effectively public API. Deletions upstream are not
detected by `MERGE` alone, which is what `--replace` (prune-then-reingest) is
for.

### ADR 8: Three writer modes

**Decision.** `push` (live Neo4j), `emit` (a replayable `.cypher` script), and
`dry-run` (count only).

**Why.** The full pipeline — clone, scan, history, generate — can be exercised
and reviewed with no database and without even the Neo4j driver installed. That
is what makes the test suite fast and hermetic, and it is what lets a cautious
user see exactly what would be written to a shared graph before writing it.

**Cost.** Every code path that wants to *read* from the graph has to handle the
non-push modes explicitly — `--since-commit auto` is the visible example, since
it cannot read `lastCommit` when there is no connection.

---

## What's implemented

- **Git**: structure + full history; four language parsers (Java, Python,
  TypeScript/JavaScript, Go); semantic labels (`Entity` / `ManagedBean` /
  `EJBBean`) from Java annotations; `mappedTable` for JPA entities; Maven
  modules and dependencies; optional per-line `:Line` nodes.
- **Incremental git**: `--since-commit <sha|auto>` with `:Repository.lastCommit`
  and safe fallback to a full ingest.
- **DB**: MySQL / PostgreSQL / SQL Server; tables, columns, indexes, views,
  stored procedures (with body + parameters), foreign keys; connection-URL
  input; auto-discovery of every non-system database on a server; opt-in
  `--sample-rows` profiling.
- **Linking**: `MAPS_TO`, `BASED_ON`, `USES_TABLE`, `CROSS_DB_REFERENCE` with
  whole-token matching and `--min-table-name-len`.
- **Orchestration**: `--replace` (prune-then-reingest), continue-on-error
  ingestion, per-repository status, `graphforge status`, `--since` GitLab
  discovery.
- **MCP**: eleven read-only tools, three of them paged, with a TTL-cached
  `get_schema`.
- **Dashboard**: graph canvas, Cypher console, label explorer, search, themes,
  keyboard shortcuts, over seven read-only endpoints.
- **Optional** VDS / service-catalog importer.
- **CI/release**: test matrix on 3.10–3.13, coverage, lint, types, build +
  wheel smoke test, secret scan, dockerised integration test; tagged releases
  to TestPyPI (`v*-rc*`) and PyPI (`v*`) via Trusted Publishing.

## Roadmap

- Flip the non-blocking CI jobs (lint / types / integration) to blocking once
  each has a verified-clean baseline — see `CONTRIBUTING.md`.
- Optional AST-based parsing as a higher-fidelity alternative to the regex
  parsers, and a real call graph on top of it.
- More languages: Kotlin, C#, Ruby and PHP already have `:File` coverage and
  would each be one parser module away from class-level structure.
- Per-file incremental diffing for the database side, matching what
  `--since-commit` does for git.
- Optional quality/coverage overlays attached to `:File` / `:Class` nodes.
- Multi-database topologies (per-domain graphs, per-domain MCP aliases).
- A captured dashboard screenshot for the README — see `docs/img/README.md`.

## Known limitations

- All four parsers are regex-based, not compilers; method/argument resolution is
  best-effort and there is no call graph (ADR 3).
- The SQL-text link passes match whole tokens but remain heuristic — a table
  name that also reads as an ordinary word can match inside a comment.
  `--min-table-name-len` filters the worst of it; `MAPS_TO` is exact.
- `find_dead_code` and the equivalent recipe report **candidates**, never proof.
- `--sample-rows` costs one `COUNT(*)` per table, and `COUNT(DISTINCT …)` per
  column on tables under the threshold. Not for a hot production replica.
- The graph indexes **schema and structure, not row data** (ADR 6).
- The graph is a point-in-time snapshot; freshness depends on how often you
  re-ingest.
