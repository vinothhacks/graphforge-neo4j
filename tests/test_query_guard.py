"""Layers 2–4 of the read-query gate: no connection is opened for a denial."""

from __future__ import annotations

import pytest
from test_mcp_tools import FakeDriver
from test_ui_api import WRITE_QUERIES, _settings, _Spy

from graphforge.mcp.server import GraphQuery
from graphforge.query_guard import READ_PROCEDURES, check_read_query, is_denied
from graphforge.ui import server as ui

MUST_REJECT = [
    "CREATE (x)",
    "MATCH (n) SET n.x=1",
    "MATCH (n) REMOVE n.flag RETURN n",
    "MATCH (n) DELETE n",
    "MATCH (n) DETACH DELETE n",
    "MERGE (a:Thing {id: 1})",
    "DROP INDEX node_id",
    "CREATE INDEX FOR (n:X) ON (n.name)",
    "DROP CONSTRAINT x",
    "PROFILE CREATE (n)",
    "CALL { CREATE (n) } RETURN 1",
    "CALL apoc.cypher.doIt('CREATE (n)', {})",
    "CALL apoc.cypher.runWrite('CREATE (n)', {})",
    "CALL apoc.trigger.add('x', 'true', {})",
    "CALL apoc.load.json('http://127.0.0.1:1/')",
    "LOAD CSV FROM 'http://127.0.0.1:1/x.csv' AS r RETURN r",
    "CALL dbms.security.createUser('a','b')",
    "SHOW USERS",
    "SHOW DATABASES",
    "SHOW SETTINGS",
    "USE system MATCH (n) RETURN n",
    "MATCH (n) RETURN n; CREATE (m)",
    "CALL some.procedure.that.does.not.exist()",
    # Backtick-quoted procedure names. Cypher accepts these; the allowlist used
    # to never see them, because the name matched no bare-identifier pattern and
    # an unparseable CALL was simply not checked. Deny-by-default now applies.
    "CALL `apoc.util.sleep`(1000)",
    "CALL `apoc.load.json`('http://127.0.0.1:1/')",
    "CALL `dbms.security.listUsers`()",
    "CALL `apoc`.util.sleep(1000)",
    "CALL apoc.`util`.sleep(1000)",
    "CALL `db.labels`() YIELD label CALL `apoc.x`() RETURN 1",
    # A CALL whose target cannot be read at all is a denial, not a pass.
    "MATCH (n) CALL",
]

MUST_ALLOW = [
    "CALL db.labels()",
    "CALL db.relationshipTypes()",
    "CALL db.propertyKeys()",
    "MATCH (n) WHERE n.name = 'CREATE' RETURN n",
    "MATCH (n:Create) RETURN n",
    "MATCH (n) RETURN n.deleted",
    "EXPLAIN MATCH (n) RETURN n",
    # A backtick-quoted identifier is data to the guard, not code: a keyword
    # inside one must not trip the write-clause check, and a `CALL ...` label
    # must not be mistaken for an actual procedure call.
    "MATCH (n:`Pending DELETE`) RETURN n",
    "MATCH (n) WHERE n.`weird CREATE prop` IS NOT NULL RETURN n",
    "MATCH (n:`CALL apoc.util.sleep`) RETURN n",
]


def test_allowlist_is_frozen_and_tiny():
    assert (
        frozenset(
            {
                "db.labels",
                "db.relationshiptypes",
                "db.propertykeys",
            }
        )
        == READ_PROCEDURES
    )


def test_must_reject_is_denied_without_touching_the_driver():
    for cypher in MUST_REJECT + WRITE_QUERIES:
        assert is_denied(cypher), cypher
        assert check_read_query(cypher)
        graph = GraphQuery(FakeDriver(), "neo4j")
        with pytest.raises(ValueError, match="read-only"):
            graph.read_cypher(cypher)
        assert graph.driver.executed == [], cypher


def test_must_allow_is_not_a_regex_false_positive():
    driver = FakeDriver(rows=[{"ok": 1}])
    graph = GraphQuery(driver, "neo4j")
    for cypher in MUST_ALLOW:
        assert check_read_query(cypher) is None, cypher
        graph.read_cypher(cypher)
    assert graph.driver.executed, "an allowed query never reached the driver"


def test_ui_must_reject_opens_no_connection():
    for cypher in MUST_REJECT:
        spy = _Spy()
        code, payload = ui.route(
            "/api/query", "", {"cypher": cypher}, _settings(), method="POST", connect=spy
        )
        assert code == 400, cypher
        assert payload.get("error")
        assert spy.connections == 0, cypher
