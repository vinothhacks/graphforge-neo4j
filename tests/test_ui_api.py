"""Dashboard JSON API: routing, input validation, and the server-side write guard.

Everything here runs against the pure `route()` function with a fake graph, so no
live Neo4j (and, apart from a few deliberate loopback tests of the origin and token
gates, no socket) is needed.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import threading
import urllib.error
import urllib.request

import pytest

from graphforge.core.config import DbSettings, Neo4jSettings, Settings
from graphforge.mcp.server import GraphQuery
from graphforge.ui import server as srv


# ---------------------------------------------------------------- fakes ----
class _Rec:
    """Stands in for a neo4j Record."""

    def __init__(self, data):
        self._data = data

    def data(self):
        return self._data


class _Tx:
    def __init__(self, log, rows):
        self.log, self.rows = log, rows

    def run(self, cypher, params=None, **_kwargs):
        self.log.append((cypher, dict(params or {})))
        return [_Rec(r) for r in self.rows]


class _Session:
    def __init__(self, log, rows):
        self.log, self.rows = log, rows

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def execute_read(self, fn, *args, **kwargs):
        return fn(_Tx(self.log, self.rows))


class _Driver:
    def __init__(self, rows=None):
        self.executed: list[tuple[str, dict]] = []
        self.rows = rows or []
        self.closed = False

    def session(self, database=None):
        return _Session(self.executed, self.rows)

    def close(self):
        self.closed = True


class _Spy:
    """A `connect` callable that hands back a real GraphQuery over a recording driver.

    Using the real GraphQuery means the guards *inside* it are genuinely exercised;
    `connections` / `executed` prove whether a request ever reached the database.
    """

    def __init__(self, rows=None):
        self.driver = _Driver(rows)
        self.connections = 0

    def __call__(self, _settings):
        self.connections += 1
        return GraphQuery(self.driver, "neo4j")

    @property
    def executed(self):
        return self.driver.executed


class _StubGraph:
    """Minimal duck-typed graph for endpoints whose Cypher we do not care about."""

    def __init__(self, schema=None, raises=None):
        self.schema = schema or {
            "labels": ["Class", "Method"],
            "relationshipTypes": ["CALLS", "DECLARES"],
            "nodeCountsByLabel": {"Class": 3, "Method": 11},
            "relationshipCountsByType": {"CALLS": 9, "DECLARES": 2},
        }
        self.raises = raises
        self.closed = False

    def get_schema(self):
        if self.raises:
            raise self.raises
        return self.schema

    def close(self):
        self.closed = True


def _settings(neo_pw="topsecret", db_pw="dbsecret"):
    return Settings(
        neo4j=Neo4jSettings(uri="bolt://x:7687", user="neo4j", password=neo_pw, database="neo4j"),
        db=DbSettings(engine="mysql", host="h", user="u", password=db_pw),
    )


WRITE_QUERIES = [
    "MATCH (n) DELETE n",
    "CREATE (x)",
    "MATCH (n) SET n.x=1",
    "MATCH (n) DETACH DELETE n",
    "MERGE (a:Thing {id: 1})",
    "MATCH (n) REMOVE n.flag RETURN n",
    "DROP INDEX node_id",
    "match (n) delete n",  # lower case must not sneak through
    "MATCH (n) RETURN n; CREATE (evil)",  # piggy-backed write
]


# ------------------------------------------------------- POST /api/query ----
def test_query_rejects_writes_without_ever_touching_the_graph():
    for cypher in WRITE_QUERIES:
        spy = _Spy()
        code, payload = srv.route(
            "/api/query", "", {"cypher": cypher}, _settings(), method="POST", connect=spy
        )
        assert code == 400, f"{cypher!r} was not rejected (got {code})"
        assert payload.get("error")
        assert spy.connections == 0, f"{cypher!r} opened a connection"
        assert spy.executed == [], f"{cypher!r} reached the database"


def test_write_guard_is_enforced_again_inside_graphquery(monkeypatch):
    """Defence in depth: even if the router's pre-check were bypassed, nothing runs.

    The patch has to land on ``query_denial``, the name route() actually calls.
    Patching anything else leaves the front line intact, the request never
    connects, and the backstop inside GraphQuery.read_cypher goes untested.
    """
    monkeypatch.setattr(srv, "query_denial", lambda _cypher: None)  # a defeated front line
    spy = _Spy()
    code, payload = srv.route(
        "/api/query",
        "",
        {"cypher": "MATCH (n) DELETE n"},
        _settings(),
        method="POST",
        connect=spy,
    )
    assert spy.connections == 1, "the pre-check still ran, so the backstop was never reached"
    assert code == 400
    assert "read-only" in payload["error"].lower()
    assert spy.executed == []  # GraphQuery.read_cypher raised before running anything


def test_graphquery_read_cypher_still_raises_on_writes():
    graph = GraphQuery(_Driver(), "neo4j")
    for cypher in ("MATCH (n) DELETE n", "CREATE (x)", "MATCH (n) SET n.x=1"):
        with pytest.raises(ValueError):
            graph.read_cypher(cypher)
    assert graph.driver.executed == []


def test_query_runs_read_only_cypher_and_shapes_rows():
    spy = _Spy(rows=[{"label": "Class", "total": 3}])
    code, payload = srv.route(
        "/api/query",
        "",
        {"cypher": "MATCH (n) RETURN labels(n) AS label, count(*) AS total"},
        _settings(),
        method="POST",
        connect=spy,
    )
    assert code == 200
    assert payload["columns"] == ["label", "total"]
    assert payload["count"] == 1
    assert payload["rows"][0]["total"] == 3
    assert spy.connections == 1 and spy.executed


def test_query_rejects_bad_bodies_and_wrong_method():
    code, _ = srv.route("/api/query", "", None, _settings(), method="GET", connect=_Spy())
    assert code == 405

    for body in (None, {}, {"cypher": "   "}, b'{"cypher": ""}'):
        code, payload = srv.route(
            "/api/query", "", body, _settings(), method="POST", connect=_Spy()
        )
        assert code == 400 and "cypher" in payload["error"]

    code, payload = srv.route(
        "/api/query", "", b"{not json", _settings(), method="POST", connect=_Spy()
    )
    assert code == 400 and "invalid JSON" in payload["error"]

    code, payload = srv.route(
        "/api/query", "", b'["a list"]', _settings(), method="POST", connect=_Spy()
    )
    assert code == 400

    code, payload = srv.route(
        "/api/query",
        "",
        {"cypher": "MATCH (n) RETURN n " + "x" * 9000},
        _settings(),
        method="POST",
        connect=_Spy(),
    )
    assert code == 400 and "too long" in payload["error"]


# ---------------------------------------- GET /api/labels/<label>/sample ----
def test_label_sample_rejects_non_identifier_labels():
    bad = [
        "Foo;DROP",
        "Foo`) DETACH DELETE (n",
        "1Foo",
        "Foo Bar",
        "Foo-Bar",
        "..",
        "Foo%60",
        "Foo%3BDROP",
        "*",
    ]
    for label in bad:
        spy = _Spy()
        code, payload = srv.route(f"/api/labels/{label}/sample", "", None, _settings(), connect=spy)
        assert code == 400, f"{label!r} was accepted (got {code})"
        assert "identifier" in payload["error"]
        assert spy.connections == 0, f"{label!r} opened a connection"


def test_label_sample_empty_label_is_not_routed():
    code, _ = srv.route("/api/labels//sample", "", None, _settings(), connect=_Spy())
    assert code == 404


def test_label_sample_accepts_a_plain_identifier():
    spy = _Spy(rows=[{"id": "n1", "labels": ["Class"], "properties": {"name": "Foo"}}])
    code, payload = srv.route("/api/labels/Class/sample", "limit=5", None, _settings(), connect=spy)
    assert code == 200
    assert payload["label"] == "Class" and payload["limit"] == 5
    assert payload["nodes"][0]["id"] == "n1"
    cypher, params = spy.executed[0]
    assert "`Class`" in cypher and params["limit"] == 5


# ------------------------------------- GET /api/node/<id>/neighbors --------
def test_node_neighbors_decodes_the_id_and_passes_it_as_a_parameter():
    spy = _Spy(
        rows=[{"rel": "CALLS", "fromId": "a", "neighborId": "b", "neighborLabels": ["Method"]}]
    )
    code, payload = srv.route(
        "/api/node/pkg%2FFile.java/neighbors", "limit=9999", None, _settings(), connect=spy
    )
    assert code == 200
    assert payload["id"] == "pkg/File.java"
    assert payload["neighbors"][0]["rel"] == "CALLS"
    cypher, params = spy.executed[0]
    assert params["id"] == "pkg/File.java"
    assert params["limit"] == srv.MAX_LIMIT  # capped
    assert "pkg/File.java" not in cypher  # never interpolated into the query text


def test_node_neighbors_rejects_blank_and_oversized_ids():
    spy = _Spy()
    code, _ = srv.route("/api/node/%20/neighbors", "", None, _settings(), connect=spy)
    assert code == 400
    code, _ = srv.route("/api/node/" + "a" * 600 + "/neighbors", "", None, _settings(), connect=spy)
    assert code == 400
    assert spy.connections == 0


# ------------------------------------------------------- GET /api/search ---
def test_search_validates_its_input():
    for query in ("", "q=", "q=%20%20", "label=Class"):
        spy = _Spy()
        code, payload = srv.route("/api/search", query, None, _settings(), connect=spy)
        assert code == 400 and "q" in payload["error"]
        assert spy.connections == 0

    spy = _Spy()
    code, payload = srv.route("/api/search", "q=" + "a" * 300, None, _settings(), connect=spy)
    assert code == 400 and "too long" in payload["error"]

    for query in ("q=x&label=Foo;DROP", "q=x&label=Foo%20Bar", "q=x&label=Class&prop=name;DROP"):
        spy = _Spy()
        code, payload = srv.route("/api/search", query, None, _settings(), connect=spy)
        assert code == 400 and "identifier" in payload["error"]
        assert spy.connections == 0


def test_search_with_a_label_uses_search_nodes():
    spy = _Spy(rows=[{"n": {"id": "c1", "name": "Foo", "path": "src/Foo.java"}}])
    code, payload = srv.route(
        "/api/search", "q=Foo&label=Class&limit=100000", None, _settings(), connect=spy
    )
    assert code == 200
    assert payload["label"] == "Class" and payload["prop"] == "name"
    assert payload["results"][0]["name"] == "Foo"
    assert payload["results"][0]["ref"] == "src/Foo.java"
    params = spy.executed[0][1]
    assert params["value"] == "Foo" and params["limit"] == srv.MAX_LIMIT


def test_search_without_a_label_is_generic_and_parameterised():
    spy = _Spy(rows=[{"id": "n1", "labels": ["File"], "name": "Foo", "ref": "src/Foo.java"}])
    code, payload = srv.route("/api/search", "q=Foo", None, _settings(), connect=spy)
    assert code == 200 and payload["label"] is None
    cypher, params = spy.executed[0]
    assert params["q"] == "Foo" and "Foo" not in cypher


# ------------------------------------------------- GET /api/graph/sample ---
def test_graph_sample_caps_the_limit_and_builds_nodes_and_links():
    rows = [
        {
            "source": "a",
            "sourceLabel": "Class",
            "target": "b",
            "targetLabel": "Method",
            "type": "DECLARES",
        },
        {
            "source": "a",
            "sourceLabel": "Class",
            "target": "c",
            "targetLabel": "Method",
            "type": "DECLARES",
        },
    ]
    spy = _Spy(rows=rows)
    code, payload = srv.route("/api/graph/sample", "limit=100000", None, _settings(), connect=spy)
    assert code == 200
    assert spy.executed[0][1]["limit"] == srv.MAX_LIMIT
    ids = {n["id"]: n for n in payload["nodes"]}
    assert set(ids) == {"a", "b", "c"}
    assert ids["a"]["degree"] == 2 and ids["b"]["degree"] == 1
    assert len(payload["links"]) == 2
    assert payload["links"][0] == {"source": "a", "target": "b", "type": "DECLARES"}


def test_graph_sample_payload_skips_incomplete_rows():
    out = srv.graph_sample_payload([{"source": "a"}, {"target": "b"}, "nonsense", {}])
    assert out == {"nodes": [], "links": []}


# ------------------------------------------------------- GET /api/schema ---
def test_schema_endpoint_shape():
    code, payload = srv.route("/api/schema", "", None, _settings(), connect=lambda _s: _StubGraph())
    assert code == 200
    assert payload["labels"] == [{"name": "Method", "count": 11}, {"name": "Class", "count": 3}]
    # Busiest first, and carrying counts: 25 alphabetical names with no numbers
    # tell the reader nothing about the graph they are looking at.
    assert payload["relationshipTypes"] == [
        {"name": "CALLS", "count": 9},
        {"name": "DECLARES", "count": 2},
    ]
    assert payload["totals"] == {
        "labels": 2,
        "relationshipTypes": 2,
        "nodes": 14,
        "relationships": 11,
    }


def test_schema_payload_tolerates_a_ragged_schema():
    out = srv.schema_payload({"nodeCountsByLabel": {"A": "not a number"}})
    assert out["labels"] == [{"name": "A", "count": 0}]
    assert out["relationshipTypes"] == []


# --------------------------------------------------- errors never 500 ------
def test_unreachable_graph_reports_503_not_500():
    def boom(_settings):
        raise RuntimeError("no route to host")

    for path in (
        "/api/schema",
        "/api/graph/sample",
        "/api/search?q=x",
        "/api/labels/Class/sample",
        "/api/node/a/neighbors",
    ):
        code, payload = srv.route(path, "", None, _settings(), connect=boom)
        assert code == 503, path
        assert "no route to host" in payload["error"]


def test_broken_graph_reports_502_not_500():
    stub = _StubGraph(raises=RuntimeError("procedure not found"))
    code, payload = srv.route("/api/schema", "", None, _settings(), connect=lambda _s: stub)
    assert code == 502
    assert "procedure not found" in payload["error"]
    assert stub.closed is True  # the connection is still released


def test_unknown_paths_are_404():
    for path in ("/api/nope", "/api/labels/Class", "/api/node/a", "/", "/etc/passwd", "/api"):
        code, payload = srv.route(path, "", None, _settings(), connect=_Spy())
        assert code == 404, path
        assert "error" in payload


# ------------------------------------------------------------- limits ------
def test_limits_are_clamped():
    assert srv.clamp_limit("999999", 20) == srv.MAX_LIMIT
    assert srv.clamp_limit(10**9, 20) == srv.MAX_LIMIT
    assert srv.clamp_limit("0", 20) == 1
    assert srv.clamp_limit("-5", 20) == 1
    assert srv.clamp_limit("abc", 20) == 20
    assert srv.clamp_limit("", 20) == 20
    assert srv.clamp_limit(None, 20) == 20
    assert srv.clamp_limit("7", 20) == 7
    assert srv.MAX_LIMIT <= 500


def test_every_endpoint_caps_its_limit():
    checks = [
        ("/api/labels/Class/sample", "limit=100000", "limit"),
        ("/api/node/abc/neighbors", "limit=100000", "limit"),
        ("/api/graph/sample", "limit=100000", "limit"),
        ("/api/search", "q=x&limit=100000", "limit"),
    ]
    for path, query, key in checks:
        spy = _Spy()
        code, _ = srv.route(path, query, None, _settings(), connect=spy)
        assert code == 200, path
        assert spy.executed[0][1][key] == srv.MAX_LIMIT, path

    spy = _Spy()
    srv.route(
        "/api/query",
        "",
        {"cypher": "MATCH (n) RETURN n", "limit": 100000},
        _settings(),
        method="POST",
        connect=spy,
    )
    cypher, _ = spy.executed[0]
    assert f"LIMIT {srv.MAX_LIMIT}" in cypher


# --------------------------------------------------------- pure helpers ----
def test_identifier_validation():
    assert srv.is_identifier("Class") and srv.is_identifier("_x9")
    for bad in ("", "Foo;DROP", "Foo Bar", "1Foo", "Foo-Bar", "Foo`", "*", "n)"):
        assert not srv.is_identifier(bad), bad


def test_rows_payload_keeps_column_order_and_unions_keys():
    out = srv.rows_payload([{"b": 1, "a": 2}, {"a": 3, "c": 4}])
    assert out["columns"] == ["b", "a", "c"]
    assert out["count"] == 2
    assert srv.rows_payload(None) == {"columns": [], "rows": [], "count": 0}


def test_status_route_masks_passwords():
    code, payload = srv.route("/api/status", "", None, _settings())
    assert code == 200
    assert payload["config"]["neo4j"]["password"] == "•••••• (set)"
    assert payload["config"]["database"]["password"] == "•••••• (set)"
    blob = json.dumps(payload, default=str)
    assert "topsecret" not in blob and "dbsecret" not in blob


@pytest.mark.parametrize(
    "authority,expected",
    [
        ("127.0.0.1", True),
        ("127.0.0.1:8000", True),
        ("localhost:8000", True),
        ("[::1]:8000", True),
        ("::1", True),
        ("evil.com", False),
        ("evil.com:8000", False),
        ("127.0.0.1.evil.com", False),
        ("user@evil.com", False),
        ("", False),
    ],
)
def test_is_loopback_host_reads_the_authority_not_the_string(authority, expected):
    assert srv.is_loopback_host(authority) is expected


def test_a_foreign_origin_is_refused_before_the_route_runs():
    """Drive-by CSRF: a page the user has open must not be able to drive the console.

    A cross-origin `fetch` with `Content-Type: text/plain` is a *simple* request
    and is sent with no preflight, so the browser will not stop it — the server
    has to. The attacker cannot read the reply, but for a procedure with a side
    effect, firing it is enough.
    """
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), srv._handler(_settings()))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    spy = _Spy()
    try:
        for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"}):
            request = urllib.request.Request(
                base + "/api/query",
                method="POST",
                data=json.dumps({"cypher": "MATCH (n) RETURN n"}).encode("utf-8"),
                headers={"Content-Type": "text/plain", **headers},
            )
            try:
                urllib.request.urlopen(request, timeout=10)
                raise AssertionError(f"server accepted a cross-origin POST: {headers}")
            except urllib.error.HTTPError as err:
                assert err.code == 403, headers
                assert "cross-origin" in json.loads(err.read().decode("utf-8"))["error"]

        # DNS rebinding: evil.com can be pointed at 127.0.0.1, so a loopback-bound
        # server must refuse any Host that is not itself loopback.
        request = urllib.request.Request(base + "/api/status", headers={"Host": "evil.example"})
        try:
            urllib.request.urlopen(request, timeout=10)
            raise AssertionError("server answered a rebound Host header")
        except urllib.error.HTTPError as err:
            assert err.code == 403
            assert "Host" in json.loads(err.read().decode("utf-8"))["error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert spy.connections == 0


def test_the_console_says_why_a_query_was_denied():
    """Every denial used to claim to be a write, whatever the real reason was."""
    code, payload = srv.route(
        "/api/query",
        "",
        {"cypher": "CALL apoc.load.json('http://x/')"},
        _settings(),
        method="POST",
        connect=_Spy(),
    )
    assert code == 400
    assert "apoc.load.json" in payload["error"], payload
    assert "not allowlisted" in payload["error"], payload


# ------------------------------------------------ origin and token gates ----
@contextlib.contextmanager
def _serving(bind_host="127.0.0.1"):
    """A real dashboard on a kernel-assigned loopback port; yields the port.

    ``bind_host`` is only what the handler is told it was bound to, so a public
    bind's rules can be exercised without opening a public socket.
    """
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), srv._handler(_settings(), bind_host))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _raw(port, method, path, headers=None, body=None):
    """Send `path` exactly as written (urllib would tidy it); return (status, payload)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        text = response.read().decode("utf-8")
    finally:
        conn.close()
    try:
        return response.status, json.loads(text)
    except ValueError:
        return response.status, text


