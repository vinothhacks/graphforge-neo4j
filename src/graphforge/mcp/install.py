"""Write graphforge's server entry into an MCP client's config.

The alternative this replaces is a README step that asks the user to find a JSON
file, merge an object into it by hand, and paste their Neo4j password in
plaintext. Two rules make that unnecessary:

* the command is resolved from the *running* interpreter, so the entry cannot end
  up pointing at some other project's virtualenv, and
* the password is never written — the entry points at a ``.env`` instead.
"""

from __future__ import annotations

import json
from pathlib import Path

from .clients import SERVER_NAME, SERVERS_KEY, McpClient, client_by_key, known_clients, server_entry


def _targets(clients: list[str] | None, project_dir: Path | None) -> list[McpClient]:
    if not clients or "all" in clients:
        return known_clients(project_dir)
    return [client_by_key(key, project_dir) for key in clients]


def plan(client: McpClient, entry: dict) -> tuple[dict, str]:
    """The config this client would end up with, and what changed."""
    config = client.read()
    servers = dict(config.get(SERVERS_KEY) or {})
    existing = servers.get(SERVER_NAME)
    action = "unchanged" if existing == entry else ("updated" if existing else "added")
    servers[SERVER_NAME] = entry
    merged = {**config, SERVERS_KEY: servers}
    return merged, action


def install(
    clients: list[str] | None = None,
    env_file: Path | None = None,
    *,
    project_dir: Path | None = None,
    dry_run: bool = False,
    command: str | None = None,
) -> list[str]:
    """Register graphforge with each requested client. Returns report lines."""
    entry = server_entry(env_file, command)
    lines: list[str] = []
    for client in _targets(clients, project_dir):
        merged, action = plan(client, entry)
        if dry_run:
            lines.append(f"{client.label}: would be {action} at {client.path}")
            lines.extend(
                "    " + ln
                for ln in json.dumps({SERVERS_KEY: {SERVER_NAME: entry}}, indent=2).splitlines()
            )
            continue
        if action == "unchanged":
            lines.append(f"{client.label}: already registered ({client.path})")
            continue
        try:
            client.path.parent.mkdir(parents=True, exist_ok=True)
            client.path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            lines.append(f"{client.label}: could not write {client.path} - {exc}")
            continue
        note = " (restart the client to pick it up)"
        lines.append(f"{client.label}: {action} in {client.path}{note}")
    return lines


def uninstall(clients: list[str] | None = None, *, project_dir: Path | None = None) -> list[str]:
    """Remove graphforge from each requested client, leaving other servers alone."""
    lines: list[str] = []
    for client in _targets(clients, project_dir):
        config = client.read()
        servers = dict(config.get(SERVERS_KEY) or {})
        if SERVER_NAME not in servers:
            lines.append(f"{client.label}: not registered")
            continue
        servers.pop(SERVER_NAME)
        try:
            client.path.write_text(
                json.dumps({**config, SERVERS_KEY: servers}, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            lines.append(f"{client.label}: could not write {client.path} - {exc}")
            continue
        lines.append(f"{client.label}: removed from {client.path}")
    return lines
