# Connect Neo4j, databases, and MCP — then search the graph

This is the path from a fresh clone to asking the graph a question. Nothing
here needs a secret in git: passwords live in `.env` (gitignored) or in a
local MCP config that you do **not** commit.

## 1. Neo4j

Start the bundled instance from the repository root:

```bash
docker compose up -d
```

| What | Where |
|------|--------|
| Neo4j Browser | [http://localhost:7474](http://localhost:7474) |
| Bolt | `bolt://127.0.0.1:7687` |
| User | `neo4j` |
| Password | whatever you set as `NEO4J_AUTH` in `docker-compose.yml` (default placeholder: `please-change-me`) |

Change the password before anything real lands in the graph, and put the same
value in a local `.env` (copy [`.env.example`](../.env.example)):

```bash
NEO4J_URI=bolt://127.0.0.1:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=please-change-me
NEO4J_DATABASE=neo4j
```

Confirm the driver can talk to it:

```bash
graphforge verify
```

`graphforge ui` then serves a dashboard at [http://localhost:8000](http://localhost:8000).
The Browser at `:7474` is Neo4j's own UI — use it for raw Cypher; use the
dashboard for search, labels, and a force-directed sample of the graph.

## 2. SQL Server (and other databases)

Ingest a catalog with a connection URL. The path after the host is the
**catalog** (database name), never the SQL Server instance:

```bash
graphforge db --url mssql://user@host:port/Catalog
```

PostgreSQL and MySQL work the same way (`postgresql://…`, `mysql://…`). Omit
the catalog to graph every non-system database on the server.

Put the password in `.env` (`DB_PASSWORD=…`) or pass `--password` on the
command line. Do not commit `.env`, and do not put a real password in any
example JSON.

**Instance vs catalog.** A name like `HOST\V2` is a SQL Server *instance*, not
a database. Point `--host` / `--port` (or the URL host) at that instance, and
put the catalog (`Sales`, `LRPDB`, …) in `--databases` or the URL path. Using
the instance name as the catalog produces "database does not exist".

Optional size profiling (one `COUNT(*)` per table):

```bash
graphforge db --url mssql://user@host:port/Catalog --sample-rows 100000
```

## 3. Git ingest (needed for codebase search)

Schema search works after `graphforge db`. Code search needs at least one
repository in the graph:

```bash
graphforge git /path/to/repo --name my-service
```

`--name` becomes `n.repo` on File / Class / Method nodes and is the value
`--repo` filters on later. Remote URLs and a JSON repository list also work;
see `graphforge git --help`.

`graphforge link` is optional: it draws edges between the code and schema
subgraphs once both are loaded.

## 4. MCP clients

The server is stdio, read-only, and talks to the same Neo4j as the CLI:

```bash
graphforge mcp
```

**Claude Desktop** (or any client that reads `mcpServers`): merge
[`examples/claude_desktop_config.json`](../examples/claude_desktop_config.json)
into the client's config. Fill in `NEO4J_PASSWORD`; leave everything else as
placeholders until you change them.

**Cursor:** copy [`examples/cursor_mcp.json`](../examples/cursor_mcp.json) to
`.cursor/mcp.json` in this repo (or into your user MCP config). That path is
gitignored — do not force-add it. The example uses the `graphforge` console
script; `python -m graphforge mcp` is the same server if the script is not
on `PATH`. Restart Cursor (or reload MCP servers) after editing.

The password in both examples is the placeholder `please-change-me`. Replace
it locally; never commit a real host, user, or password.

## 5. Search the graph

After ingest, any of these hit the same case-insensitive index:

```bash
graphforge search booking
graphforge search Foo --kind code --repo my-service
graphforge search order --kind schema
graphforge search widget --kind all --limit 50
```

`--kind` is `code` (File / Class / Method; the default), `schema` (Table /
Column / StoredProcedure), or `all`. An empty query exits `2`. The same Neo4j
flags as `verify` apply (`--neo4j-uri`, `--env`, …).

In the dashboard, the search box on `/` calls `GET /api/search`. From an MCP
client, prefer:

| Tool | What it searches |
|------|------------------|
| `search_codebase` | Code and/or schema, case-insensitive, optional `repo`, paged |
| `find_code` | File / Class / Method only (same match as `kind=code`) |
| `find_table` | Table / Column by name |
| `find_procedure` | Stored procedures by name or SQL body |

Paged tools return `{rows, total, limit, offset, hasMore}`. Keep asking with
`offset = offset + limit` while `hasMore` is true.
