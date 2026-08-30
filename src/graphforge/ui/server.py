"""A dependency-free status dashboard served from Python's stdlib http.server.

`graphforge ui` starts this. It shows the resolved configuration (passwords
masked — never displayed), Neo4j connection health, node counts by label, and
per-repository / per-database load status pulled live from the graph.

Beyond `/` and `/api/status` it exposes a handful of small read-only JSON
endpoints that back the dashboard's graph canvas, Cypher console, label
explorer and search box:

* ``GET  /api/schema``                     — labels with counts + relationship types
* ``GET  /api/labels/<label>/sample``      — sample nodes of one label
* ``GET  /api/node/<id>/neighbors``        — 1-hop neighbourhood of a node id
* ``GET  /api/search?q=&label=&prop=``     — substring search
* ``GET  /api/graph/sample``               — nodes/links for the canvas viz
* ``POST /api/query`` ``{"cypher": ...}``  — read-only Cypher

Routing lives in the pure :func:`route` function (strings in, ``(status,
payload)`` out) so it can be unit-tested without sockets or a live database.
Every endpoint validates its own input and re-applies the MCP write guard
**server-side** — the browser is never trusted.
"""
from __future__ import annotations

import contextlib
import json
import logging
import secrets
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from ..core.config import Settings
from ..mcp.server import _IDENT as _IDENT_RE
from ..mcp.server import GraphQuery
from ..query_guard import check_read_query as _query_denial

log = logging.getLogger("graphforge.ui")

#: Hard ceiling on any client-supplied ``limit`` — a browser cannot ask for the world.
MAX_LIMIT = 500
#: Largest accepted request body (the Cypher console posts a few hundred bytes).
MAX_BODY = 64 * 1024
#: Hosts the guided-ingest endpoints will answer on. Anything else and they do
#: not exist: binding publicly turns the dashboard back into a pure viewer.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "[::1]"})


def _host_only(netloc: str) -> str:
    """Strip credentials and port from a ``Host`` / ``Origin`` authority."""
    value = (netloc or "").strip()
    if "@" in value:
        value = value.rsplit("@", 1)[1]
    if value.startswith("["):  # bracketed IPv6, optionally with a port
        return value[: value.index("]") + 1] if "]" in value else value
    if value.count(":") == 1:  # host:port — a bare IPv6 has more colons
        value = value.split(":", 1)[0]
    return value


def is_loopback_host(host: str) -> bool:
    """True when an authority names this machine and nothing else."""
    return _host_only(host).lower() in LOOPBACK_HOSTS


def _mask(secret: str) -> str:
    return "•••••• (set)" if secret else "— (not set)"


def build_status(settings: Settings) -> dict[str, Any]:
    """Assemble the dashboard payload. Never raises — connection errors are reported."""
    neo = settings.neo4j
    db = settings.db
    status: dict[str, Any] = {
        "config": {
            "neo4j": {"uri": neo.uri, "user": neo.user, "database": neo.database,
                      "password": _mask(neo.password)},
            "database": {"engine": db.engine, "host": db.host, "port": db.port,
                         "user": db.user, "password": _mask(db.password),
                         "databases": db.names or "(auto-discover)"},
            "options": {"batchSize": neo.batch_size, "includeLines": neo.include_lines},
        },
        "neo4j": {"connected": False},
        "labels": {}, "relationshipTypes": [], "repositories": [], "databases": [],
    }
    try:
        gq = GraphQuery.connect(neo)
        try:
            schema = gq.get_schema()
            status["neo4j"]["connected"] = True
            status["labels"] = schema.get("nodeCountsByLabel", {})
            status["relationshipTypes"] = schema.get("relationshipTypes", [])
            status["totals"] = {
                "nodes": sum(schema.get("nodeCountsByLabel", {}).values()),
                "labels": len(schema.get("labels", [])),
                "relationshipTypes": len(schema.get("relationshipTypes", [])),
                "relationships": sum(
                    _as_int(v) for v in (schema.get("relationshipCountsByType") or {}).values()),
            }
            status["repositories"] = gq._read(
                "MATCH (r:Repository) RETURN r.name AS name, r.status AS status, "
                "r.files AS files, r.commits AS commits, r.lastIngestedAt AS lastIngestedAt "
                "ORDER BY r.name")
            status["databases"] = gq._read(
                "MATCH (d:Database) OPTIONAL MATCH (d)-[:HAS_SCHEMA]->(:Schema)-[:HAS_TABLE]->(t:Table) "
                "RETURN d.name AS name, d.engine AS engine, count(t) AS tables ORDER BY d.name")
        finally:
            gq.close()
    except Exception as exc:  # noqa: BLE001  # dashboard must render config even if the graph is unreachable
        status["neo4j"]["error"] = str(exc)
    return status