class _FakeJobs:
    """Records whether route() ever handed a request to the ingest runner."""

    def __init__(self):
        self.started = []

    def start(self, kind, source, name=""):
        self.started.append((kind, source, name))
        return self

    def payload(self):
        return {"id": "fake"}


#: Ways to spell the ingest path, sent over the wire byte-for-byte.
INGEST_SPELLINGS = [
    "/api/ingest",
    "/api//ingest",
    "/api///ingest",
    "/api/ingest/",
    "/api//ingest//",
    "/api/./ingest",
    "api/ingest",
    "//api//ingest",
    "/api//ingest?kind=git",
]


@pytest.mark.parametrize(
    "path,reaches_ingest",
    [
        ("/api/ingest", True),
        ("/api//ingest", True),
        ("/api///ingest", True),
        ("/api/ingest/", True),
        ("/api//ingest//", True),
        ("api/ingest", True),
        ("/api//ingest?kind=git", True),
        ("http://example.invalid/api//ingest", True),
        ("/api/./ingest", False),  # a dot segment is literal: an unknown endpoint
        ("/api/ingest/.", False),
    ],
)
def test_the_token_gate_sees_every_path_the_router_sends_to_ingest(path, reaches_ingest):
    """The gate and the router read one split of the path, so they cannot disagree.

    The gate used to test ``path.startswith("/api/ingest")`` while route() dropped
    empty segments, so ``POST /api//ingest`` started an ingest with no token.
    """
    jobs = _FakeJobs()
    srv.route(path, "", {"kind": "git", "source": "."}, _settings(), method="POST", ingest=jobs)
    assert bool(jobs.started) is reaches_ingest, path
    assert srv.requires_token(path, "POST"), f"{path!r} needs no token"


