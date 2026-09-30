"""Background ingest jobs started from the dashboard.

The dashboard is otherwise strictly read-only, and the Cypher console stays that
way. This is the one deliberate exception: without it, "create a knowledge graph"
means leaving the browser for a sequence of CLI commands, which is the single
biggest thing standing between a new user and a populated graph.

No ingest logic lives here. A job is a thread that calls the same
:class:`~graphforge.git.ingest.GitIngestor` / :class:`~graphforge.db.ingest.DbIngestor`
the CLI calls, with a log handler attached so the browser can watch it happen.

Threading: jobs run on a worker thread while the (threaded) HTTP server reads
them. :attr:`IngestJobs._lock` guards the job registry and each :attr:`Job._lock`
guards that job's fields; when both are needed the registry lock is taken first.
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote

from ..core.config import Settings
from ..git.clone import _PREFIX, userinfo_span

log = logging.getLogger("graphforge.ui.ingest")

#: Jobs kept in memory. The dashboard is a single local process; this is a
#: progress view, not an audit trail.
MAX_JOBS = 20
#: Log lines retained per job — enough to see what happened, bounded so a large
#: ingest cannot grow the process without limit.
MAX_LINES = 400
#: Failure reasons quoted in a job's ``error``; the rest are in its log.
MAX_REASONS = 3

KINDS = ("git", "db")

#: The userinfo of any URL in free text: ``scheme://<this part>@host``. Greedy up
#: to the last ``@`` before the path, so an unencoded ``@`` in a password cannot
#: leave half of it behind. It stops at whitespace, or it would run across a log
#: line into the next ``@``; that is why a single URL is parsed instead (see
#: :func:`redact_url`), since there a space can be part of the password.
_USERINFO = re.compile(r"://[^/?#\s]*@")
#: What ends a URL's authority, as git reads it.
_AUTHORITY_END = re.compile(r"[/?#]")
#: A URL username at least this long is treated as a token and scrubbed wherever
#: it appears, even beside a password. Shorter ones are usually login names
#: (``git``, ``postgres``) and are only hidden inside URLs: blanking every "git"
#: would make the log unreadable, and no forge issues tokens this short.
_TOKEN_MIN = 16
#: Schemes on which a username given alone is the credential itself
#: (``https://TOKEN@host``), so it is scrubbed wherever it appears, however short.
_TOKEN_SCHEMES = ("http", "https")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _redact_text(text: str) -> str:
    """Hide the userinfo of every URL in a piece of free text, such as a log line."""
    return _USERINFO.sub("://***@", text)


def _authority_end(text: str, start: int) -> int:
    match = _AUTHORITY_END.search(text, start)
    return match.start() if match else len(text)


def _userinfo_span(url: str) -> tuple[int, int] | None:
    """Where the userinfo of ``url`` is: :func:`userinfo_span`, or further on.

    Further when what that leaves before the path is no host. The git layer
    reads ``gf:hunter2:1/xyz@db/shop``, a password with an unencoded ``/``, as
    host ``gf:hunter2`` and port ``1``, and finds no userinfo, which would put
    the password on the page whole. But no host holds a second ``:`` (outside
    ``[...]``), so the userinfo runs on to the ``@`` after the cut, as it does
    there for a port that is not a number (``gf:hunter2/xyz@db/shop``).
    """
    span = userinfo_span(url)
    head = _PREFIX.match(url)
    if not head:
        return span
    start = span[1] + 1 if span else head.end()
    cut = _authority_end(url, start)
    host = url[start:cut]
    tail = host.rpartition("]")[2] if host.startswith("[") else host
    later = url.find("@", cut)
    if tail.count(":") < 2 or later < 0:
        return span
    return head.end(), url.rfind("@", 0, _authority_end(url, later))


def redact_url(value: str) -> str:
    """Hide the credentials in a connection URL before it is displayed or logged.

    A database source is typically ``postgresql://user:secret@host/db``, and a
    git source may be ``https://TOKEN@host/repo.git``, where the token *is* the
    username. So the whole userinfo goes, not just the password:
    ``https://***@host/repo.git``. The dashboard's standing promise is that
    credentials are never displayed, and a job log rendered into the page is a
    display like any other.

    The userinfo is found by the rule the git layer strips credentials with
    (:func:`~graphforge.git.clone.userinfo_span`), so what is hidden here is
    what git would have sent: a password holding an unencoded space
    (``postgresql://gf:my pass@db/shop``), a ``[`` or ``]``, or a ``/``
    (``https://alice:pa/ss@host/r.git``), which ``urlsplit`` either raises on
    or cuts short. Where that rule leaves a host that cannot be one, more is
    hidden (see :func:`_userinfo_span`).
    """
    text = str(value or "")
    span = _userinfo_span(text)
    if span:
        text = text[: span[0]] + "***" + text[span[1] :]
    return _redact_text(text)


def _secret_forms(raw: str) -> tuple[list[str], list[str]]:
    """``(passwords, tokens)`` in ``raw``, each as written and percent-decoded.

    A token is a username to scrub wherever it appears, not just inside URLs:
    one long enough to be a token (:data:`_TOKEN_MIN`), or one an http(s) URL
    gives with no password, which is how forges take a token of any length.

    Drivers log whichever form they hold: the URL keeps ``p%40ss`` while a
    connection error may quote the decoded ``p@ss``.
    """
    span = _userinfo_span(raw)
    if not span:
        return [], []
    username, colon, password = raw[span[0] : span[1]].partition(":")

    def forms(value: str | None) -> list[str]:
        found = {f for f in (value, unquote(value or "")) if f}
        return sorted(found, key=len, reverse=True)  # longest first: no partial hits

    scheme = raw[: span[0]].strip().partition(":")[0].lower()
    alone = not (colon and password) and scheme in _TOKEN_SCHEMES
    tokens = [u for u in forms(username) if alone or len(u) >= _TOKEN_MIN]
    return forms(password), tokens


class IngestFailed(RuntimeError):
    """Every source in a run failed, though the ingestor itself returned normally."""

    def __init__(self, reason: str, failed: int):
        super().__init__(reason)
        self.failed = failed


@dataclass
class Job:
    """One ingest, as the browser sees it.

    ``state`` is ``running``, then ``done`` or ``failed``. A ``done`` job with a
    non-zero ``failed`` loaded some sources but not all; ``error`` then holds the
    reasons for the ones that failed.
    """

    id: str
    kind: str
    source: str  # redacted — this is what the browser sees
    name: str = ""
    raw: str = ""  # the real source, never serialised
    state: str = "running"  # running | done | failed
    started: str = field(default_factory=_now)
    finished: str = ""
    error: str = ""
    summary: str = ""
    failed: int = 0  # sources the ingestor could not load
    lines: deque = field(default_factory=lambda: deque(maxlen=MAX_LINES))
    #: Error-level messages from the worker: why sources failed, if any did.
    reasons: list[str] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )

    def scrub(self, text: str) -> str:
        """Remove this job's credentials from anything on its way to the browser.

        The ingestors log freely and are not obliged to know that their output
        will be rendered into a page; this is the choke point that makes sure it
        is safe when it is.
        """
        out = str(text or "")
        if self.raw and self.raw != self.source:
            out = out.replace(self.raw, self.source)
        passwords, tokens = _secret_forms(self.raw) if self.raw else ([], [])
        for secret in passwords:
            out = out.replace(secret, "***")
        if tokens:
            words = "|".join(re.escape(t) for t in tokens)
            out = re.sub(rf"(?<!\w)(?:{words})(?!\w)", "***", out)
        # Last, so the same URL quoted with a different path or a trailing
        # slash (git's error text does both) is covered too. The pattern, not
        # redact_url: a log line is not a URL, and parsing one as if it were
        # would read "https://host failed; ask admin@corp" as userinfo.
        return _redact_text(out)

    def log(self, line: str, *, reason: str = "") -> None:
        """Append a line to the job's log and, if given, a failure reason.

        Called on the worker thread while an HTTP thread may be rendering
        :meth:`payload`, hence the lock.
        """
        with self._lock:
            self.lines.append(line)
            if reason and len(self.reasons) < MAX_LINES:
                self.reasons.append(reason)

    def failure_reason(self, failed: int) -> str:
        """Why ``failed`` sources failed, from what the worker logged, as one line."""
        with self._lock:
            reasons = list(self.reasons)
        if not reasons:
            return f"{failed} source(s) failed; see the log for details"
        text = "; ".join(reasons[:MAX_REASONS])
        more = len(reasons) - MAX_REASONS
        return f"{text} (+{more} more in the log)" if more > 0 else text

    def finish(self, state: str, *, summary: str = "", error: str = "", failed: int = 0) -> None:
        """Publish the outcome in one step, so no reader sees half of it."""
        with self._lock:
            self.summary = summary
            self.error = error
            self.failed = failed
            self.finished = _now()
            self.state = state

    def payload(self) -> dict[str, Any]:
        # Snapshot under the lock and scrub outside it: iterating ``lines``
        # while the worker appends to it raises "deque mutated during iteration".
        with self._lock:
            lines = list(self.lines)
            state, finished, failed = self.state, self.finished, self.failed
            error, summary = self.error, self.summary
        return {
            "id": self.id,
            "kind": self.kind,
            "source": self.source,
            "name": self.name,
            "state": state,
            "started": self.started,
            "finished": finished,
            "failed": failed,
            "error": self.scrub(error),
            "summary": self.scrub(summary),
            "lines": [self.scrub(line) for line in lines],
        }


class _Capture(logging.Handler):
    """Feed graphforge's own log records into a job's line buffer.

    With ``thread`` set, only records logged on that thread are taken, so a
    dashboard request failing meanwhile is not mistaken for part of the ingest.
    """

    def __init__(self, job: Job, thread: int | None = None):
        super().__init__(level=logging.INFO)
        self.job = job
        self.thread = thread

    def emit(self, record: logging.LogRecord) -> None:
        if self.thread is not None and record.thread != self.thread:
            return
        # A malformed log record must not take the ingest down with it.
        with contextlib.suppress(Exception):
            message = self.job.scrub(record.getMessage())
            self.job.log(
                f"{record.levelname.lower()}: {message}",
                reason=message if record.levelno >= logging.ERROR else "",
            )


class IngestJobs:
    """Starts ingests and reports on them. One instance per running dashboard.

    One ingest runs at a time. A job stays ``running`` until its worker has
    detached from the ``graphforge`` logger, so the next one cannot start while
    the previous one is still restoring the logger level it changed.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._jobs: dict[str, Job] = {}
        self._order: deque = deque(maxlen=MAX_JOBS)
        self._lock = threading.Lock()

    # -- api ---------------------------------------------------------------
    def start(self, kind: str, source: str, name: str = "") -> Job:
        kind = (kind or "").strip().lower()
        source = (source or "").strip()
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        if not source:
            raise ValueError("source is required (a repository path/URL, or a database URL)")

        job = Job(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            source=redact_url(source),
            raw=source,
            name=name.strip(),
        )
        with self._lock:
            # Check and register in one step: the server is threaded, and two
            # requests that both passed a check made outside the lock would both
            # start an ingest.
            if self._running() is not None:
                raise ValueError("an ingest is already running - wait for it to finish")
            if len(self._order) == self._order.maxlen and self._order:
                self._jobs.pop(self._order[0], None)
            self._jobs[job.id] = job
            self._order.append(job.id)
        threading.Thread(
            target=self._run, args=(job,), daemon=True, name=f"gf-ingest-{job.id}"
        ).start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = [self._jobs[i] for i in reversed(self._order) if i in self._jobs]
        return [job.payload() for job in jobs]

    def running(self) -> Job | None:
        with self._lock:
            return self._running()

    def _running(self) -> Job | None:
        """The running job, if any. The caller holds :attr:`_lock`."""
        for job in self._jobs.values():
            if job.state == "running":
                return job
        return None

    # -- worker ------------------------------------------------------------
    def _run(self, job: Job) -> None:
        handler = _Capture(job, thread=threading.get_ident())
        root = logging.getLogger("graphforge")
        previous = root.level
        root.addHandler(handler)
        if root.level > logging.INFO or root.level == logging.NOTSET:
            root.setLevel(logging.INFO)
        job.log(f"starting {job.kind} ingest of {job.source}")
        # Whatever escapes below (even a BaseException), the job must not stay
        # "running": that would refuse every later ingest until a restart.
        outcome: dict[str, Any] = {"state": "failed", "error": "the ingest stopped unexpectedly"}
        try:
            summary, loaded, failed = self._ingest(job)
            # The ingestors record a failed source rather than raise, so one bad
            # repo cannot abort a batch. Read it back here, or a failed clone or
            # a rejected login would be reported as a success.
            reason = job.failure_reason(failed) if failed else ""
            if failed and not loaded:
                raise IngestFailed(reason, failed)
            if failed:
                summary = f"{summary} ({failed} failed)"
            outcome = {"state": "done", "summary": summary, "failed": failed, "error": reason}
            job.log(summary)
            # The schema snapshot is TTL-cached, so without this the counts the
            # dashboard shows would not move for up to a minute after an ingest
            # the user just watched finish.
            from ..mcp.server import clear_schema_cache

            clear_schema_cache()
        except Exception as exc:  # noqa: BLE001 — a failed ingest is a result, not a crash
            from ..core.errors import neo4j_advice

            error = neo4j_advice(exc, self.settings) or str(exc)
            outcome = {"state": "failed", "error": error}
            job.log(f"failed: {error}")
            if isinstance(exc, IngestFailed):  # the ingestor has already logged why
                outcome["failed"] = exc.failed
            else:
                log.exception("dashboard ingest failed")
        finally:
            root.removeHandler(handler)
            root.setLevel(previous)
            # Published last, under the registry lock: until this line the job
            # counts as running, so start() cannot overlap this cleanup.
            with self._lock:
                job.finish(**outcome)

    def _ingest(self, job: Job) -> tuple[str, int, int]:
        """Run the ingest: ``(summary, sources loaded, sources failed)``."""
        from ..core.neo4j_writer import Neo4jWriter

        with Neo4jWriter(self.settings.neo4j) as writer:
            if job.kind == "git":
                from ..git.ingest import GitIngestor

                if _userinfo_span(job.raw) != userinfo_span(job.raw):
                    # The git layer would take part of the password for the host
                    # and hand git the URL, password and all, and derive the
                    # clone's name from what follows the cut (see _userinfo_span).
                    # Refusing it costs nothing: no such URL works as written.
                    raise ValueError(
                        "could not read the repository URL; "
                        "percent-encode any / # ? @ in the password"
                    )
                ingestor = GitIngestor(writer, self.settings.git)
                ingestor.apply_schema()
                spec = (
                    {"url": job.raw, "name": job.name or None}
                    if "://" in job.raw or job.raw.endswith(".git")
                    else {"path": job.raw, "name": job.name or None}
                )
                stats = ingestor.ingest([spec])
                return (
                    f"{stats['repos']} repositories, {stats['files']} files, "
                    f"{stats['commits']} commits",
                    stats["repos"],
                    int(stats.get("failed") or 0),
                )

            from ..db import DbIngestor, parse_db_url

            try:
                source = parse_db_url(job.raw)
            except ValueError:
                # urllib quotes the part of the URL it could not read, and with
                # an unencoded / ? or # in the password that part is the
                # password up to that character: a fragment Job.scrub cannot
                # find, since it knows the password only whole. So the message
                # is replaced, not scrubbed, and ``from None`` keeps it out of
                # the traceback in the terminal too.
                raise ValueError(
                    "could not parse the database URL; check the scheme, host and port, "
                    "and percent-encode any / # ? @ in the password"
                ) from None
            db_ingestor = DbIngestor(writer)
            db_ingestor.apply_schema()
            stats = db_ingestor.ingest_sources([source])
            return (
                f"{stats['databases']} databases, {stats['tables']} tables, "
                f"{stats['columns']} columns",
                stats["databases"],
                int(stats.get("failed") or 0),
            )
