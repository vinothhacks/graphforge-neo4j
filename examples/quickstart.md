# Quickstart

## 0. Install

```bash
pip install -e ".[all]"        # core + mysql + postgres + mssql + mcp
cp .env.example .env           # then edit .env with your Neo4j password
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

```bash
# a local checkout
graphforge git /path/to/repo --name my-service

# or a remote URL
graphforge git https://github.com/octocat/Hello-World.git

# or many repos from a file
graphforge git -c config/repositories.json
```

Preview what would be written, without a database:

```bash
graphforge git /path/to/repo --emit repo.cypher   # replayable script
graphforge git /path/to/repo --dry-run            # just count operations
```

## 3b. Ingest database schemas

```bash
graphforge db --engine mysql    --host 127.0.0.1 --port 3306 --user ro --password *** --databases shop,inventory
graphforge db --engine postgres --host 127.0.0.1 --port 5432 --user ro --password *** --databases analytics --schemas public
graphforge db --engine mssql    --host 127.0.0.1 --port 1433 --user ro --password *** --databases Sales
# or:
graphforge db -c config/databases.json
```

## 4. Verify

```bash
graphforge verify
```

## 5. Query from any MCP client

```bash
graphforge mcp        # serves over stdio
```

Then merge `examples/claude_desktop_config.json` into your MCP client config.

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
