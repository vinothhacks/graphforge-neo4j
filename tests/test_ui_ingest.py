"""Guided ingest: the one place the dashboard may change the graph.

Three invariants matter here. It must not exist at all on a non-loopback bind;
it must refuse a request that cannot present the per-run token; and it must never
render a credential into the page, which is the promise the rest of the dashboard
already keeps.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

import pytest

from graphforge.core.config import DbSettings, GitSettings, Neo4jSettings, Settings
from graphforge.ui import ingest as ingest_mod
from graphforge.ui import server as srv
from graphforge.ui.ingest import IngestJobs, Job, _Capture, redact_url

SECRET = "s3cr3t-pw"
DB_URL = f"postgresql://gf:{SECRET}@db.internal:5432/shop"
#: A token passed as the URL's username, the way forges accept one over https.
TOKEN = "gf-test-token-0123456789abcdef"
TOKEN_URL = f"https://{TOKEN}@example.invalid/org/repo.git"


def _settings(**overrides):
    return Settings(
        neo4j=Neo4jSettings(uri="bolt://x:7687", user="neo4j", password="pw", database="neo4j"),
        db=DbSettings(engine="mysql", host="h"),
        **overrides,
    )


# --------------------------------------------------------- fake ingestors ---
class _FakeWriter:
    """Stands in for Neo4jWriter: these tests are about the job, not the graph."""

    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def apply_schema(self, *_args, **_kwargs):
        return None


def _fake_git(monkeypatch, behave):
    """Replace GitIngestor with one whose ``ingest`` returns ``behave(spec)``.

    Returns the list of specs it was handed.
    """
    seen = []

    class FakeGit:
        def __init__(self, _writer, _settings):
            pass

        def apply_schema(self):
            return None

        def ingest(self, specs):
            seen.extend(specs)
            return behave(specs[0])

    monkeypatch.setattr("graphforge.core.neo4j_writer.Neo4jWriter", _FakeWriter)
    monkeypatch.setattr("graphforge.git.ingest.GitIngestor", FakeGit)
    return seen


def _fake_db(monkeypatch, behave):
    """Replace DbIngestor with one whose ``ingest_sources`` returns ``behave(source)``."""

    class FakeDb:
        def __init__(self, _writer):
            pass

        def apply_schema(self):
            return None

        def ingest_sources(self, sources):
            return behave(sources[0])

    monkeypatch.setattr("graphforge.core.neo4j_writer.Neo4jWriter", _FakeWriter)
    monkeypatch.setattr("graphforge.db.DbIngestor", FakeDb)


def _finish(job, timeout=10.0):
    """Wait for a job's worker thread to exit, cleanup included."""
    deadline = time.monotonic() + timeout
    while job.state == "running" and time.monotonic() < deadline:
        time.sleep(0.01)
    for thread in threading.enumerate():
        if thread.name == f"gf-ingest-{job.id}":
            thread.join(max(0.0, deadline - time.monotonic()))
    assert job.state != "running", "the ingest never finished"
    return job.payload()


