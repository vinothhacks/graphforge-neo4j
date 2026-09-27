# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed — BREAKING

- **Database drivers are now optional extras.** `pip install graphforge-neo4j` no
  longer installs `mysql-connector-python`, `psycopg2-binary` and `pyodbc`; nor
  `requests`, which only GitLab group discovery uses. Every one of them was
  already imported lazily behind a guard, so bundling them only meant that
  someone who wanted to graph a git repository still had to build `pyodbc` —
  which needs a system ODBC driver and is the most likely of the three to fail
  outright.

  **To restore the previous behaviour in one line:**

  ```bash
  pip install 'graphforge-neo4j[all]'
  ```

  Or install just what you use: `[mysql]`, `[postgres]`, `[mssql]`, `[gitlab]`,
  `[mcp]`. Running `graphforge db` against an engine whose driver is missing now
  names the exact command to fix it, rather than the `pip install
  'graphforge[mssql]'` it used to print — an extra that never existed, on a
  distribution that is not called that.

- **The `mcp` extra now requires `mcp` 2.x.** 0.2.0 asked for `mcp>=1.2.0`. The
  server is now built on the 2.x `MCPServer` API, so on 1.x `graphforge mcp`
  stops with an error naming the upgrade instead of serving. Upgrading
  `graphforge-neo4j` on its own leaves an installed `mcp` 1.x in place; upgrade
  through the extra:

  ```bash
  pip install -U "graphforge-neo4j[mcp]"
  ```

- **`read_cypher` and the dashboard console refuse much of what 0.2.0 ran.**
  0.2.0 checked a query against one regular expression for write keywords, and
  capped rows only by appending a `LIMIT` when it found none. Now:

  - Only `db.labels`, `db.relationshipTypes` and `db.propertyKeys` can be
    `CALL`ed. Every other procedure is refused, Neo4j's own included:
    `db.schema.visualization`, `db.indexes` and `dbms.components` no longer run.
  - `USE`, `LOAD CSV` and more than one statement are refused, and
    `read_cypher` refuses a query over 8,000 characters, as the console already
    did.
  - A `LIMIT` above 500 is an error, and so is a `read_cypher` `limit` above 500
    (the console still clamps its own `limit` to 500).
  - At most `limit` rows come back (200 by default), even when the query's own
    `LIMIT` is larger: `MATCH (n) RETURN n LIMIT 400` returns 200 rows.
  - A namespaced function call — `apoc.text.join(…)`, anything under `gds.*`,
    any user-defined namespace — is refused, and so are `SHOW`, `TERMINATE`,
    `ALTER`, `GRANT`, `DENY`, `REVOKE`, `START`, `STOP` and the other
    administration commands. Neo4j's own function namespaces (`date.*`,
    `datetime.*`, `localdatetime.*`, `localtime.*`, `time.*`, `duration.*`,
    `point.*`, `vector.similarity.*`) and un-namespaced built-ins such as
    `count` and `toLower` still run.
  - The query runs with a 30-second transaction timeout.

  A query that used APOC, or any procedure but those three, in 0.2.0 no longer
  runs through graphforge at all: run it in Neo4j Browser or `cypher-shell`,
  which the guard does not sit in front of. The reasons are under Security.

### Security

- **Read guard: closed a bypass.** A backtick-quoted procedure name
  (`` CALL `apoc.util.sleep`(1000) ``) matched no bare-identifier pattern, and an
  unparseable `CALL` was simply not checked, so the deny-by-default allowlist was
  never consulted. The allowlist now applies to every `CALL` site: a target that
  cannot be resolved is a denial. Quoted identifiers are masked, which also fixes
  the mirror-image false positive that rejected ``MATCH (n:`Pending DELETE`)``.
- **Read guard: every check now runs on one lexer that follows Neo4j's rules.**
  The masker disagreed with Neo4j about where a literal ended: it read `''` as an
  escaped quote, ignored backslash escapes, and ended a `//` comment only at a
  line feed. So a string with a backslash-escaped quote, or a comment ended by a
  carriage return, could hide a following `CALL` or write clause from every
  check. An unterminated literal, or a character Cypher has no use for, is now a
  denial. `USE` and administration commands are refused wherever a clause can
  begin — past `EXPLAIN` / `PROFILE` / `CYPHER …`, after `UNION`, at the top of a
  subquery — not only at the start of the query, so `CALL { USE other … }` can no
  longer switch database.
