"""Dashboard JSON API: routing, input validation, and the server-side write guard.

Everything here runs against the pure `route()` function with a fake graph, so no
live Neo4j (and, apart from one deliberate loopback smoke test, no socket) is needed.
"""

from __future__ import annotations

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


def test_write_guard_is_enforced_again_inside_graphquery():
    """Defence in depth: even if the router's pre-check were bypassed, nothing runs."""

    def _bypassed(_cypher):  # simulate a front-line check that has been defeated
        return False

    spy = _Spy()
    original = srv.is_write_query
    srv.is_write_query = _bypassed
    try:
        code, payload = srv.route(
            "/api/query",
            "",
            {"cypher": "MATCH (n) DELETE n"},
            _settings(),
            method="POST",
            connect=spy,
        )
    finally:
        srv.is_write_query = original
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
