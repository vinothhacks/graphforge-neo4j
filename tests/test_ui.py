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


def _dashboard_html():
    import graphforge.ui.server as srv
    return (Path(srv.__file__).parent / "dashboard.html").read_text(encoding="utf-8")


def test_dashboard_html_is_self_contained():
    html = _dashboard_html()
    assert "graphforge" in html
    assert "/api/status" in html
    assert "http" not in html.split("</style>")[0] or "cdn" not in html.lower()  # no external CDN


def test_dashboard_html_has_no_external_references_at_all():
    """No CDN, no absolute URL, no external stylesheet/script/font — one file, zero network."""
    html = _dashboard_html()
    low = html.lower()
    for needle in ("cdn", "http", "://", "@import", "integrity=", "crossorigin",
                   "unpkg", "jsdelivr", "googleapis", "//fonts"):
        assert needle not in low, f"dashboard.html references {needle!r}"
    assert "<script src" not in low.replace("\n", " ")
    assert "<link" not in low  # no external stylesheet or preconnect


def test_dashboard_html_ships_the_vanilla_enhancements():
    html = _dashboard_html()
    # every endpoint the dashboard talks to is same-origin and relative
    for endpoint in ("/api/status", "/api/schema", "/api/query",
                     "/api/graph/sample", "/api/search?q=", "/api/labels/", "/api/node/"):
        assert endpoint in html, endpoint
    assert "<canvas" in html and "requestAnimationFrame" in html   # force-directed viz
    assert "localStorage" in html                                   # theme + query history
    assert ":root.light" in html                                    # readable light theme
    assert "<noscript>" in html                                     # degrades without JS
    assert 'class="skel"' in html or "skel" in html                 # shimmer loading states
    assert "<textarea" in html                                      # cypher console
    assert "modal" in html                                          # label explorer