- **Read guard: namespaced functions are refused.** The allowlist covered
  procedures only, and APOC also ships functions that run nested Cypher
  (`apoc.cypher.runFirstColumn*`), so a function call could reach what `CALL`
  could not. Neo4j never loads a user-defined function into the root namespace,
  so a namespaced call outside Neo4j's own namespaces is plugin code. This is a
  behaviour change; see Changed — BREAKING.
- **Dashboard: drive-by CSRF and DNS rebinding.** A cross-origin `fetch` with
  `Content-Type: text/plain` is a simple request and is sent with no preflight, so
  any page you had open could reach the console. An `Origin` that differs in
  scheme, host *or port* is now refused. Comparing hosts alone would let a page
  on any other localhost port, a dev server say, pass as the dashboard. The one
  exception is a public (non-loopback) bind, which may sit behind a reverse
  proxy that terminates TLS: there an `https` `Origin` whose host and port match
  the `Host` header (no port meaning 443) is accepted too. A loopback-bound
  server refuses every scheme, host or port mismatch, and any `Host` that is not
  loopback. Every non-GET API request except the read-only console needs the
  per-run token, decided on the same path segments the router dispatches on, so
  a spelling such as `/api//ingest` cannot skip the check.
- **Git credentials no longer travel in argv or the remote URL**, where they
  reached `ps`, `.git/config`, and the git error text that `GitError` carried up
  to the console. They go through a short-lived credential-helper file, and git
  output is scrubbed before it is logged or raised. That includes credentials
  written into the URL itself (`https://user:secret@host/r.git`,
  `https://TOKEN@host/r.git`), which 0.2.0 passed to git verbatim: they are split
  off and sent the same way. A URL that names only a user
  (`https://alice@host/…`) asks your own credential manager for that user. Your
  own helpers are only ever asked, through `git credential fill`: the clone,
  fetch or pull is given the temporary file alone, so git never saves what
  graphforge sends into your credential store. The file also answers for hosts
  where `credential.useHttpPath` is on, which Git for Windows turns on for
  `https://dev.azure.com`. The dashboard's job log hides a URL's whole userinfo
  (`https://***@host/…`), not just the password, since for `https://TOKEN@host`
  the username is the token.
- **`:Repository.url` no longer stores credentials.** 0.2.0 saved the URL exactly
  as given, `user:secret@` included, and the dashboard's label explorer shows
  that property. Only the bare URL is stored now, and it is also what logs and
  failure summaries name. **If you ever ingested a URL with credentials in it,
  run `graphforge git` on that repository again** to overwrite the stored value.
  `MATCH (r:Repository) WHERE r.url CONTAINS '@' RETURN r.name, r.url` lists the
  candidates (an `ssh://git@…` URL matches too, harmlessly).
- **A credential 0.2.0 stored in a clone's origin URL is removed when nothing is
  lost.** A clone that 0.2.0 made with `GIT_TOKEN` or `GIT_USERNAME` /
  `GIT_PASSWORD` set holds that credential in the origin URL in its
  `.git/config`, and so does one it made with neither set from a URL with
  credentials in it, which git saves as given. On each update graphforge
  compares the stored credential with the login this run uses: the one in the
  repository URL you pass, if that URL carries one for the same scheme and host,
  and otherwise the settings' (`oauth2:<GIT_TOKEN>` if a token is set, else
  `GIT_USERNAME`, with `GIT_PASSWORD` if set). A user with no password, stored
  or passed, is paired with `GIT_PASSWORD` when it is `GIT_USERNAME`, and with
  `GIT_TOKEN` when it is `oauth2`. The stored credential is removed, with
  `git remote set-url origin <bare URL>` and the log line "Removed the
  credentials stored in …'s origin URL", only when it is exactly that login,
  user and password alike, or when it names a user with no password and the run
  logs in as that user. So a clone made with the settings is cleaned up by a
  plain URL as long as the settings still hold the credential it stored, and one
  made from a URL with credentials in it by passing that URL again.

  In every other case the origin is left as it is, since nothing has shown that
  the new login works and the stored one may be its only copy, and a warning is
  logged: another user in the URL, with or without a password; the same user
  with a different or mistyped password; a rotated `GIT_TOKEN` or
  `GIT_PASSWORD`; a URL for another host; nothing in the URL or the settings.
  For the same user with another secret, the warning says the stored
  credentials differ from the password in the repository URL (when the URL
  carries one for that host), from `GIT_PASSWORD`, or from `GIT_TOKEN`,
  whichever this run brings, that git sends the stored ones instead, and which
  `git remote set-url` command switches to the new ones. Otherwise it says to
  move the credential into `GIT_TOKEN`, or `GIT_USERNAME` and `GIT_PASSWORD`,
  and then run that command. Only a credential written in `.git/config` counts:
  one that your own `insteadOf` rule adds is never touched or reported.