@pytest.mark.parametrize(
    "path,method,expected",
    [
        ("/api/ingest", "GET", False),  # polling job status changes nothing
        ("/api/ingest/abc", "get", False),
        ("/api/query", "POST", False),  # read-only console; must work on a public bind
        ("/api//query/", "POST", False),
        ("/api/status", "POST", True),  # fail-closed: not exempt, so guarded
        ("/api/anything-new", "POST", True),
        ("/", "POST", False),  # not an API path, never routed
    ],
)
def test_requires_token_is_fail_closed_for_every_non_get(path, method, expected):
    assert srv.requires_token(path, method) is expected


def test_no_spelling_of_the_ingest_path_skips_the_token_over_the_wire():
    """Regression: ``POST /api//ingest`` used to answer 202 with no token at all."""
    # An unknown kind: were the gate ever bypassed again, route() rejects the
    # body, so no real ingest starts on the machine running the tests.
    body = json.dumps({"kind": "not-a-kind", "source": "."})
    json_headers = {"Content-Type": "application/json"}
    with _serving() as port:
        for path in INGEST_SPELLINGS:
            code, payload = _raw(port, "POST", path, json_headers, body)
            assert code == 403, (path, code, payload)
            assert "X-GF-Token" in payload["error"], path
        # A wrong token is refused too, and a non-ASCII one is a 403, not a 500:
        # compare_digest raises on non-ASCII str, so the gate compares bytes.
        for wrong in ("not-the-token", "töken"):
            headers = {**json_headers, "X-GF-Token": wrong}
            code, payload = _raw(port, "POST", "/api/ingest", headers, body)
            assert code == 403, (wrong, code, payload)

        # A gate, not a wall: the page's own token gets the same odd spelling
        # through to route(), which then judges the body on its merits.
        _, html = _raw(port, "GET", "/")
        token = html.split('<meta name="gf-token" content="', 1)[1].split('"', 1)[0]
        code, payload = _raw(
            port, "POST", "/api//ingest", {**json_headers, "X-GF-Token": token}, body
        )
        assert code == 400 and "kind" in payload["error"], payload


