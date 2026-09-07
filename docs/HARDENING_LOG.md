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
| 0 | Baseline, safety, format debt | **complete** — `audit/p00-gate` |
| 1 | Packaging & pip | **complete** — `audit/p01-gate` |
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

**Reformat (a697f48).** `ruff format src tests` — 61 files reformatted, 8 already
clean, nothing outside `src/` and `tests/` touched. AST-preservation proved by an
identical suite either side: 290 passed / 10 deselected before, 290 passed / 10
deselected after.

**CI alignment (860ec03).** Both ruff pins moved 0.8.6 -> 0.16.5 together, and the
format step became `ruff format --check`. CI's lint job simulated verbatim on this
host: `ruff check --output-format=github src tests` clean, `ruff format --check src
tests` reports 69 files already formatted, exit 0.

**Docker recovered mid-phase.** The daemon came back and both playground
containers restarted from their existing volumes
(`graphforge-playground_playground_neo4j_data`, `..._pg_data`), on the recorded
ports 7474/7687 and 55433.

**S-2 re-proved — the graph is intact.** `graphforge verify --env audit.env
--neo4j-uri bolt://127.0.0.1:7687` reports `Repository 4` and ~1,294 nodes,
matching the plan's recorded ~1,290. `graphforge status` names all four with the
recorded counts:

| repository | status | files | commits |
|---|---|---|---|
| e2e-inc | completed | 2 | 2 |
| e2e-replace | completed | 1 | 2 |
| e2e-twice | completed | 1 | 1 |
| graphforge | completed | 97 | 19 |

**S-1 harness created.** `audit.env` (playground Neo4j + Postgres 55433) and
`audit.env.empty` (the volume-less throwaway `graphforge-audit-empty` on 7688/7475,
which cannot outlive the pass). Every `DB_*` in both points at localhost, so an
accidental read cannot reach the corporate host.

**Defect in the plan's own safety assumption.** The plan stated `audit.env` would
be "matched by `.gitignore`'s `*.env`". That pattern matches only names *ending*
in `.env`, so `audit.env.empty` was **not** ignored and would have been committed.
Fixed with a scoped `audit.env*` rule. `*.env.*` was rejected because it would
also match `.env.example` and defeat the `!.env.example` negation directly above
it. Verified: both audit files ignored, `.env.example` still tracked.

**Gate — G-1 and G-2 both green:**

| Gate | Command | Result |
|---|---|---|
| Suite | `pytest -q -m "not e2e_critical and not e2e_full"` | **290 passed**, 10 deselected |
| Lint | `ruff check src tests` | All checks passed |
| Format | `ruff format --check src tests` | 69 files already formatted |
| Types | `mypy` | Success, 40 source files |
| S-1 | session env free of `NEO4J_*`/`DB_*`/`GF_*`; `audit.env` in use | proved |
| S-2 | 4 repos, counts unchanged | proved |

**Live browser smoke test (Playwright MCP, not yet the P2 harness).** `graphforge
ui --env audit.env --neo4j-uri bolt://127.0.0.1:7687 --port 8765`, driven in a
real browser. Recorded because it is evidence from an actual run, not because it
replaces P2.

| Check | Evidence |
|---|---|
| Page loads | title `graphforge dashboard`, `Neo4j connected` |
| Tiles reconcile with the CLI | 1,294 nodes / 1,803 relationships / 4 repositories / 1 database |
| Label counts reconcile | all 20 chips equal the `graphforge verify` output (Method 751, Class 295, File 116, ...) |
| Repositories table reconciles | matches `graphforge status` row for row: graphforge 97/19, e2e-inc 2/2, e2e-replace 1/2, e2e-twice 1/1 |
| Graph canvas renders | 118 nodes / 120 relationships sampled, labelled, with a legend |
| **Security — no credential on the page** | Neo4j and DB passwords both render `•••••• (set)`; harness rule satisfied |
| **S-1 confirmed in the browser** | config panel shows `postgres / 127.0.0.1:55433 / shopdb` — `audit.env`, not the corporate `.env` |
| Console | **1 error: `GET /favicon.ico` 404** |

The favicon 404 is not a new defect: P10 already plans to serve `/favicon.ico`
from `server.py` as a base64 PNG, because `tests/test_ui.py:84,88` ban the
substrings `http`, `://` and `<link` anywhere in `dashboard.html`, which rules out
both a `<link>` tag and a parseable SVG data URI. This run confirms the 404 the
plan predicted.

The dashboard currently on the branch is the `a81f06d` redesign — the P10
*baseline*. It reads as one long scrolling document, which is what P10's app-shell
grid, persistent rail and hash routing are meant to replace.

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

### P1 continued — packaging

**Stale `build/` tree deleted.** 39 `.py` files, untracked and gitignored, with
`build/lib/graphforge/mcp/server.py` dated Aug 30 against `src/`'s Sep 5 — six
days stale and predating every change in this pass. `build_py` never removes
stale files, so a build could have shipped the old module.

