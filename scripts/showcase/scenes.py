"""What the showcase video shows and says. Edit this file, not render.py.

A product tour: the live graphforge dashboard and the knowledge graph it serves. No
repository or code walkthrough. Each scene is one view held on screen for exactly as long as
its narration takes to speak. ``render.py`` synthesises the narration first, measures it,
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
    {"zoom": ["canvas >> nth=0", -300]}    wheel over the element centre (negative = zoom in)
    {"fill": ["textarea", "MATCH (n) RETURN n LIMIT 5"]}
    {"wait": 800}                          milliseconds
"""

from __future__ import annotations

GITHUB_REPO = "vinothhacks/graphforge-neo4j"
BRANCH = "feature/audit-e2e-guard"

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

# Generative models, resolved through OpenRouter. These are the ONLY model IDs in the
# project; preflight checks each against the live catalogue and prints what it finds, so a
# wrong ID is caught before any request is made.
#
# The IDs originally requested were `meta/muse-image` and `minimax/hailuo-3-max`. As of
# 2026-09-06 neither is in OpenRouter's catalogue (430 models; the meta/muse-* entries are
# text-only, and OpenRouter lists no video-output model at all). If either appears, change
# the value here and preflight will confirm it.
MODELS = {
    "image": "google/gemini-3.1-flash-image",
    "video": None,  # no OpenRouter video model exists; the clip stage animates the images with ffmpeg
}

# Prompts for the two reference images. One word of text each, on purpose: image models
# render a single large word faithfully and garble anything smaller or longer.
IMAGE_PROMPTS = {
    "github-social": (
        "Striking, high-contrast 16:9 hero banner for a developer tool named graphforge. "
        "Deep midnight-blue to indigo gradient background with a soft radial glow. "
        "Centre-right: the single word 'graphforge' in very large, bold, crisp white geometric "
        "sans-serif letters -- this is the ONLY text in the image, spelled exactly, with "
        "generous margin from every edge. Left half: a luminous knowledge graph with a sense "
        "of depth -- glowing spheres in amber, teal, electric blue and magenta joined by thin "
        "light trails, soft bloom, slight depth of field. No labels on the nodes, no other "
        "words, no logos, no watermark. Cinematic, premium, editorial tech aesthetic."
    ),
    "linkedin-post": (
        "Eye-catching 16:9 announcement image for LinkedIn. Dark charcoal background with a "
        "radial glow behind the centre. A bold, luminous network graph bursting outward from "
        "one bright central node -- nodes in amber, teal, electric blue and magenta, glowing "
        "edges, fine particle sparkles. Centred over it, the single word 'graphforge' in huge, "
        "bold, clean white sans-serif letters -- the ONLY text in the image, spelled exactly, "
        "with wide margins from all edges. No other words, no small labels, no logos, no "
        "watermark. Vibrant, modern, premium, scroll-stopping."
    ),
}