# --------------------------------------------------------------------------
# request helpers (pure — unit-testable without sockets)
# --------------------------------------------------------------------------
def clamp_limit(raw: Any, default: int = 25, maximum: int = MAX_LIMIT) -> int:
    """Coerce a client-supplied limit into ``1 <= n <= maximum``."""
    if raw is None or raw == "":
        return max(1, min(default, maximum))
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return max(1, min(default, maximum))
    return max(1, min(value, maximum))


def is_identifier(name: str) -> bool:
    """True for a plain Cypher identifier — the only thing we ever interpolate."""
    return bool(name) and _IDENT_RE.fullmatch(name) is not None


def query_denial(cypher: str) -> str | None:
    """Reuse the MCP read-query gate so both surfaces reject the same things.

    Returns the guard's own reason, so the console can say *why* — a query
    rejected for calling an unlisted procedure no longer claims to be a write.
    """
    return _query_denial(cypher or "")


def is_write_query(cypher: str) -> bool:
    """Back-compat boolean form of :func:`query_denial`."""
    return query_denial(cypher) is not None


def _first(params: dict[str, list[str]], key: str, default: str = "") -> str:
    values = params.get(key) or []
    return values[0] if values else default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_body(body: Any) -> dict[str, Any]:
    """Decode a JSON request body. Raises ValueError on anything unusable."""
    if body is None or body == b"" or body == "":
        return {}
    if isinstance(body, dict):
        return body
    if isinstance(body, bytes | bytearray):
        body = bytes(body).decode("utf-8", "replace")
    data = json.loads(body)  # JSONDecodeError is a ValueError
    if not isinstance(data, dict):
        raise ValueError("request body must be a JSON object")
    return data


def schema_payload(schema: dict[str, Any]) -> dict[str, Any]:
    """Reshape ``GraphQuery.get_schema()`` into the dashboard's schema payload."""
    counts = schema.get("nodeCountsByLabel") or {}
    names = list(schema.get("labels") or counts.keys())
    pairs = [(str(n), _as_int(counts.get(n))) for n in names]
    pairs.sort(key=lambda p: (-p[1], p[0]))
    labels = [{"name": n, "count": c} for n, c in pairs]
    rel_counts = schema.get("relationshipCountsByType") or {}
    rel_pairs = [(str(r), _as_int(rel_counts.get(r)))
                 for r in (schema.get("relationshipTypes") or [])]
    # Busiest first, like the labels: 25 alphabetical chips with no numbers say
    # nothing about the graph, and the useful ones end up buried mid-list.
    rel_pairs.sort(key=lambda p: (-p[1], p[0]))
    rels = [{"name": n, "count": c} for n, c in rel_pairs]
    return {"labels": labels, "relationshipTypes": rels,
            "totals": {"labels": len(labels), "relationshipTypes": len(rels),
                       "nodes": sum(item["count"] for item in labels),
                       "relationships": sum(item["count"] for item in rels)}}


#: Separators inside a deterministic node id, most specific last.
_ID_SEPARATORS = ("#", "/", "\\", ":")


def caption_for(node_id: str, name: Any = None) -> str:
    """A short, human name for a node on the canvas.

    Ids are deterministic URIs like
    ``postgres://127.0.0.1/shopdb/public/products#product_id``. Truncating one to
    fit under a dot gives ``postgres://127.0.0…`` — identical for every node in
    the database, so six different tables all read the same. The last segment is
    the part that actually identifies the node.
    """
    text = str(name).strip() if name not in (None, "") else ""
    if text:
        return text
    tail = str(node_id)
    cut = max(tail.rfind(sep) for sep in _ID_SEPARATORS)
    return tail[cut + 1:] or tail


