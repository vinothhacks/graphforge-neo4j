# Dashboard E2E Runbook

Browser coverage for `graphforge ui`. Pytest covers the HTTP API and the CLI;
this table covers what only a real browser can: the DOM, the canvas, and the
interactions.

**Harness rules:**
- Security steps run first.
- Never record a pass without an evidence value from the actual run.
- A step that reads a credential out of the page is a failure, not a note.

The current pass was driven over the Chrome DevTools Protocol against a local
`graphforge ui` on a loopback port, with the playground stack from
`examples/docker-compose.yml` and a `.env` containing no real hosts or
credentials. `docs/img/README.md` has the capture recipe and its secret-leak
checklist.

---

## Step results

| Step id | What was done | Assertion / evidence | Status |
|---------|---------------|----------------------|--------|
| `js-parses` | `node --check` over every inline `<script>` | Guarded by `tests/test_ui.py::test_the_dashboard_javascript_parses`. There is no build step, so a stray duplicate `const` otherwise takes the whole page down silently — which is exactly what happened once during this pass. | `PASS` |
| `no-console-errors` | `Runtime.exceptionThrown` collected across every step below | `[]` | `PASS` |
| `no-external-fetch` | Static assertion in `tests/test_ui.py` | No CDN, font, absolute URL or `@import` anywhere in `dashboard.html` | `PASS` |
| `write-rejected-400` | `POST /api/query {"cypher":"CREATE (n)"}` | HTTP 400, `{"error": "read-only query: CREATE is not allowed"}` — the guard's own reason, not a fixed "writes are rejected" string | `PASS` |
| `cross-origin-rejected` | `POST /api/query` with `Origin: https://evil.example` and `Content-Type: text/plain` (a *simple* request, so no preflight protects us) | HTTP 403 `cross-origin request rejected` | `PASS` |
| `rebound-host-rejected` | `GET /api/status` with `Host: evil.example` against a loopback bind | HTTP 403 `unexpected Host header` | `PASS` |
| `ingest-requires-token` | `POST /api/ingest` with no `X-GF-Token`, then with a wrong one | HTTP 403 both times, `missing or invalid X-GF-Token` | `PASS` |
| `ingest-absent-when-public` | Handler built with a non-loopback bind | `<meta name="gf-token">` absent from the page; ingest endpoints 403 | `PASS` |
| `password-never-served` | `document.body.innerHTML` scanned after a database ingest | Neither the Neo4j nor the Postgres password present. Job log shows `postgresql://graphforge:***@127.0.0.1:55433/shopdb` | `PASS` |
| `first-run-empty-state` | Dashboard opened against an empty graph | `#firstrun` visible; graph, labels, relationship and console cards hidden; tiles read `0`. Captured as `docs/img/dashboard-empty.png` | `PASS` |
| `ingest-from-the-page` | Clicked **Add a repository**, filled the form, clicked **Start** | Job ran to completion: `1 repositories, 97 files, 19 commits`. Banner `Loaded …`. Tiles went `0` → `1,193` nodes and the first-run panel hid itself | `PASS` |
| `ingest-refreshes-counts` | Tiles read immediately after a job finished | Counts updated in the same tick — the TTL-cached schema snapshot is invalidated on success, so the numbers do not lag a minute behind an ingest the user just watched | `PASS` |
| `captions-are-names` | `G.nodes[*].caption` | `customers`, `orders`, `Hello.py`, `cli.py`, `bc114a3` — not `postgres://127.0.0…` repeated for every node | `PASS` |
| `caption-collision` | Dense cluster inspected at default zoom | Overlapping captions are dropped rather than drawn on top of each other; highest-degree wins | `PASS` |
| `legend-filters` | Clicked the `Column` legend chip | `G.hidden === ["Column"]`, canvas redrew without them; clicking again restored | `PASS` |
| `detail-panel` | Selected the highest-degree node | Heading `orders`, full id, `Table` chip, degree 15, neighbours grouped into 6 relationship types (`USES_TABLE·2`, `BASED_ON·1`, `HAS_INDEX·3`, `REFERENCES·3`, …) | `PASS` |
| `theme-persist` | Toggled the theme and reloaded | Class flips to `light` and back; `localStorage['gf.theme']` persists; canvas palette re-tunes via `--lightness` | `PASS` |
| `screenshot` | 1440×900, dark, settled layout, hub node selected | Saved to `docs/img/dashboard.png` and embedded in `README.md` | `PASS` |

## Known limitation

`GET /api/graph/sample` takes the first *N* relationships in store order. That is
deliberate — store order keeps related edges adjacent, so the canvas shows a
connected neighbourhood. Sampling at random was tried and reverted: it returns
edges that rarely share a node, and draws a field of disconnected pairs instead
of a graph (170 nodes for 120 relationships, against 118 for the same limit).

The cost is coverage: where one subgraph dwarfs the other, the smaller one may
not appear on the canvas at any sample size. Reach it through the label chips or
the search box. A stratified per-type sample would fix this properly and has not
been written.
