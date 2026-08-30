"""MCP tool layer: the new reports, pagination, the schema cache, and backward compat.

Everything runs against a fake driver that records the Cypher and parameters it is
handed and replays canned rows, so no live Neo4j is needed. The cache tests drive an
injected clock rather than sleeping.
"""
from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

from graphforge.core.config import DbSettings, Neo4jSettings, Settings
from graphforge.mcp import server as mcp
from graphforge.mcp.server import _IDENT, GraphQuery


# ---------------------------------------------------------------- fakes ----
class _Rec:
    """Stands in for a neo4j Record."""

    def __init__(self, data):
        self._data = data

    def data(self):
        return self._data


class _Tx:
    def __init__(self, driver):
        self.driver = driver

    def run(self, cypher, params=None, **_kwargs):
        self.driver.executed.append((cypher, dict(params or {})))
        return [_Rec(row) for row in self.driver.rows_for(cypher)]


class _Session:
    def __init__(self, driver):
        self.driver = driver

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def execute_read(self, fn, *args, **kwargs):
        return fn(_Tx(self.driver))


class FakeDriver:
    """Records every (cypher, params) and answers from a substring-keyed script.

    ``script`` is an ordered list of ``(needle, rows)``: the first needle found in
    the query text wins, so a multi-query tool can be handed a different result set
    per statement. Anything unmatched falls back to ``rows``.
    """

    def __init__(self, rows=None, script=None):
        self.executed: list[tuple[str, dict]] = []
        self.rows = list(rows or [])
        self.script = list(script or [])
        self.closed = False

    def rows_for(self, cypher):
        for needle, rows in self.script:
            if needle in cypher:
                return rows
        return self.rows

    def session(self, database=None):
        return _Session(self)

    def close(self):
        self.closed = True

    # -- assertions helpers --
    @property
    def cyphers(self) -> list[str]:
        return [c for c, _ in self.executed]

    def matching(self, needle: str) -> list[tuple[str, dict]]:
        return [(c, p) for c, p in self.executed if needle in c]

    def one(self, needle: str) -> tuple[str, dict]:
        hits = self.matching(needle)
        assert len(hits) == 1, f"expected exactly one query containing {needle!r}, got {len(hits)}"
        return hits[0]


class Clock:
    """A hand-cranked monotonic clock — expiry is tested in microseconds, not minutes."""

    def __init__(self, now: float = 1000.0):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def _settings(neo_pw="topsecret", db_pw="dbsecret"):
    return Settings(
        neo4j=Neo4jSettings(uri="bolt://x:7687", user="neo4j", password=neo_pw, database="neo4j"),
        db=DbSettings(engine="mysql", host="h", user="u", password=db_pw),
    )


# ============================================================ backward compat ==
def test_ui_server_still_imports_the_names_it_depends_on():
    """graphforge.ui.server does `from ..mcp.server import GraphQuery, _IDENT`."""
    from graphforge.ui import server as ui

    assert ui.GraphQuery is GraphQuery
    assert ui._IDENT_RE is _IDENT
    # There is exactly one write guard, and it is query_guard. The old `_WRITE`
    # regex it replaced is gone; nothing may reintroduce a second one.
    assert not hasattr(ui, "_WRITE_RE")
    for name in ("read_cypher", "search_nodes", "get_schema", "node_neighbors", "_read"):
        assert callable(getattr(GraphQuery, name)), name


def test_legacy_positional_calls_return_the_old_shapes():
    """Every pre-existing method keeps its positional signature and list/dict result."""
    driver = FakeDriver(rows=[{"n": {"id": "c1", "name": "Foo"}}])
    graph = GraphQuery(driver, "neo4j")

    rows = graph.search_nodes("Class", "name", "Foo", 5)
    assert type(rows) is list and rows[0]["n"]["name"] == "Foo"
    assert type(graph.find_code("Foo", 5)) is list
    assert type(graph.find_table("orders", 5)) is list
    assert type(graph.node_neighbors("c1", 3)) is list
    assert type(graph.find_procedure("sp_", 5)) is list
    assert type(graph.read_cypher("MATCH (n) RETURN n")) is list

    schema_driver = FakeDriver(script=[
        ("db.labels", [{"label": "Class"}]),
        ("db.relationshipTypes", [{"relationshipType": "IMPORTS"}]),
        ("count(n) AS c", [{"c": 7}]),
        ("count(r) AS c", [{"c": 4}]),
    ])
    schema = GraphQuery(schema_driver, "neo4j").get_schema()
    assert schema == {"labels": ["Class"], "relationshipTypes": ["IMPORTS"],
                      "nodeCountsByLabel": {"Class": 7},
                      "relationshipCountsByType": {"IMPORTS": 4}}


def test_search_nodes_rejects_injected_identifiers_as_before():
    graph = GraphQuery(FakeDriver(), "neo4j")
    for label, prop in (("Foo`) DETACH DELETE (n", "name"), ("Class", "name;DROP"), ("1Foo", "name")):
        with pytest.raises(ValueError):
            graph.search_nodes(label, prop, "x")
        with pytest.raises(ValueError):
            graph.search_nodes_page(label, prop, "x")
    assert graph.driver.executed == []