def graph_sample_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold ``(source, target, type)`` rows into ``{nodes, links}`` for the canvas."""
    nodes: dict[str, dict[str, Any]] = {}
    links: list[dict[str, Any]] = []

    def _node(node_id: str, label: Any, name: Any) -> dict[str, Any]:
        return {"id": node_id, "label": str(label or "Node"),
                "caption": caption_for(node_id, name), "degree": 0}

    for row in rows or []:
        if not isinstance(row, dict):
            continue
        source, target = row.get("source"), row.get("target")
        if source is None or target is None:
            continue
        source, target = str(source), str(target)
        nodes.setdefault(source, _node(source, row.get("sourceLabel"), row.get("sourceName")))
        nodes.setdefault(target, _node(target, row.get("targetLabel"), row.get("targetName")))
        nodes[source]["degree"] += 1
        nodes[target]["degree"] += 1
        links.append({"source": source, "target": target, "type": str(row.get("type") or "REL")})
    return {"nodes": list(nodes.values()), "links": links}


def rows_payload(rows: Any) -> dict[str, Any]:
    """Wrap result rows with a stable column order for the console table."""
    rows = list(rows or [])
    columns: list[str] = []
    for row in rows:
        if isinstance(row, dict):
            for key in row:
                if key not in columns:
                    columns.append(key)
    return {"columns": columns, "rows": rows, "count": len(rows)}


def search_payload(rows: Any) -> list[dict[str, Any]]:
    """Flatten ``search_nodes`` output (``[{"n": {...}}]``) into display rows."""
    out: list[dict[str, Any]] = []
    for row in rows or []:
        node = row.get("n") if isinstance(row, dict) else None
        if isinstance(node, dict):
            out.append({"id": node.get("id"), "name": node.get("name"),
                        "ref": node.get("path") or node.get("fqn"), "properties": node})
        elif isinstance(row, dict):
            out.append(row)
    return out


# --------------------------------------------------------------------------
# cypher used by the read-only endpoints (all identifiers validated first)
# --------------------------------------------------------------------------
_GRAPH_SAMPLE_CYPHER = (
    # Deliberately the first $limit relationships in store order, not a random
    # sample. Store order keeps related edges adjacent, so the canvas shows a
    # connected neighbourhood; sampling at random returns edges that rarely share
    # a node and draws a field of disconnected pairs instead of a graph.
    # The cost is coverage: in a graph where one subgraph dwarfs the other, the
    # smaller one may not appear. Use the label chips or search to reach it.
    "MATCH (n)-[r]->(m) WITH n, r, m LIMIT $limit "
    "RETURN coalesce(n.id, toString(id(n))) AS source, head(labels(n)) AS sourceLabel, "
    # `short` is a Commit's 7-character sha; without it a commit captions as its
    # full `repo@40-char-hash` id, which is unreadable at any truncation.
    "coalesce(n.name, n.short, n.path, n.fqn) AS sourceName, "
    "coalesce(m.id, toString(id(m))) AS target, head(labels(m)) AS targetLabel, "
    "coalesce(m.name, m.short, m.path, m.fqn) AS targetName, "
    "type(r) AS type"
)
_GENERIC_SEARCH_CYPHER = (
    "MATCH (n) WHERE toString(n.name) CONTAINS $q OR toString(n.id) CONTAINS $q "
    "OR toString(n.path) CONTAINS $q "
    "RETURN coalesce(n.id, toString(id(n))) AS id, labels(n) AS labels, n.name AS name, "
    "coalesce(n.path, n.fqn, n.name) AS ref LIMIT $limit"
)


def _connect(settings: Settings) -> GraphQuery:
    return GraphQuery.connect(settings.neo4j)


def _with_graph(settings: Settings, connect: Callable[[Settings], Any] | None,
                work: Callable[[Any], Any]) -> tuple[int, Any]:
    """Open a graph connection, run `work`, and turn every failure into JSON.

    Nothing here ever escapes as an unhandled exception: an unreachable graph
    is 503, a rejected query is 400, any other graph error is 502.
    """
    opener = connect or _connect
    try:
        graph = opener(settings)
    except Exception as exc:  # noqa: BLE001 — offline graph must not 500 the dashboard
        return 503, {"error": f"Neo4j unavailable: {exc}"}
    try:
        return 200, work(graph)
    except ValueError as exc:  # the write guard / identifier checks inside GraphQuery
        return 400, {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — a bad graph is an upstream problem
        return 502, {"error": str(exc)}
    finally:
        closer = getattr(graph, "close", None)
        if callable(closer):
            with contextlib.suppress(Exception):
                closer()


def route(path: str, query: str = "", body: Any = None, settings: Settings | None = None,
          *, method: str = "GET",
          connect: Callable[[Settings], Any] | None = None,
          ingest: Any = None) -> tuple[int, Any]:
    """Resolve an ``/api/...`` request to ``(http_status, json_payload)``.

    Pure with respect to the network: pass `connect` to inject a fake graph.
    Input validation happens *before* any connection is opened, so a rejected
    request provably never reaches the database.
    """
    settings = settings if settings is not None else Settings()
    method = (method or "GET").upper()
    parsed = urlsplit(path or "/")
    clean = parsed.path or "/"
    params = parse_qs(query if query else parsed.query, keep_blank_values=True)
    limit_param = _first(params, "limit", "")

    parts = [p for p in clean.strip("/").split("/") if p]
    if not parts or parts[0] != "api":
        return 404, {"error": f"not found: {clean}"}
    rest = parts[1:]

    # -- POST -------------------------------------------------------------
    if rest == ["query"]:
        if method != "POST":
            return 405, {"error": "use POST for /api/query"}
        try:
            payload = parse_body(body)
        except ValueError as exc:
            return 400, {"error": f"invalid JSON body: {exc}"}
        cypher = str(payload.get("cypher") or payload.get("query") or "").strip()
        if not cypher:
            return 400, {"error": "missing 'cypher' in request body"}
        if len(cypher) > 8000:
            return 400, {"error": "query too long (max 8000 characters)"}
        # Server-side read guard: checked here *before* connecting, and again
        # inside GraphQuery.read_cypher. The client is never trusted.
        denial = query_denial(cypher)
        if denial:
            return 400, {"error": denial}
        limit = clamp_limit(payload.get("limit", limit_param), 200)
        return _with_graph(settings, connect,
                           lambda g: rows_payload(g.read_cypher(cypher, limit=limit)))

    if rest == ["ingest"] and method == "POST":
        # Absent when the dashboard is not bound to loopback: the endpoints do
        # not exist rather than existing-and-refusing, so a public bind exposes
        # no ingest surface at all.
        if ingest is None:
            return 404, {"error": "ingest is disabled (dashboard is not bound to loopback)"}
        try:
            payload = parse_body(body)
        except ValueError as exc:
            return 400, {"error": f"invalid JSON body: {exc}"}
        try:
            job = ingest.start(str(payload.get("kind") or ""),
                               str(payload.get("source") or ""),
                               str(payload.get("name") or ""))
        except ValueError as exc:
            return 400, {"error": str(exc)}
        return 202, job.payload()

    if method != "GET":
        return 405, {"error": f"{method} not allowed for {clean}"}

    # -- GET --------------------------------------------------------------
    if rest == ["status"]:
        payload = build_status(settings)
        payload["ingest"] = {"enabled": ingest is not None,
                             "running": bool(ingest and ingest.running())}
        return 200, payload

    if rest == ["ingest"]:
        if ingest is None:
            return 404, {"error": "ingest is disabled (dashboard is not bound to loopback)"}
        return 200, {"jobs": ingest.recent()}

    if len(rest) == 2 and rest[0] == "ingest":
        if ingest is None:
            return 404, {"error": "ingest is disabled (dashboard is not bound to loopback)"}
        job = ingest.get(rest[1])
        if job is None:
            return 404, {"error": f"no such ingest job: {rest[1]}"}
        return 200, job.payload()

    if rest == ["schema"]:
        return _with_graph(settings, connect, lambda g: schema_payload(g.get_schema()))

    if rest == ["graph", "sample"]:
        limit = clamp_limit(limit_param, 50)
        return _with_graph(settings, connect, lambda g: graph_sample_payload(
            g._read(_GRAPH_SAMPLE_CYPHER, {"limit": limit})))

    if rest == ["search"]:
        text = _first(params, "q").strip()
        if not text:
            return 400, {"error": "missing 'q' query parameter"}
        if len(text) > 200:
            return 400, {"error": "'q' is too long (max 200 characters)"}
        label = _first(params, "label").strip()
        prop = _first(params, "prop").strip() or "name"
        limit = clamp_limit(limit_param, 25)
        if label:
            if not is_identifier(label):
                return 400, {"error": f"invalid label {label!r}: expected a simple identifier"}
            if not is_identifier(prop):
                return 400, {"error": f"invalid property {prop!r}: expected a simple identifier"}
            return _with_graph(settings, connect, lambda g: {
                "query": text, "label": label, "prop": prop, "limit": limit,
                "results": search_payload(g.search_nodes(label, prop, text, limit))})
        return _with_graph(settings, connect, lambda g: {
            "query": text, "label": None, "limit": limit,
            "results": g._read(_GENERIC_SEARCH_CYPHER, {"q": text, "limit": limit})})

    if len(rest) == 3 and rest[0] == "labels" and rest[2] == "sample":
        label = unquote(rest[1])
        if not is_identifier(label):
            return 400, {"error": f"invalid label {label!r}: expected a simple identifier"}
        limit = clamp_limit(limit_param, 20)
        cypher = (f"MATCH (n:`{label}`) RETURN coalesce(n.id, toString(id(n))) AS id, "
                  "labels(n) AS labels, properties(n) AS properties LIMIT $limit")
        return _with_graph(settings, connect, lambda g: {
            "label": label, "limit": limit, "nodes": g._read(cypher, {"limit": limit})})

    if len(rest) == 3 and rest[0] == "node" and rest[2] == "neighbors":
        node_id = unquote(rest[1])
        if not node_id.strip():
            return 400, {"error": "missing node id"}
        if len(node_id) > 512:
            return 400, {"error": "node id too long (max 512 characters)"}
        limit = clamp_limit(limit_param, 50)
        return _with_graph(settings, connect, lambda g: {
            "id": node_id, "limit": limit, "neighbors": g.node_neighbors(node_id, limit)})

    return 404, {"error": f"unknown endpoint: {clean}"}


# --------------------------------------------------------------------------
# http plumbing
# --------------------------------------------------------------------------
def _handler(settings: Settings, bind_host: str = "127.0.0.1", *, allow_ingest: bool = True):
    html = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
    loopback_bind = is_loopback_host(bind_host)

    # Ingest exists only on a loopback bind. The token is minted per run and
    # served inside the HTML: a cross-origin page cannot read another origin's
    # document, so it cannot obtain the header it would need to forge a request.
    jobs = None
    token = ""
    if loopback_bind and allow_ingest:
        from .ingest import IngestJobs

        jobs = IngestJobs(settings)
        token = secrets.token_urlsafe(24)
        html = html.replace("<!--gf-token-->",
                            f'<meta name="gf-token" content="{token}" />')

    class Handler(BaseHTTPRequestHandler):
        server_version = "graphforge-ui"

        def _rejected_origin(self) -> str | None:
            """Reason to refuse this request outright, or ``None`` to proceed.

            The dashboard is a local tool with no authentication, so the browser
            is the only thing standing between it and any page the user happens
            to have open. Two holes get closed here:

            * **DNS rebinding** — ``evil.com`` can be made to resolve to
              ``127.0.0.1``, so a loopback-bound server must refuse any ``Host``
              that is not itself loopback.
            * **Drive-by CSRF** — a cross-origin ``fetch`` with
              ``Content-Type: text/plain`` is a *simple* request and is sent with
              no preflight, so a foreign ``Origin`` is refused rather than run.
            """
            if loopback_bind and not is_loopback_host(self.headers.get("Host", "")):
                return "unexpected Host header (dashboard is bound to loopback)"
            origin = (self.headers.get("Origin") or "").strip()
            if not origin:
                return None
            if origin.lower() == "null":
                return "cross-origin request rejected"
            if _host_only(urlsplit(origin).netloc).lower() != _host_only(
                    self.headers.get("Host", "")).lower():
                return "cross-origin request rejected"
            return None

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: Any) -> None:
            self._send(code, "application/json", json.dumps(payload, default=str).encode("utf-8"))

        def _dispatch(self, method: str, body: bytes | None = None) -> None:
            refused = self._rejected_origin()
            if refused:
                self._json(403, {"error": refused})
                return
            parsed = urlsplit(self.path)
            if parsed.path == "/api" or parsed.path.startswith("/api/"):
                # Anything that can change the graph needs the per-run token.
                if (parsed.path.startswith("/api/ingest") and method != "GET"
                        and (not token or self.headers.get("X-GF-Token") != token)):
                    self._json(403, {"error": "missing or invalid X-GF-Token"})
                    return
                try:
                    code, payload = route(parsed.path, parsed.query, body, settings,
                                          method=method, ingest=jobs)
                except Exception as exc:  # noqa: BLE001 — last-ditch: still answer with JSON
                    log.exception("dashboard route failed for %s", self.path)
                    code, payload = 500, {"error": str(exc)}
                self._json(code, payload)
            elif method == "GET" and parsed.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", html.encode("utf-8"))
            else:
                self._send(404, "text/plain", b"not found")

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            length = _as_int(self.headers.get("Content-Length"), 0)
            if length > MAX_BODY:
                self._json(413, {"error": "request body too large"})
                return
            body = self.rfile.read(length) if length > 0 else b""
            self._dispatch("POST", body)

        def log_message(self, *_args):  # quiet
            return

    return Handler


def serve(settings: Settings, host: str = "127.0.0.1", port: int = 8000) -> None:
    server = ThreadingHTTPServer((host, port), _handler(settings, host))
    url = f"http://{host}:{port}"
    print(f"[graphforge] dashboard on {url}  (Ctrl+C to stop)")
    if not is_loopback_host(host):
        # /api/status reports the resolved Neo4j URI, database names, repository
        # names and hostnames. Passwords are masked; none of the rest is.
        print(f"[graphforge] WARNING: bound to {host}, not loopback - the dashboard "
              "has no authentication and /api/status exposes your configuration")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[graphforge] dashboard stopped")
    finally:
        server.server_close()