- `docker-compose.yml` no longer sets `apoc.*` unrestricted. graphforge needs no
  APOC at all, and unrestricted grants `apoc.load.jdbc` / `apoc.load.json` —
  outbound network and filesystem access — to anything that reaches the server.

### Fixed

- `read_cypher` corrupted multi-line Cypher. An existing `LIMIT` was detected by
  searching the raw text for `" LIMIT "`, so a newline-formatted query looked
  uncapped and got a second clause appended — `LIMIT 3\nLIMIT 200`, a syntax
  error, on the shape an agent actually writes. A standalone `CALL`, which takes
  no `LIMIT` clause, got one appended too. A `" LIMIT "` inside a string literal
  suppressed the cap, and so did a `LIMIT` inside a `CALL { }`, `EXISTS { }`,
  `COUNT { }` or `COLLECT { }` subquery, which caps nothing the outer query
  returns: only a `LIMIT` after the final top-level `RETURN` counts now. That
  appended clause was the only cap 0.2.0 had, so a query with its own `LIMIT`,
  however large, or with an unbounded earlier `UNION` branch returned every row
  it produced. The rows are now counted as they are fetched and reading stops at
  `limit`, so nothing past it is built in memory (see Changed — BREAKING).
- Neo4j driver errors escaped as ~35-line tracebacks with exit code 1 instead of
  the documented 2 — on `verify`, `init`, `status`, `search`, `ui`, and `mcp`.
- `graphforge mcp` exited when the database was unreachable, so the client showed
  only "server exited". The connection is now lazy: the client connects, the
  tools list, and the reason reaches whoever asks a question. A failed
  connection is retried on the next call, and starting Neo4j is enough, with no
  client restart. After two failures in a row, each call within 5 seconds of the
  last attempt gets that attempt's reason back, with when it was checked and
  when it will be checked again, instead of dialling a Neo4j that is down: an
  agent looping over tools cannot hammer it, or wait out a connect timeout on
  every call.
- A 5,000-file repository printed 5,000 progress bars, one per file.
- `status` and `search` tables misaligned on any value longer than its fixed
  column width — which is most real file paths.
- `graphforge --version` reported a hardcoded `0.1.0` that had drifted from
  `pyproject.toml`.

### Added

- `graphforge quickstart` — guided first run: configure, connect, create the
  schema, ingest, link, and register an MCP client.
- `graphforge doctor` — checks the install, the connection and the graph, and
  prints the command that fixes whatever is wrong. It reports an `mcp` older
  than 2.0 as unusable rather than "installed". An MCP client config it cannot
  parse gets an `mcp config` warning naming the file, since `mcp install` leaves
  that file alone until it is fixed or moved. It and `quickstart` read
  `GF_ALLOW_EMPTY_PASSWORD` like every other boolean setting, so `false` and `0`
  mean off.
- `graphforge mcp install [--client …]` — writes the entry for Claude Code,
  Claude Desktop and Cursor. Resolves the console script from the running
  interpreter rather than `PATH`, and points at your `.env`, so no password is
  written into a client config. A config it cannot parse (a trailing comma, say;
  a byte-order mark is fine) is left untouched with a message, not overwritten
  with a merge that would erase the other servers in it. `--remove --dry-run`
  only reports what it would remove.
- Dashboard: guided ingest, a first-run empty state, a clickable legend, a node
  detail panel, canvas zoom/pan/drag/fit, and real node names on the canvas. An
  ingest in which every source failed, such as a failed clone or a rejected
  login, is reported as failed. One that loaded only some sources is reported as
  done, with the number that failed and why.
- [SECURITY.md](SECURITY.md), and [ADR 9](docs/DESIGN.md) on the read guard.
- `lint` and `types` are now blocking in CI; the secret scan covers GitHub, AWS,
  Slack and private-key formats across all tracked files, not `glpat-` in three
  file types; coverage has a floor (70%, measured baseline 79%).