def test_read_cypher_still_rejects_writes():
    graph = GraphQuery(FakeDriver(), "neo4j")
    for cypher in ("MATCH (n) DELETE n", "CREATE (x)", "MATCH (n) SET n.x=1",
                   "MERGE (a:Thing {id: 1})", "MATCH (n) DETACH DELETE n",
                   "match (n) delete n", "MATCH (n) RETURN n; CREATE (evil)",
                   "MATCH (n) REMOVE n.flag RETURN n", "DROP INDEX node_id",
                   "CALL apoc.create.node(['X'], {})"):
        with pytest.raises(ValueError):
            graph.read_cypher(cypher)
    assert graph.driver.executed == [], "a write reached the database"


def test_read_cypher_still_appends_a_limit():
    graph = GraphQuery(FakeDriver(), "neo4j")
    graph.read_cypher("MATCH (n) RETURN n", limit=17)
    assert "LIMIT 17" in graph.driver.cyphers[0]
    graph.read_cypher("MATCH (n) RETURN n LIMIT 3", limit=17)
    assert "LIMIT 17" not in graph.driver.cyphers[1]


def test_a_limit_on_its_own_line_is_not_a_missing_limit():
    """Multi-line Cypher is what an agent writes, and it used to get two LIMITs.

    The old detector looked for `" LIMIT "` — with a leading space — so a
    newline-formatted query looked uncapped and got a second clause appended,
    producing `LIMIT 3\\nLIMIT 200`: a syntax error on every real server.
    """
    graph = GraphQuery(FakeDriver(), "neo4j")
    graph.read_cypher("MATCH (n)\nRETURN n\nLIMIT 3", limit=200)
    sent = graph.driver.cyphers[0]
    assert sent.upper().count("LIMIT") == 1, sent


def test_a_limit_inside_a_string_literal_does_not_suppress_the_cap():
    """`' LIMIT '` as data used to read as `LIMIT` as code, skipping the row cap."""
    graph = GraphQuery(FakeDriver(), "neo4j")
    graph.read_cypher("MATCH (n) WHERE n.doc CONTAINS ' LIMIT ' RETURN n", limit=25)
    assert "LIMIT 25" in graph.driver.cyphers[0]


def test_a_standalone_call_is_capped_without_being_made_invalid():
    """`CALL db.labels()` takes no LIMIT clause, so the cap is applied to the rows."""
    driver = FakeDriver(rows=[{"label": f"L{i}"} for i in range(50)])
    graph = GraphQuery(driver, "neo4j")
    rows = graph.read_cypher("CALL db.labels()", limit=10)
    assert "LIMIT" not in graph.driver.cyphers[0].upper(), "appending LIMIT breaks a standalone CALL"
    assert len(rows) == 10, "a standalone CALL returned more rows than the cap"


def test_ui_routes_still_work_over_the_new_module():
    """Acceptance: the dashboard's own routes still resolve through GraphQuery."""
    from graphforge.ui import server as ui

    code, payload = ui.route("/api/status", "", None, _settings())
    assert code == 200 and payload["config"]["neo4j"]["password"] == "•••••• (set)"

    driver = FakeDriver(rows=[{"n": {"id": "c1", "name": "Foo", "path": "src/Foo.java"}}])
    connect = lambda _s: GraphQuery(driver, "neo4j")  # noqa: E731
    code, payload = ui.route("/api/search", "q=Foo&label=Class", None, _settings(), connect=connect)
    assert code == 200 and payload["results"][0]["name"] == "Foo"

    code, payload = ui.route("/api/query", "", {"cypher": "MATCH (n) RETURN n"},
                             _settings(), method="POST", connect=connect)
    assert code == 200 and payload["count"] == 1

    code, payload = ui.route("/api/query", "", {"cypher": "MATCH (n) DELETE n"},
                             _settings(), method="POST", connect=connect)
    assert code == 400 and payload["error"]

    code, payload = ui.route("/api/schema", "", None, _settings(),
                             connect=lambda _s: GraphQuery(FakeDriver(script=[
                                 ("db.labels", [{"label": "Class"}]),
                                 ("db.relationshipTypes", []),
                                 ("count(n) AS c", [{"c": 4}])]), "neo4j"))
    assert code == 200 and payload["labels"] == [{"name": "Class", "count": 4}]


# ================================================================= pagination ==
def _page_driver(rows, total):
    return FakeDriver(script=[("count(n) AS total", [{"total": total}])], rows=rows)


def test_search_nodes_passes_limit_and_offset_to_cypher():
    graph = GraphQuery(FakeDriver(), "neo4j")
    graph.search_nodes("Class", "name", "Foo", 10, 20)
    cypher, params = graph.driver.executed[0]
    assert "SKIP $offset" in cypher and "LIMIT $limit" in cypher
    assert cypher.index("SKIP") < cypher.index("LIMIT")
    assert params == {"value": "Foo", "limit": 10, "offset": 20}
    assert "Foo" not in cypher  # still parameterised, never interpolated


