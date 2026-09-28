"""Layers 2–4 of the read-query gate: no connection is opened for a denial."""

from __future__ import annotations

import pytest
from test_mcp_tools import FakeDriver
from test_ui_api import WRITE_QUERIES, _settings, _Spy

from graphforge.mcp.server import GraphQuery
from graphforge.query_guard import (
    BUILTIN_FUNCTION_NAMESPACES,
    READ_PROCEDURES,
    check_read_query,
    is_denied,
    mask_query,
)
from graphforge.ui import server as ui

# Every query in the dicts below was admitted by the previous guard, which ran
# regexes over masked text. Keys are the query; values are what the denial must
# name, which proves the right layer caught it rather than a lucky neighbour.

#: A backtick name containing CALL. The CALL-target regex ran while quoted names
#: were still intact, ate this name's closing backtick and the next name's opening
#: one, and the re-paired backticks then blanked the real CALL between them.
REPAIRED_BACKTICKS = {
    "MATCH (n:`a CALL `) CALL apoc.util.sleep(1) MATCH (m:`b`) RETURN 1": "apoc.util.sleep",
    "MATCH (n) WHERE n.`CALL ` = 1 CALL dbms.security.listUsers() YIELD username "
    "RETURN n.`z`": "dbms.security",
}

#: Cypher strings take backslash escapes, and a doubled quote is two strings, not
#: an escape. The masker had both the wrong way round, so real code read as string
#: content, with the unmatched quote tucked into a trailing comment.
STRING_ESCAPES = {
    "RETURN '\\'' CALL apoc.util.sleep(1) //'": "apoc.util.sleep",
    'RETURN "\\"" CALL apoc.util.sleep(1) //"': "apoc.util.sleep",
    'RETURN "\\"" ; CREATE (n) //"': "multi-statement",
    # A `//` comment ends at a carriage return as well as at a line feed.
    "MATCH (n) // note\rDETACH DELETE n": "DETACH",
}

#: LOAD CSV / USE / SHOW were regexes anchored at the start of the text, so a
#: preceding clause or a PROFILE / CYPHER prefix walked past them, and the other
#: administration commands were never checked at all.
CLAUSES_PAST_THE_START = {
    "WITH 1 AS x LOAD CSV FROM 'http://example.invalid/x.csv' AS r RETURN r": "LOAD CSV",
    "PROFILE LOAD CSV FROM 'http://example.invalid/x.csv' AS r RETURN r": "LOAD CSV",
    "CYPHER runtime=slotted LOAD CSV FROM 'http://example.invalid/x.csv' AS r RETURN r": "LOAD CSV",
    "MATCH (n) LOAD /* gap */ CSV FROM 'http://example.invalid/x.csv' AS r RETURN r": "LOAD CSV",
    "CALL { LOAD CSV FROM 'http://example.invalid/x.csv' AS r RETURN r } RETURN r": "LOAD CSV",
    "PROFILE SHOW USERS": "SHOW",
    "CYPHER 5 SHOW DATABASES": "SHOW",
    "EXPLAIN CYPHER planner=cost SHOW TRANSACTIONS": "SHOW",
    "PROFILE USE system MATCH (n) RETURN n": "USE",
    "CYPHER 5 runtime=slotted USE system MATCH (n) RETURN n": "USE",
    "CALL { USE system MATCH (n) RETURN n } RETURN n": "USE",
    # A subquery may import variables with a WITH of simple references before its
    # USE; Neo4j accepts each of these forms there.
    "WITH 1 AS x CALL { WITH x USE system MATCH (n) RETURN n } RETURN n": "USE",
    "WITH 1 AS x, 2 AS y CALL { WITH x, y USE system MATCH (n) RETURN n } RETURN n": "USE",
    "WITH 1 AS x CALL { WITH x AS x USE system MATCH (n) RETURN n } RETURN n": "USE",
    "WITH 1 AS x, 2 AS y CALL { WITH *, ((x)), y AS `y` USE system MATCH (n) RETURN n } "
    "RETURN n": "USE",
    "MATCH (n) RETURN n UNION USE system MATCH (n) RETURN n": "USE",
    "MATCH (n) RETURN n UNION ALL USE system MATCH (n) RETURN n": "USE",
    # Cypher 25 query composition: each NEXT part and conditional branch is a query.
    "MATCH (n) RETURN n NEXT USE system MATCH (m) RETURN m": "USE",
    "WHEN true THEN USE system MATCH (n) RETURN n ELSE RETURN 1 AS n": "USE",
    "RETURN CASE WHEN true THEN COLLECT { WHEN true THEN USE system MATCH (n) RETURN n } END": "USE",
    # `case` is a legal variable name, so it must not open a CASE that hides a branch.
    "WHEN true THEN MATCH (case) RETURN 1 AS n ELSE USE system MATCH (n) RETURN n": "USE",
    # A `{ }` standing where a query part begins is read as one, whichever Cypher
    # version accepts it there.
    "WHEN true THEN { USE system MATCH (n) RETURN n }": "USE",
    "MATCH (n) RETURN n UNION { USE system MATCH (n) RETURN n }": "USE",
    "{ SHOW USERS }": "SHOW",
    "TERMINATE TRANSACTIONS 'neo4j-transaction-1'": "TERMINATE",
    "STOP DATABASE neo4j": "STOP",
    "START DATABASE neo4j": "START",
    "GRANT ROLE admin TO bob": "GRANT",
    "DENY TRAVERSE ON GRAPH * TO bob": "DENY",
    "REVOKE ROLE admin FROM bob": "REVOKE",
    "RENAME ROLE reader TO writer": "RENAME",
    "ENABLE SERVER 'x'": "ENABLE",
    "DEALLOCATE DATABASES FROM SERVER 'x'": "DEALLOCATE",
    "REALLOCATE DATABASES": "REALLOCATE",
    "DRYRUN REALLOCATE DATABASES": "DRYRUN",
}

