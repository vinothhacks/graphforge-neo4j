"""Clone or update Git repositories by shelling out to the `git` binary.

No third-party Git library required. Credentials, if provided, are injected into
the remote URL only for the clone/fetch subprocess and are never logged.
"""
from __future__ import annotations

import logging
import os
import subprocess
from typing import Optional, Tuple
from urllib.parse import quote, urlparse, urlunparse

log = logging.getLogger("graphforge.git.clone")


class GitError(RuntimeError):
    pass


class GitHandler:
    def __init__(self, clone_dir: str, username: str = "", password: str = "", token: str = ""):
        self.clone_dir = os.path.abspath(clone_dir)
        self.username = username
        self.password = password
        self.token = token
        os.makedirs(self.clone_dir, exist_ok=True)

    # -- helpers -----------------------------------------------------------
    def _auth_url(self, url: str) -> str:
        """Inject credentials into an http(s) URL for private remotes."""
        if not url.startswith(("http://", "https://")):
            return url
        if not (self.token or self.username):
            return url
        parts = urlparse(url)
        if self.token:
            userinfo = f"oauth2:{quote(self.token, safe='')}"
        else:
            userinfo = quote(self.username, safe="")
            if self.password:
                userinfo += f":{quote(self.password, safe='')}"
        netloc = f"{userinfo}@{parts.hostname}"
        if parts.port:
            netloc += f":{parts.port}"
        return urlunparse(parts._replace(netloc=netloc))

    @staticmethod
    def _run(args, cwd: Optional[str] = None, timeout: int = 900) -> Tuple[int, str, str]:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def check_git(self) -> bool:
        try:
            code, _, _ = self._run(["--version"])
            return code == 0
        except (FileNotFoundError, subprocess.SubprocessError):
            return False

    def repo_path(self, name: str) -> str:
        return os.path.join(self.clone_dir, name)

    def is_cloned(self, name: str) -> bool:
        return os.path.isdir(os.path.join(self.repo_path(name), ".git"))

    # -- operations --------------------------------------------------------
    def clone(self, url: str, name: str, branch: Optional[str] = None) -> str:
        dest = self.repo_path(name)
        args = ["clone"]
        if branch:
            args += ["--branch", branch]
        args += [self._auth_url(url), dest]
        code, _, err = self._run(args)
        if code != 0:
            raise GitError(f"clone failed for {name}: {err.strip()}")
        log.info("Cloned %s", name)
        return dest

    def update(self, name: str, branch: Optional[str] = None) -> str:
        dest = self.repo_path(name)
        self._run(["fetch", "--all", "--tags", "--prune"], cwd=dest)
        if branch:
            self._run(["checkout", branch], cwd=dest)
        code, _, err = self._run(["pull", "--ff-only"], cwd=dest)
        if code != 0:
            log.warning("pull for %s reported: %s", name, err.strip())
        return dest

    def clone_or_update(self, url: str, name: str, branch: Optional[str] = None) -> str:
        if self.is_cloned(name):
            return self.update(name, branch)
        return self.clone(url, name, branch)

    def resolve(self, spec: dict, default_branch: str = "main") -> str:
        """Return a local path for a repo spec (either a `path` or a `url`)."""
        if spec.get("path"):
            path = os.path.abspath(spec["path"])
            if not os.path.isdir(path):
                raise GitError(f"local path not found: {path}")
            return path
        if spec.get("url"):
            name = spec.get("name") or _name_from_url(spec["url"])
            branch = spec.get("branch") or default_branch
            return self.clone_or_update(spec["url"], name, branch)
        raise GitError(f"repo spec needs a 'url' or 'path': {spec!r}")


def _name_from_url(url: str) -> str:
    tail = url.rstrip("/").split("/")[-1]
    return tail.removesuffix(".git")
