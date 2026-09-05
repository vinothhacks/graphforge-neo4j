# Hardening log

The phase ledger for the 0.3.0 hardening pass. One section per phase, written as
the phase runs, so `docs/HARDENING_REPORT.md` can be assembled from measured
evidence rather than recollection.

**Harness rules** (lifted from `docs/DASHBOARD_E2E.md`):
- Security steps run first.
- Never record a pass without an evidence value from the actual run.
- A step that reads a credential out of the page is a failure, not a note.

**Standing safety rules:**
- **S-1 — never touch the corporate database.** `.env`'s `DB_*` block points at a
  real corporate MySQL host and `load_dotenv` searches upward from CWD. Every
  command passes `--env audit.env` *and* an explicit `--url`. `--dry-run` is not a
  safe rehearsal: it skips Neo4j but still connects to the source.
- **S-2 — never destroy the graph.** Additive only, test data under `audit-*` /
  `gf-scale-*`. No `DETACH DELETE` on 7687, no `--replace` on repo `graphforge`.

**Global gates at every checkpoint:**
- **G-1** — `pytest -q -m "not e2e_critical and not e2e_full and not e2e_browser"`
  at or above the running baseline, plus `ruff check`, `ruff format --check`, and
  `mypy` clean.
- **G-2** — S-1 and S-2 re-proved.

| # | Phase | Status |
|---|---|---|
| 0 | Baseline, safety, format debt | in progress |
| 1 | Packaging & pip | mcp 2.x fix landed early (see below) |
| 2 | Browser harness + UI security | not started |
| 3 | UI-A shell, status, tiles, first-run, theme, keys | not started |
| 4 | UI-B canvas, inspector, legend | not started |
| 5 | UI-C search, console, labels, tables | not started |
| 6 | UI-D guided ingest | not started |
| 7 | Concurrency & pooled driver | not started |
| 8 | Ingest truthfulness & large-repo scale | not started |
| 9 | Databases + VDS | not started |
| 10 | UI redesign — Langflow grade | not started |
| 11 | MCP + CLI sweep | not started |
| 12 | CI, docs, final MD | not started |

---

## Phase 0 — Baseline, safety, format debt

**Environment drift found on 2026-09-04.** The plan's measured baseline did not
reproduce, because the host toolchain had changed since it was written:

| Item | Plan baseline (2026-08-31) | Found | Action |
|---|---|---|---|
| `graphforge` | 0.3.0 installed | **not installed anywhere** | reinstalled editable into Python 3.12 |
| `playwright` | 1.62.0 + chromium | **absent** | needed for P2; not yet installed |
| `ruff` | 0.8.6 | 0.16.5 | see below |
| `mypy` | (unstated) | 2.3.1 | baseline still clean after the mcp fix |
| `pytest` | 9.1.1 | 9.1.1 | unchanged |
| Docker daemon | playground stack up | **down**, no ports listening | blocks P0 container work, P8, P9 |

The missing `graphforge` install is also why this session's `graphforge` MCP
server reported `CONNECTION_CLOSED`: the configured executable
(`…/hermes/hermes-agent/venv/Scripts/graphforge.exe`) does not exist.

**S-1 precondition re-proved:** session env free of `NEO4J_*` / `DB_*` / `GF_*`.

**Baseline measured (this host):**

| Gate | Command | Result |
|---|---|---|
| Offline suite | `pytest -q -m "not e2e_critical and not e2e_full"` | **288 passed**, 10 deselected, 47.7 s |
| Lint | `ruff check src tests` (0.16.5) | All checks passed |
| Format | `ruff format --check src tests` | **61 files would be reformatted**, 8 clean |
| Types | `mypy` | **1 error** (mcp 2.x; see Phase 1) |
| CLI | `graphforge --version` | `graphforge 0.3.0` |