- Tests for `git/clone.py` and `git/discover.py`, which had none, and for
  `cmd_status` / `cmd_verify`.

- Layered read-query gate (`graphforge.query_guard`): procedure allowlist, clause
  denies, limit cap; Neo4j read transactions for anything that still runs.
- [docs/FEATURE_AUDIT.md](docs/FEATURE_AUDIT.md) — README / BUILD_PLAN / code / tests.
- pytest markers `e2e_critical` / `e2e_full` and CI jobs (PR Neo4j HTTP/CLI, no
  Chromium; main/nightly playground). Dashboard browser coverage is the Playwright
  MCP agent runbook ([docs/DASHBOARD_E2E.md](docs/DASHBOARD_E2E.md)), not CI.
- `graphforge search QUERY [--kind code|schema|all] [--repo NAME]` — case-insensitive
  codebase / schema search over the graph.
- MCP tool `search_codebase` (same query as the CLI; paged).
- [docs/CONNECT.md](docs/CONNECT.md) — Neo4j Browser / Bolt, SQL Server catalog vs
  instance, git ingest, Cursor/Claude MCP wiring, and how to search afterwards.
- [`examples/cursor_mcp.json`](examples/cursor_mcp.json) — Cursor MCP stub
  (`please-change-me` only; copy to `.cursor/mcp.json`, which stays gitignored).

### Changed

- MCP `find_code` is case-insensitive (`Foo` finds `foo`).

## [0.2.0] - 2026-08-08

### Added

**Packaging & sources** *(carried forward — now released)*

- Published to PyPI as **`graphforge-neo4j`** (`pip install graphforge-neo4j`).
- Database **connection-URL** input: `graphforge db --url postgresql://user:pass@host:5432/dbname`
  (also `mysql://`, `mssql://`).
- **Auto-discovery**: point at a server without naming a database and graphforge
  graphs every non-system database it finds, reporting tables loaded for each.
- **Local web dashboard**: `graphforge ui` (see below for everything it grew
  since).
- Bundled MySQL / PostgreSQL / SQL Server drivers by default (SQL Server still
  needs a system ODBC driver at run time).
- **Continue-on-error** ingestion: a single failing database/repo/file no longer
  aborts the whole run; failures are collected and summarised at the end.

**Languages — code structure beyond Java**

- Three new structural parsers joining Java, all under `graphforge/git/parsers/`:
  - **Python** (`.py`) — classes with bases, `def` / `async def`, decorators.
    `class X(Enum)` is bucketed as an enum, `Protocol` / `ABC` bases as an
    interface.
  - **TypeScript / JavaScript** (`.ts` `.tsx` `.js` `.jsx`) — classes,
    `interface` / `type`, `enum`, imports, re-exports and decorators, plus an
    `exports` key.
  - **Go** (`.go`) — `type … struct` → classes, `type … interface` →
    interfaces, `const ( … iota )` blocks → enums, funcs including methods with
    a receiver.
- All four parsers return the same dict shape, so `git/scan.py` and
  `git/ingest.py` map every language through one code path. `:Class.language`
  and `:Method.language` record which parser produced a node.
- `scan.namespace_for()` builds language-appropriate FQNs: Java keeps the
  declared package (existing graph ids are unchanged), Go uses the package
  directory, Python and TypeScript use the dotted module path.
- Free functions with no owning class (Go funcs, module-level Python `def`s)
  attach to their file with a new `CONTAINS_METHOD` edge.

**Incremental git ingest**

- `graphforge git --since-commit <sha>` ingests only the commits after `sha`,
  and re-scans only the files those commits touched.
- `--since-commit auto` continues from `:Repository.lastCommit`, which every
  successful run now records alongside `status`, `lastIngestedAt`, `files` and
  `commits`.
- Falls back to a full ingest — with a log line explaining why — when nothing is
  stored yet, when the stored sha is missing from the repository (force-push,
  fresh clone), or when the writer is not in `push` mode (`--emit` / `--dry-run`
  have no connection to read from).
- New git helpers behind it: `history.changed_paths()`, `history.head_commit()`,
  `history.commit_exists()`.

**Database schema sampling**

- `graphforge db --sample-rows N` (opt-in, default `0` = off): `COUNT(*)` every
  table into `:Table.approxRows`, and for tables of at most N rows also
  `COUNT(DISTINCT col)` into `:Column.approxCardinality`. Enough to tell a
  lookup table from a fact table without copying any rows into the graph. Schema
  and table identifiers are validated before interpolation; a table that refuses
  the probe is logged and skipped rather than aborting the run.