# --------------------------------------------------------------------------- scenes
SCENES: list[dict] = [
    # The intro is not recorded: render.py builds it from the generated images.
    {"id": "intro", "kind": "title", "title": "graphforge"},
    {
        "id": "ui-overview",
        "kind": "dashboard",
        "title": "graphforge — a knowledge graph of code and data",
        "url": f"{DASHBOARD_URL}/",
        # The page paints its shell before it has data. Narration must not begin until
        # the status pill confirms the tiles are populated.
        "ready": "text=Neo4j connected",
        "actions": [{"top": True}],
        "narration": (
            "This is graphforge, live on a real knowledge graph. One thousand two hundred and "
            "ninety-four nodes across twenty labels. One thousand eight hundred and three "
            "relationships of twenty-five types. Four code repositories and one database schema, "
            "forged into a single Neo4j graph you can explore, query, and hand to an AI agent."
        ),
    },
    {
        "id": "ui-graph",
        "kind": "dashboard",
        "title": "The graph: every dot is a node, coloured by label",
        "url": None,
        "actions": [
            {"scroll_to": "Graph"},
            {"wait": 600},
            {"hover": "canvas >> nth=0"},
            {"wait": 900},
            {"drag": ["canvas >> nth=0", 160, 80]},
            {"wait": 700},
            {"zoom": ["canvas >> nth=0", -240]},
            {"wait": 600},
        ],
        "narration": (
            "The graph view. A force-directed sample of the graph, laid out live in the browser. "
            "Every dot is a node, coloured by label: methods in amber, files in green, classes "
            "in blue, modules in pink, and the repository itself at the centre. Hover to "
            "identify a node, drag to move it, scroll to zoom in, and drag the background to "
            "pan around."
        ),
    },
    {
        "id": "ui-graph-structure",
        "kind": "dashboard",
        "title": "Code and data in one connected structure",
        "url": None,
        "actions": [
            {"zoom": ["canvas >> nth=0", 260]},
            {"wait": 700},
            {"drag": ["canvas >> nth=0", -120, 60]},
            {"wait": 600},
            {"hover": "canvas >> nth=0"},
        ],
        "narration": (
            "What the graph holds. Files contain classes, classes contain methods, and commits "
            "record which files changed. On the data side, databases hold schemas, schemas hold "
            "tables, tables hold columns, and stored procedures use tables. Code and data are "
            "not two systems here. They are one connected structure, and the links between them "
            "are what you get to ask about."
        ),
    },
    {
        "id": "ui-labels",
        "kind": "dashboard",
        "title": "Twenty labels, live counts, click to sample",
        "url": None,
        "actions": [
            {"scroll_to": "Nodes by label"},
            {"wait": 700},
            {"click": "button:has-text('Method')"},
            {"wait": 1200},
        ],
        "narration": (
            "Twenty labels, each with its live count. Click one to sample it. Here, Method: seven "
            "hundred and fifty-one of them, pulled straight from the graph, so you can see exactly "
            "what was ingested."
        ),
    },
    {
        "id": "ui-rels",
        "kind": "dashboard",
        "title": "Twenty-five relationship types",
        "url": None,
        "actions": [{"scroll_to": "Relationship types"}, {"wait": 800}],
        "narration": (
            "Twenty-five relationship types. CONTAINS METHOD and IMPORTS dominate the code side. "
            "CHANGED links commits to the files they touched. FOREIGN KEY, HAS COLUMN and USES "
            "TABLE describe the database side. Together they are the edges you traverse."
        ),
    },
    {
        "id": "ui-cypher",
        "kind": "dashboard",
        "title": "Ask the graph anything — read-only Cypher",
        "url": None,
        "actions": [
            {"scroll_to": "Cypher console"},
            {"wait": 600},
            {"click": "button:has-text('Run')"},
            {"wait": 1500},
        ],
        "narration": (
            "Ask the graph anything in Cypher. This query counts nodes by label. The console is "
            "strictly read-only: writes and unsafe procedure calls are rejected on the server "
            "before a transaction is ever opened, so exploring is always safe."
        ),
    },
    {
        "id": "ui-tables",
        "kind": "dashboard",
        "title": "Every ingested source, side by side",
        "url": None,
        "actions": [{"scroll_to": "Repositories"}, {"wait": 800}],
        "narration": (
            "Every ingested source, side by side. Four repositories with their file and commit "
            "counts, and the Postgres schema with its five tables, all living in the same graph."
        ),
    },
    {
        "id": "ui-config",
        "kind": "dashboard",
        "title": "Configuration at a glance — passwords never displayed",
        "url": None,
        "actions": [{"scroll_to": "Neo4j"}, {"wait": 800}],
        "narration": (
            "Configuration at a glance: which Neo4j instance and which database source are in "
            "use. Passwords are never displayed."
        ),
    },
    {
        "id": "ui-outro",
        "kind": "dashboard",
        "title": "graphforge",
        "url": None,
        "actions": [{"top": True}, {"wait": 500}, {"scroll_to": "Graph"}, {"wait": 500}],
        "narration": (
            "graphforge. Git repositories and relational databases, forged into one Neo4j "
            "knowledge graph. Explore it here in the dashboard, or let an AI agent query it over "
            "the Model Context Protocol."
        ),
    },
]
