"""Guided ingest: the one place the dashboard may change the graph.

Three invariants matter here. It must not exist at all on a non-loopback bind;
it must refuse a request that cannot present the per-run token; and it must never
render a credential into the page, which is the promise the rest of the dashboard
already keeps.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from graphforge.core.config import DbSettings, Neo4jSettings, Settings
from graphforge.ui import server as srv
from graphforge.ui.ingest import IngestJobs, Job, redact_url

SECRET = "s3cr3t-pw"
DB_URL = f"postgresql://gf:{SECRET}@db.internal:5432/shop"


def _settings():
    return Settings(
        neo4j=Neo4jSettings(uri="bolt://x:7687", user="neo4j", password="pw", database="neo4j"),
        db=DbSettings(engine="mysql", host="h"),
    )


# ------------------------------------------------------------- redaction ----
@pytest.mark.parametrize(
    "url,expected",
    [
        (DB_URL, "postgresql://gf:***@db.internal:5432/shop"),
        ("postgresql://gf@db.internal/shop", "postgresql://gf@db.internal/shop"),
        ("/plain/local/path", "/plain/local/path"),
        ("", ""),
    ],
)
def test_redact_url_hides_only_the_password(url, expected):
    assert redact_url(url) == expected


def test_a_job_never_serialises_the_credential():
    job = Job(id="j1", kind="db", source=redact_url(DB_URL), raw=DB_URL)
    job.lines.append(f"connecting to {DB_URL}")
    job.summary = f"loaded from {DB_URL}"
    job.error = f"could not reach {DB_URL}"

    blob = json.dumps(job.payload())
    assert SECRET not in blob, "a password reached the browser"
    assert "***" in blob
    assert job.raw == DB_URL, "the real URL must still be usable by the ingestor"


def test_the_log_handler_scrubs_records_it_did_not_write():
    """The ingestors log freely; scrubbing happens where the lines are captured."""
    jobs = IngestJobs(_settings())
    job = Job(id="j2", kind="db", source=redact_url(DB_URL), raw=DB_URL)
    assert SECRET not in job.scrub(f"psycopg2 connecting: {DB_URL}")
    assert SECRET not in job.scrub(f"password={SECRET} rejected")
    assert jobs.running() is None


# ------------------------------------------------------------- validation ---
def test_start_rejects_an_unknown_kind_and_an_empty_source():
    jobs = IngestJobs(_settings())
    with pytest.raises(ValueError, match="kind must be one of"):
        jobs.start("shell", "whatever")
    with pytest.raises(ValueError, match="source is required"):
        jobs.start("git", "   ")


def test_only_one_ingest_runs_at_a_time():
    jobs = IngestJobs(_settings())
    jobs._jobs["busy"] = Job(id="busy", kind="git", source=".", raw=".")
    jobs._order.append("busy")
    with pytest.raises(ValueError, match="already running"):
        jobs.start("git", ".")


# ----------------------------------------------------------------- routing --
def test_ingest_endpoints_do_not_exist_when_disabled():
    """A public bind must expose no ingest surface at all, not one that refuses."""
    for path, method in (
        ("/api/ingest", "POST"),
        ("/api/ingest", "GET"),
        ("/api/ingest/abc", "GET"),
    ):
        code, payload = srv.route(
            path, "", {"kind": "git", "source": "."}, _settings(), method=method, ingest=None
        )
        assert code == 404, (path, method)
        assert "disabled" in payload["error"]


def test_status_reports_whether_ingest_is_available():
    code, payload = srv.route(
        "/api/status",
        "",
        None,
        _settings(),
        connect=lambda _s: (_ for _ in ()).throw(RuntimeError("down")),
    )
    assert code == 200
    assert payload["ingest"] == {"enabled": False, "running": False}


def test_route_reports_a_missing_job():
    jobs = IngestJobs(_settings())
    code, payload = srv.route("/api/ingest/nope", "", None, _settings(), ingest=jobs)
    assert code == 404
    assert "no such ingest job" in payload["error"]


# ---------------------------------------------------------- over the wire ---
def _serve(allow_ingest=True, host="127.0.0.1"):
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), srv._handler(_settings(), host, allow_ingest=allow_ingest)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}"


def _post(base, path, body, headers=None):
    request = urllib.request.Request(
        base + path,
        method="POST",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


def test_an_ingest_without_the_token_is_refused():
    server, thread, base = _serve()
    try:
        code, payload = _post(base, "/api/ingest", {"kind": "git", "source": "."})
        assert code == 403
        assert "X-GF-Token" in payload["error"]

        code, payload = _post(
            base, "/api/ingest", {"kind": "git", "source": "."}, {"X-GF-Token": "guessed"}
        )
        assert code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_token_is_served_in_the_page_a_cross_origin_caller_cannot_read():
    server, thread, base = _serve()
    try:
        with urllib.request.urlopen(base + "/", timeout=10) as response:
            html = response.read().decode("utf-8")
        # The tag, not the querySelector string that reads it -- that is always present.
        assert '<meta name="gf-token" content="' in html, (
            "the page cannot authenticate its own requests"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_public_bind_serves_no_token_and_no_ingest():
    server, thread, base = _serve(host="0.0.0.0")
    try:
        with urllib.request.urlopen(base + "/", timeout=10) as response:
            html = response.read().decode("utf-8")
        assert '<meta name="gf-token"' not in html, "a token was minted for a public bind"
        code, _ = _post(base, "/api/ingest", {"kind": "git", "source": "."})
        assert code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