#: Only CALL sites were held to the allowlist; function calls were never looked
#: at, so plugin functions got past "deny every apoc.*". runFirstColumn* runs
#: the Cypher it is handed as a string.
PLUGIN_FUNCTIONS = {
    "RETURN apoc.cypher.runFirstColumnMany('MATCH (n) RETURN n', {}) AS x": "apoc.cypher",
    "RETURN apoc.cypher.runFirstColumnSingle('MATCH (n) RETURN n', {}) AS x": "apoc.cypher",
    "RETURN `apoc`.cypher.runFirstColumnMany('MATCH (n) RETURN n', {}) AS x": "apoc.cypher",
    "RETURN `apoc.cypher.runFirstColumnMany`('MATCH (n) RETURN n', {}) AS x": "apoc.cypher",
    "RETURN apoc . cypher /* gap */ . runFirstColumnMany ('MATCH (n) RETURN n', {}) AS x": (
        "apoc.cypher"
    ),
    "MATCH (n) WHERE apoc.text.levenshteinDistance(n.name, 'x') < 2 RETURN n": "apoc.text",
    "CALL db.labels() YIELD label RETURN apoc.util.md5([label]) AS h": "apoc.util",
    "RETURN gds.similarity.cosine([1], [1]) AS s": "gds.similarity",
}

#: Found alongside the above: GQL's synonym for CREATE, and a LIMIT over the cap
#: written with digit separators, which Neo4j runs and the old `\d+\b` misread.
#: Neo4j 5 refuses a hex or octal LIMIT outright; those are denied as a precaution.
OTHER_GAPS = {
    "INSERT (n:Thing)": "INSERT",
    "MATCH (n) RETURN n LIMIT 0x3E8": "LIMIT",
    "MATCH (n) RETURN n LIMIT 0o1750": "LIMIT",
    "MATCH (n) RETURN n LIMIT 1_000": "LIMIT",
}

#: Where the lexer cannot vouch for the text it refuses rather than guesses, so a
#: future disagreement with Neo4j's lexer fails closed.
FAIL_CLOSED = {
    "MATCH (n) RETURN n /* never closed": "unterminated comment",
    "MATCH (n) WHERE n.name = 'never closed RETURN n": "unterminated string",
    "MATCH (n:`never closed) RETURN n": "unterminated quoted identifier",
    "\ufeffUSE system MATCH (n) RETURN n": "unexpected character",
    "// only a comment": "empty query",
    "EXPLAIN": "empty query",
}