# ------------------------------------------------------------- redaction ----
@pytest.mark.parametrize(
    "url,expected",
    [
        (DB_URL, "postgresql://***@db.internal:5432/shop"),
        ("postgresql://gf@db.internal/shop", "postgresql://***@db.internal/shop"),
        (TOKEN_URL, "https://***@example.invalid/org/repo.git"),
        # The driver reads the password as "my pass"; a pattern that stops at
        # whitespace showed the whole URL.
        ("postgresql://gf:my pass@db/shop", "postgresql://***@db/shop"),
        # urlsplit raises on the unbalanced "["; userinfo_span, which reads the
        # authority by hand, still finds the token.
        ("https://tok@[::1/r.git", "https://***@[::1/r.git"),
        # userinfo_span reads only a string that starts with the URL; quoted or
        # labelled, it is left to the free-text pattern, which must still hide it.
        ('"https://tok@example.invalid/r.git"', '"https://***@example.invalid/r.git"'),
        ("repo: https://tok@example.invalid/r.git", "repo: https://***@example.invalid/r.git"),
        # urlsplit reads the first as host "alice", port "pa", and raises on the
        # second; git sends the whole userinfo in both, so all of it is hidden.
        ("https://alice:pa/ss@example.invalid/r.git", "https://***@example.invalid/r.git"),
        ("https://alice:s3cr]et@example.invalid/r.git", "https://***@example.invalid/r.git"),
        # The git layer reads these as host "gf:hunter2" (or "b:c") with a valid
        # or empty port, and so finds no userinfo, or too little; no host holds
        # two colons, so the userinfo runs on to the "@" after the cut.
        ("postgresql://gf:hunter2:1/xyz@db/shop", "postgresql://***@db/shop"),
        ("postgresql://gf:hunter2:/xyz@db/shop", "postgresql://***@db/shop"),
        ("postgresql://gf:hunter2:5432?xyz@db/shop", "postgresql://***@db/shop"),
        ("postgresql://gf:hunter2:#xyz@db/shop", "postgresql://***@db/shop"),
        ("https://alice:hunter2:1/xyz@example.invalid/r.git", "https://***@example.invalid/r.git"),
        ("https://a@b:c:1/xyz@example.invalid/r.git", "https://***@example.invalid/r.git"),
        ("https://[::1]:1:2/xyz@example.invalid/r.git", "https://***@example.invalid/r.git"),
        # one colon, and a valid IPv6 host and port, are hosts like any other
        ("https://alice:pw@[::1]:8443/r.git", "https://***@[::1]:8443/r.git"),
        ("https://example.invalid:8443/@scope/r.git", "https://example.invalid:8443/@scope/r.git"),
        # an "@" in the path of a valid authority is not userinfo
        ("https://example.invalid/@scope/r.git", "https://example.invalid/@scope/r.git"),
        ("https://example.invalid/org/repo.git", "https://example.invalid/org/repo.git"),
        ("/plain/local/path", "/plain/local/path"),
        ("", ""),
    ],
)
def test_redact_url_hides_the_whole_userinfo(url, expected):
    """A username can be the secret (``https://TOKEN@host``), so none of it is shown."""
    assert redact_url(url) == expected


def test_a_password_with_a_space_never_reaches_the_browser(monkeypatch):
    url = "postgresql://gf:my pass@db.internal:5432/shop"
    _fake_db(monkeypatch, lambda _source: {"databases": 1, "tables": 1, "columns": 1})
    payload = _finish(IngestJobs(_settings()).start("db", url))

    assert payload["source"] == "postgresql://***@db.internal:5432/shop"
    assert "my pass" not in json.dumps(payload), "a password reached the browser"


# U+2100 ("a/c") hides a "/" that urllib finds under NFKC, and it quotes the
# authority it read ("gf:hunter2\u2100") in refusing it. With a colon before the
# cut, the git layer's rule found no userinfo at all, and the source showed it.
@pytest.mark.parametrize(
    "cut",
    ["/", "?", "#", "\u2100/", ":1/", ":/", ":5432?", ":#"],
    ids=["slash", "query", "hash", "nfkc", "port", "empty-port", "port-query", "colon-hash"],
)
def test_a_database_url_urllib_cannot_parse_quotes_no_part_of_the_password(
    monkeypatch, caplog, cut
):
    """urllib gives up on these URLs quoting the password up to the unencoded
    character ("hunter2"), and Job.scrub knows only the whole one to hide."""
    url = f"postgresql://gf:hunter2{cut}xyz@db.internal/shop"
    handed = []
    _fake_db(monkeypatch, lambda source: handed.append(source) or {"databases": 1})
    with caplog.at_level(logging.ERROR, logger="graphforge"):
        payload = _finish(IngestJobs(_settings()).start("db", url))

    assert payload["source"] == "postgresql://***@db.internal/shop"
    assert payload["lines"][0] == "starting db ingest of postgresql://***@db.internal/shop"
    assert "hunter2" not in json.dumps(payload), "part of the password reached the browser"
    assert payload["state"] == "failed"
    assert "percent-encode" in payload["error"]
    assert not handed, "an unparsable URL was handed to the ingestor"
    assert "dashboard ingest failed" in caplog.text
    assert "hunter2" not in caplog.text, "part of the password reached the terminal log"