**On the ruff version.** CI (`ci.yml`) and `.pre-commit-config.yaml` both pin
0.8.6; the host has 0.16.5. `ruff check src tests` is clean and the format scope
is identical (61 / 8) under both, so the upgrade carries no lint risk. The tree is
formatted once with 0.16.5 and CI plus pre-commit are pinned to match, rather than
freezing a formatter from January 2025.

**CI was red, and the stale comment said otherwise.** `ci.yml`'s `lint` job is
blocking (the only `continue-on-error: true` is on an unrelated job) and runs
`ruff format --diff`, which exits non-zero against 61 unformatted files. The
comment above the job claiming the step "stays informational" describes no
mechanism that makes it so.

## Phase 1 — Packaging & pip

Landed out of order, because it blocked G-1 for every later phase.

**Escaped defect: the MCP server was dead on arrival for any fresh install.**
`pyproject.toml` declared `mcp = ["mcp>=1.2.0"]` with no upper bound. The SDK's
2.x release renamed `FastMCP` to `MCPServer` and moved it out of
`mcp.server.fastmcp`, so `build_server()` raised `ModuleNotFoundError` on its lazy
import at `src/graphforge/mcp/server.py:763`.

Three independent blind spots kept it invisible:
1. **The tests stubbed the SDK.** `_build_server_with_fakes` injected fake
   `mcp` / `mcp.server` / `mcp.server.fastmcp` modules, so the real import never
   ran and all 288 tests passed against a broken code path.
2. **CI's mypy could not see it.** The `types` job installs `-e ".[dev]"`, and
   `dev` carries no `mcp`, so the module was absent and
   `ignore_missing_imports = true` silenced the error.
3. **No upper bound**, so every fresh install resolved to the incompatible major.

**Resolution: migrated to mcp 2.x** rather than pinning `mcp<2`.

- `from mcp.server.mcpserver import MCPServer`; `MCPServer("graphforge")`.
- `@server.tool()` is unchanged and graphforge never used `Context`, so the 12
  tool definitions are untouched. `.run()` takes no stdio-specific kwargs, so the
  entrypoint is untouched.
- **Constraint bumped to `mcp>=2`** in both places. This is a breaking change to
  the `[mcp]` extra and needs a CHANGELOG entry in P12.
- **New concurrency hazard, fixed here.** 2.x runs sync tool handlers in worker
  threads (verified: handlers execute on `AnyIO worker thread`). `LazyGraph.connect`
  was check-then-act on `self._graph`, unreachable under 1.x's serialised event
  loop but racy under 2.x — two calls could each build a driver. Now
  double-checked under a `threading.Lock`. **P7 still owns the real pool.**
- The eager `import neo4j` in `LazyGraph.__init__` (the `66f2e9d` deadlock fix) is
  unchanged and still required.
- **Wire-format delta for P11 to verify:** 2.x derives an `output_schema` from the
  `-> str` annotation and now also returns
  `structured_content={"result": "<json>"}`. The `TextContent` payload is
  unchanged, so the change is additive.

**Blind spot closed.** The stub is gone; `_build_server_with_fakes` now builds a
real `MCPServer` and fakes only Neo4j, through the `graph=` seam the code already
provides. Tool callables are recovered via `Tool.fn`, so every existing assertion
survives unedited. Two regression tests added:
`test_build_server_uses_the_real_mcp_package` and
`test_tools_are_registered_on_the_real_protocol_surface`.

**Evidence:**

| Check | Result |
|---|---|
| Offline suite | **290 passed**, 10 deselected, 43.2 s (288 baseline + 2 new) |
| `ruff check src tests` | All checks passed |
| `mypy` | Success, 40 source files (was 1 error) |
| MCP over real stdio | `initialize` handshake OK (protocol `2025-06-18`); `tools/list` returned **12 tools**; stderr clean; **no database required** |

Still open for P1 proper: delete the stale `build/` tree, `python -m build`,
`twine check --strict`, wheel package-data verification, 24 venvs × 12 helps.