def test_search_nodes_page_reports_total_and_has_more():
    driver = _page_driver([{"n": {"id": f"c{i}"}} for i in range(10)], total=25)
    page = GraphQuery(driver, "neo4j").search_nodes_page("Class", "name", "Foo", 10, 0)
    assert sorted(page) == ["hasMore", "limit", "offset", "rows", "total"]
    assert page["total"] == 25 and page["limit"] == 10 and page["offset"] == 0
    assert len(page["rows"]) == 10 and page["hasMore"] is True
    # the count query carries the search value but no paging parameters
    _, params = driver.one("count(n) AS total")
    assert params == {"value": "Foo"}


def test_last_page_reports_has_more_false():
    driver = _page_driver([{"n": {"id": "c1"}}, {"n": {"id": "c2"}}], total=22)
    page = GraphQuery(driver, "neo4j").search_nodes_page("Class", "name", "Foo", 10, 20)
    assert page["offset"] == 20 and page["total"] == 22 and page["hasMore"] is False


def test_empty_page_past_the_end_is_not_more():
    driver = _page_driver([], total=3)
    page = GraphQuery(driver, "neo4j").find_code_page("Foo", 10, 99)
    assert page["rows"] == [] and page["total"] == 3 and page["hasMore"] is False


def test_find_code_and_find_table_pages_share_the_shape_and_the_where_clause():
    for method, page_method, needle in (("find_code", "find_code_page", "n:File OR n:Class OR n:Method"),
                                        ("find_table", "find_table_page", "n:Table OR n:Column")):
        driver = _page_driver([{"name": "orders"}], total=4)
        graph = GraphQuery(driver, "neo4j")
        page = getattr(graph, page_method)("ord", 1, 2)
        assert page == {"rows": [{"name": "orders"}], "total": 4, "limit": 1,
                        "offset": 2, "hasMore": True}, method
        rows_cypher, rows_params = driver.executed[0]
        count_cypher, count_params = driver.one("count(n) AS total")
        assert needle in rows_cypher and needle in count_cypher, method
        assert rows_params == {"t": "ord", "limit": 1, "offset": 2}, method
        assert count_params == {"t": "ord"}, method
        # the page and the plain list run the same filter
        assert type(getattr(graph, method)("ord", 1, 2)) is list


def test_pagination_clamps_nonsense_values():
    graph = GraphQuery(FakeDriver(), "neo4j")
    graph.search_nodes("Class", "name", "x", limit=0, offset=-5)
    assert graph.driver.executed[0][1]["limit"] == 1
    assert graph.driver.executed[0][1]["offset"] == 0
    graph.find_table("x", limit=-3, offset=-1)
    assert graph.driver.executed[1][1] == {"t": "x", "limit": 1, "offset": 0}


def test_pagination_is_additive_so_old_calls_are_unchanged():
    """The offset argument is keyword-defaulted: a 4-arg call still means page one."""
    graph = GraphQuery(FakeDriver(), "neo4j")
    graph.search_nodes("Class", "name", "Foo", 25)
    assert graph.driver.executed[0][1] == {"value": "Foo", "limit": 25, "offset": 0}


# =========================================================== search_codebase ==
def test_search_codebase_uses_tolower_and_defaults_to_code():
    driver = _page_driver([{"name": "Foo", "ref": "a.Foo", "repo": "svc"}], total=1)
    page = GraphQuery(driver, "neo4j").search_codebase("FOO")
    cypher, params = driver.executed[0]
    assert "toLower" in cypher and "toLower($t)" in cypher
    assert "n:File OR n:Class OR n:Method" in cypher
    assert "n:Table OR n:Column OR n:StoredProcedure" not in cypher
    assert params["t"] == "FOO" and params["limit"] == 25 and params["offset"] == 0
    assert page["total"] == 1 and page["hasMore"] is False
    assert sorted(page) == ["hasMore", "limit", "offset", "rows", "total"]


def test_search_codebase_kind_schema_matches_tables_columns_and_procedures():
    driver = _page_driver([{"name": "booking", "database": "shop"}], total=3)
    page = GraphQuery(driver, "neo4j").search_codebase("Book", kind="schema")
    cypher, params = driver.executed[0]
    assert "toLower" in cypher
    assert "n:Table OR n:Column OR n:StoredProcedure" in cypher
    assert "n:File OR n:Class OR n:Method" not in cypher
    assert params["t"] == "Book"
    assert page["rows"][0]["database"] == "shop"


def test_search_codebase_kind_all_covers_code_and_schema():
    driver = FakeDriver()
    GraphQuery(driver, "neo4j").search_codebase("x", kind="ALL")
    cypher = driver.executed[0][0]
    assert "n:File OR n:Class OR n:Method" in cypher
    assert "n:Table OR n:Column OR n:StoredProcedure" in cypher
    assert "toLower($t)" in cypher