def test_a_git_password_cut_short_after_a_colon_never_reaches_the_browser(monkeypatch):
    """git's error text quotes the URL it was given, and the ingestor logs it."""
    url = "https://alice:hunter2:1/xyzzy@example.invalid/r.git"

    def rejected(spec):
        logging.getLogger("graphforge.git.ingest").error(
            "failed to ingest r: fatal: Authentication failed for '%s/' (password %s)",
            spec["url"],
            "hunter2:1/xyzzy",
        )
        return {"repos": 0, "files": 0, "commits": 0, "failed": 1}

    _fake_git(monkeypatch, rejected)
    payload = _finish(IngestJobs(_settings()).start("git", url))

    assert payload["source"] == "https://***@example.invalid/r.git"
    assert payload["lines"][0] == "starting git ingest of https://***@example.invalid/r.git"
    blob = json.dumps(payload)
    assert "hunter2" not in blob and "xyzzy" not in blob, "the password reached the browser"
    assert payload["state"] == "failed"


def test_a_short_token_given_alone_is_scrubbed_wherever_it_appears():
    """Over https a username with no password is the credential, however short."""
    url = "https://shorttok@example.invalid/r.git"
    job = Job(id="t6", kind="git", source=redact_url(url), raw=url)
    assert job.scrub("remote: token shorttok bad") == "remote: token *** bad"


@pytest.mark.parametrize(
    "url",
    ["https://alice:pa/ss@example.invalid/r.git", "https://alice:s3cr]et@example.invalid/r.git"],
)
def test_a_password_urlsplit_cannot_read_is_still_scrubbed(url):
    """The git layer sends these passwords whole, so the log scrub must find them whole."""
    job = Job(id="t8", kind="git", source=redact_url(url), raw=url)
    password = url.split("alice:", 1)[1].split("@", 1)[0]
    assert password not in job.scrub(f"authentication failed for alice with {password}")


def test_a_log_line_is_not_parsed_as_a_url():
    """Read as one URL, this sentence has userinfo "example.invalid failed; ask admin"."""
    job = Job(id="t7", kind="git", source=".", raw=".")
    line = "https://example.invalid failed; ask admin@corp.example"
    assert job.scrub(line) == line


def test_a_token_given_as_the_username_never_reaches_the_browser():
    job = Job(id="t1", kind="git", source=redact_url(TOKEN_URL), raw=TOKEN_URL)
    job.lines.append(f"git clone {TOKEN_URL}")
    # git quotes the URL back with its own trailing slash, so it is not the raw string.
    job.lines.append(f"fatal: Authentication failed for '{TOKEN_URL}/'")
    job.lines.append(f"token {TOKEN} was rejected")
    job.error = f"could not clone https://{TOKEN}@example.invalid/"

    payload = job.payload()
    assert TOKEN not in json.dumps(payload), "a token reached the browser"
    assert payload["source"] == "https://***@example.invalid/org/repo.git"


def test_credentials_are_scrubbed_as_written_and_percent_decoded():
    user, password = "someone@corp.example", "p@ss/w0rd"
    url = f"https://{quote(user, safe='')}:{quote(password, safe='')}@example.invalid/r.git"
    job = Job(id="t2", kind="git", source=redact_url(url), raw=url)
    forms = (user, quote(user, safe=""), password, quote(password, safe=""))
    for form in forms:
        job.lines.append(f"auth rejected for {form}!")

    blob = json.dumps(job.payload())
    for form in forms:
        assert form not in blob, f"{form!r} reached the browser"


@pytest.mark.parametrize(
    "url",
    ["ssh://git@example.invalid/org/repo.git", "https://git:pw@example.invalid/org/repo.git"],
)
def test_a_short_login_name_is_hidden_in_urls_but_the_log_stays_readable(url):
    """Blanking every "git" in the log would hide nothing secret and wreck it."""
    job = Job(id="t3", kind="git", source=redact_url(url), raw=url)
    assert job.scrub(f"starting git ingest of {url}") == (
        "starting git ingest of " + redact_url(url)
    )
    assert redact_url(url).endswith("://***@example.invalid/org/repo.git")


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


def test_start_hands_the_ingestor_the_real_source_and_shows_a_redacted_one(monkeypatch):
    seen = _fake_git(monkeypatch, lambda _spec: {"repos": 1, "files": 2, "commits": 3})
    jobs = IngestJobs(_settings())
    payload = _finish(jobs.start("git", TOKEN_URL))

    assert seen[0]["url"] == TOKEN_URL, "the ingestor must get the source unchanged"
    assert payload["source"] == "https://***@example.invalid/org/repo.git"
    assert TOKEN not in json.dumps(payload)
    assert payload["state"] == "done"
    assert (payload["failed"], payload["error"]) == (0, "")


