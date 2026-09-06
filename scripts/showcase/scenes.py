"""What the showcase video shows and says. Edit this file, not render.py.

Each scene is one page held on screen for exactly as long as its narration
takes to speak. ``render.py`` synthesises the narration first, measures it,
then drives the browser to match -- so the two cannot drift apart.

Optional per-scene ``"ready": <selector>``: after navigation, wait for this before the
narration clock starts. Use it for pages that paint a shell first and fill in later.

Action DSL (executed in order, after the narration clock starts, before the hold):
    {"top": True}                          scroll to the top of the page
    {"scroll": 900, "steps": 6}            wheel down 900px in 6 smooth steps
    {"scroll_to": "Relationship types"}    scroll a heading into view
    {"click": "button:has-text('Run')"}    Playwright selector
    {"hover": "canvas >> nth=0"}
    {"drag": ["canvas >> nth=0", 120, 60]} press at the element centre, move by dx/dy
    {"fill": ["textarea", "MATCH (n) RETURN n LIMIT 5"]}
    {"wait": 800}                          milliseconds
"""

from __future__ import annotations

GITHUB_REPO = "vinothhacks/graphforge-neo4j"
BRANCH = "feature/audit-e2e-guard"
GH = f"https://github.com/{GITHUB_REPO}"

# The commit the narration singles out: the mcp 1.x -> 2.x migration.
MCP_COMMIT = "a66fdd6bf23c10fc4306b329644f62f5409c7d94"

# The dashboard is started by render.py from audit.env (never .env -- see S-1 in
# docs/HARDENING_LOG.md) on this port, and torn down afterwards.
UI_PORT = 8765
DASHBOARD_URL = f"http://127.0.0.1:{UI_PORT}"

# Microsoft neural voice used by edge-tts. Others worth trying:
# en-US-AriaNeural, en-US-ChristopherNeural, en-GB-RyanNeural, en-IN-PrabhatNeural.
VOICE = "en-US-GuyNeural"
VOICE_RATE = "+0%"

# Burn a lower-third title into the recording for each scene.
TITLES = True

# Generative models, resolved through OpenRouter. These are the ONLY model IDs in
# the project; preflight checks each against the live catalogue and prints what it
# finds, so a wrong ID is caught before any request is made.
#
# The IDs originally requested were `meta/muse-image` and `minimax/hailuo-3-max`.
# As of 2026-09-06 neither is in OpenRouter's catalogue (430 models; the meta/muse-*
# entries are text-only, and OpenRouter lists no video-output model at all). If
# either appears, change the value here and preflight will confirm it.
MODELS = {
    "image": "google/gemini-2.5-flash-image",
    "video": None,  # no OpenRouter video model exists; the clip stage animates the images with ffmpeg
}

# Prompts for the two reference images. Kept factual so the model illustrates
# the real product rather than inventing one.
IMAGE_PROMPTS = {
    "github-social": (
        "A clean, modern 2:1 wide social-preview banner for an open-source developer tool "
        "called 'graphforge'. Dark navy background. On the left, a glowing force-directed "
        "knowledge graph: nodes in amber, teal, blue and pink connected by thin lines, "
        "suggesting source files, classes, methods, database tables and columns linked "
        "together. On the right, the word 'graphforge' in large bold white sans-serif "
        "lettering with the tagline 'Git + databases -> one Neo4j knowledge graph' beneath "
        "it in smaller light-grey text. Small tasteful icons for Git, Neo4j and a terminal. "
        "Flat vector style, high contrast, no photographic elements, no extra words."
    ),
    "linkedin-post": (
        "A polished LinkedIn post image, landscape, for announcing an engineering project. "
        "Dark slate background with a subtle grid. Centre: a stylised network graph of "
        "colourful nodes (amber, teal, blue, pink) fanning out from a central node, "
        "representing code and database structures joined into one knowledge graph. "
        "Top-left: bold white title 'graphforge'. Bottom: three short white labels in a row "
        "reading 'Python', 'Neo4j', 'MCP'. Professional, minimal, tech-forward, flat "
        "illustration style, no people, no photographs, no other text."
    ),
}