@pytest.mark.parametrize(
    "origin,host,expected",
    [
        ("http://127.0.0.1:8000", "127.0.0.1:8000", True),
        ("http://localhost:8000", "localhost:8000", True),
        ("http://LOCALHOST:8000", "localhost:8000", True),
        ("http://127.0.0.1", "127.0.0.1", True),
        ("http://127.0.0.1:80", "127.0.0.1", True),  # the default port, spelt out
        ("http://127.0.0.1", "127.0.0.1:80", True),
        ("http://[::1]:8000", "[::1]:8000", True),
        ("http://[::1]", "[::1]:80", True),
        ("http://[0:0:0:0:0:0:0:1]:8000", "[::1]:8000", True),
        # another port on the same host is another origin, however local
        ("http://127.0.0.1:9999", "127.0.0.1:8000", False),
        ("http://localhost:3000", "localhost:8000", False),
        ("http://[::1]:9999", "[::1]:8000", False),
        ("http://127.0.0.1:8000", "127.0.0.1", False),
        ("http://127.0.0.1", "127.0.0.1:8000", False),
        # another scheme: this server has no TLS, so an https page is never it
        ("https://127.0.0.1:8000", "127.0.0.1:8000", False),
        ("https://127.0.0.1", "127.0.0.1:443", False),
        ("ftp://127.0.0.1:8000", "127.0.0.1:8000", False),
        # another host
        ("http://localhost:8000", "127.0.0.1:8000", False),
        ("http://evil.example:8000", "127.0.0.1:8000", False),
        # not an origin at all
        ("null", "127.0.0.1:8000", False),
        ("", "127.0.0.1:8000", False),
        ("127.0.0.1:8000", "127.0.0.1:8000", False),
        ("http://127.0.0.1:8000/", "127.0.0.1:8000", False),
        ("http://user@127.0.0.1:8000", "127.0.0.1:8000", False),
        ("http://127.0.0.1:8000", "", False),
        ("http://127.0.0.1:abc", "127.0.0.1:abc", False),
        ("http://[::1", "[::1", False),
    ],
)
def test_same_origin_compares_scheme_host_and_port(origin, host, expected):
    assert srv.is_same_origin(origin, host) is expected


