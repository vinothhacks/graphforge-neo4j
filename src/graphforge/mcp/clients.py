"""Where MCP clients keep their server list, and how graphforge registers itself.

One table, two consumers: ``graphforge doctor`` reads it to say whether anything
is wired up, and ``graphforge mcp install`` writes through it. Every client in
use today reads the same ``mcpServers`` object, so the only thing that really
differs is the path.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: The key every known client stores its servers under.
SERVERS_KEY = "mcpServers"
#: The name graphforge registers itself as.
SERVER_NAME = "graphforge"


class ClientConfigError(ValueError):
    """A client config exists but could not be read or understood.

    Such a file must never be rewritten: whatever graphforge failed to parse is
    the user's other servers and settings, and writing a merge over it would
    erase them.
    """


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
        """The client's current config, or ``{}`` when it has none yet.

        Only a missing (or empty) file means "no config". Anything else that
        stops the file being understood raises :class:`ClientConfigError`
        rather than reading as ``{}``: this used to swallow every error, and
        ``install`` then wrote its merge over the file, erasing every other
        server in it. ``utf-8-sig`` is not optional either -- Notepad and
        PowerShell 5.1's ``Out-File`` both write a BOM, which plain ``utf-8``
        rejects.
        """
        try:
            text = self.path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise ClientConfigError(f"cannot read {self.path}: {exc}") from exc
        except ValueError as exc:  # UnicodeDecodeError: not text at all
            raise ClientConfigError(f"cannot parse {self.path}: {exc}") from exc
        if not text.strip():
            return {}
        try:
            config = json.loads(text)
        except ValueError as exc:
            raise ClientConfigError(f"cannot parse {self.path}: {exc}") from exc
        if not isinstance(config, dict):
            raise ClientConfigError(
                f"cannot parse {self.path}: expected a JSON object, found {type(config).__name__}"
            )
        servers = config.get(SERVERS_KEY)
        if servers is not None and not isinstance(servers, dict):
            raise ClientConfigError(
                f"cannot parse {self.path}: {SERVERS_KEY!r} should be an object, "
                f"found {type(servers).__name__}"
            )
        return config

    def write(self, config: dict) -> None:
        """Replace the config in one step, so a failure mid-write cannot truncate it.

        The JSON goes to a temporary file beside the real one, which is then
        renamed over it. ``os.replace`` is atomic within a filesystem, so the
        client sees either the old file or the new one, never half of either.
        A symlinked config is written through to its target rather than
        replaced by a regular file.
        """
        target = self.path.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(config, indent=2) + "\n"
        fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            # mkstemp creates the file 0600. Keep the permissions the user had, and
            # give a new config the mode a plain open() would: 0666 less the umask.
            # Skipped on Windows, where the only bit is read-only, and copying it
            # would stop the temporary file being cleaned up if the rename fails.
            if os.name != "nt":
                if target.exists():
                    mode = stat.S_IMODE(target.stat().st_mode)
                else:
                    umask = os.umask(0)
                    os.umask(umask)
                    mode = 0o666 & ~umask
                os.chmod(tmp, mode)
            try:
                os.replace(tmp, target)
            except PermissionError:
                # Windows refuses to replace a file another process holds open
                # without FILE_SHARE_DELETE; an in-place write still gets through,
                # as it always did before. Only that case gives up atomicity.
                if os.name != "nt" or not target.exists():
                    raise
                target.write_text(text, encoding="utf-8")
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def servers(self) -> dict:
        """The client's registered servers. Raises :class:`ClientConfigError`."""
        return self.read().get(SERVERS_KEY) or {}

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
