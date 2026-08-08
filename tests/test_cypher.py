import pytest

from graphforge.core.cypher import (
    NodeRef,
    Operation,
    escape_cypher_string,
    lit,
    merge_node,
    merge_rel,
)


def test_escape():
    assert escape_cypher_string("a'b") == "a\\'b"
    assert escape_cypher_string("a\\b") == "a\\\\b"
    assert escape_cypher_string("l1\nl2") == "l1\\nl2"
    assert escape_cypher_string(None) == ""


def test_lit_types():
    assert lit(None) == "null"
    assert lit(True) == "true"
    assert lit(False) == "false"
    assert lit(3) == "3"
    assert lit("x") == "'x'"
    assert lit(["a", 1]) == "['a', 1]"
    assert lit({"k": "v"}) == "{k: 'v'}"


def test_merge_node_params():
    op = merge_node("File", {"id": "r/p"}, {"name": "p", "totalLines": 5})
    assert "MERGE (n:File {id: $k_id})" in op.cypher
    assert op.params["k_id"] == "r/p"
    assert op.params["p_name"] == "p"
    assert op.params["p_totalLines"] == 5


def test_to_script_param_collision():
    # $p_id must not partially replace inside $p_identifier
    op = Operation("SET n.a = $p_id, n.b = $p_identifier", {"p_id": 1, "p_identifier": 2})
    script = op.to_script()
    assert "n.a = 1" in script
    assert "n.b = 2" in script


def test_merge_rel():
    op = merge_rel(NodeRef("A", {"id": "x"}), "REL", NodeRef("B", {"id": "y"}), {"w": 1})
    assert "MERGE (a)-[r:REL]->(b)" in op.cypher
    assert op.params == {"a_id": "x", "b_id": "y", "r_w": 1}


def test_rejects_unsafe_identifier():
    with pytest.raises(ValueError):
        merge_node("A;DROP", {"id": "x"})
    with pytest.raises(ValueError):
        merge_rel(NodeRef("A", {"id": "x"}), "REL) DELETE n //", NodeRef("B", {"id": "y"}))
