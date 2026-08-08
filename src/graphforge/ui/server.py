"""A dependency-free status dashboard served from Python's stdlib http.server.

`graphforge ui` starts this. It shows the resolved configuration (passwords
masked — never displayed), Neo4j connection health, node counts by label, and
per-repository / per-database load status pulled live from the graph.
"""
from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict

from ..core.config import Settings

log = logging.getLogger("graphforge.ui")


def _mask(secret: str) -> str:
    return "•••••• (set)" if secret else "— (not set)"


def build_status(settings: Settings) -> Dict[str, Any]:
    """Assemble the dashboard payload. Never raises — connection errors are reported."""
    neo = settings.neo4j
    db = settings.db
    status: Dict[str, Any] = {
        "config": {
            "neo4j": {"uri": neo.uri, "user": neo.user, "database": neo.database,
                      "password": _mask(neo.password)},
            "database": {"engine": db.engine, "host": db.host, "port": db.port,
                         "user": db.user, "password": _mask(db.password),
                         "databases": db.names or "(auto-discover)"},
            "options": {"batchSize": neo.batch_size, "includeLines": neo.include_lines},
        },
        "neo4j": {"connected": False},
        "labels": {}, "relationshipTypes": [], "repositories": [], "databases": [],
    }
    try:
        from ..mcp.server import GraphQuery

        gq = GraphQuery.connect(neo)
        try:
            schema = gq.get_schema()
            status["neo4j"]["connected"] = True
            status["labels"] = schema.get("nodeCountsByLabel", {})
            status["relationshipTypes"] = schema.get("relationshipTypes", [])
            status["totals"] = {
                "nodes": sum(schema.get("nodeCountsByLabel", {}).values()),
                "labels": len(schema.get("labels", [])),
                "relationshipTypes": len(schema.get("relationshipTypes", [])),
            }
            status["repositories"] = gq._read(
                "MATCH (r:Repository) RETURN r.name AS name, r.status AS status, "
                "r.files AS files, r.commits AS commits, r.lastIngestedAt AS lastIngestedAt "
                "ORDER BY r.name")
            status["databases"] = gq._read(
                "MATCH (d:Database) OPTIONAL MATCH (d)-[:HAS_SCHEMA]->(:Schema)-[:HAS_TABLE]->(t:Table) "
                "RETURN d.name AS name, d.engine AS engine, count(t) AS tables ORDER BY d.name")
        finally:
            gq.close()
    except Exception as exc:  # graph unreachable — still show config
        status["neo4j"]["error"] = str(exc)
    return status


def _handler(settings: Settings):
    html = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/api/status"):
                body = json.dumps(build_status(settings), default=str).encode("utf-8")
                self._send(200, "application/json", body)
            elif self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", html.encode("utf-8"))
            else:
                self._send(404, "text/plain", b"not found")

        def log_message(self, *_args):  # quiet
            return

    return Handler


def serve(settings: Settings, host: str = "127.0.0.1", port: int = 8000) -> None:
    server = ThreadingHTTPServer((host, port), _handler(settings))
    url = f"http://{host}:{port}"
    print(f"[graphforge] dashboard on {url}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[graphforge] dashboard stopped")
    finally:
        server.server_close()
