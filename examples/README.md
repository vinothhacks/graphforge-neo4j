# examples

Everything in this directory is runnable. Nothing here is imported by the
package — it exists to get you from "installed" to "asking the graph real
questions" without inventing your own fixtures.

| Path | What it is |
|------|-----------|
| [`quickstart.md`](quickstart.md) | The five-minute path: install → `init` → ingest → query. |
| [`docker-compose.yml`](docker-compose.yml) | A local playground: Neo4j 5 + PostgreSQL 16, Postgres auto-seeded. |
| [`seed/01-shop-schema.sql`](seed/01-shop-schema.sql) | The schema the playground loads — 5 tables, FKs, indexes, 2 views, 2 functions, a few rows. |
| [`queries/`](queries/) | The [README recipes](../README.md#recipes) as standalone `.cypher` files. |
| [`claude_desktop_config.json`](claude_desktop_config.json) | MCP client wiring for Claude Desktop (or any stdio MCP client). |

---

## The playground, end to end

Two containers, one seeded database, and a graph you can actually traverse.

```bash
# 1. Start Neo4j + PostgreSQL and wait for both to answer queries.
docker compose -f examples/docker-compose.yml up -d --wait

# 2. Point graphforge at them. Do this once instead of repeating the flags:
cat > .env <<'EOF'
NEO4J_URI=bolt://127.0.0.1:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=graphforge-playground
NEO4J_DATABASE=neo4j
EOF

# 3. Constraints and indexes.
graphforge init

# 4. Ingest the seeded schema. --sample-rows is optional; it adds
#    :Table.approxRows / :Column.approxCardinality.
graphforge db --url postgresql://graphforge:graphforge-playground@127.0.0.1:5432/shopdb \
              --sample-rows 100000

# 5. Ingest some code. graphforge's own checkout works — it exercises the
#    Python parser, and any repo with Java/TS/Go exercises the others.
graphforge git . --name graphforge

# 6. Connect the two subgraphs (MAPS_TO / BASED_ON / USES_TABLE / CROSS_DB_REFERENCE).
graphforge link

# 7. Look at it.
graphforge verify        # node counts per label
graphforge ui            # dashboard at http://localhost:8000
```

Then run the recipes. In the dashboard's Cypher console, in the Neo4j Browser
at <http://localhost:7474>, or from a shell:

```bash
cat examples/queries/fk-hotspots.cypher        # then paste the first statement
```

The seed is built so the interesting queries return rows immediately:
`customers`, `products` and `orders` all have inbound foreign keys, both views
name their tables in the definition text (so `BASED_ON` fires), and both
functions reference tables in their body (so `USES_TABLE` fires).

`MAPS_TO` needs a JPA entity, which the seed cannot provide on its own — ingest
a Java repository whose `@Table(name = "orders")` matches, and the edge appears.

**Tear down**, dropping the volumes so the seed re-runs next time:

```bash
docker compose -f examples/docker-compose.yml down -v
```

### Which compose file?

| File | Use it when |
|------|-------------|
| [`../docker-compose.yml`](../docker-compose.yml) | You have your own repos and databases and just need a Neo4j to write into. Neo4j only, APOC on, persistent. |
| [`docker-compose.yml`](docker-compose.yml) | You want a working graph in one minute with nothing of your own. Neo4j + seeded PostgreSQL, persistent. |
| [`../docker-compose.ci.yml`](../docker-compose.ci.yml) | You are reproducing a CI failure. Throwaway, no APOC, driven by [`../scripts/integration_test.sh`](../scripts/integration_test.sh). |

---

## The queries

Each file leads with a comment block naming the question it answers, what it
requires (which ingest commands must have run), and which parts of the graph it
reads. Most carry commented-out variants underneath.

| File | Answers |
|------|---------|
| [`queries/top-authors-by-files.cypher`](queries/top-authors-by-files.cypher) | Who actually knows this code? |
| [`queries/highest-churn-files.cypher`](queries/highest-churn-files.cypher) | What keeps changing? |
| [`queries/fk-hotspots.cypher`](queries/fk-hotspots.cypher) | Which tables is the schema built around? |
| [`queries/entity-to-table-map.cypher`](queries/entity-to-table-map.cypher) | Where does the code meet the database? |
| [`queries/dead-code-candidates.cypher`](queries/dead-code-candidates.cypher) | What can we probably delete? (candidates, never proof) |
| [`queries/column-blast-radius.cypher`](queries/column-blast-radius.cypher) | What breaks if I change this column? |

Two of them take parameters. In the Neo4j Browser or `cypher-shell`:

```cypher
:param repo   => 'graphforge';
:param column => 'customer_id';
```

---

## MCP

[`claude_desktop_config.json`](claude_desktop_config.json) is a complete
`mcpServers` entry. Merge it into your client's config, set the Neo4j password,
restart the client, and the [eleven read-only
tools](../README.md#query-it-from-an-mcp-client) appear. `graphforge mcp` speaks
stdio, so any MCP client works the same way.
