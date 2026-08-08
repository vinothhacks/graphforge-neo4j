# Quickstart

Five minutes from nothing to a graph you can query. If you have no repositories
or databases handy, skip to the [playground](README.md#the-playground-end-to-end)
instead — it brings its own.

## 0. Install

Requires **Python 3.10+** and the `git` CLI.

```bash
pip install graphforge-neo4j          # or, from a checkout: pip install -e ".[all]"
cp .env.example .env                  # then edit .env with your Neo4j password
```

## 1. Start Neo4j (optional helper)

```bash
docker compose up -d           # Neo4j at bolt://127.0.0.1:7687
```

## 2. Create constraints & indexes

```bash
graphforge init
```

## 3a. Ingest a Git repository (structure + history)

Structure is parsed for Java, Python, TypeScript/JavaScript and Go; every other
file type still lands as a `:File` with its history.

```bash
# a local checkout
graphforge git /path/to/repo --name my-service

# or a remote URL
graphforge git https://github.com/octocat/Hello-World.git

# or many repos from a file
graphforge git -c config/repositories.json
```

Re-running is idempotent. To ingest only what is new since the last run:

```bash
graphforge git /path/to/repo --name my-service --since-commit auto
```

Preview what would be written, without a database:

```bash
graphforge git /path/to/repo --emit repo.cypher   # replayable script
graphforge git /path/to/repo --dry-run            # just count operations
```

## 3b. Ingest database schemas

```bash
# by connection URL (omit the database name to graph every non-system database)
graphforge db --url postgresql://ro:secret@127.0.0.1:5432/analytics
graphforge db --url mysql://ro:secret@127.0.0.1:3306/shop

# or with explicit flags
graphforge db --engine mysql    --host 127.0.0.1 --port 3306 --user ro --password '***' --databases shop,inventory
graphforge db --engine postgres --host 127.0.0.1 --port 5432 --user ro --password '***' --databases analytics --schemas public
graphforge db --engine mssql    --host 127.0.0.1 --port 1433 --user ro --password '***' --databases Sales

# or from a config file
graphforge db -c config/databases.json
```

Optional: annotate tables with row counts (`COUNT(*)` per table — opt-in, and
not something to point at a busy production replica without thinking).

```bash
graphforge db --url postgresql://ro:secret@127.0.0.1:5432/analytics --sample-rows 100000
```

## 3c. Connect the two subgraphs (optional)

```bash
graphforge link        # MAPS_TO, BASED_ON, USES_TABLE, CROSS_DB_REFERENCE
```

## 4. Verify

```bash
graphforge verify      # node counts per label
graphforge status      # per-repository ingest status
graphforge ui          # dashboard at http://localhost:8000
```

## 5. Query from any MCP client

```bash
graphforge mcp        # serves over stdio
```

Then merge [`claude_desktop_config.json`](claude_desktop_config.json) into your
MCP client config and restart it. Eleven read-only tools appear — see the
[README](../README.md#query-it-from-an-mcp-client).

## Example Cypher, once loaded

```cypher
// Who changed the most files?
MATCH (a:Author)-[:AUTHORED]->(c:Commit)-[:CHANGED]->(f:File)
RETURN a.name, count(DISTINCT f) AS files ORDER BY files DESC LIMIT 10;

// Files with the most churn
MATCH (c:Commit)-[ch:CHANGED]->(f:File)
RETURN f.path, sum(ch.insertions + ch.deletions) AS churn ORDER BY churn DESC LIMIT 10;

// Tables with the most foreign-key references (schema hotspots)
MATCH (t:Table)<-[:REFERENCES]-(other:Table)
RETURN t.name, count(other) AS refs ORDER BY refs DESC LIMIT 10;

// Every column of a table
MATCH (t:Table {name:'orders'})-[:HAS_COLUMN]->(col:Column)
RETURN col.name, col.dataType, col.isNullable ORDER BY col.ordinal;
```

More, with explanations and variants, in [`queries/`](queries/).