# --------------------------------------------------------------------------- scenes
SCENES: list[dict] = [
    # The intro is not recorded: render.py builds it from the generated images.
    {"id": "intro", "kind": "title", "title": "graphforge"},
    # ----------------------------------------------------------------- GitHub
    {
        "id": "gh-readme",
        "kind": "github",
        "title": "What graphforge is",
        "url": f"{GH}/tree/{BRANCH}",
        "actions": [{"wait": 600}, {"scroll": 700, "steps": 7}],
        "narration": (
            "This is graphforge. It takes Git repositories and relational databases and forges "
            "them into a single Neo4j knowledge graph. Files, classes, methods, commits, tables "
            "and columns, all connected. That graph is served two ways: to people, through a "
            "browser dashboard, and to AI agents, through the Model Context Protocol."
        ),
    },
    {
        "id": "gh-commits",
        "kind": "github",
        "title": "How the work was done: one contract per commit",
        "url": f"{GH}/commits/{BRANCH}",
        "actions": [{"wait": 600}, {"scroll": 1200, "steps": 10}],
        "narration": (
            "Here is how the work was done. This branch carries a hardening pass, and every "
            "commit follows the same contract. Each one names the phase it belongs to, states "
            "exactly what was verified, test counts, lint, types, and points at the evidence in "
            "the hardening log. Because phases are tagged at their gates, any one of them can be "
            "reverted as a single unit."
        ),
    },
    {
        "id": "gh-commit-mcp",
        "kind": "github",
        "title": "The key find: an MCP server dead on arrival",
        "url": f"{GH}/commit/{MCP_COMMIT}",
        "actions": [{"wait": 600}, {"scroll": 900, "steps": 9}],
        "narration": (
            "This commit is the most important find of the pass. The MCP server was dead on "
            "arrival for any fresh install: the SDK had renamed its server class in version two, "
            "and the dependency had no upper bound. Three independent blind spots hid it. The "
            "tests stubbed the SDK entirely, the type check in CI installed an extra that did not "
            "include it, and nothing pinned the version. The fix migrated to the new API, added a "
            "lock the new threading model needed, and replaced a test stub that could never have "
            "noticed."
        ),
    },
    {
        "id": "gh-log",
        "kind": "github",
        "title": "The ledger: measured evidence per phase",
        "url": f"{GH}/blob/{BRANCH}/docs/HARDENING_LOG.md",
        "actions": [{"wait": 700}, {"scroll": 1800, "steps": 14}],
        "narration": (
            "The hardening log is the ledger. Every phase records what was actually measured "
            "rather than what was hoped. Two safety rules run through all of it. S one: never "
            "touch the corporate database, so every command uses an isolated audit environment. "
            "S two: never destroy the graph, so the four repositories and their counts are "
            "re-proved at every checkpoint. And two global gates, the test suite, lint, format "
            "and types, must be green before any phase closes."
        ),
    },
    {
        "id": "gh-ci",
        "kind": "github",
        "title": "CI: every gate is blocking",
        "url": f"{GH}/blob/{BRANCH}/.github/workflows/ci.yml",
        "actions": [{"wait": 700}, {"scroll": 1600, "steps": 12}],
        "narration": (
            "Continuous integration enforces the same bar on every push. Lint and type checks are "
            "blocking. A driver-range job tests both ends of the Neo4j version pin, because a "
            "range is a claim about two versions and only one of them had ever been tested. "
            "Packaging, an integration run and a secret scan complete the set."
        ),
    },
    {
        "id": "gh-tags",
        "kind": "github",
        "title": "Gate tags: the containment mechanism",
        "url": f"{GH}/tags",
        "actions": [{"wait": 600}, {"scroll": 300, "steps": 3}],
        "narration": (
            "Gate tags are the containment mechanism. Each closed phase receives an audit tag, "
            "p zero zero, p zero one, so a fault is contained to the phase that caused it, and the "
            "exact range to revert is always known."
        ),
    },
    {
        "id": "gh-techstack",
        "kind": "github",
        "title": "Tech stack, straight from pyproject.toml",
        "url": f"{GH}/blob/{BRANCH}/pyproject.toml",
        "actions": [{"wait": 700}, {"scroll": 1400, "steps": 12}],
        "narration": (
            "The tech stack, straight from the project file. Python three point ten through three "
            "point thirteen. The official Neo4j driver, pinned from five point fourteen up to but "
            "not including seven, with both ends verified live. MCP version two for the agent "
            "interface. Database drivers are optional extras, psycopg2 for Postgres, mysql "
            "connector, and pyodbc for SQL Server, so the core install stays light. Ruff, mypy "
            "and pytest guard quality, Playwright drives the browser tests, and Docker provides a "
            "Neo4j and Postgres playground."
        ),
    },
    # -------------------------------------------------------------- dashboard
    {
        "id": "ui-overview",
        "kind": "dashboard",
        "title": "The dashboard, live on the playground graph",
        "url": f"{DASHBOARD_URL}/",
        # The page paints its shell before it has data. Narration must not begin
        # until the status pill confirms the tiles are populated.
        "ready": "text=Neo4j connected",
        "actions": [{"top": True}],
        "narration": (
            "Now the product itself, running against the playground graph. One thousand two "
            "hundred and ninety-four nodes across twenty labels. One thousand eight hundred and "
            "three relationships of twenty-five types. Four repositories ingested, and one "
            "database schema. Every number here reconciles exactly with the command-line verify "
            "output."
        ),
    },
    {
        "id": "ui-graph",
        "kind": "dashboard",
        "title": "Graph view: force-directed, coloured by label",
        "url": None,
        "actions": [
            {"scroll_to": "Graph"},
            {"wait": 800},
            {"hover": "canvas >> nth=0"},
            {"wait": 900},
            {"drag": ["canvas >> nth=0", 140, 70]},
            {"wait": 600},
        ],
        "narration": (
            "The graph view samples relationships and lays them out with a force-directed "
            "simulation. Each colour is a label: methods, files, classes, modules, and the "
            "repository itself. Hover a node to identify it, drag to rearrange, scroll to zoom, "
            "and pan the background. The legend keeps the colours honest."
        ),
    },
    {
        "id": "ui-labels",
        "kind": "dashboard",
        "title": "Twenty labels, click one to sample it",
        "url": None,
        "actions": [
            {"scroll_to": "Nodes by label"},
            {"wait": 700},
            {"click": "button:has-text('Method')"},
            {"wait": 1200},
        ],
        "narration": (
            "Twenty labels, each with its live count. Clicking one, Method here, samples nodes of "
            "that label straight from the graph, so you can inspect exactly what was ingested."
        ),
    },
    {
        "id": "ui-rels",
        "kind": "dashboard",
        "title": "Twenty-five relationship types",
        "url": None,
        "actions": [{"scroll_to": "Relationship types"}, {"wait": 800}],
        "narration": (
            "Twenty-five relationship types. CONTAINS METHOD and IMPORTS dominate, then CHANGED, "
            "which links commits to the files they touched, and the structural links between "
            "databases, schemas, tables and columns."
        ),
    },
    {
        "id": "ui-cypher",
        "kind": "dashboard",
        "title": "Cypher console: read-only, guarded server-side",
        "url": None,
        "actions": [
            {"scroll_to": "Cypher console"},
            {"wait": 600},
            {"click": "button:has-text('Run')"},
            {"wait": 1500},
        ],
        "narration": (
            "A Cypher console for asking the graph anything, but strictly read-only. Writes, "
            "procedure calls outside an allow-list, and multi-statement queries are rejected by a "
            "layered guard on the server before a transaction is ever opened. The client cannot "
            "bypass it."
        ),
    },
    {
        "id": "ui-tables",
        "kind": "dashboard",
        "title": "Repositories and databases ingested",
        "url": None,
        "actions": [{"scroll_to": "Repositories"}, {"wait": 800}],
        "narration": (
            "The repositories table shows each ingest with its file and commit counts and status. "
            "Beside it, the database table: the Postgres shop schema, five tables, ingested "
            "alongside the code."
        ),
    },
    {
        "id": "ui-config",
        "kind": "dashboard",
        "title": "Configuration: passwords are never displayed",
        "url": None,
        "actions": [{"scroll_to": "Neo4j"}, {"wait": 800}],
        "narration": (
            "Configuration panels show exactly which Neo4j instance and which database source are "
            "in use, and note that passwords are never displayed. This instance is reading the "
            "isolated audit environment, not the real dot env file. Safety rule S one, visible in "
            "the browser."
        ),
    },
    # ------------------------------------------------------------------ outro
    {
        "id": "outro",
        "kind": "github",
        "title": "Reproducible: scripts/showcase in the repo",
        "url": f"{GH}/tree/{BRANCH}/scripts/showcase",
        "actions": [{"wait": 700}],
        "narration": (
            "Everything you have seen is reproducible. The scripts that recorded this walkthrough, "
            "generated the narration and assembled the video live in the repository, under "
            "scripts, showcase. The video and the reference images are attached to the release. "
            "Thanks for watching."
        ),
    },
]
