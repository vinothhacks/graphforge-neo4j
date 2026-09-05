from graphforge.core.cypher import merge_node
from graphforge.core.neo4j_writer import Neo4jWriter, _split_statements


def test_split_statements_ignores_semicolons_in_strings():
    assert len(_split_statements("CREATE (a); CREATE (b);")) == 2
    assert len(_split_statements("MERGE (n {p:'a;b'});")) == 1
    assert len(_split_statements("// c; comment\nCREATE (a);")) == 1


def test_emit_mode(tmp_path):
    out = tmp_path / "o.cypher"
    with Neo4jWriter(settings=None, emit_path=str(out)) as w:
        assert w.mode == "emit"
        w.write([merge_node("X", {"id": "1"}, {"n": "a"})])
    assert w.ops_written == 1
    text = out.read_text()
    assert "MERGE (n:X {id: '1'})" in text
    assert "n.n = 'a'" in text


def test_dry_run_counts_only():
    with Neo4jWriter(settings=None, dry_run=True) as w:
        assert w.mode == "dry-run"
        w.write([merge_node("X", {"id": "1"}), merge_node("Y", {"id": "2"})])
    assert w.ops_written == 2


def test_apply_schema_emit(tmp_path):
    out = tmp_path / "s.cypher"
    with Neo4jWriter(settings=None, emit_path=str(out)) as w:
        n = w.apply_schema(
            "CREATE CONSTRAINT a IF NOT EXISTS FOR (x:X) REQUIRE x.id IS UNIQUE;\n"
            "CREATE INDEX b IF NOT EXISTS FOR (x:X) ON (x.name);"
        )
    assert n == 2
    assert "CONSTRAINT" in out.read_text()