**`MANIFEST.in` added.** setuptools put `tests/*.py` in the sdist and stopped
there, so `tests/test_mssql_fixtures.py` shipped without the
`tests/fixtures/mssql/information_schema.json` it reads — the sdist's own suite
could not run. Verified before (`ABSENT`) and after
(`graphforge_neo4j-0.3.0/tests/fixtures/mssql/information_schema.json`).

**Package data now resolved with `importlib.resources`.** `neo4j_writer.load_schema`
and the dashboard read used `Path(__file__)`, which assumes an unpacked install
on a real filesystem. Chained `joinpath` because the multi-argument form is 3.11+
and this package supports 3.10. Proved from a real `site-packages` wheel install,
not the source tree: all three `.cypher` files and `dashboard.html` load.

**`neo4j>=5.0.0` → `>=5.14,<7`**, with both ends probed against the live
playground rather than asserted: 5.14.0 and 6.3.0 each give `verify` exit 0 and
`Repository=4`. A blocking `driver-range` CI job now holds them there.

**Import cost: 260 ms (plan) → 83.9 ms**, against a target of 120 ms.
`importlib.metadata` and `tqdm` are both now completely off the import path
(0 references each). Two changes were needed, because either alone does nothing:
`__version__` became a PEP 562 module `__getattr__`, *and* `cli.py` stopped doing
`from . import __version__` at module level, which would have triggered it at
import time anyway. argparse's built-in `version` action needs the string up
front, so it was replaced with an action that resolves on use. `tqdm` moved to a
first-use import inside `neo4j_writer.tqdm` — verified to still return a real
`tqdm.std.tqdm`, not the fallback.

#### Two defects the phase found

**1. A missing driver exited 0.** `graphforge db --url mysql://…` on a bare
install reported `pushed 0 operations`, exited **0**, and logged the
`pip install 'graphforge-neo4j[mysql]'` line at a level `-v` hides. Any script or
CI step would have read that as success. Cause: `db/ingest.py` catches every
exception per-database — correct for one bad credential in a ten-database run,
wrong for a missing driver, which fails *every* database on that engine. Fixed
with `MissingExtra(RuntimeError)` in `core/errors.py`, raised by all three
drivers and deliberately re-raised at both catch sites. It subclasses
`RuntimeError`, so the CLI's existing handler — whose comment already named
"missing driver" as an expected user-facing failure — turns it into `error: …`
and exit 2 unchanged.

| extra | before | after |
|---|---|---|
| mysql | exit 0, hint hidden behind `-v` | exit 2, `pip install 'graphforge-neo4j[mysql]'`, no traceback |
| postgres | exit 0, hint hidden | exit 2, `pip install 'graphforge-neo4j[postgres]'`, no traceback |
| mssql | exit 0, hint hidden | exit 2, `pip install 'graphforge-neo4j[mssql]'`, no traceback |
| mcp | bare `ModuleNotFoundError` | exit 2, `pip install -U 'graphforge-neo4j[mcp]'`, no traceback |

**2. The friendly port-conflict message never fired on Windows.** `cli.py`
checked `errno in (48, 98, 10013, 10048)`, but 10013/10048 are WSA codes that
live on `.winerror` and can never appear in `errno`. Measured: Windows raises
`PermissionError` with `errno=13` (the translated POSIX value) and
`winerror=10013`. So a taken port printed a raw
`[WinError 10013] An attempt was made to access a socket…` with no next-port
hint — exactly the branch the plan noted CI has never run. Now checks both
attributes; against a real occupied socket it gives
`error: port 49687 is not available - try 'graphforge ui --port 49688'`.

Both defects got regression tests, and both tests were **proved to have teeth**
by reverting the fix and confirming the failure: `DID NOT RAISE MissingExtra`,
and `PermissionError: [WinError 10013]` respectively.

**Evidence:**

| Check | Result |
|---|---|
| Offline suite | **292 passed**, 10 deselected (290 + 2 new regression tests) |
| `ruff check` / `ruff format --check` / `mypy` | clean · 69 formatted · Success, 40 files |
| `python -m build` + `twine check --strict` | PASSED, wheel and sdist |
| Package data | verified by unzipping, and by loading from an installed wheel |
| Matrix — py3.10/3.11/3.13 × 8 extras | **24/24**, each `--version` + `--help` + 12 subcommand helps |
| Extras guards absent | 4/4 exit 2, actionable line, no traceback |
| Port conflict | friendly message, exit 2, no traceback |
| Driver range | 5.14.0 and 6.3.0 both `verify` exit 0, `Repository=4` |
| S-2 | 4 repos, counts unchanged |

One caveat recorded honestly: on the first matrix run `py3.13-dev` failed to
install mypy with a uv Windows trampoline error
(`Failed to update Windows PE resources … os error -2147024786`). It passed
cleanly on retry (`12/12`), so it was environmental, not a packaging defect.

Note the matrix axis: the plan specifies Python version × extras. An earlier run
in this session used install-source × extras on 3.12 only — that is also all
green (wheel, sdist and editable × 8 extras) but it is **not** the specified
matrix, and the table above is the specified one.