def test_search_codebase_honours_repo_limit_and_offset():
    driver = _page_driver([{"name": "a"}], total=9)
    page = GraphQuery(driver, "neo4j").search_codebase(
        "a", kind="code", repo="svc", limit=1, offset=2)
    cypher, params = driver.executed[0]
    assert "n.repo = $repo" in cypher
    assert params == {"t": "a", "limit": 1, "offset": 2, "repo": "svc"}
    count_cypher, count_params = driver.one("count(n) AS total")
    assert "n.repo = $repo" in count_cypher
    assert count_params == {"t": "a", "repo": "svc"}
    assert page == {"rows": [{"name": "a"}], "total": 9, "limit": 1,
                    "offset": 2, "hasMore": True}


def test_search_codebase_omits_repo_param_when_blank():
    driver = FakeDriver()
    GraphQuery(driver, "neo4j").search_codebase("x", repo="  ")
    cypher, params = driver.executed[0]
    assert "AND n.repo = $repo" not in cypher
    assert "repo" not in params


def test_search_codebase_rejects_unknown_kind():
    with pytest.raises(ValueError, match="kind"):
        GraphQuery(FakeDriver(), "neo4j").search_codebase("x", kind="files")


def test_find_code_where_is_case_insensitive():
    driver = FakeDriver()
    rows = GraphQuery(driver, "neo4j").find_code("Foo")
    assert type(rows) is list
    cypher, params = driver.executed[0]
    assert "toLower" in cypher and "toLower($t)" in cypher
    assert "toString(n.name) CONTAINS $t" not in cypher
    assert params["t"] == "Foo"


def test_search_codebase_page_matches_find_code_page():
    driver = _page_driver([{"name": "orders"}], total=4)
    page = GraphQuery(driver, "neo4j").search_codebase("ord", limit=1, offset=2)
    assert page == {"rows": [{"name": "orders"}], "total": 4, "limit": 1,
                    "offset": 2, "hasMore": True}


# =============================================================== schema cache ==
def _schema_driver(count=1):
    return FakeDriver(script=[
        ("db.labels", [{"label": "Class"}]),
        ("db.relationshipTypes", [{"relationshipType": "IMPORTS"}]),
        ("count(n) AS c", [{"c": count}]),
    ])


def _label_queries(driver):
    return len(driver.matching("db.labels"))


def test_get_schema_serves_a_cache_hit_inside_the_ttl():
    clock = Clock()
    graph = GraphQuery(_schema_driver(), "neo4j", clock=clock)
    first = graph.get_schema()
    clock.advance(59.0)
    second = graph.get_schema()
    assert first == second
    assert _label_queries(graph.driver) == 1, "cache miss inside the TTL"


def test_get_schema_refetches_once_the_ttl_expires():
    clock = Clock()
    graph = GraphQuery(_schema_driver(), "neo4j", clock=clock)
    graph.get_schema()
    clock.advance(60.0)  # exactly at the boundary: expired
    graph.get_schema()
    clock.advance(0.5)
    graph.get_schema()
    assert _label_queries(graph.driver) == 2


def test_get_schema_honours_a_custom_ttl_argument():
    clock = Clock()
    graph = GraphQuery(_schema_driver(), "neo4j", clock=clock)
    graph.get_schema(ttl=5)
    clock.advance(4.0)
    graph.get_schema(ttl=5)
    assert _label_queries(graph.driver) == 1
    clock.advance(2.0)
    graph.get_schema(ttl=5)
    assert _label_queries(graph.driver) == 2


def test_zero_ttl_bypasses_the_cache_entirely():
    clock = Clock()
    graph = GraphQuery(_schema_driver(), "neo4j", clock=clock)
    graph.get_schema(ttl=0)
    graph.get_schema(ttl=0)
    assert _label_queries(graph.driver) == 2
    graph.get_schema()  # nothing was stored, so the default TTL still has to read
    assert _label_queries(graph.driver) == 3


def test_refresh_forces_a_read_and_reprimes_the_cache():
    clock = Clock()
    graph = GraphQuery(_schema_driver(), "neo4j", clock=clock)
    graph.get_schema()
    graph.get_schema(refresh=True)
    assert _label_queries(graph.driver) == 2
    graph.get_schema()
    assert _label_queries(graph.driver) == 2, "refresh should leave a fresh entry behind"


def test_invalidate_schema_cache_drops_the_entry():
    clock = Clock()
    graph = GraphQuery(_schema_driver(), "neo4j", clock=clock)
    graph.get_schema()
    graph.invalidate_schema_cache()
    graph.get_schema()
    assert _label_queries(graph.driver) == 2


def test_schema_ttl_can_be_set_per_instance():
    clock = Clock()
    graph = GraphQuery(_schema_driver(), "neo4j", clock=clock, schema_ttl=10)
    graph.get_schema()
    clock.advance(11)
    graph.get_schema()
    assert _label_queries(graph.driver) == 2


def test_a_caller_mutating_the_result_cannot_poison_the_cache():
    clock = Clock()
    graph = GraphQuery(_schema_driver(), "neo4j", clock=clock)
    schema = graph.get_schema()
    schema["labels"].append("Injected")
    schema["nodeCountsByLabel"]["Class"] = 999
    again = graph.get_schema()
    assert again["labels"] == ["Class"] and again["nodeCountsByLabel"] == {"Class": 1}
    assert _label_queries(graph.driver) == 1