@pytest.mark.parametrize(
    "origin,host,expected",
    [
        # a TLS-terminating proxy forwards the Host the browser sent it
        ("https://graph.example.com", "graph.example.com", True),
        ("https://graph.example.com", "graph.example.com:443", True),
        ("https://graph.example.com:8443", "graph.example.com:8443", True),
        ("https://[::1]:8443", "[0:0:0:0:0:0:0:1]:8443", True),
        # plain http is judged exactly as on loopback
        ("http://graph.example.com", "graph.example.com", True),
        ("http://graph.example.com:8000", "graph.example.com:8000", True),
        ("http://graph.example.com", "graph.example.com:443", False),
        ("http://localhost:3000", "localhost:8000", False),
        # host and port must still agree
        ("https://evil.example", "graph.example.com", False),
        ("https://graph.example.com:8443", "graph.example.com", False),
        ("https://graph.example.com", "graph.example.com:8000", False),
        ("https://graph.example.com", "graph.example.com:80", False),
        ("ftp://graph.example.com", "graph.example.com", False),
        ("null", "graph.example.com", False),
        ("https://user@graph.example.com", "graph.example.com", False),
    ],
)
def test_a_public_bind_accepts_its_tls_proxy_origin(origin, host, expected):
    """Regression: behind a TLS proxy every console POST was refused as cross-origin.

    The Origin is https (port 443) while the forwarded Host was read as http
    (port 80). Only the scheme is inferred; host and port are compared as ever.
    """
    assert srv.is_same_origin(origin, host, tls_front_end=True) is expected