# --------------------------------------------------------------- outcome ----
def test_a_run_where_every_source_failed_is_reported_as_failed(monkeypatch, tmp_path):
    """The real GitIngestor records a bad path in stats["failed"]; it does not raise."""
    monkeypatch.setattr("graphforge.core.neo4j_writer.Neo4jWriter", _FakeWriter)
    jobs = IngestJobs(_settings(git=GitSettings(repo_dir=str(tmp_path / "clones"))))
    payload = _finish(jobs.start("git", str(tmp_path / "no-such-repo")))

    assert payload["state"] == "failed", "a run that loaded nothing was reported as a success"
    assert payload["failed"] == 1
    assert "local path not found" in payload["error"], "the logged reason was lost"
    assert jobs.running() is None


def test_a_rejected_database_login_is_a_failure_with_its_reason(monkeypatch):
    def rejected(source):
        logging.getLogger("graphforge.db.ingest").error(
            "could not list databases on %s: %s",
            source["host"],
            f'FATAL: password authentication failed for user "gf" ({DB_URL})',
        )
        return {"databases": 0, "tables": 0, "columns": 0, "failed": 1}

    _fake_db(monkeypatch, rejected)
    payload = _finish(IngestJobs(_settings()).start("db", DB_URL))

    assert payload["state"] == "failed"
    assert "password authentication failed" in payload["error"]
    assert SECRET not in json.dumps(payload)


def test_a_partial_failure_reports_what_loaded_and_what_did_not(monkeypatch):
    def partly(_source):
        logger = logging.getLogger("graphforge.db.ingest")
        logger.info("auto-discovered 3 database(s) on db.internal")
        logger.error("failed to ingest database %r: %s", "billing", "permission denied")
        return {"databases": 2, "tables": 7, "columns": 40, "failed": 1}

    _fake_db(monkeypatch, partly)
    payload = _finish(IngestJobs(_settings()).start("db", DB_URL))

    assert payload["state"] == "done", "the databases that loaded are in the graph"
    assert payload["failed"] == 1
    assert "2 databases" in payload["summary"] and "1 failed" in payload["summary"]
    assert "permission denied" in payload["error"]


@pytest.mark.parametrize("loaded", [0, 2], ids=["all-failed", "partial"])
def test_the_error_quotes_the_first_reasons_and_counts_the_rest(monkeypatch, loaded):
    """Every failure quoted in full would bury the job's one-line error; the log
    keeps them all, so the error names the first few and says how many remain."""
    names = [f"db{i}" for i in range(1, 6)]

    def failing(_source):
        logger = logging.getLogger("graphforge.db.ingest")
        for name in names:
            logger.error("failed to ingest database %s: permission denied", name)
        return {"databases": loaded, "tables": 0, "columns": 0, "failed": len(names)}

    _fake_db(monkeypatch, failing)
    payload = _finish(IngestJobs(_settings()).start("db", DB_URL))

    assert payload["state"] == ("done" if loaded else "failed")
    assert payload["failed"] == 5
    quoted = "; ".join(f"failed to ingest database {n}: permission denied" for n in names[:3])
    assert payload["error"] == f"{quoted} (+2 more in the log)"
    for name in names[3:]:
        assert f"error: failed to ingest database {name}: permission denied" in payload["lines"]


@pytest.mark.parametrize("count,suffix", [(3, ""), (4, " (+1 more in the log)")])
def test_the_count_of_unquoted_reasons_appears_only_when_some_are_left(count, suffix):
    job = Job(id="t8", kind="db", source=".", raw=".")
    for i in range(count):
        job.log(f"error: r{i}", reason=f"r{i}")
    assert job.failure_reason(count) == "r0; r1; r2" + suffix


def test_another_threads_error_is_not_taken_as_the_ingests_reason():
    """The capture handler sits on the shared logger; a dashboard request failing
    meanwhile must not show up as the reason the ingest failed."""
    job = Job(id="t4", kind="git", source=".", raw=".")
    handler = _Capture(job, thread=threading.get_ident())
    record = logging.LogRecord(
        "graphforge.ui.server", logging.ERROR, __file__, 0, "boom", None, None
    )
    record.thread = threading.get_ident() + 1
    handler.emit(record)
    assert not job.lines
    assert job.failure_reason(1) == "1 source(s) failed; see the log for details"