def test_instances_built_from_a_driver_do_not_share_a_cache():
    """Two ad-hoc GraphQuery objects are independent — no cross-talk between graphs."""
    clock = Clock()
    a = GraphQuery(_schema_driver(count=1), "neo4j", clock=clock)
    b = GraphQuery(_schema_driver(count=2), "neo4j", clock=clock)
    assert a.get_schema()["nodeCountsByLabel"] == {"Class": 1}
    assert b.get_schema()["nodeCountsByLabel"] == {"Class": 2}


def test_connections_to_the_same_graph_share_the_cache():
    """The dashboard rebuilds a GraphQuery per request; the cache must survive that."""
    mcp.clear_schema_cache()
    try:
        clock = Clock()
        key = ("neo4j", "bolt://x:7687", "neo4j", "neo4j")
        first = GraphQuery(_schema_driver(), "neo4j", clock=clock, cache_key=key)
        first.get_schema()
        first.close()
        second = GraphQuery(_schema_driver(), "neo4j", clock=clock, cache_key=key)
        second.get_schema()
        assert _label_queries(second.driver) == 0, "a fresh connection missed the shared cache"
        clock.advance(61)
        second.get_schema()
        assert _label_queries(second.driver) == 1
        mcp.clear_schema_cache()
        third = GraphQuery(_schema_driver(), "neo4j", clock=clock, cache_key=key)
        third.get_schema()
        assert _label_queries(third.driver) == 1
    finally:
        mcp.clear_schema_cache()


def test_default_ttl_is_sixty_seconds():
    assert mcp.DEFAULT_SCHEMA_TTL == 60.0
    assert "staleness" in GraphQuery.get_schema.__doc__.lower()


# ============================================================= explain_impact ==
_IMPACT_SCRIPT = [
    # ordered: the link/entity needles must be tried before the plainer ones,
    # because the UNION queries also mention (v:View) / (p:StoredProcedure)
    ("MATCH (t:Table) WHERE toLower(t.name) = toLower($target)", []),
    ("BASED_ON", [{"kind": "View", "database": "sales", "name": "v_daily", "table": "orders"},
                  {"kind": "StoredProcedure", "database": "sales", "name": "sp_close_order",
                   "table": "orders"}]),
    ("MAPS_TO", [{"repo": "erp", "entity": "com.acme.Order", "name": "Order",
                  "table": "orders", "via": "MAPS_TO"}]),
    ("MATCH (c:Column)", [{"database": "sales", "table": "orders", "column": "order_id",
                           "dataType": "int"},
                          {"database": "sales", "table": "order_line", "column": "order_id",
                           "dataType": "int"}]),
    ("FOREIGN_KEY", [{"fromTable": "order_line", "fromColumn": "order_id",
                      "toTable": "orders", "toColumn": "order_id"}]),
    ("MATCH (i:Index)", [{"database": "sales", "table": "orders", "name": "ix_order",
                          "columns": ["order_id"], "isUnique": True}]),
    ("MATCH (p:StoredProcedure)", [{"database": "sales", "procedure": "sp_close_order"}]),
    ("MATCH (v:View)", [{"database": "sales", "view": "v_open_orders"}]),
]


def test_explain_impact_groups_findings_by_severity():
    driver = FakeDriver(script=_IMPACT_SCRIPT)
    report = GraphQuery(driver, "neo4j").explain_impact("order_id")

    assert report["kind"] == "column" and report["target"] == "order_id"
    assert report["tables"] == ["order_line", "orders"]
    assert sorted(report["direct"]) == ["columns", "foreignKeys", "indexes", "tables"]
    assert sorted(report["transitive"]) == ["entities", "storedProcedures", "views"]
    assert len(report["direct"]["columns"]) == 2
    assert report["direct"]["foreignKeys"][0]["toTable"] == "orders"
    assert report["direct"]["indexes"][0]["name"] == "ix_order"
    assert report["transitive"]["entities"][0]["entity"] == "com.acme.Order"
    # direct: 0 tables + 2 columns + 1 fk + 1 index; transitive: 2 views + 1 proc + 1 entity
    assert report["counts"]["direct"] == 4 and report["counts"]["transitive"] == 4
    assert isinstance(report["summary"], str)
    assert "order_id" in report["summary"] and "direct impact" in report["summary"]
    assert "2 columns" in report["summary"] and "1 JPA entity" in report["summary"]


def test_explain_impact_records_how_each_transitive_hit_was_found():
    driver = FakeDriver(script=_IMPACT_SCRIPT)
    report = GraphQuery(driver, "neo4j").explain_impact("order_id")

    views = {row["view"]: row["via"] for row in report["transitive"]["views"]}
    assert views == {"v_daily": ["link"], "v_open_orders": ["definition"]}
    procs = report["transitive"]["storedProcedures"]
    assert procs == [{"database": "sales", "procedure": "sp_close_order",
                      "via": ["definition", "link"]}], "text + link evidence must merge"


