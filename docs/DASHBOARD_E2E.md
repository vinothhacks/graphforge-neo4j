# Dashboard E2E Runbook (Playwright MCP)

Executed via Cursor's `user-Playwright` MCP server against local playground
`graphforge ui` on `http://127.0.0.1:8000`.

**Harness rules:**
- Security tests run first.
- Pytest covers HTTP/CLI only; Playwright MCP covers the browser DOM/interactions.
- Never assert pass without an MCP step evidence record.

---

## Step results

| Step id | MCP operations | Assertion / Evidence | Status |
|---------|----------------|----------------------|--------|
| `no-external-fetch` | `browser_network_requests` `{static:true}` | 4 requests captured (`/`, `/api/status`, `/api/schema`, `/api/graph/sample`). All hosts `127.0.0.1:8000`. No third-party CDN/fonts/trackers. | `PASS` |
| `write-rejected-400` | `browser_evaluate` `fetch('/api/query', {method:'POST', body:'{"cypher":"CREATE (n)"}'})` | Returned HTTP 400 with the guard's own reason (`{"error": "read-only query: CREATE is not allowed"}`). Additional checks for `MATCH (n) SET`, `LOAD CSV`, `CALL apoc...` also returned 400. | `PASS` |
| `password-never-served` | `browser_evaluate` `document.documentElement.outerHTML` | Secrets `graphforge-playground` and `please-change-me` absent. Masked strings `•••••• (set)` present. | `PASS` |
| `canvas-expand` | Canvas click via bounding box offset | Highlighted node degree and neighbours populated in `#nodeinfo` (`products#product_id · Column · degree 2 · 2 neighbours...`). | `PASS` |
| `console-history` | Cypher query entry + `Control+Enter` | History dropdown populated; query executed returning 5 rows for `MATCH (t:Table) RETURN t.name`. | `PASS` |
| `label-explorer` | Click `Table` chip | Modal opened displaying sample `Table` nodes (`customers`, `order_items`, `orders`, `products`, `shipments`). | `PASS` |
| `search-all-labels` / `search-scoped` | Type `customers` in `#q` | Dropdown showed 8 hits (Table, Column, Index); scoped search via API returned 1 row for Table label. | `PASS` |
| `theme-persist` | Click theme toggle, `browser_navigate` `http://127.0.0.1:8000` | Theme state persisted in `localStorage` (`gf.theme`) across reload (`themeLabel: Dark` after switching to light, verified dark re-activated for screenshot). | `PASS` |
| `keys-r-slash-esc` | `browser_press_key` (`Escape`, `/`, `r`) | `Escape` closed modal; `/` focused `#q` search input (`activeElement.id === 'q'`); `r` reloaded status. | `PASS` |
| `dashboard-screenshot` | `browser_take_screenshot` (1440x900 viewport, dark mode, settled graph) | Saved to `docs/img/dashboard.png` (223 KB) and wired into `README.md`. | `PASS` |
