"""Background ingest jobs started from the dashboard.

The dashboard is otherwise strictly read-only, and the Cypher console stays that
way. This is the one deliberate exception: without it, "create a knowledge graph"
means leaving the browser for a sequence of CLI commands, which is the single
biggest thing standing between a new user and a populated graph.

No ingest logic lives here. A job is a thread that calls the same
:class:`~graphforge.git.ingest.GitIngestor` / :class:`~graphforge.db.ingest.DbIngestor`
the CLI calls, with a log handler attached so the browser can watch it happen.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ..core.config import Settings

log = logging.getLogger("graphforge.ui.ingest")

#: Jobs kept in memory. The dashboard is a single local process; this is a
#: progress view, not an audit trail.
MAX_JOBS = 20
#: Log lines retained per job — enough to see what happened, bounded so a large
#: ingest cannot grow the process without limit.
MAX_LINES = 400

KINDS = ("git", "db")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def redact_url(value: str) -> str:
    """Blank the password in a connection URL before it is displayed or logged.

    A database source is typically ``postgresql://user:secret@host/db``. The
    dashboard's standing promise is that passwords are never displayed, and a
    job log rendered into the page is a display like any other.
    """
    text = str(value or "")
    try:
        parts = urlsplit(text)
    except ValueError:
        return text
    if not parts.password:
        return text
    userinfo = parts.username or ""
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit(parts._replace(netloc=f"{userinfo}:***@{host}"))


@dataclass
class Job:
    """One ingest, as the browser sees it."""

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
    lines: deque = field(default_factory=lambda: deque(maxlen=MAX_LINES))

    def scrub(self, text: str) -> str:
        """Remove this job's credentials from anything on its way to the browser.

        The ingestors log freely and are not obliged to know that their output
        will be rendered into a page; this is the choke point that makes sure it
        is safe when it is.
        """
        out = str(text or "")
        if self.raw and self.raw != self.source:
            out = out.replace(self.raw, self.source)
        password = urlsplit(self.raw).password if self.raw else None
        if password:
            out = out.replace(password, "***")
        return out

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "source": self.source,
            "name": self.name,
            "state": self.state,
            "started": self.started,
            "finished": self.finished,
            "error": self.scrub(self.error),
            "summary": self.scrub(self.summary),
            "lines": [self.scrub(line) for line in self.lines],
        }


class _Capture(logging.Handler):
    """Feed graphforge's own log records into a job's line buffer."""

    def __init__(self, job: Job):
        super().__init__(level=logging.INFO)
        self.job = job

    def emit(self, record: logging.LogRecord) -> None:
        # A malformed log record must not take the ingest down with it.
        with contextlib.suppress(Exception):
            self.job.lines.append(
                f"{record.levelname.lower()}: {self.job.scrub(record.getMessage())}"
            )


class IngestJobs:
    """Starts ingests and reports on them. One instance per running dashboard."""

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
        if self.running():
            raise ValueError("an ingest is already running - wait for it to finish")

        job = Job(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            source=redact_url(source),
            raw=source,
            name=name.strip(),
        )
        with self._lock:
            if len(self._order) == self._order.maxlen and self._order:
                self._jobs.pop(self._order[0], None)
            self._jobs[job.id] = job
            self._order.append(job.id)
        threading.Thread(
            target=self._run, args=(job,), daemon=True, name=f"gf-ingest-{job.id}"
        ).start()
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def recent(self) -> list[dict[str, Any]]:
        with self._lock:
            ids = list(self._order)
        return [self._jobs[i].payload() for i in reversed(ids) if i in self._jobs]

    def running(self) -> Job | None:
        for job in self._jobs.values():
            if job.state == "running":
                return job
        return None

    # -- worker ------------------------------------------------------------
    def _run(self, job: Job) -> None:
        handler = _Capture(job)
        root = logging.getLogger("graphforge")
        previous = root.level
        root.addHandler(handler)
        if root.level > logging.INFO or root.level == logging.NOTSET:
            root.setLevel(logging.INFO)
        job.lines.append(f"starting {job.kind} ingest of {job.source}")
        try:
            job.summary = self._ingest(job)
            job.state = "done"
            job.lines.append(job.summary)
            # The schema snapshot is TTL-cached, so without this the counts the
            # dashboard shows would not move for up to a minute after an ingest
            # the user just watched finish.
            from ..mcp.server import clear_schema_cache

            clear_schema_cache()
        except Exception as exc:  # noqa: BLE001 — a failed ingest is a result, not a crash
            from ..core.errors import neo4j_advice

            job.state = "failed"
            job.error = neo4j_advice(exc, self.settings) or str(exc)
            job.lines.append(f"failed: {job.error}")
            log.exception("dashboard ingest failed")
        finally:
            job.finished = _now()
            root.removeHandler(handler)
            root.setLevel(previous)

    def _ingest(self, job: Job) -> str:
        from ..core.neo4j_writer import Neo4jWriter

        with Neo4jWriter(self.settings.neo4j) as writer:
            if job.kind == "git":
                from ..git.ingest import GitIngestor

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
                    f"{stats['commits']} commits"
                )

            from ..db import DbIngestor, parse_db_url

            db_ingestor = DbIngestor(writer)
            db_ingestor.apply_schema()
            stats = db_ingestor.ingest_sources([parse_db_url(job.raw)])
            return (
                f"{stats['databases']} databases, {stats['tables']} tables, "
                f"{stats['columns']} columns"
            )