**MCP server — now 11 tools**

- `explain_impact(target, kind)` — impact report for a column *or* table name,
  split into `direct` (breaks by definition: tables, columns, foreign keys,
  indexes) and `transitive` (views, stored procedures, JPA entities that merely
  reference them), with a one-line `summary`. `kind` is `auto` / `column` /
  `table`.
- `find_dead_code(repo, days, limit)` — files and classes with no commit in
  `days` days and no in-repo importer. Every response carries an explicit caveat
  that this is absence of evidence, not proof: reflection, dependency injection,
  config-driven wiring, framework entry points and cross-repo callers are all
  invisible to it.
- `blast_radius_of_file(path)` — the classes a file declares, their methods, and
  the files whose classes import them.

**Dashboard**

- Force-directed `<canvas>` graph visualisation of a bounded sample of the
  graph; click a node to expand its neighbours.
- In-browser **Cypher console** with query history. Writes are rejected
  **server-side** with HTTP 400 before a connection is opened, and re-checked
  inside `GraphQuery.read_cypher` — the client is never trusted.
- **Label explorer**: click a label in the counts table for a modal of sample
  nodes and their properties.
- **Search box** across all labels, or scoped to a label and property.
- **Light / dark theme** toggle, persisted to `localStorage` and applied before
  first paint so there is no flash.
- **Keyboard shortcuts**: `r` refresh, `/` focus search, `Esc` close modal /
  clear results, `Ctrl`/`Cmd`+`Enter` to run the query from the Cypher box.
- Six new read-only endpoints alongside the existing `/api/status`:
  `GET /api/schema`, `/api/graph/sample`, `/api/search`,
  `/api/labels/<label>/sample`, `/api/node/<id>/neighbors`, and
  `POST /api/query`. All input validation happens before any connection is
  opened, so a rejected request provably never reaches the database.
- Still zero-dependency: stdlib HTTP server, one HTML file, no npm, no build
  step, no CDN.

**CI, release & tooling**

- CI jobs: `test` on Python 3.10 / 3.11 / 3.12 / 3.13, `coverage` (XML
  artifact), `lint` (ruff), `types` (mypy), `build` + wheel smoke test,
  `secret-scan`, and a dockerised `integration` job against real Neo4j +
  PostgreSQL. `lint`, `types` and `integration` are deliberately
  `continue-on-error: true` until each has a verified-clean first run — each job
  carries a comment saying exactly what to fix before flipping it to blocking.
- `release.yml` publishes on tags via PyPI **Trusted Publishing** (OIDC, no
  stored token): `v*-rc*` / `v*-alpha*` / `v*-beta*` → **TestPyPI**
  (environment `testpypi`), plain `v*` → **PyPI** (environment `pypi`). The
  build job refuses to publish if the tag disagrees with the built version, and
  attaches a GitHub Release with the matching changelog section.
- `scripts/changelog_section.py` — extracts the section for a tag from this file
  to use as the release body, falling back through PEP 440 spellings to
  `Unreleased`.
- `scripts/integration_test.sh` — end-to-end test driving the real CLI against
  real services: seed PostgreSQL, build a throwaway git repo with a JPA entity,
  `init` → `db` → `git` → `link`, then assert the labels, counts and link edges
  that landed.
- `docker-compose.ci.yml` — throwaway Neo4j + PostgreSQL stack for that test.
  No APOC (which proves the link passes need no plugins), no persistence, small
  heap, healthchecks that wait for Bolt to actually answer queries.
- `.pre-commit-config.yaml` — ruff lint + format pinned to the same version CI
  uses, plus end-of-file / trailing-whitespace / YAML / TOML / large-file /
  private-key / merge-conflict checks.
- `pytest-cov`, `mypy` and `pre-commit` added to the `dev` extra; `[tool.mypy]`
  and `[tool.coverage]` configured in `pyproject.toml`.

**Docs & examples**

- `examples/docker-compose.yml` + `examples/seed/01-shop-schema.sql` — a local
  playground: Neo4j 5 and a PostgreSQL 16 that seeds itself with a small shop
  schema (5 tables, foreign keys, indexes, 2 views, 2 functions), so
  `graphforge db` and `graphforge link` have something real to work on within a
  minute.
- `examples/queries/` — six annotated `.cypher` recipes, each stating the
  question it answers, what must have been ingested, and commented-out variants.