def test_explain_impact_looks_for_entities_by_mapped_table_and_by_maps_to():
    driver = FakeDriver(script=_IMPACT_SCRIPT)
    GraphQuery(driver, "neo4j").explain_impact("order_id")

    cypher, params = driver.one("MAPS_TO")
    assert "e.mappedTable" in cypher and "(e:Class)-[:MAPS_TO]->(t:Table)" in cypher
    assert " UNION " in cypher, "both routes to an entity must be covered"
    assert params == {"tables": ["order_line", "orders"]}


def test_explain_impact_auto_detects_a_table_and_scopes_its_queries():
    script = list(_IMPACT_SCRIPT)
    script[0] = ("MATCH (t:Table) WHERE toLower(t.name) = toLower($target)",
                 [{"database": "sales", "schema": "dbo", "name": "orders"}])
    driver = FakeDriver(script=script)
    report = GraphQuery(driver, "neo4j").explain_impact("orders")

    assert report["kind"] == "table"
    assert report["direct"]["tables"] == [{"database": "sales", "schema": "dbo", "name": "orders"}]
    # the table variant filters on c.table / i.table, not on the column name
    assert "toLower(c.table) = toLower($t)" in driver.one("MATCH (c:Column)")[0]
    assert "toLower(i.table) = toLower($t)" in driver.one("MATCH (i:Index)")[0]
    assert driver.one("MATCH (i:Index)")[1] == {"t": "orders"}
    assert "orders" in report["tables"]


def test_explain_impact_can_be_forced_to_read_a_name_as_a_column():
    script = list(_IMPACT_SCRIPT)
    script[0] = ("MATCH (t:Table) WHERE toLower(t.name) = toLower($target)",
                 [{"database": "sales", "schema": "dbo", "name": "orders"}])
    driver = FakeDriver(script=script)
    report = GraphQuery(driver, "neo4j").explain_impact("orders", kind="column")
    assert report["kind"] == "column"
    assert "toLower(c.name) CONTAINS toLower($col)" in driver.one("MATCH (c:Column)")[0]


def test_explain_impact_rejects_an_unknown_kind():
    graph = GraphQuery(FakeDriver(), "neo4j")
    for kind in ("view", "procedure", "COLUMNS", "; DROP"):
        with pytest.raises(ValueError):
            graph.explain_impact("orders", kind)
    assert graph.driver.executed == []


def test_explain_impact_with_no_matches_skips_the_join_queries():
    driver = FakeDriver(rows=[])
    report = GraphQuery(driver, "neo4j").explain_impact("nope")
    assert report["tables"] == []
    assert report["counts"]["direct"] == 0 and report["counts"]["transitive"] == 0
    assert driver.matching("MAPS_TO") == [] and driver.matching("BASED_ON") == []
    assert "0 columns" in report["summary"]


# ============================================================== find_dead_code ==
_DEAD_SCRIPT = [
    ("RETURN f.path AS path", [
        {"path": "src/legacy/Unused.java", "repo": "erp",
         "lastChanged": "2019-04-02", "classes": ["com.acme.Unused"]},
        {"path": "src/legacy/Orphan.java", "repo": "erp", "lastChanged": "", "classes": []},
    ]),
    ("RETURN cls.fqn AS fqn", [
        {"fqn": "com.acme.Unused", "name": "Unused", "path": "src/legacy/Unused.java",
         "repo": "erp", "lastChanged": "2019-04-02", "language": "java"},
    ]),
]


def test_find_dead_code_returns_candidates_with_their_last_change():
    driver = FakeDriver(script=_DEAD_SCRIPT)
    out = GraphQuery(driver, "neo4j").find_dead_code("erp", days=365)

    assert out["repo"] == "erp" and out["days"] == 365
    assert [f["path"] for f in out["files"]] == ["src/legacy/Unused.java", "src/legacy/Orphan.java"]
    assert out["files"][0]["lastChanged"] == "2019-04-02"
    assert out["files"][1]["lastChanged"] is None, "a never-committed file reports null, not ''"
    assert out["classes"][0]["fqn"] == "com.acme.Unused"
    assert out["counts"] == {"files": 2, "classes": 1}
    assert "candidates" in out["summary"].lower()


def test_find_dead_code_cutoff_is_days_ago_and_is_a_parameter():
    driver = FakeDriver(script=_DEAD_SCRIPT)
    out = GraphQuery(driver, "neo4j").find_dead_code("erp", days=180)
    expected = (datetime.now(timezone.utc) - timedelta(days=180)).date().isoformat()
    assert out["cutoff"] == expected

    files_cypher, files_params = driver.one("RETURN f.path AS path")
    assert files_params == {"repo": "erp", "cutoff": expected, "limit": 200}
    assert "erp" not in files_cypher, "the repo name is never interpolated"
    assert expected not in files_cypher