# ----------------------------------------------------------- concurrency ----
def test_a_log_line_arriving_mid_render_does_not_break_the_payload(monkeypatch):
    """The HTTP thread renders a job while its worker is still logging.

    Deterministic stand-in for the race: the worker's log handler fires, on its
    own thread, while payload() is part-way through the lines. Iterating the
    live deque there raised "deque mutated during iteration" -- a 500.
    """
    job = Job(id="t5", kind="git", source=".", raw=".")
    for i in range(3):
        job.lines.append(f"line {i}")
    handler = _Capture(job)
    real_scrub = Job.scrub
    raced = []

    def scrub_while_the_worker_logs(self, text):
        if text == "line 0" and not raced:
            raced.append(True)
            record = logging.LogRecord(
                "graphforge.git.ingest", logging.INFO, __file__, 0, "late line", None, None
            )
            worker = threading.Thread(target=handler.emit, args=(record,))
            worker.start()
            worker.join(5)
            assert not worker.is_alive(), "the log handler blocked behind a render"
        return real_scrub(self, text)

    monkeypatch.setattr(Job, "scrub", scrub_while_the_worker_logs)
    payload = job.payload()

    assert raced, "the race was never staged"
    assert payload["lines"] == ["line 0", "line 1", "line 2"]
    assert list(job.lines)[-1] == "info: late line"


def test_two_simultaneous_starts_cannot_both_win(monkeypatch):
    """The server is threaded: a check made outside the lock let both requests in."""
    release = threading.Event()

    def slow(_spec):
        release.wait(10)
        return {"repos": 1, "files": 0, "commits": 0}

    _fake_git(monkeypatch, slow)
    # Hold both requests at the same point inside start(), so they really race.
    barrier = threading.Barrier(2, timeout=2)
    calls = itertools.count()
    real_redact = ingest_mod.redact_url

    def redact_in_step(value):
        if next(calls) < 2:
            with contextlib.suppress(threading.BrokenBarrierError):
                barrier.wait()
        return real_redact(value)

    monkeypatch.setattr(ingest_mod, "redact_url", redact_in_step)
    jobs = IngestJobs(_settings())
    started, refused = [], []

    def request(n):
        try:
            started.append(jobs.start("git", f"repo-{n}"))
        except ValueError as exc:
            refused.append(str(exc))

    requests = [threading.Thread(target=request, args=(n,)) for n in range(2)]
    for thread in requests:
        thread.start()
    for thread in requests:
        thread.join(10)
    release.set()
    for job in started:
        _finish(job)

    assert len(started) == 1, f"{len(started)} ingests started at once"
    assert len(refused) == 1 and "already running" in refused[0]


def test_back_to_back_jobs_leave_the_logger_level_as_they_found_it(monkeypatch):
    """Each job raises the shared "graphforge" logger to INFO and puts it back.

    A job used to report "done" before restoring it, so a second job started in
    that gap saved INFO as the level to restore, and left it stuck there.
    """
    shared = logging.getLogger("graphforge")
    before = shared.level
    shared.setLevel(logging.WARNING)
    release, entered = threading.Event(), threading.Event()

    def behave(spec):
        if spec.get("path") == "second":
            entered.set()
            release.wait(10)
        return {"repos": 1, "files": 0, "commits": 0}

    _fake_git(monkeypatch, behave)
    jobs = IngestJobs(_settings())
    tried, second = [], []

    def start_another():
        # Runs on the first job's worker once its ingest has succeeded, before
        # it has put the logger back: the tightest gap a second request can hit.
        # Only once: every successful job calls this, the second one included.
        if tried:
            return
        tried.append(True)
        try:
            second.append(jobs.start("git", "second"))
        except ValueError:
            return  # refused because the first still counts as running: fine
        entered.wait(10)  # the second job has now saved the level it will restore

    monkeypatch.setattr("graphforge.mcp.server.clear_schema_cache", start_another)
    try:
        _finish(jobs.start("git", "first"))
        release.set()
        for job in second:
            _finish(job)
        assert shared.level == logging.WARNING, logging.getLevelName(shared.level)
    finally:
        release.set()
        shared.setLevel(before)


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