- `examples/README.md` — an index of everything under `examples/`, plus the
  end-to-end playground walkthrough and a "which compose file?" table.
- README gained a **Recipes** section, a **Language coverage** table, an
  **Examples** index, per-command flag tables, the full 11-tool MCP table, and a
  rewritten Dashboard section.
- `docs/DESIGN.md` gained a **Decisions** section: eight ADR-style records
  covering the Python floor, the zero-dependency dashboard, regex parsers, the
  `graphforge-neo4j` distribution name, separate-by-default subgraphs,
  schema-not-rows, deterministic `id` keys, and the three writer modes.
- `docs/img/README.md` — how to capture the dashboard screenshot, including what
  to load first and what to check for before publishing an image of your own
  configuration.

### Changed

- **Python 3.10+ is now required** (was 3.9). 3.9 is end-of-life; 3.10 gives
  PEP 604 unions and PEP 585 builtin generics at runtime. Typing throughout
  `src/` has been modernised accordingly (`X | None` instead of
  `typing.Optional[X]`, `list[str]` instead of `typing.List[str]`), and the
  classifier list now advertises 3.10–3.13.
- **Ruff is now actually enforced.** `[tool.ruff.lint].select` was previously
  unset, which meant ruff ran only its own defaults and every entry in `ignore`
  was decorative. `select` now lists E, W, F, I, UP, B, BLE, C4, PIE, RET, SIM,
  PYI and RUF, and `ignore` shrank from 14 entries to 7 — each remaining one
  annotated with the reason it is there and what would have to change to remove
  it. `target-version` moved to `py310`.
- **Link passes match whole tokens, not substrings.** Every SQL separator in a
  view/procedure definition is replaced with a space and both sides padded, so
  `os` matches a standalone `os` and never the `os` inside `os_config` or
  `position`. Still plain Cypher — no APOC, and no regex-escaping of table names
  (which routinely contain `$`, `#` and other metacharacters).
  `link.passes.matches_table()` is a Python mirror of the Cypher test, so the
  rule is unit-testable without a live Neo4j.
- **`graphforge link --min-table-name-len N`** (default 4) skips identifiers
  shorter than N in the SQL-text passes: a table called `log` or `seq` shows up
  as a column alias far too often to count as evidence.
- `search_nodes`, `find_code` and `find_table` are **paged**. They now return
  `{rows, total, limit, offset, hasMore}` instead of a bare list, so an agent
  can walk a large result set with `offset = offset + limit` rather than having
  it dumped into its context.
- `get_schema` is **cached** for 60 seconds by default. The cache is
  process-wide and keyed on the connection settings rather than the driver
  object, because the dashboard opens a fresh connection per request and a
  per-instance cache would never register a hit. Pass `refresh=true` to force a
  read, or `ttl=0` to bypass it.
- `graphforge ui` help text and docs now describe the dashboard as more than a
  status page.

### Fixed

- **PostgreSQL's default schema filter is no longer applied to other engines.**
  `PG_SCHEMAS` (default `public`) was being used as the default schema list for
  every engine, so a SQL Server ingest with no explicit `--schemas` silently
  looked for a `public` schema and returned nothing. `cli._default_schemas()`
  now applies it only to `postgres` / `postgresql`; other engines discover their
  own schemas (SQL Server falls back to `dbo`).
- `src/graphforge/__init__.py` caught bare `Exception` around the
  `importlib.metadata` version lookup; narrowed to `ImportError`.

### Notes

- Import package and CLI remain `graphforge`; only the PyPI distribution name is
  `graphforge-neo4j` (the name `graphforge` was already taken). See ADR 4 in
  `docs/DESIGN.md`.
- The `lint`, `types` and `integration` CI jobs are non-blocking on purpose —
  their configuration was authored without a ruff/mypy/docker binary available
  to verify it. Flipping them to blocking is tracked as a good first issue in
  `CONTRIBUTING.md`. *(lint and types became blocking in 0.3.0.)*
- No dashboard screenshot is committed yet: capturing one needs a real browser
  against a real graph. `docs/img/README.md` has the recipe. *(Committed in
  0.3.0.)*

## [0.1.0]

### Added
- Initial release: Git (structure + history) and relational-schema (MySQL,
  PostgreSQL, SQL Server) ingestion into Neo4j; optional code↔DB linking; a
  read-only MCP server; emit / dry-run / push writer modes.
