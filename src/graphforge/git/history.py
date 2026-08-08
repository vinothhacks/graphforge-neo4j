"""Extract commit history (commits, authors, parents, file changes, branches,
tags) by parsing `git` porcelain output.

Two `git log` passes are merged per commit: `--numstat` gives per-file
insertions/deletions, `--name-status` gives the change type (A/M/D/R). Renames
are normalised to the new path so :File identity stays stable.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional

log = logging.getLogger("graphforge.git.history")

_MARK = "@@C@@"
_US = "\x1f"  # unit separator, unlikely to appear in commit metadata
_FMT = _MARK + _US + _US.join(
    ["%H", "%h", "%an", "%ae", "%aI", "%cn", "%ce", "%cI", "%P", "%s"]
)


@dataclass
class HistoryData:
    authors: Dict[str, dict] = field(default_factory=dict)
    commits: List[dict] = field(default_factory=list)
    branches: List[dict] = field(default_factory=list)
    tags: List[dict] = field(default_factory=list)
    head: str = ""


def _run(repo_path: str, args: List[str]) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo_path, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=900,
    )
    if proc.returncode != 0:
        log.debug("git %s -> %s", " ".join(args), proc.stderr.strip())
    return proc.stdout


def _normalise_path(path: str) -> str:
    """Turn a numstat/name-status rename token into the resulting path."""
    if "=>" in path:
        # forms: "old => new"  or  "dir/{old => new}/file"
        path = path.replace("{", "").replace("}", "")
        left, _, right = path.partition("=>")
        merged = (left.strip() + right.strip()) if "/" in right and right.strip().startswith("/") else right.strip()
        path = merged or right.strip()
    return path.strip()


def _parse_log(repo_path: str, mode: str, limit: int):
    """Return (per_commit_files, headers) for --numstat or --name-status."""
    args = ["log", "--all", f"--pretty=format:{_FMT}", f"--{mode}"]
    if limit and limit > 0:
        args += ["-n", str(limit)]
    out = _run(repo_path, args)

    per_commit: Dict[str, List[dict]] = {}
    current: Optional[str] = None
    header_fields: Dict[str, dict] = {}

    for line in out.splitlines():
        if line.startswith(_MARK):
            parts = line.split(_US)
            # parts[0] == _MARK
            current = parts[1]
            header_fields[current] = {
                "hash": parts[1], "short": parts[2],
                "authorName": parts[3], "authorEmail": parts[4], "authoredAt": parts[5],
                "committerName": parts[6], "committerEmail": parts[7], "committedAt": parts[8],
                "parents": parts[9].split() if parts[9] else [],
                "message": parts[10] if len(parts) > 10 else "",
            }
            per_commit.setdefault(current, [])
            continue
        if not line.strip() or current is None:
            continue
        cols = line.split("\t")
        if mode == "numstat" and len(cols) == 3:
            ins = 0 if cols[0] == "-" else int(cols[0] or 0)
            dele = 0 if cols[1] == "-" else int(cols[1] or 0)
            per_commit[current].append(
                {"path": _normalise_path(cols[2]), "insertions": ins, "deletions": dele}
            )
        elif mode == "name-status" and len(cols) >= 2:
            per_commit[current].append(
                {"path": _normalise_path(cols[-1]), "changeType": cols[0][0]}
            )
    return per_commit, header_fields


def extract_history(repo_path: str, limit: int = 0) -> HistoryData:
    data = HistoryData()

    numstat, headers = _parse_log(repo_path, "numstat", limit)
    namestat, _ = _parse_log(repo_path, "name-status", limit)

    for h, meta in headers.items():
        # merge per-file insertions/deletions with change type
        type_by_path = {r["path"]: r.get("changeType", "M") for r in namestat.get(h, [])}
        files: List[dict] = []
        total_ins = total_del = 0
        for row in numstat.get(h, []):
            files.append({
                "path": row["path"],
                "insertions": row["insertions"],
                "deletions": row["deletions"],
                "changeType": type_by_path.get(row["path"], "M"),
            })
            total_ins += row["insertions"]
            total_del += row["deletions"]
        # files that appear only in name-status (e.g. pure renames, binary)
        seen = {f["path"] for f in files}
        for path, ct in type_by_path.items():
            if path not in seen:
                files.append({"path": path, "insertions": 0, "deletions": 0, "changeType": ct})

        commit = dict(meta)
        commit["files"] = files
        commit["insertions"] = total_ins
        commit["deletions"] = total_del
        commit["filesChanged"] = len(files)
        data.commits.append(commit)

        aid = (meta["authorEmail"] or meta["authorName"]).lower()
        data.authors[aid] = {"name": meta["authorName"], "email": meta["authorEmail"]}
        cid = (meta["committerEmail"] or meta["committerName"]).lower()
        data.authors.setdefault(cid, {"name": meta["committerName"], "email": meta["committerEmail"]})

    data.commits.sort(key=lambda c: c.get("authoredAt", ""))
    data.branches = _branches(repo_path)
    data.tags = _tags(repo_path)
    data.head = _run(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
    return data


def _branches(repo_path: str) -> List[dict]:
    out = _run(repo_path, ["branch", "-a", "--format=%(refname:short)" + _US + "%(objectname)"])
    seen, result = set(), []
    for line in out.splitlines():
        if _US not in line:
            continue
        name, commit = line.split(_US, 1)
        if name.endswith("/HEAD") or name in seen:
            continue
        seen.add(name)
        result.append({"name": name, "commit": commit})
    return result


def _tags(repo_path: str) -> List[dict]:
    fmt = "%(refname:short)" + _US + "%(objectname)" + _US + "%(*objectname)"
    out = _run(repo_path, ["tag", "--format=" + fmt])
    result = []
    for line in out.splitlines():
        cols = line.split(_US)
        if len(cols) < 2 or not cols[0]:
            continue
        deref = cols[2] if len(cols) > 2 and cols[2] else cols[1]
        result.append({"name": cols[0], "commit": deref})
    return result