def test_find_dead_code_tests_both_staleness_and_inbound_references():
    driver = FakeDriver(script=_DEAD_SCRIPT)
    GraphQuery(driver, "neo4j").find_dead_code("erp")

    for needle in ("RETURN f.path AS path", "RETURN cls.fqn AS fqn"):
        cypher, _ = driver.one(needle)
        assert "(commit:Commit)-[:CHANGED]->(f)" in cypher, needle
        assert "lastChanged < $cutoff" in cypher, needle
        assert "lastChanged IS NULL OR lastChanged = ''" in cypher, needle
        assert "[:IMPORTS]->(ref:Class)" in cypher, needle
        assert "ref.fqn = cls.fqn" in cypher, needle          # bridges the external stub node
        assert "importerFile.repo = $repo" in cypher, needle  # same repo only
        assert "importerFile <> f" in cypher, needle          # a file importing itself is not use
        assert "importers = 0" in cypher, needle


def test_find_dead_code_is_explicit_that_results_are_only_candidates():
    doc = (GraphQuery.find_dead_code.__doc__ or "").lower()
    assert "candidates, not proof" in doc
    assert "reflection" in doc and "dependency injection" in doc
    out = GraphQuery(FakeDriver(), "neo4j").find_dead_code("erp")
    assert "reflection" in out["caveat"].lower()
    assert "never proof" in out["caveat"].lower()


def test_find_dead_code_normalises_hostile_arguments():
    driver = FakeDriver()
    out = GraphQuery(driver, "neo4j").find_dead_code("erp", days=-5, limit=0)
    assert out["days"] == 0
    assert out["cutoff"] == datetime.now(timezone.utc).date().isoformat()
    assert driver.executed[0][1]["limit"] == 1


# ========================================================= blast_radius_of_file ==
_BLAST_ROWS = [
    {"file": "src/Order.java", "repo": "erp", "class": "com.acme.Order", "className": "Order",
     "language": "java",
     "methods": [{"name": "total", "visibility": "public", "line": 12},
                 {"name": "close", "visibility": "private", "line": 30}],
     "callers": [{"file": "src/Cart.java", "class": "com.acme.Cart", "repo": "erp"},
                 {"file": "src/Invoice.java", "class": "com.acme.Invoice", "repo": "erp"}]},
    {"file": "src/Order.java", "repo": "erp", "class": "com.acme.OrderLine",
     "className": "OrderLine", "language": "java",
     "methods": [{"name": "price", "visibility": "public", "line": 60}],
     "callers": [{"file": "src/Cart.java", "class": "com.acme.Cart", "repo": "erp"}]},
]


def test_blast_radius_uses_one_traversal():
    driver = FakeDriver(rows=_BLAST_ROWS)
    GraphQuery(driver, "neo4j").blast_radius_of_file("src/Order.java")
    assert len(driver.executed) == 1, "blast_radius_of_file must be a single traversal"

    cypher, params = driver.executed[0]
    assert params == {"path": "src/Order.java"}
    assert "src/Order.java" not in cypher
    assert "f.path = $path OR f.id = $path" in cypher
    assert "(f)-[:CONTAINS_CLASS]->(cls:Class)" in cypher
    assert "(cls)-[:HAS_METHOD]->(m:Method)" in cypher
    assert "[:IMPORTS]->(ref:Class)" in cypher and "ref.fqn = cls.fqn" in cypher
    assert "callerFile <> f" in cypher


def test_blast_radius_shapes_classes_methods_and_callers():
    graph = GraphQuery(FakeDriver(rows=_BLAST_ROWS), "neo4j")
    out = graph.blast_radius_of_file("src/Order.java")

    assert out["found"] is True and out["file"] == "src/Order.java" and out["repo"] == "erp"
    assert [c["class"] for c in out["classes"]] == ["com.acme.Order", "com.acme.OrderLine"]
    assert [m["name"] for m in out["classes"][0]["methods"]] == ["total", "close"]
    assert out["counts"] == {"classes": 2, "methods": 3, "callerFiles": 2}
    assert [c["file"] for c in out["callers"]] == ["src/Cart.java", "src/Invoice.java"]
    assert out["callers"][0]["classes"] == ["com.acme.Cart"]
    assert "2 classes" in out["summary"] and "3 methods" in out["summary"]
    assert "2 files" in out["summary"]


def test_blast_radius_drops_the_null_placeholders_optional_match_collects():
    """A class with no methods and no callers yields collect()'d maps full of nulls."""
    rows = [{"file": "src/Lonely.java", "repo": "erp", "class": "com.acme.Lonely",
             "className": "Lonely", "language": "java",
             "methods": [{"name": None, "visibility": None, "line": None}],
             "callers": [{"file": None, "class": None, "repo": None}]}]
    out = GraphQuery(FakeDriver(rows=rows), "neo4j").blast_radius_of_file("src/Lonely.java")
    assert out["classes"][0]["methods"] == [] and out["classes"][0]["callers"] == []
    assert out["callers"] == []
    assert out["counts"] == {"classes": 1, "methods": 0, "callerFiles": 0}
    assert "1 class" in out["summary"] and "0 methods" in out["summary"]


