"""Clone or update Git repositories by shelling out to the `git` binary.

No third-party Git library required. Credentials, if provided, are handed to the
subprocess through a private, short-lived `credential.helper=store` file — never
through the remote URL. A URL carrying credentials leaks them into `ps` output,
into `.git/config`, and into git's own error text, which then gets logged. The
scrubber below is the second line of defence for that last one.
"""
from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import tempfile
from collections.abc import Iterator
from urllib.parse import quote, urlparse

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
    def _userinfo(self) -> str:
        """The `user:secret` half of a credential entry, or "" when unauthenticated."""
        if self.token:
            return f"oauth2:{quote(self.token, safe='')}"
        if not self.username:
            return ""
        userinfo = quote(self.username, safe="")
        if self.password:
            userinfo += f":{quote(self.password, safe='')}"
        return userinfo

    @contextlib.contextmanager
    def _credentials_for(self, url: str) -> Iterator[list[str]]:
        """Yield `git -c` flags that authenticate `url` without exposing the secret.

        The secret goes into a temporary file that is removed on the way out.
        `mkstemp` opens it owner-only on POSIX; on Windows the mode bits do not
        carry, and the per-user temp directory's ACL is what protects it. Any
        inherited helper is cleared first so a stale global credential store
        cannot answer instead.
        """
        userinfo = self._userinfo() if url.startswith(("http://", "https://")) else ""
        if not userinfo:
            yield []
            return
        parts = urlparse(url)
        netloc = parts.hostname or ""
        if parts.port:
            netloc += f":{parts.port}"
        fd, path = tempfile.mkstemp(prefix="graphforge-cred-")
        try:
            with contextlib.suppress(OSError, NotImplementedError):
                os.chmod(path, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(f"{parts.scheme}://{userinfo}@{netloc}\n")
            # Forward slashes: git treats a backslash in a -c value as an escape.
            helper = "store --file=" + path.replace("\\", "/")
            yield ["-c", "credential.helper=", "-c", "credential.helper=" + helper]
        finally:
            with contextlib.suppress(OSError):
                os.remove(path)

    def _scrub(self, text: str) -> str:
        """Redact secrets from git output before it reaches a log or an exception."""
        out = text or ""
        for secret in (self.token, self.password):
            if not secret:
                continue
            for form in (secret, quote(secret, safe="")):
                out = out.replace(form, "***")
        return out

    @staticmethod
    def _run(args, cwd: str | None = None, timeout: int = 900) -> tuple[int, str, str]:
        env = {
            **os.environ,
            # Never block on an interactive prompt: output is captured, so a
            # prompt would hang until the timeout rather than ask anyone.
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
        }
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
            env=env,
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
    def clone(self, url: str, name: str, branch: str | None = None) -> str:
        dest = self.repo_path(name)
        with self._credentials_for(url) as cred:
            args = [*cred, "clone"]
            if branch:
                args += ["--branch", branch]
            args += [url, dest]
            code, _, err = self._run(args)
        if code != 0:
            raise GitError(f"clone failed for {name}: {self._scrub(err).strip()}")
        log.info("Cloned %s", name)
        return dest

    def update(self, name: str, branch: str | None = None) -> str:
        dest = self.repo_path(name)
        remote = self.remote_url(dest)
        with self._credentials_for(remote) as cred:
            self._run([*cred, "fetch", "--all", "--tags", "--prune"], cwd=dest)
            if branch:
                self._run(["checkout", branch], cwd=dest)
            code, _, err = self._run([*cred, "pull", "--ff-only"], cwd=dest)
        if code != 0:
            log.warning("pull for %s reported: %s", name, self._scrub(err).strip())
        return dest

    def remote_url(self, dest: str) -> str:
        """The `origin` URL of an existing clone, or "" if it has none."""
        code, out, _ = self._run(["remote", "get-url", "origin"], cwd=dest)
        return out.strip() if code == 0 else ""

    def clone_or_update(self, url: str, name: str, branch: str | None = None) -> str:
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
