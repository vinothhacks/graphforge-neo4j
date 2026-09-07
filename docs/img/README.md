# docs/img

Images used by the README and the docs.

`dashboard.png` (a populated graph, node selected) and `dashboard-empty.png`
(the first-run state) are both embedded in the README's
[Screenshots](../../README.md#screenshots) section.

Both were taken from a real browser against a real graph, and any replacement
must be too: a hand-drawn mock would show new users something that is not what
they will get. Here is the recipe for retaking them.

## Capturing `dashboard.png`

**1. Get a graph with something in it.** An empty graph produces an empty
canvas, which is not worth a screenshot. The fastest route is the playground:

```bash
docker compose -f examples/docker-compose.yml up -d --wait
cat > .env <<'EOF'
NEO4J_URI=bolt://127.0.0.1:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=graphforge-playground
NEO4J_DATABASE=neo4j
EOF

graphforge init
graphforge db --url postgresql://graphforge:graphforge-playground@127.0.0.1:5432/shopdb
graphforge git . --name graphforge      # any repo; this one exercises the Python parser
graphforge link
```

Ingesting a repository *and* a database matters: the point of the picture is
that both subgraphs are in one place.

**2. Serve the dashboard.**

```bash
graphforge ui        # http://localhost:8000
```

**3. Frame it.** Before capturing:

- Let the force-directed canvas settle (a second or two), then click one node so
  its neighbours are expanded — a moving-but-settled layout reads far better
  than a hairball.
- Make sure the **node counts by label** table shows both git labels
  (`Repository`, `File`, `Class`, `Commit`, `Author`) and database labels
  (`Database`, `Table`, `Column`, `View`, `StoredProcedure`).
- Run something in the Cypher console so the results table is populated —
  `MATCH (t:Table)<-[:REFERENCES]-(o:Table) RETURN t.name, count(o) ORDER BY 2 DESC`
  is a good one.
- Browser window around **1440 x 900** (or a 1280-wide viewport at 2x). Wider
  than that and the text is unreadable when GitHub scales it down.
- **Dark theme** matches the rest of the README's tone, but either is fine — the
  toggle is in the header.

**4. Check for secrets before you save.** The dashboard masks passwords, but it
does show your resolved configuration: **Neo4j URI, database names, repository
names, hostnames.** Use the playground values above, or crop/blur anything
internal. A screenshot is the easiest way to leak an internal hostname into a
public repository, and CI's secret scan will not catch it.

**5. Save it as `docs/img/dashboard.png`.** PNG, not JPEG (text and thin canvas
lines alias badly under JPEG). Keep it under ~500 KB; `oxipng -o4` or
`pngquant --quality 65-85` will usually get a 1440-wide capture there without
visible loss.

**6. Reference it.** In [`README.md`](../../README.md), replace the paragraph
under `### Screenshots` with:

```markdown
<img src="docs/img/dashboard.png" alt="graphforge dashboard: force-directed graph canvas, node counts by label, and the Cypher console" width="900">
```

Use a relative path (`docs/img/dashboard.png`), not a URL to a branch — relative
paths keep working in forks and in the PyPI long description.

## Other images

Same rules for anything else added here: real captures only, no secrets, PNG for
UI, SVG for diagrams, and every file referenced from a document. An unreferenced
image is dead weight — the repo has no place for one.
