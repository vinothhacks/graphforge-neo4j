"""Tests for the second-pass features: semantic parsing, linking, VDS, clear."""

import pytest

from graphforge.core.config import GitSettings
from graphforge.core.cypher import NodeRef, set_label
from graphforge.core.neo4j_writer import Neo4jWriter
from graphforge.db.vds import VdsIngestor
from graphforge.git.ingest import GitIngestor
from graphforge.git.parsers.java import extract
from graphforge.link.passes import PASSES


# ---- semantic Java parsing ------------------------------------------------
def test_entity_with_table_annotation():
    info = extract(["@Entity", '@Table(name="vessel_master")', "public class Vessel {}"])
    c = info["classes"][0]
    assert c["stereotype"] == "Entity"
    assert c["mappedTable"] == "vessel_master"
    assert set(c["annotations"]) == {"Entity", "Table"}


def test_entity_defaults_table_to_class_name():
    c = extract(["@Entity", "public class Foo {}"])["classes"][0]
    assert c["stereotype"] == "Entity"
    assert c["mappedTable"] == "Foo"


def test_ejb_and_managedbean_stereotypes():
    assert extract(["@Stateless", "public class Bean {}"])["classes"][0]["stereotype"] == "EJBBean"
    assert (
        extract(["@ManagedBean", "public class Ctrl {}"])["classes"][0]["stereotype"]
        == "ManagedBean"
    )


def test_plain_class_has_no_stereotype():
    c = extract(["public class Plain {}"])["classes"][0]
    assert c["stereotype"] == ""
    assert c["mappedTable"] == ""


# ---- set_label builder ----------------------------------------------------
def test_set_label():
    op = set_label(NodeRef("Class", {"id": "x"}), "Entity")
    assert "SET n:Entity" in op.cypher
    assert op.params == {"k_id": "x"}
    with pytest.raises(ValueError):
        set_label(NodeRef("Class", {"id": "x"}), "Bad Label")


# ---- link passes ----------------------------------------------------------
def test_link_passes_render():
    assert set(PASSES) == {"maps-to", "based-on", "uses-table", "cross-db"}
    assert "MAPS_TO" in PASSES["maps-to"]().to_script()
    assert "BASED_ON" in PASSES["based-on"]().to_script()
    assert "USES_TABLE" in PASSES["uses-table"]().to_script()
    assert "CROSS_DB_REFERENCE" in PASSES["cross-db"]().to_script()


# ---- clear / --replace ----------------------------------------------------
def test_clear_repo_emit(tmp_path):
    out = tmp_path / "c.cypher"
    with Neo4jWriter(settings=None, emit_path=str(out)) as w:
        n = GitIngestor(w, GitSettings()).clear_repo("myrepo")
    text = out.read_text()
    assert n >= 8
    assert "DETACH DELETE" in text
    assert "'myrepo'" in text
    assert "IN TRANSACTIONS" in text  # batched Line delete


# ---- VDS catalog mapping --------------------------------------------------
def test_vds_write_rows(tmp_path):
    rows = [
        {
            "sid": 1,
            "servicename": "getrskmaster",
            "query": "SELECT * FROM rskmaster",
            "coretable": "rskmaster",
            "groupby": "",
            "orderby": "",
            "tablename": "rskmaster",
            "columnname": "rskcode",
            "fieldname": "code",
        },
        {
            "sid": 1,
            "servicename": "getrskmaster",
            "query": "SELECT * FROM rskmaster",
            "coretable": "rskmaster",
            "groupby": "",
            "orderby": "",
            "tablename": "vesselmaster",
            "columnname": "vcode",
            "fieldname": "vc",
        },
    ]
    out = tmp_path / "v.cypher"
    with Neo4jWriter(settings=None, emit_path=str(out)) as w:
        stats = VdsIngestor(w).write_rows(rows, "mysql", "h", "nfr_shore")
    text = out.read_text()
    assert stats["services"] == 1  # one service (sid=1) across both rows
    assert stats["queries"] == 1
    assert stats["whereFields"] == 2  # two distinct where-fields
    assert "MERGE (n:VDSService {id: 'vds://mysql://h/nfr_shore/1'})" in text
    for rel in ("HAS_QUERY", "USES_TABLE", "HAS_WHERE_FIELD", "REFERENCES_TABLE"):
        assert rel in text
