"""Whole-token table matching in the SQL-text link passes.

The BASED_ON / USES_TABLE / CROSS_DB_REFERENCE passes used to do a naive
`CONTAINS toLower(t.name)` substring test, so a table called `os` matched every
definition mentioning `os_config`, `position` or `cost`. They now compare
space-delimited tokens.
"""

from graphforge.cli import build_parser
from graphforge.core.neo4j_writer import Neo4jWriter
from graphforge.link.passes import (
    DEFAULT_MIN_NAME_LEN,
    PASSES,
    LinkRunner,
    matches_table,
)

TEXT_PASSES = ("based-on", "uses-table", "cross-db")

# A view definition that mentions short table names only *inside* longer words.
VIEW_SQL = """
CREATE VIEW v_active AS
SELECT o.order_id, o.os_version, p.position, p.cost_total
FROM   dbo.order_header o
JOIN   sys_identity     p ON p.identity_key = o.order_id
WHERE  o.status_code = 'OPEN';
"""


def _simulate(op, definition, table_name):
    """Evaluate the pass's own Cypher token test in Python.

    Driven by the parameters the Operation actually emits, so the check cannot
    drift away from the statement that Neo4j will run:
        ' ' + reduce(s = toLower(def), sep IN $seps | replace(s, sep, ' ')) + ' '
        CONTAINS ' ' + toLower(t.name) + ' '
    """
    if len(table_name) < op.params["min"]:
        return False
    text = definition.lower()
    for sep in op.params["seps"]:
        text = text.replace(sep, " ")
    return (" " + table_name.lower() + " ") in (" " + text + " ")


def _naive(definition, table_name):
    """The old rule, kept as the thing we are regression-testing against."""
    return table_name.lower() in definition.lower()


# ---- the regression -------------------------------------------------------
def test_short_names_no_longer_match_inside_longer_words():
    op = PASSES["based-on"](min_name_len=2)
    for short in ("os", "id", "cost"):
        assert _naive(VIEW_SQL, short) is True, f"{short} used to match"
        assert _simulate(op, VIEW_SQL, short) is False, f"{short} still matches"
        assert matches_table(VIEW_SQL, short, min_name_len=2) is False


def test_real_table_references_are_still_found():
    op = PASSES["based-on"]()
    for real in ("order_header", "sys_identity", "v_active"):
        assert _simulate(op, VIEW_SQL, real) is True
        assert matches_table(VIEW_SQL, real) is True


def test_tokens_survive_every_sql_separator():
    op = PASSES["uses-table"]()
    for definition in (
        "SELECT * FROM dbo.orders;",
        "select*from orders,customers",
        "INSERT INTO [orders](id)VALUES(1)",
        "UPDATE\n\torders\nSET x=1",
        "SELECT 1 FROM `orders`",
        'JOIN "orders" ON 1=1',
    ):
        assert _simulate(op, definition, "orders") is True, definition


def test_min_name_length_defaults_to_four_and_is_configurable():
    assert DEFAULT_MIN_NAME_LEN == 4
    for name in TEXT_PASSES:
        assert PASSES[name]().params["min"] == 4
        assert PASSES[name](min_name_len=7).params["min"] == 7
    # a 3-character table is now ignored by default (it was allowed before)
    assert matches_table("select * from log", "log") is False
    assert matches_table("select * from log", "log", min_name_len=3) is True


def test_every_text_pass_uses_token_matching():
    for name in TEXT_PASSES:
        script = PASSES[name]().to_script()
        assert "CONTAINS (' ' + toLower(t.name) + ' ')" in script
        assert "reduce(s = toLower(" in script
        # the naive substring form is gone
        assert "CONTAINS toLower(t.name)" not in script
    # MAPS_TO is an exact match and is deliberately unaffected
    assert "toLower(t.name) = toLower(e.mappedTable)" in PASSES["maps-to"]().to_script()


def test_matches_table_is_defensive():
    assert matches_table("", "orders") is False
    assert matches_table("select * from orders", "") is False
    assert matches_table("SELECT * FROM ORDERS", "orders") is True  # case-insensitive


# ---- CLI plumbing ---------------------------------------------------------
def test_cli_min_table_name_len_flag(tmp_path):
    args = build_parser().parse_args(["link"])
    assert args.min_table_name_len == DEFAULT_MIN_NAME_LEN

    args = build_parser().parse_args(["link", "--based-on", "--min-table-name-len", "6"])
    assert args.min_table_name_len == 6

    out = tmp_path / "link.cypher"
    with Neo4jWriter(settings=None, emit_path=str(out)) as w:
        assert LinkRunner(w).run(["based-on"], min_name_len=6) == 1
    assert "size(t.name) >= 6" in out.read_text()


def test_link_runner_rejects_unknown_passes(tmp_path):
    import pytest

    out = tmp_path / "link.cypher"
    with Neo4jWriter(settings=None, emit_path=str(out)) as w, pytest.raises(ValueError):
        LinkRunner(w).run(["nope"])
