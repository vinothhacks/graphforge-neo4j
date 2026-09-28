"""Clone or update Git repositories by shelling out to the `git` binary.

No third-party Git library required. Credentials, if provided, are handed to the
subprocess through a private, short-lived `credential.helper=store` file — never
through the remote URL. A URL carrying credentials leaks them into `ps` output,
into `.git/config`, and into git's own error text, which then gets logged. The
scrubber below is the second line of defence for that last one.

That holds for credentials the caller embeds in the URL too
(`https://user:secret@host/r.git`, `https://TOKEN@host/r.git`): they are split
off with :func:`strip_credentials` and sent through the same helper file, and
only the bare URL ever reaches git or the graph. A URL that names a user but no
password (`https://alice@bitbucket.org/...`, as clone buttons give them) names
who to log in as: the password the user's own credential manager holds for that
user is looked up beforehand and sent through the same file. When it holds none,
the name may be the credential itself (`https://TOKEN@host`) and goes alone.
Either way no helper of the user's takes part in a clone or fetch, where git's
`credential approve` would save what worked into their long-lived store.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shlex
import subprocess
import tempfile
from collections.abc import Iterator
from urllib.parse import quote, unquote

log = logging.getLogger("graphforge.git.clone")

#: Schemes whose credentials git asks a credential helper for.
_HTTP = ("http", "https")

#: Where a URL's authority starts: after `scheme://`, or a scheme-relative `//`.
_PREFIX = re.compile(r"[\x00-\x20]*(?:([A-Za-z][A-Za-z0-9+.-]*):)?//")
#: What ends an authority, as git reads a URL (`credential_from_url`).
_AUTHORITY_END = re.compile(r"[/?#]")


class GitError(RuntimeError):
    pass


def _git_env(**extra: str) -> dict[str, str]:
    return {
        **os.environ,
        # Never block on an interactive prompt: output is captured, so a
        # prompt would hang until the timeout rather than ask anyone.
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
        **extra,
    }


def _authority_end(text: str, start: int) -> int:
    match = _AUTHORITY_END.search(text, start)
    return match.start() if match else len(text)


def _bad_port(host: str) -> bool:
    """Whether ``host`` (`name[:port]` or `[v6][:port]`) has a port that is no port.

    More than one `:` outside the brackets is never a host and port: it is what a
    password holding a `:` leaves behind when an unencoded `/`, `?` or `#` later
    in it cuts the authority short (`gf:hunter2:1/xyz@db`).
    """
    tail = host.rpartition("]")[2] if host.startswith("[") else host
    if tail.count(":") > 1:
        return True
    _, colon, port = tail.rpartition(":")
    return bool(colon and port) and not (port.isascii() and port.isdigit() and int(port) < 65536)


def _userinfo_end(rest: str) -> int:
    """Index of the `@` that ends the userinfo in ``rest`` (what follows `//`), or -1.

    Found by hand, not with `urlsplit`, which raises on a `[` or `]` anywhere in
    the authority (`alice:s3cr]et@host`) and would leave the secret in place. As
    git reads it, the authority ends at the first `/`, `?` or `#`, and the
    userinfo at its last `@`. A password holding one of those three unencoded
    (`alice:pa/ss@host`) cuts the authority short and leaves a port that is no
    number, so the URL cannot work as written: then the userinfo runs on to the
    `@` after the cut. A valid authority followed by a path with an `@` in it
    (`https://host/@scope/r.git`) is left alone.
    """
    end = _authority_end(rest, 0)
    at = rest.rfind("@", 0, end)
    if _bad_port(rest[at + 1 : end]):
        later = rest.find("@", end)
        if later >= 0:
            at = rest.rfind("@", 0, _authority_end(rest, later))
    return at


def userinfo_span(url: str) -> tuple[int, int] | None:
    """``(start, end)`` of the userinfo in ``url``, its `@` excluded, or ``None``.

    The same boundary :func:`strip_credentials` cuts at, for callers that must
    hide the userinfo rather than drop it (the dashboard shows
    ``https://***@host/r.git``). It never raises (see `_userinfo_end`).
    """
    match = _PREFIX.match(url)
    if not match:
        return None
    at = _userinfo_end(url[match.end() :])
    return None if at < 0 else (match.end(), match.end() + at)


def _split_credentials(url: str) -> tuple[str, str | None, str | None]:
    """Split ``url`` into (url without credentials, username, password).

    Username and password come back percent-decoded, or ``None`` when absent. An
    http(s) URL loses its whole userinfo, since that is where credentials live.
    On other schemes the user is part of the address (``ssh://git@host/r.git``)
    and stays, so no username is returned; only a password is split off, which
    ssh would never take from a URL anyway. It never raises: a malformed URL
    loses its userinfo all the same (see `_userinfo_end`).
    """
    match = _PREFIX.match(url)
    if not match:
        return url, None, None
    head, rest = url[: match.end()], url[match.end() :]
    at = _userinfo_end(rest)
    if at < 0:
        return url, None, None
    user, colon, password = rest[:at].partition(":")
    secret = unquote(password) if colon else None
    host = rest[at + 1 :]
    if (match.group(1) or "").lower() in _HTTP:
        return head + host, unquote(user), secret
    if not colon:
        return url, None, None
    return head + (f"{user}@" if user else "") + host, None, secret


def strip_credentials(url: str) -> str:
    """``url`` with any embedded credentials removed: safe to log, store or show."""
    return _split_credentials(url)[0]


def _authority(url: str) -> tuple[str, str]:
    """(scheme, authority as written) of a credential-free URL; never raises."""
    match = _PREFIX.match(url)
    if not match:
        return "", ""
    rest = url[match.end() :]
    return (match.group(1) or "").lower(), rest[: _authority_end(rest, 0)]


def _endpoint(url: str) -> tuple[str, str]:
    """(scheme, host[:port]) of a credential-free URL, for comparing two of them."""
    scheme, netloc = _authority(url)
    return scheme, netloc.lower()


def _private_file(text: str) -> str:
    """Write ``text`` to a new owner-only temp file and return its path."""
    fd, path = tempfile.mkstemp(prefix="graphforge-cred-")
    try:
        with contextlib.suppress(OSError, NotImplementedError):
            os.chmod(path, 0o600)
        # newline="\n": credential-store splits on LF only, so the CR a Windows
        # text-mode write adds would land in the host and the entry never match.
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(path)
        raise
    return path


class GitHandler:
    def __init__(self, clone_dir: str, username: str = "", password: str = "", token: str = ""):
        self.clone_dir = os.path.abspath(clone_dir)
        self.username = username
        self.password = password
        self.token = token
        os.makedirs(self.clone_dir, exist_ok=True)

    # -- helpers -----------------------------------------------------------
    def _credential(self, target: str, *sources: str) -> tuple[str, str | None]:
        """(user, secret) to authenticate ``target`` with, or ("", None) for none.

        The secret is ``None`` when only a user is known. Credentials embedded in
        a URL are per-call and win over the settings: the first of ``sources`` on
        the same scheme and host as ``target`` that carries a password is used. A
        URL naming only a user decides who to log in as, so a later source's
        password counts only for that same user; failing one, the settings supply
        it (their password for their username, the token for `oauth2`).
        """
        origin = _endpoint(target)
        named: str | None = None
        for source in sources:
            clean, user, password = _split_credentials(source)
            if (not user and password is None) or _endpoint(clean) != origin:
                continue  # nothing in it, or another host's: never hand that over
            if password is None:
                named = user if named is None else named
            elif named is None or user == named:
                return user or "", password
        if named is not None:
            if self.password and named == self.username:
                return named, self.password
            if self.token and named == "oauth2":
                return named, self.token
            return named, None
        if self.token:
            return "oauth2", self.token
        if not self.username:
            return "", None
        return self.username, self.password or None

    @contextlib.contextmanager
    def _credentials_for(
        self, url: str, *sources: str, cwd: str | None = None
    ) -> Iterator[list[str]]:
        """Yield `git -c` flags that authenticate `url` without exposing the secret.

        The entry is written for `url`'s scheme and host; the credentials come
        from the first of ``sources`` (by default `url` itself) that embeds any,
        else from the settings. See `_credential`. ``cwd`` is where git will run,
        whose repository config the user's own helpers are looked up in.

        The secret goes into a temporary file that is removed on the way out.
        `mkstemp` opens it owner-only on POSIX; on Windows the mode bits do not
        carry, and the per-user temp directory's ACL is what protects it. Any
        inherited helper is cleared first, so a stale global credential store
        cannot answer instead, and so the `credential approve` git runs after a
        success, which calls `store` on every configured helper, reaches this
        file alone.

        With only a user, the inherited helpers (Git Credential Manager, say)
        are asked beforehand for that user's password, and what they answer goes
        through the file like any other credential. They are never left to
        answer git itself: with a helper that answers every name
        (`echo password=$TOKEN`), git's `credential approve` would then save the
        caller's URL token in the user's long-lived store. If none answers, the
        name may be the secret itself (`https://TOKEN@host`, or a token in
        GIT_USERNAME), and it goes with an empty password: what git itself sends
        for `https://TOKEN@host` when its prompt is answered with Enter.
        """
        clean = strip_credentials(url)
        scheme, netloc = _authority(clean)
        user, secret = ("", None)
        if scheme in _HTTP:
            user, secret = self._credential(clean, *(sources or (url,)))
        if not user and secret is None:
            yield []
            return
        if secret is None:
            user, secret = self._held_credential(clean, user, cwd) or (user, "")
        userinfo = f"{quote(user, safe='')}:{quote(secret, safe='')}"
        path = _private_file(f"{scheme}://{userinfo}@{netloc}\n")
        try:
            # git runs the helper through sh, so the path is quoted as one word: a
            # temp dir under "C:/Users/John Doe" would otherwise split in two.
            # Forward slashes keep Windows separators from reading as escapes.
            store = "credential.helper=store --file=" + shlex.quote(path.replace("\\", "/"))
            # The file's one entry has no path. With credential.useHttpPath on
            # (Git for Windows sets it for dev.azure.com) git would ask with the
            # repository path too, and the entry would never match.
            use_host = "credential.useHttpPath=false"
            yield ["-c", "credential.helper=", "-c", use_host, "-c", store]
        finally:
            with contextlib.suppress(OSError):
                os.remove(path)

    @staticmethod
    def _held_credential(url: str, user: str, cwd: str | None = None) -> tuple[str, str] | None:
        """(user, password) the user's own credential helpers hold for ``user``, or None.

        Asked with `git credential fill`, which stores nothing; the user comes
        back as the helper gave it. No askpass program is run: an empty
        GIT_ASKPASS makes git skip core.askPass and SSH_ASKPASS too, and an
        answer typed into a prompt is not a password a helper holds. A name git
        cannot take on one line is never asked: `a\\nhost=other` would fetch
        another host's password for this one.
        """
        if not (url + user).isprintable():
            return None
        try:
            proc = subprocess.run(
                ["git", "credential", "fill"],
                input=f"url={url}\nusername={user}\n\n",
                cwd=cwd,
                # stderr is not captured: a helper that hangs leaves a child
                # holding it open, and on Windows the timeout then waits for
                # that pipe to close instead of ending the call.
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
                env=_git_env(GIT_ASKPASS=""),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        lines = (line.partition("=") for line in proc.stdout.splitlines())
        answer = {key: value for key, _, value in lines}
        if proc.returncode != 0 or "password" not in answer:
            return None
        return answer.get("username", user), answer["password"]

    def _scrub(self, text: str, *urls: str) -> str:
        """Redact secrets from git output before it reaches a log or an exception.

        ``urls`` add the secrets embedded in them to the settings-level ones.
        """
        out = text or ""
        secrets = [self.token, self.password]
        for url in urls:
            _, user, password = _split_credentials(url)
            # No password means `https://TOKEN@host`: the name is the secret.
            secrets.append(password or user or "")
        for secret in secrets:
            if not secret:
                continue
            for form in (secret, quote(secret, safe="")):
                out = out.replace(form, "***")
        return out

    @staticmethod
    def _run(args, cwd: str | None = None, timeout: int = 900) -> tuple[int, str, str]:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=_git_env(),
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
            args += [strip_credentials(url), dest]
            code, _, err = self._run(args)
        if code != 0:
            raise GitError(f"clone failed for {name}: {self._scrub(err, url).strip()}")
        log.info("Cloned %s", name)
        return dest

    def update(self, name: str, branch: str | None = None, url: str = "") -> str:
        """Fetch and fast-forward an existing clone.

        ``url`` is the address the caller asked for this time; credentials
        embedded in it win over any in the clone's own `origin` and then over
        the settings.
        """
        dest = self.repo_path(name)
        raw = self._origin(dest)
        remote = strip_credentials(raw)
        if raw != remote:
            self._unembed_origin(name, dest, url)
        with self._credentials_for(remote, url, raw, cwd=dest) as cred:
            self._run([*cred, "fetch", "--all", "--tags", "--prune"], cwd=dest)
            if branch:
                self._run(["checkout", branch], cwd=dest)
            code, _, err = self._run([*cred, "pull", "--ff-only"], cwd=dest)
        if code != 0:
            log.warning("pull for %s reported: %s", name, self._scrub(err, url, raw).strip())
        return dest

    def _unembed_origin(self, name: str, dest: str, url: str) -> None:
        """Take the secret out of a clone whose `origin` was saved with one.

        An older graphforge, or a hand-made clone, leaves the credentialed URL in
        `.git/config`: v0.2.0 wrote the settings' own `oauth2:<GIT_TOKEN>` (or
        `user:GIT_PASSWORD`) into every clone it made. It is stripped only when
        that loses nothing, because the stored URL holds exactly the credential
        this run authenticates with anyway: this call's ``url``, for the same
        host, carries the same user and password (or the same lone user), or,
        with none in the ``url``, the settings hold the very credential stored.
        A stored URL that names a user and no password holds nothing but the
        name, so it goes whenever the run logs in as that user. Another user, or
        the same user with another password, does not count: nothing has shown
        that the new credential works, and the stored one may be its only copy.
        Then the config is what authenticates the clone, since git sends a
        password in the URL without asking any helper, and it is left alone
        with a warning.
        """
        code, stored, _ = self._run(["config", "--get", "remote.origin.url"], cwd=dest)
        stored = stored.strip()
        remote = strip_credentials(stored)
        if code != 0 or remote == stored:
            return  # the secret comes from an insteadOf rule the user set, not this clone
        kept = self._credential(remote, stored)  # the stored URL's own, paired as a run would
        current = self._credential(remote, url)  # what every run brings: the url's, or settings'
        name_only = _split_credentials(stored)[2] is None
        if kept == current or (name_only and kept[0] == current[0]):
            code, _, err = self._run(["remote", "set-url", "origin", remote], cwd=dest)
            if code == 0:
                log.info("Removed the credentials stored in %s's origin URL", name)
            else:
                log.warning(
                    "could not reset origin for %s: %s", name, self._scrub(err, url).strip()
                )
            return
        if current[1] is not None and current[0] == kept[0]:
            # Same user, other secret: a token rotated since v0.2.0 stored it, or a
            # new (or mistyped) password in the URL.
            clean, _, password = _split_credentials(url)
            if password is not None and _endpoint(clean) == _endpoint(remote):
                source = "the password in the repository URL"
            elif current == (self.username, self.password):
                source = "GIT_PASSWORD in the git settings"
            else:
                source = "GIT_TOKEN in the git settings"
            log.warning(
                "%s: the origin URL in .git/config embeds credentials that differ from "
                "%s, and git sends the stored ones instead. If the new ones are "
                "current, run `git remote set-url origin %s` in %s",
                name,
                source,
                remote,
                dest,
            )
            return
        log.warning(
            "%s: the origin URL in .git/config embeds credentials. Put them in the git "
            "settings instead (GIT_TOKEN, or GIT_USERNAME and GIT_PASSWORD), then run "
            "`git remote set-url origin %s` in %s",
            name,
            remote,
            dest,
        )

    def _origin(self, dest: str) -> str:
        """The `origin` URL exactly as `.git/config` holds it, credentials and all."""
        code, out, _ = self._run(["remote", "get-url", "origin"], cwd=dest)
        return out.strip() if code == 0 else ""

    def remote_url(self, dest: str) -> str:
        """The `origin` URL of an existing clone, without credentials, or "" if none."""
        return strip_credentials(self._origin(dest))

    def clone_or_update(self, url: str, name: str, branch: str | None = None) -> str:
        if self.is_cloned(name):
            return self.update(name, branch, url)
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
    # Stripped first: for `https://TOKEN@host` the tail is the netloc, token and all,
    # and the name becomes a directory, a node id and a log line.
    tail = strip_credentials(url).rstrip("/").split("/")[-1]
    return tail.removesuffix(".git")