REVIEWED_BYPASSES = {
    **REPAIRED_BACKTICKS,
    **STRING_ESCAPES,
    **CLAUSES_PAST_THE_START,
    **PLUGIN_FUNCTIONS,
    **OTHER_GAPS,
    **FAIL_CLOSED,
}

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
    "CALL (apoc.util.sleep)(1)",
    "CALL $procedure()",
    # USE is still the first clause of a subquery that has a scope clause.
    "CALL () { USE system MATCH (n) RETURN n } RETURN n",
    *REVIEWED_BYPASSES,
]

#: Neo4j accepts ``case`` and ``end`` as variable names, so counting CASE ... END
#: to tell a CASE's THEN / ELSE from a conditional query's branches drifted: the
#: variable ``end`` closed the CASE early, and the value after the next THEN /
#: ELSE was read as the first clause of a branch (START, USE).
CASE_END_AS_NAMES = [
    "MATCH (start)-->(end) RETURN CASE WHEN end.name IS NULL THEN start ELSE end END AS s",
    "MATCH (use)-->(end) RETURN CASE WHEN use.x THEN end ELSE use END AS s",
    "MATCH (start)-->(end) CALL { WITH start, end "
    "RETURN CASE WHEN end.x THEN start ELSE end END AS s } RETURN s",
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
    # A keyword used as a name is a name: a label, a property, a variable, a key.
    "MATCH (n:Call) RETURN n",
    "MATCH (n) RETURN n.show, n.use, n.load, n.call",
    "MATCH (show:Show) RETURN show",
    "RETURN {load: 1, use: 2, show: 3, call: 4, delete: 5} AS m",
    # Backslash escapes: the string does not end at \' and the ; inside it is data.
    "RETURN 'it\\'s' AS s",
    "RETURN 'a\\';' AS s",
    'RETURN "say \\"hi\\"" AS s',
    # Neo4j's own function namespaces, and a prefix in front of an ordinary read.
    "RETURN date.truncate('month', date()) AS d",
    "PROFILE MATCH (n) RETURN n",
    *CASE_END_AS_NAMES,
]