@pytest.mark.parametrize(
    "origin,host",
    [
        ("https://localhost", "localhost"),
        ("https://127.0.0.1", "127.0.0.1:443"),
        ("https://graph.example.com", "graph.example.com"),
    ],
)
def test_without_a_tls_front_end_an_https_origin_is_never_this_server(origin, host):
    """On loopback an https page on the same host is another local server."""
    assert srv.is_same_origin(origin, host) is False


def test_a_page_on_another_local_port_is_refused_over_the_wire():
    """Regression: only hostnames were compared, so any localhost dev server passed."""
    body = json.dumps({"cypher": "MATCH (n) DELETE n"})  # route() refuses it if reached
    with _serving() as port:
        other = port + 1 if port < 65535 else port - 1
        for origin in (
            f"http://127.0.0.1:{other}",
            "http://127.0.0.1",
            f"https://127.0.0.1:{port}",
            f"http://localhost:{port}",
        ):
            code, payload = _raw(
                port, "POST", "/api/query", {"Origin": origin, "Content-Type": "text/plain"}, body
            )
            assert code == 403, origin
            assert "cross-origin" in payload["error"], origin

        # The page's own origin, and no Origin at all, still reach route(), which
        # then refuses the write on its own terms.
        for headers in ({"Origin": f"http://127.0.0.1:{port}"}, {}):
            code, payload = _raw(
                port, "POST", "/api/query", {"Content-Type": "text/plain", **headers}, body
            )
            assert code == 400 and "read-only" in payload["error"], headers