def test_blast_radius_of_a_file_with_no_classes():
    rows = [{"file": "docs/readme.md", "repo": "erp", "class": None, "className": None,
             "language": None, "methods": [], "callers": []}]
    out = GraphQuery(FakeDriver(rows=rows), "neo4j").blast_radius_of_file("docs/readme.md")
    assert out["found"] is True and out["classes"] == []
    assert out["counts"] == {"classes": 0, "methods": 0, "callerFiles": 0}


def test_blast_radius_of_an_unknown_path_says_so():
    out = GraphQuery(FakeDriver(rows=[]), "neo4j").blast_radius_of_file("nope.java")
    assert out["found"] is False and out["classes"] == [] and out["callers"] == []
    assert "no :file node" in out["summary"].lower()


# ================================================================ tool surface ==
class _FakeFastMCP:
    """Records the tools build_server registers, standing in for FastMCP."""

    def __init__(self, name):
        self.name = name
        self.tools: dict[str, object] = {}

    def tool(self):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


def _build_server_with_fakes(driver):
    """Run build_server() with the mcp package and the Neo4j connection faked out."""
    servers: list[_FakeFastMCP] = []

    def _factory(name):
        server = _FakeFastMCP(name)
        servers.append(server)
        return server

    fastmcp = types.ModuleType("mcp.server.fastmcp")
    fastmcp.FastMCP = _factory
    mcp_server = types.ModuleType("mcp.server")
    mcp_server.fastmcp = fastmcp
    mcp_pkg = types.ModuleType("mcp")
    mcp_pkg.server = mcp_server
    saved = {name: sys.modules.get(name) for name in ("mcp", "mcp.server", "mcp.server.fastmcp")}
    try:
        sys.modules.update({"mcp": mcp_pkg, "mcp.server": mcp_server,
                            "mcp.server.fastmcp": fastmcp})
        # Inject the graph rather than monkeypatching GraphQuery.connect: the
        # connection is lazy now, so a patch that only spans build_server would
        # be long gone by the time a tool actually calls through.
        mcp.build_server(Neo4jSettings(uri="bolt://x:7687", database="neo4j"),
                         graph=GraphQuery(driver, "neo4j"))
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
    return servers[0]


def test_build_server_registers_every_documented_tool():
    server = _build_server_with_fakes(FakeDriver())
    expected = {"get_schema", "read_cypher", "search_nodes", "node_neighbors", "find_code",
                "search_codebase", "find_table", "find_procedure", "impact_of_column",
                "explain_impact", "find_dead_code", "blast_radius_of_file"}
    assert set(server.tools) == expected
    assert server.name == "graphforge"
    for name, fn in server.tools.items():
        assert (fn.__doc__ or "").strip(), f"{name} has no description for the client"


def test_module_docstring_lists_the_new_tools():
    doc = mcp.__doc__ or ""
    for name in ("explain_impact", "find_dead_code", "blast_radius_of_file", "search_codebase"):
        assert f"``{name}``" in doc, name
    assert "offset" in doc and "hasMore" in doc


def test_paged_tools_answer_with_rows_total_and_hasmore():
    driver = _page_driver([{"name": "orders"}], total=9)
    server = _build_server_with_fakes(driver)
    for tool, args in (("search_nodes", ("Table", "name", "ord", 1, 0)),
                       ("find_code", ("ord", 1, 0)),
                       ("find_table", ("ord", 1, 0)),
                       ("search_codebase", ("ord", "code", "", 1, 0))):
        payload = json.loads(server.tools[tool](*args))
        assert payload["total"] == 9 and payload["hasMore"] is True, tool
        assert payload["limit"] == 1 and payload["offset"] == 0, tool
        assert isinstance(payload["rows"], list), tool


def test_new_tools_return_json_documents():
    driver = FakeDriver(script=_IMPACT_SCRIPT + _DEAD_SCRIPT)
    server = _build_server_with_fakes(driver)

    impact = json.loads(server.tools["explain_impact"]("order_id"))
    assert impact["summary"] and impact["direct"]["columns"]

    dead = json.loads(server.tools["find_dead_code"]("erp", 90))
    assert dead["days"] == 90 and dead["caveat"]

    blast = json.loads(server.tools["blast_radius_of_file"]("src/Order.java"))
    assert "counts" in blast and "summary" in blast


def test_read_cypher_tool_still_refuses_a_write():
    server = _build_server_with_fakes(FakeDriver())
    with pytest.raises(ValueError):
        server.tools["read_cypher"]("MATCH (n) DETACH DELETE n")


def test_get_schema_tool_exposes_ttl_and_refresh():
    driver = _schema_driver()
    server = _build_server_with_fakes(driver)
    server.tools["get_schema"]()
    server.tools["get_schema"]()          # served from the cache
    assert _label_queries(driver) == 1
    server.tools["get_schema"](refresh=True)
    assert _label_queries(driver) == 2
    server.tools["get_schema"](0)         # ttl=0 bypasses
    assert _label_queries(driver) == 3
    mcp.clear_schema_cache()