#: Also accepted, but kept out of MUST_ALLOW because the live e2e suite runs that
#: list against a real server: these need parameters, a newer Neo4j 5 minor
#: (CALL scope clauses, vector functions) or are only interesting to the lexer.
ALLOW_OFFLINE = [
    "MATCH (n) WHERE n.kind = $call RETURN n",
    "MATCH (use:Use)-[:LOAD]->(csv:Csv) RETURN use, csv",
    "MATCH (n) RETURN n {.show, .use} AS m",
    # Inside CASE ... END, THEN / ELSE are followed by a value, not a clause.
    "MATCH (n) WITH n.start AS start, n.stop AS stop "
    "RETURN CASE WHEN start < stop THEN start ELSE stop END AS first",
    "MATCH (show) RETURN CASE WHEN show.x THEN show ELSE CASE WHEN true THEN show END END AS s",
    "RETURN datetime.fromepochmillis(0) AS t, duration.between(date(), date()) AS gap",
    "RETURN point.distance(point({x: 0, y: 0}), point({x: 3, y: 4})) AS d",
    "RETURN vector.similarity.cosine([1.0, 0.0], [1.0, 0.0]) AS s",
    "RETURN localtime.statement() AS a, time.transaction() AS b, localdatetime.realtime() AS c",
    "RETURN toLower('A') AS a, count(*) AS c, `toUpper`('b') AS b",
    "CALL () { MATCH (n) RETURN count(n) AS c } RETURN c",
    "MATCH (n) CALL (n) { RETURN n.name AS name } RETURN name",
    "MATCH (n) CALL (*) { RETURN 1 AS one } RETURN one",
    "CALL `db`.`labels`() YIELD label RETURN label",
    "CALL db /* gap */ . labels() YIELD label RETURN label",
    "CYPHER runtime=slotted MATCH (n) RETURN n",
    "CYPHER 5 EXPLAIN MATCH (n) RETURN n",
    "MATCH (n) RETURN n UNION ALL MATCH (n) RETURN n",
    "MATCH (n) RETURN n LIMIT 0x1F4",
    "MATCH (n) RETURN n;",
    "MATCH (n) RETURN n; ;",
    "RETURN 'x' AS `weird ``name```",
    "RETURN 'C:\\\\temp\\\\' AS path",
    "MATCH (n) // a comment\nRETURN n",
    "MATCH (n)\r\nRETURN n // trailing",
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


def test_builtin_function_namespaces_are_neo4j_own():
    assert (
        frozenset(
            {
                "date",
                "datetime",
                "localdatetime",
                "localtime",
                "time",
                "duration",
                "point",
                "vector.similarity",
            }
        )
        == BUILTIN_FUNCTION_NAMESPACES
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
    for cypher in MUST_ALLOW + ALLOW_OFFLINE:
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


@pytest.mark.parametrize(("cypher", "named"), list(REVIEWED_BYPASSES.items()))
def test_each_reviewed_bypass_is_denied_by_the_right_layer(cypher, named):
    reason = check_read_query(cypher)
    assert reason is not None, f"{cypher!r} was admitted"
    assert named in reason, f"{cypher!r} was denied for the wrong reason: {reason}"


def test_a_real_call_between_two_quoted_names_survives_masking():
    """The CALL-target rewrite ran before quoted names were blanked and swallowed the call."""
    cypher = "MATCH (n:`a CALL `) CALL apoc.util.sleep(1) MATCH (m:`b`) RETURN 1"
    masked = mask_query(cypher)
    assert "CALL apoc.util.sleep(1)" in masked
    assert "a CALL" not in masked


def test_mask_query_ends_strings_and_comments_where_neo4j_does():
    # `"\" LIMIT 5"` is one string, so its LIMIT is data, not a clause.
    assert "LIMIT" not in mask_query('RETURN "\\" LIMIT 5" AS s')
    assert "LIMIT" not in mask_query("RETURN '\\' LIMIT 5' AS s")
    # `'\''` is a complete string, so what follows it is code.
    assert "CALL x()" in mask_query("RETURN '\\'' CALL x() //'")
    # A line comment stops at a carriage return.
    assert mask_query("RETURN 1 // c\rLIMIT 5").endswith("\rLIMIT 5")
    # Block comments keep their line breaks; every offset is preserved.
    assert mask_query("/* a\nb */RETURN 1") == "    \n    RETURN 1"
    for cypher in MUST_REJECT + MUST_ALLOW + ALLOW_OFFLINE:
        assert len(mask_query(cypher)) == len(cypher), cypher


def test_read_cypher_still_caps_a_query_whose_only_limit_is_inside_a_string():
    """read_cypher decides whether to append LIMIT from masked text; an escape used to fool it."""
    graph = GraphQuery(FakeDriver(), "neo4j")
    graph.read_cypher('MATCH (n) WHERE n.doc = "\\" LIMIT 5" RETURN n', limit=25)
    assert "LIMIT 25" in graph.driver.cyphers[0]


def test_a_call_scope_clause_opens_a_subquery_whose_body_is_still_checked():
    """``CALL (x) { ... }`` is a subquery, not a procedure; the old guard refused every one."""
    assert check_read_query("MATCH (n) CALL (n) { RETURN n.name AS name } RETURN name") is None
    for cypher, named in {
        "CALL () { USE system MATCH (n) RETURN n } RETURN n": "USE",
        "MATCH (n) CALL (n) { SHOW USERS } RETURN n": "SHOW",
        "MATCH (n) CALL (*) { CALL apoc.util.sleep(1) } RETURN n": "apoc.util.sleep",
        "MATCH (n) CALL (n) { RETURN apoc.cypher.runFirstColumnMany('x', {}) AS x } RETURN x": (
            "apoc.cypher"
        ),
    }.items():
        reason = check_read_query(cypher)
        assert reason is not None and named in reason, (cypher, reason)


@pytest.mark.parametrize("cypher", CASE_END_AS_NAMES)
def test_case_and_end_as_variables_do_not_open_a_branch(cypher):
    """Only a query part that begins with WHEN has branches; a CASE's THEN / ELSE are values."""
    assert check_read_query(cypher) is None, cypher


def test_old_false_positives_are_reads_again():
    """The old guard refused these reads; each is a name that looks like a keyword."""
    for cypher, old_reason in {
        "MATCH (n:Call) RETURN n": "CALL target",
        "RETURN {delete: true} AS m": "DELETE",
        "RETURN 'a\\';' AS s": "multi-statement",
    }.items():
        assert check_read_query(cypher) is None, f"{cypher!r} still denied ({old_reason})"