def test_the_console_works_behind_a_tls_proxy_on_a_public_bind():
    """Regression: an https Origin with a forwarded Host was a 403 on every POST."""
    body = json.dumps({"cypher": "MATCH (n) DELETE n"})  # route() refuses it if reached

    def post(port, origin, host):
        headers = {"Origin": origin, "Host": host, "Content-Type": "text/plain"}
        return _raw(port, "POST", "/api/query", headers, body)

    with _serving("0.0.0.0") as port:
        # Reaching route() is the point: it then refuses the write on its own terms.
        for host in ("graph.example.com", "graph.example.com:443"):
            code, payload = post(port, "https://graph.example.com", host)
            assert code == 400 and "read-only" in payload["error"], (host, code, payload)
        for origin in (
            "https://evil.example",
            "https://graph.example.com:8443",
            "http://graph.example.com:3000",
        ):
            code, payload = post(port, origin, "graph.example.com")
            assert code == 403 and "cross-origin" in payload["error"], (origin, code, payload)

    # A loopback bind has no proxy to allow for: an https page is another server.
    with _serving() as port:
        code, payload = post(port, "https://localhost", "localhost")
        assert code == 403 and "cross-origin" in payload["error"], (code, payload)


# ------------------------------------------------- one loopback smoke test --
def test_handler_serves_the_shell_and_rejects_writes_over_the_wire():
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), srv._handler(_settings()))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"  # loopback only, kernel-assigned port
    try:
        with urllib.request.urlopen(base + "/", timeout=10) as resp:
            assert resp.status == 200
            assert "graphforge" in resp.read().decode("utf-8")

        request = urllib.request.Request(
            base + "/api/query",
            method="POST",
            data=json.dumps({"cypher": "MATCH (n) DELETE n"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(request, timeout=10)
            raise AssertionError("the server accepted a write query")
        except urllib.error.HTTPError as err:
            assert err.code == 400
            assert "error" in json.loads(err.read().decode("utf-8"))

        try:
            urllib.request.urlopen(base + "/api/nope", timeout=10)
            raise AssertionError("unknown endpoint did not 404")
        except urllib.error.HTTPError as err:
            assert err.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
