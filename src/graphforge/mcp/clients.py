"""Where MCP clients keep their server list, and how graphforge registers itself.

One table, two consumers: ``graphforge doctor`` reads it to say whether anything
is wired up, and ``graphforge mcp install`` writes through it. Every client in
use today reads the same ``mcpServers`` object, so the only thing that really
differs is the path.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

#: The key every known client stores its servers under.
SERVERS_KEY = "mcpServers"
#: The name graphforge registers itself as.
SERVER_NAME = "graphforge"


@dataclass(frozen=True)
class McpClient:
    """A client we know how to write a server entry for."""

    key: str
    label: str
    path: Path
    #: True when the file is per-project rather than per-user, so it belongs
    #: next to the repository and is usually gitignored.
    project_scoped: bool = False

    def read(self) -> dict:
        """The client's current config, or ``{}`` when it has none yet."""
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            return {}

    def servers(self) -> dict:
        config = self.read()
        servers = config.get(SERVERS_KEY)
        return servers if isinstance(servers, dict) else {}

    def has_graphforge(self) -> bool:
        return SERVER_NAME in self.servers()


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _claude_desktop_path() -> Path:
    """Claude Desktop's config, which is the one location that is OS-specific."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or (_home() / "AppData" / "Roaming"))
        return base / "Claude" / "claude_desktop_config.json"
    if sys.platform == "darwin":
        return _home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return _home() / ".config" / "Claude" / "claude_desktop_config.json"


def known_clients(project_dir: Path | None = None) -> list[McpClient]:
    """Every client we can install into, project-scoped ones first."""
    root = Path(project_dir or Path.cwd())
    return [
        McpClient("claude-code", "Claude Code (project)", root / ".mcp.json", True),
        McpClient("cursor", "Cursor (project)", root / ".cursor" / "mcp.json", True),
        McpClient("claude-desktop", "Claude Desktop", _claude_desktop_path()),
    ]


def client_by_key(key: str, project_dir: Path | None = None) -> McpClient:
    for client in known_clients(project_dir):
        if client.key == key:
            return client
    valid = ", ".join(c.key for c in known_clients(project_dir))
    raise ValueError(f"unknown MCP client {key!r}; use one of: {valid}, all")


def server_entry(env_file: Path | None = None, command: str | None = None) -> dict:
    """The server entry to install.

    Two deliberate choices. The command is *this* interpreter's console script,
    not whatever ``graphforge`` happens to resolve to on PATH — a stale entry
    pointing into an unrelated project's virtualenv is exactly the failure this
    replaces. And credentials are passed by pointing at a ``.env`` file rather
    than copied into the client config, so the password lives in one place.
    """
    entry: dict = {"command": command or default_command(), "args": ["mcp"]}
    if env_file is not None:
        entry["args"] += ["--env", str(Path(env_file).resolve())]
    return entry


def default_command() -> str:
    """The graphforge console script that belongs to the running interpreter."""
    scripts = Path(sys.executable).parent
    for name in ("graphforge.exe", "graphforge"):
        candidate = scripts / name
        if candidate.exists():
            return str(candidate)
        # POSIX virtualenvs put scripts in bin/ alongside python; Windows uses
        # Scripts/ next to python.exe. Try the sibling layout too.
        candidate = scripts / "Scripts" / name
        if candidate.exists():
            return str(candidate)
    return "graphforge"
