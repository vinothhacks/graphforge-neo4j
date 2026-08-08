"""Dashboard status payload: password masking + graceful degradation."""
from pathlib import Path

from graphforge.core.config import DbSettings, Neo4jSettings, Settings
from graphforge.ui import build_status


def _settings(neo_pw="topsecret", db_pw="dbsecret"):
    return Settings(
        neo4j=Neo4jSettings(uri="bolt://x:7687", user="neo4j", password=neo_pw, database="neo4j"),
        db=DbSettings(engine="mysql", host="h", user="u", password=db_pw),
    )


def test_passwords_are_masked_and_never_leaked():
    st = build_status(_settings())
    assert st["config"]["neo4j"]["password"] == "•••••• (set)"
    assert st["config"]["database"]["password"] == "•••••• (set)"
    blob = str(st)
    assert "topsecret" not in blob and "dbsecret" not in blob


def test_empty_password_shows_not_set():
    st = build_status(_settings(neo_pw="", db_pw=""))
    assert "not set" in st["config"]["neo4j"]["password"]
    assert "not set" in st["config"]["database"]["password"]


def test_degrades_gracefully_without_neo4j():
    # No live Neo4j / driver here: build_status must not raise, just report offline.
    st = build_status(_settings())
    assert st["neo4j"]["connected"] is False
    assert "config" in st  # config still shown


def test_dashboard_html_is_self_contained():
    import graphforge.ui.server as srv
    html = (Path(srv.__file__).parent / "dashboard.html").read_text(encoding="utf-8")
    assert "graphforge" in html
    assert "/api/status" in html
    assert "http" not in html.split("</style>")[0] or "cdn" not in html.lower()  # no external CDN
