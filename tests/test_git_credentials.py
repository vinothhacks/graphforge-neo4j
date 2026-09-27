"""A git secret must never reach argv, the remote URL, a log line or an exception.

Credentials used to be spliced into the remote URL. That put the token in `ps`
output for every local user, wrote it into `.git/config`, and let git echo it
back inside the error text that `GitError` then carried up to the console.

The same goes for credentials a caller writes into the repository URL itself
(`https://user:secret@host/r.git`, `https://TOKEN@host/r.git`): those used to be
handed to `git clone` as they were, and stored on :Repository.url in the graph.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from graphforge.git.clone import GitError, GitHandler, strip_credentials

TOKEN = "glpat-NOTAREALTOKEN12345"
PASSWORD = "p@ss word/with+specials"
URL_TOKEN = "glpat-INTHEURL67890"

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _handler(tmp_path, **kwargs) -> GitHandler:
    return GitHandler(str(tmp_path / "repos"), **kwargs)


def _helper_path(flags: list[str]) -> str:
    """The credential file a helper flag names, split into words the way sh would."""
    words = shlex.split(flags[-1].removeprefix("credential.helper="))
    assert len(words) == 2 and words[0] == "store", f"helper is not one store command: {words}"
    return words[1].removeprefix("--file=")


def _recording(handler: GitHandler, passthrough: bool = False) -> list[dict]:
    """Swap `_run` for a recorder that also snapshots the files git would read.

    With ``passthrough``, everything but the network commands still runs for real.
    """
    calls: list[dict] = []
    real = handler._run

    def run(args, cwd=None, timeout=900):
        args = list(args)
        helpers = [a for a in args if a.startswith("credential.helper=store")]
        helper = Path(_helper_path(helpers)).read_text(encoding="utf-8") if helpers else ""
        calls.append({"args": args, "helper": helper})
        if passthrough and not {"clone", "fetch", "pull", "checkout"} & set(args):
            return real(args, cwd=cwd, timeout=timeout)
        return 0, "", ""

    handler._run = run  # type: ignore[method-assign]
    return calls


def _git(cwd, *args) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)
    return proc.stdout.strip()


@pytest.fixture(autouse=True)
def hermetic_git(tmp_path, monkeypatch):
    """Keep the developer's own git config (helpers, insteadOf, ...) out of the test.

    Every test, since the handler itself asks the user's helpers about a lone
    user. Their askpass program too (VS Code's terminal sets one): git tries it
    before GIT_TERMINAL_PROMPT, so a helper that answers nothing would put up a
    prompt, or take its answer, instead of failing the test. An empty
    GIT_ASKPASS makes git skip core.askPass and SSH_ASKPASS as well.
    """
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("GCM_INTERACTIVE", "never")
    monkeypatch.setenv("GIT_ASKPASS", "")


#: A helper that answers every user with one password, as CI's `echo password=$TOKEN` does.
ANY_USER_HELPER = '\thelper = "!f() { test \\"$1\\" = get && echo password=from-env; }; f"\n'


def _user_store(tmp_path, content: str = "", ahead: str = "") -> Path:
    """A `store` helper in the global config, standing in for the user's own manager.

    ``ahead`` holds config lines for other helpers, which git asks first.
    """
    store = tmp_path / "manager store"
    store.write_text(content, encoding="utf-8", newline="\n")
    helper = str(store).replace("\\", "/")
    Path(os.environ["GIT_CONFIG_GLOBAL"]).write_text(
        f"[credential]\n{ahead}\thelper = store --file='{helper}'\n",
        encoding="utf-8",
        newline="\n",
    )
    return store


def _fill(flags: list[str], host: str = "example.invalid") -> tuple[dict, str]:
    """Ask git itself which credential the helper flags produce for ``host``."""
    proc = subprocess.run(
        ["git", *flags, "credential", "fill"],
        input=f"protocol=https\nhost={host}\n\n".encode(),
        capture_output=True,
        check=False,
        timeout=30,
    )
    out = proc.stdout.decode("utf-8", "replace")
    answer = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return answer, proc.stderr.decode("utf-8", "replace")


def _clone_with_origin(handler: GitHandler, name: str, origin: str) -> Path:
    """An existing clone whose .git/config holds ``origin`` verbatim."""
    dest = Path(handler.repo_path(name))
    _git(dest.parent, "init", "-q", str(dest))
    _git(dest, "remote", "add", "origin", origin)
    return dest


@pytest.fixture
def auth_server(monkeypatch):
    """An http remote on localhost that wants Basic auth: (host:port, what each request sent).

    Any authenticated request succeeds, which is all git needs to run
    `credential approve` and hand the credential to every helper it has.
    """
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            auth = self.headers.get("Authorization", "")
            seen.append(base64.b64decode(auth.split()[-1]).decode() if auth else "")
            self.send_response(200 if auth else 401)
            if not auth:
                self.send_header("WWW-Authenticate", 'Basic realm="r"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "*")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{server.server_address[1]}", seen
    server.shutdown()
    server.server_close()


@pytest.fixture
def git_server(tmp_path, monkeypatch):
    """A real repository, `r.git`, served over smart HTTP by `git http-backend`.

    Yields (host:port, what each request sent, the `(user, password)` logins it
    accepts). A request with any other login gets a 401, as from a forge, so a
    clone succeeds only with a login the test has added.
    """
    work, root = tmp_path / "work", tmp_path / "served"
    _git(tmp_path, "init", "-q", str(work))
    ident = ("-c", "user.name=t", "-c", "user.email=t@t")
    _git(work, *ident, "commit", "-q", "--allow-empty", "-m", "1")
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(root / "r.git"))
    seen: list[str] = []
    accepted: set[tuple[str, str]] = set()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            auth = self.headers.get("Authorization", "")
            login = base64.b64decode(auth.split()[-1]).decode() if auth else ""
            seen.append(login)
            if tuple(login.split(":", 1)) not in accepted:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="r"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            path, _, query = self.path.partition("?")
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            cgi = {
                **os.environ,
                "GIT_PROJECT_ROOT": str(root),
                "GIT_HTTP_EXPORT_ALL": "1",
                "PATH_INFO": path,
                "QUERY_STRING": query,
                "REQUEST_METHOD": self.command,
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(len(body)),
                "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
                "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
                "REMOTE_USER": login.partition(":")[0],
            }
            out = subprocess.run(
                ["git", "http-backend"], input=body, env=cgi, capture_output=True, timeout=60
            ).stdout
            head, _, content = out.partition(b"\r\n\r\n")
            headers = [line.decode().partition(":")[::2] for line in head.split(b"\r\n")]
            status = next((v for k, v in headers if k.lower() == "status"), "200").split()[0]
            self.send_response(int(status))
            for key, value in headers:
                if key.lower() != "status":
                    self.send_header(key, value.strip())
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        do_POST = do_GET

        def log_message(self, *args):
            pass

    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "*")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{server.server_address[1]}", seen, accepted
    server.shutdown()
    server.server_close()


# -- the helper file ---------------------------------------------------------
def test_the_secret_goes_in_a_file_that_is_deleted_afterwards(tmp_path):
    handler = _handler(tmp_path, token=TOKEN)
    with handler._credentials_for("https://gitlab.example/group/repo.git") as flags:
        assert flags[:2] == ["-c", "credential.helper="], "inherited helpers not cleared"
        path = _helper_path(flags)
        assert os.path.exists(path)
        raw = Path(path).read_bytes()
        content = raw.decode("utf-8")
        assert TOKEN in content, "the helper file must actually carry the credential"
        assert content.startswith("https://oauth2:")
        assert "gitlab.example" in content
        # credential-store splits on LF only; a CR would become part of the host
        assert b"\r" not in raw, "helper file written with CRLF: git can never match it"
    assert not os.path.exists(path), "credential file outlived the operation"


def test_a_port_is_kept_so_the_credential_matches_the_remote(tmp_path):
    handler = _handler(tmp_path, username="ci", password=PASSWORD)
    with handler._credentials_for("https://git.example:8443/g/r.git") as flags:
        content = Path(_helper_path(flags)).read_text(encoding="utf-8")
    assert "git.example:8443" in content


def test_the_helper_path_stays_one_word_for_git_s_shell(tmp_path, monkeypatch):
    """git runs the helper through sh: an unquoted "John O'Doe" path breaks in two."""
    spaced = tmp_path / "John O'Doe" / "Temp"
    spaced.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "tempdir", str(spaced))
    handler = _handler(tmp_path, username="ci", password=PASSWORD)
    with handler._credentials_for("https://example.invalid/g/r.git") as flags:
        path = _helper_path(flags)
        assert "\\" not in path, "backslashes would read as escapes"
        assert Path(path).parent == spaced
        assert os.path.exists(path)


@needs_git
def test_git_reads_the_credential_through_a_path_with_a_space(tmp_path, monkeypatch, hermetic_git):
    """Regression: with the temp dir under a profile like "C:/Users/John Doe", no
    credential reached git and every private clone and fetch failed.

    Run through git itself, this also catches the helper file being written with
    CRLF on Windows, which made the entry unmatchable even without a space.
    """
    spaced = tmp_path / "John O'Doe" / "Temp"
    spaced.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "tempdir", str(spaced))
    handler = _handler(tmp_path, username="ci", password=PASSWORD)
    with handler._credentials_for("https://example.invalid/g/r.git") as flags:
        answer, stderr = _fill(flags)
    assert answer.get("username") == "ci", stderr
    assert answer.get("password") == PASSWORD


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:octocat/Hello-World.git",
        "ssh://git@example/x.git",
        "https://github.com/public/repo.git",  # public: no credentials configured
    ],
)
def test_no_credential_plumbing_when_there_is_nothing_to_send(tmp_path, url):
    handler = _handler(tmp_path) if url.startswith("https") else _handler(tmp_path, token=TOKEN)
    with handler._credentials_for(url) as flags:
        assert flags == []


def test_the_remote_url_handed_to_git_carries_no_credentials(tmp_path):
    """Regression: the URL in argv is the plain one the user gave us."""
    seen: list[list[str]] = []

    handler = _handler(tmp_path, token=TOKEN)
    handler._run = lambda args, cwd=None, timeout=900: (  # type: ignore[method-assign]
        seen.append(list(args)) or (0, "", "")
    )
    url = "https://gitlab.example/group/repo.git"
    handler.clone(url, "repo")

    argv = seen[0]
    assert url in argv, "the clean URL should be what git is asked to clone"
    joined = " ".join(argv)
    assert TOKEN not in joined, "token leaked into the command line"
    assert "oauth2:" not in joined, "credentials were spliced into the remote URL"


# -- credentials the caller wrote into the URL --------------------------------
@pytest.mark.parametrize(
    ("url", "clean"),
    [
        ("https://alice:s3cret@example.invalid/g/r.git", "https://example.invalid/g/r.git"),
        (
            f"https://{URL_TOKEN}@example.invalid:8443/g/r.git",
            "https://example.invalid:8443/g/r.git",
        ),
        ("http://a%40b:p%2Fw@example.invalid/r.git", "http://example.invalid/r.git"),
        ("https://example.invalid/g/r.git", "https://example.invalid/g/r.git"),
        # the ssh user is part of the address, not a secret
        ("ssh://git@example.invalid/g/r.git", "ssh://git@example.invalid/g/r.git"),
        ("ssh://git:pw@example.invalid/g/r.git", "ssh://git@example.invalid/g/r.git"),
        ("git@example.invalid:g/r.git", "git@example.invalid:g/r.git"),
        ("/srv/repos/r", "/srv/repos/r"),
        ("", ""),
        # a bracket in the authority makes urlsplit raise: the secret used to stay
        ("https://alice:p]ss@example.invalid/r.git", "https://example.invalid/r.git"),
        ("https://alice:p[ss@example.invalid/r.git", "https://example.invalid/r.git"),
        (f"https://{URL_TOKEN}[x@example.invalid", "https://example.invalid"),
        ("https://alice:pw@[bogus]/r.git", "https://[bogus]/r.git"),
        # an unencoded "/", "?" or "#" in the password cuts the authority short,
        # leaving `alice:pa` as a host with a port that is no number
        ("https://alice:pa/ss@example.invalid/g/r.git", "https://example.invalid/g/r.git"),
        ("https://alice:pa?ss@example.invalid/g/r.git", "https://example.invalid/g/r.git"),
        ("https://alice:pa#ss@example.invalid/g/r.git", "https://example.invalid/g/r.git"),
        # ...and so does a password holding a ":" before the cut: `alice:hunter2:1`
        # read at its last ":" looked like a sound host with port 1, and the whole
        # URL, password included, went to git unchanged
        ("https://alice:hunter2:1/xyz@example.invalid/r.git", "https://example.invalid/r.git"),
        ("https://alice:hunter2:/xyz@example.invalid/r.git", "https://example.invalid/r.git"),
        ("https://alice:hunter2:443?xyz@example.invalid/r.git", "https://example.invalid/r.git"),
        ("https://[::1]:1:2/xyz@example.invalid/r.git", "https://example.invalid/r.git"),
        # a bracketed IPv6 host with a real port is sound, colons and all
        ("https://alice:pw@[::1]:8443/g/r.git", "https://[::1]:8443/g/r.git"),
        # ...while an "@" after a sound authority is part of the path, not userinfo
        ("https://example.invalid/@scope/r.git", "https://example.invalid/@scope/r.git"),
        ("https://example.invalid:8443/g/r.git#v1@x", "https://example.invalid:8443/g/r.git#v1@x"),
    ],
)
def test_strip_credentials(url, clean):
    assert strip_credentials(url) == clean


@pytest.mark.parametrize(
    ("url", "entry"),
    [
        ("https://alice:s3cret@example.invalid/g/r.git", "https://alice:s3cret@example.invalid\n"),
        # a lone name may be the token-as-username form: no helper of the user's
        # holds it, so it goes with an empty password, their helpers switched off
        (f"https://{URL_TOKEN}@example.invalid/g/r.git", f"https://{URL_TOKEN}:@example.invalid\n"),
        # Regression: a "]" made clone die on a bare ValueError, for a URL git takes
        (
            "https://alice:s3cr]et@example.invalid/g/r.git",
            "https://alice:s3cr%5Det@example.invalid\n",
        ),
        # and a "/" put the whole URL, password and all, on git's command line
        (
            "https://alice:s3/cret@example.invalid/g/r.git",
            "https://alice:s3%2Fcret@example.invalid\n",
        ),
    ],
)
def test_url_credentials_travel_through_the_helper_not_argv(tmp_path, url, entry):
    handler = _handler(tmp_path)  # nothing in the settings: the URL is the only source
    calls = _recording(handler)
    handler.clone(url, "r")

    argv = calls[0]["args"]
    assert "https://example.invalid/g/r.git" in argv
    joined = " ".join(argv)
    secrets = ("s3cret", "s3cr]et", "s3/cret", URL_TOKEN)
    assert not any(s in joined for s in secrets), "secret passed to git clone"
    assert calls[0]["helper"] == entry, "embedded credentials were not sent at all"
    assert "credential.helper=" in argv, "the user's own helpers could store the secret"


def test_a_malformed_host_is_left_for_git_to_reject(tmp_path):
    """urlsplit raises on it, which used to escape as a bare ValueError."""
    handler = _handler(tmp_path, token=TOKEN)
    with handler._credentials_for("https://[bogus/r.git") as flags:
        entry = Path(_helper_path(flags)).read_text(encoding="utf-8")
    assert entry == f"https://oauth2:{TOKEN}@[bogus\n"


@needs_git
@pytest.mark.parametrize(
    ("url", "username", "password"),
    [
        ("https://alice:p%40ss%20w@example.invalid/g/r.git", "alice", "p@ss w"),
        (f"https://{URL_TOKEN}@example.invalid/g/r.git", URL_TOKEN, ""),
        ("https://alice:s3cr]et@example.invalid/g/r.git", "alice", "s3cr]et"),
    ],
)
def test_git_receives_url_credentials_from_the_helper(
    tmp_path, hermetic_git, url, username, password
):
    handler = _handler(tmp_path, token=TOKEN)
    with handler._credentials_for(url) as flags:
        answer, stderr = _fill(flags)
    assert answer.get("username") == username, stderr
    assert answer.get("password") == password


def test_url_credentials_take_precedence_over_the_settings(tmp_path):
    handler = _handler(tmp_path, token=TOKEN)
    calls = _recording(handler)
    handler.clone("https://alice:s3cret@example.invalid/g/r.git", "r")
    assert calls[0]["helper"] == "https://alice:s3cret@example.invalid\n"


def test_a_url_naming_the_settings_user_keeps_the_settings_password(tmp_path):
    handler = _handler(tmp_path, username="ci", password="pw")
    calls = _recording(handler)
    handler.clone("https://ci@example.invalid/g/r.git", "r")
    assert calls[0]["helper"] == "https://ci:pw@example.invalid\n"


def test_oauth2_in_the_url_pairs_with_the_settings_token(tmp_path):
    """Regression: `https://oauth2@host` used to send `oauth2:` with an empty password."""
    handler = _handler(tmp_path, token=TOKEN)
    with handler._credentials_for("https://oauth2@example.invalid/g/r.git") as flags:
        assert flags[:2] == ["-c", "credential.helper="]
        assert Path(_helper_path(flags)).read_text(encoding="utf-8") == (
            f"https://oauth2:{TOKEN}@example.invalid\n"
        )


@needs_git
@pytest.mark.parametrize(
    "settings",
    [{}, {"token": TOKEN}, {"username": "bob", "password": "pw"}],
    ids=["no-settings", "settings-token", "settings-other-user"],
)
def test_a_url_naming_only_a_user_asks_the_user_s_own_helper_first(
    tmp_path, monkeypatch, hermetic_git, settings
):
    """Regression: the Bitbucket / Azure DevOps clone-button form `https://alice@host`.

    It used to count as a whole credential: git got an empty password, so the one
    Git Credential Manager holds for alice was never asked for and private clones
    failed. The global store here stands in for that manager, with another account
    ahead of alice's to show that it is asked for the user the URL names. What it
    answers goes through the private file, the manager itself switched off, so git
    stores nothing back into it.
    """
    spaced = tmp_path / "John O'Doe" / "Temp"
    spaced.mkdir(parents=True)
    _user_store(
        tmp_path, "https://bob:wrong@example.invalid\nhttps://alice:from-manager@example.invalid\n"
    )
    monkeypatch.setattr(tempfile, "tempdir", str(spaced))
    handler = _handler(tmp_path, **settings)
    with handler._credentials_for("https://alice@example.invalid/team/r.git") as flags:
        assert flags[:2] == ["-c", "credential.helper="], "the user's own helpers left on"
        answer, stderr = _fill(flags)
    assert answer.get("username") == "alice", stderr
    assert answer.get("password") == "from-manager"


@needs_git
@pytest.mark.parametrize(
    ("url", "settings"),
    [
        (f"http://{URL_TOKEN}@{{host}}/r.git", {}),
        (f"http://{URL_TOKEN}@{{host}}/r.git", {"token": TOKEN}),  # the URL's user wins
        ("http://{host}/r.git", {"username": URL_TOKEN}),  # a token in GIT_USERNAME
    ],
    ids=["url-token", "url-token-over-settings", "settings-username-token"],
)
def test_a_lone_name_never_lands_in_the_user_s_own_credential_store(
    tmp_path, auth_server, url, settings
):
    """Regression: `https://TOKEN@host` left the token in the user's own helper for good.

    No helper of theirs held the name, so the private file answered it with an
    empty password. The server took it, and git's `credential approve` then
    stored it in every helper configured: plaintext ~/.git-credentials, Windows
    Credential Manager or the macOS Keychain, reused by every later git command
    against that host. The global store here stands in for those.
    """
    host, seen = auth_server
    before = "https://someone:else@elsewhere.invalid\n"
    store = _user_store(tmp_path, before)
    handler = _handler(tmp_path, **settings)

    with contextlib.suppress(GitError):  # no real repository: authenticating is enough
        handler.clone(url.format(host=host), "r")

    assert f"{URL_TOKEN}:" in seen, f"the server never got the token: {seen}"
    assert store.read_text(encoding="utf-8") == before, "the token outlived the run"


@needs_git
def test_a_helper_that_answers_every_user_does_not_open_the_store_to_git(tmp_path, git_server):
    """Regression: a helper answering any name left `TOKEN:<its password>` in the store.

    Asked whether the user's helpers hold the URL's user, the `echo password=...`
    kind always says yes. They were then left configured for the clone itself,
    and after it succeeded git's `credential approve` saved the caller's URL token
    in every one of them, including the store behind it.
    """
    host, seen, accepted = git_server
    accepted.add((URL_TOKEN, "from-env"))
    before = "https://someone:else@elsewhere.invalid\n"
    store = _user_store(tmp_path, before, ahead=ANY_USER_HELPER)

    dest = _handler(tmp_path).clone(f"http://{URL_TOKEN}@{host}/r.git", "r")

    assert _git(dest, "rev-parse", "HEAD"), "the clone did not succeed"
    assert f"{URL_TOKEN}:from-env" in seen, "not what git itself would have sent"
    assert store.read_text(encoding="utf-8") == before, "the token landed in the user's store"


@needs_git
def test_a_clone_button_url_clones_with_the_password_the_user_s_manager_holds(tmp_path, git_server):
    """The case asking the user's helpers is for: `https://alice@bitbucket.org/...`."""
    host, seen, accepted = git_server
    accepted.add(("alice", "from-manager"))
    held = f"http://bob:wrong@{host}\nhttp://alice:from-manager@{host}\n"
    store = _user_store(tmp_path, held)

    dest = _handler(tmp_path).clone(f"http://alice@{host}/r.git", "r")

    assert _git(dest, "rev-parse", "HEAD"), "the clone did not succeed"
    assert "alice:from-manager" in seen
    assert store.read_text(encoding="utf-8") == held, "git wrote back into the user's store"


@needs_git
@pytest.mark.parametrize(
    ("url", "settings", "login", "held"),
    [
        (
            "http://alice@{host}/r.git",
            {},
            ("alice", "from-manager"),
            "http://alice:from-manager@{host}/r.git\n",
        ),
        ("http://{host}/r.git", {"token": TOKEN}, ("oauth2", TOKEN), ""),
        ("http://bob:pw@{host}/r.git", {}, ("bob", "pw"), ""),
    ],
    ids=["clone-button", "settings-token", "url-password"],
)
def test_a_host_that_scopes_credentials_by_path_still_gets_them(
    tmp_path, git_server, caplog, url, settings, login, held
):
    """Regression: with credential.useHttpPath on, no clone or fetch sent a credential.

    Git for Windows turns it on for https://dev.azure.com in its system config, so
    the Azure DevOps clone-button URL failed. git then asks helpers with the path
    too, and the private file's entry, written for scheme and host, never matched.
    The user's manager here holds alice's password for that path, as GCM does.
    """
    host, seen, accepted = git_server
    accepted.add(login)
    url, held = url.format(host=host), held.format(host=host)
    store = _user_store(tmp_path, held)
    with Path(os.environ["GIT_CONFIG_GLOBAL"]).open("a", encoding="utf-8", newline="\n") as cfg:
        cfg.write(f'[credential "http://{host}"]\n\tuseHttpPath = true\n')
    handler = _handler(tmp_path, **settings)

    dest = handler.clone(url, "r")
    assert _git(dest, "rev-parse", "HEAD"), "the clone did not succeed"
    seen.clear()
    with caplog.at_level(logging.WARNING, logger="graphforge.git.clone"):
        handler.update("r", url=url)

    assert ":".join(login) in seen, f"the fetch sent no credential: {seen}"
    assert "pull for r reported" not in caplog.text
    assert store.read_text(encoding="utf-8") == held, "git wrote back into the user's store"


@needs_git
def test_the_user_a_helper_answers_with_is_the_one_sent(tmp_path):
    """A helper may answer with a user of its own, as git would then send."""
    renames = (
        '\thelper = "!f() { test \\"$1\\" = get && echo username=bot && echo password=pw; }; f"\n'
    )
    _user_store(tmp_path, ahead=renames)
    with _handler(tmp_path)._credentials_for("https://alice@example.invalid/g/r.git") as flags:
        entry = Path(_helper_path(flags)).read_text(encoding="utf-8")
    assert entry == "https://bot:pw@example.invalid\n"


@needs_git
def test_a_hung_helper_cannot_hold_the_probe_past_its_timeout(tmp_path, monkeypatch):
    """Regression: on Windows the probe waited for a hung helper however short its timeout.

    The helper's child kept the captured stderr pipe open, and once git was killed
    subprocess waited for that pipe to close. The probe's timeout is cut to 2 s here.
    """
    Path(os.environ["GIT_CONFIG_GLOBAL"]).write_text(
        '[credential]\n\thelper = "!f() { sleep 20; }; f"\n', encoding="utf-8", newline="\n"
    )
    real = subprocess.run

    def run(args, **kwargs):
        if list(args[:3]) == ["git", "credential", "fill"]:
            kwargs["timeout"] = 2
        return real(args, **kwargs)

    monkeypatch.setattr("graphforge.git.clone.subprocess.run", run)
    started = time.monotonic()
    with _handler(tmp_path)._credentials_for("https://alice@example.invalid/g/r.git") as flags:
        elapsed = time.monotonic() - started
        entry = Path(_helper_path(flags)).read_text(encoding="utf-8")
    assert elapsed < 12, f"the probe took {elapsed:.0f}s against a 2s timeout"
    assert entry == "https://alice:@example.invalid\n"


def test_a_user_only_url_does_not_borrow_another_user_s_secret(tmp_path):
    handler = _handler(tmp_path)
    target = "https://example.invalid/g/r.git"
    sources = ("https://bob@example.invalid/g/r.git", "https://alice:s3cret@example.invalid/")
    with handler._credentials_for(target, *sources) as flags:
        assert Path(_helper_path(flags)).read_text(encoding="utf-8") == (
            "https://bob:@example.invalid\n"
        )
        assert "s3cret" not in " ".join(flags)


@needs_git
@pytest.mark.parametrize("held", [True, False], ids=["held", "not-held"])
def test_a_settings_username_without_a_password_names_the_user(tmp_path, held):
    """It used to write an entry git could never match.

    The password the user's helper holds for that name goes through the private
    file; failing one, the name goes out alone.
    """
    _user_store(tmp_path, "https://ci:from-manager@example.invalid\n" if held else "")
    handler = _handler(tmp_path, username="ci")
    with handler._credentials_for("https://example.invalid/g/r.git") as flags:
        assert flags[:2] == ["-c", "credential.helper="]
        assert Path(_helper_path(flags)).read_text(encoding="utf-8") == (
            f"https://ci:{'from-manager' if held else ''}@example.invalid\n"
        )
        answer, stderr = _fill(flags)
    assert answer.get("username") == "ci", stderr
    assert answer.get("password") == ("from-manager" if held else "")


@needs_git
def test_an_askpass_program_is_not_taken_for_a_helper_that_holds_the_user(tmp_path, monkeypatch):
    """VS Code's terminal sets GIT_ASKPASS: what it answers is no stored password.

    Asking it would also put up a prompt in the middle of an unattended run.
    """
    log = tmp_path / "askpass.log"
    script = tmp_path / "askpass.sh"
    script.write_text(
        f'#!/bin/sh\necho "$1" >> "{log.as_posix()}"\necho from-askpass\n',
        encoding="utf-8",
        newline="\n",
    )
    script.chmod(0o755)
    monkeypatch.setenv("GIT_ASKPASS", script.as_posix())
    handler = _handler(tmp_path)
    with handler._credentials_for("https://alice@example.invalid/g/r.git") as flags:
        assert "credential.helper=" in flags
    assert not log.exists(), "the askpass program was run"


def test_a_user_with_a_control_character_is_never_asked_about(tmp_path):
    """A newline in the name would start a new line of `git credential fill` input.

    `a\\nhost=other.invalid` would ask for other.invalid's password, which the
    private file would then send to this host.
    """
    _user_store(tmp_path, "https://a:other-host@other.invalid\n")
    handler = _handler(tmp_path)
    url = "https://a%0Ahost=other.invalid@example.invalid/g/r.git"
    with handler._credentials_for(url) as flags:
        assert Path(_helper_path(flags)).read_text(encoding="utf-8") == (
            "https://a%0Ahost%3Dother.invalid:@example.invalid\n"
        )


@needs_git
@pytest.mark.parametrize("held", [True, False], ids=["held", "not-held"])
def test_every_credential_file_is_removed_afterwards(tmp_path, held):
    _user_store(tmp_path, f"https://{URL_TOKEN}:pw@example.invalid\n" if held else "")
    handler = _handler(tmp_path)
    with handler._credentials_for(f"https://{URL_TOKEN}@example.invalid/g/r.git") as flags:
        path = _helper_path(flags)
        assert (
            Path(path)
            .read_text(encoding="utf-8")
            .endswith(f":{'pw' if held else ''}@example.invalid\n")
        )
    assert not os.path.exists(path), "a credential file outlived the operation"


def test_url_credentials_are_never_sent_to_another_host(tmp_path):
    handler = _handler(tmp_path)
    target = "https://example.invalid/g/r.git"
    with handler._credentials_for(target, "https://alice:s3cret@other.invalid/g/r.git") as flags:
        assert flags == []


@pytest.mark.parametrize(
    ("url", "secret"),
    [
        ("https://alice:s3cret@example.invalid/g/r.git", "s3cret"),
        (f"https://{URL_TOKEN}@example.invalid/g/r.git", URL_TOKEN),
    ],
)
def test_url_secrets_are_scrubbed_from_git_errors(tmp_path, url, secret):
    handler = _handler(tmp_path)
    handler._run = lambda args, cwd=None, timeout=900: (  # type: ignore[method-assign]
        128,
        "",
        f"fatal: unable to access '{url}': The requested URL returned error: 403",
    )
    with pytest.raises(GitError) as err:
        handler.clone(url, "r")
    assert secret not in str(err.value)


@pytest.mark.parametrize(
    "url",
    [f"https://{URL_TOKEN}@example.invalid", f"https://{URL_TOKEN}[x@example.invalid"],
)
def test_a_name_derived_from_a_token_url_does_not_carry_the_token(tmp_path, url):
    handler = _handler(tmp_path)
    _recording(handler)
    path = handler.resolve({"url": url})
    assert URL_TOKEN not in path, "the token became the clone directory's name"


# -- existing clones -----------------------------------------------------------
@needs_git
def test_remote_url_never_returns_stored_credentials(tmp_path, hermetic_git):
    handler = _handler(tmp_path)
    dest = _clone_with_origin(handler, "r", "https://alice:s3cret@example.invalid/g/r.git")
    assert handler.remote_url(str(dest)) == "https://example.invalid/g/r.git"


@needs_git
def test_update_moves_credentials_out_of_an_existing_origin(tmp_path, hermetic_git):
    url = "https://alice:s3cret@example.invalid/g/r.git"
    handler = _handler(tmp_path)
    dest = _clone_with_origin(handler, "r", url)
    calls = _recording(handler, passthrough=True)

    handler.clone_or_update(url, "r")

    assert _git(dest, "config", "--get", "remote.origin.url") == "https://example.invalid/g/r.git"
    assert "s3cret" not in (dest / ".git" / "config").read_text(encoding="utf-8")
    fetch = next(c for c in calls if "fetch" in c["args"])
    assert fetch["helper"] == "https://alice:s3cret@example.invalid\n", "fetch lost its credentials"
    assert not any("s3cret" in " ".join(c["args"]) for c in calls), (
        "secret passed on a command line"
    )


@needs_git
def test_update_keeps_an_origin_that_is_the_only_copy_of_its_secret(tmp_path, hermetic_git, caplog):
    """Stripping it would leave the next run nothing to authenticate with."""
    handler = _handler(tmp_path)
    dest = _clone_with_origin(handler, "r", "https://alice:s3cret@example.invalid/g/r.git")
    calls = _recording(handler, passthrough=True)

    with caplog.at_level(logging.WARNING, logger="graphforge.git.clone"):
        handler.clone_or_update("https://example.invalid/g/r.git", "r")

    assert "s3cret" in (dest / ".git" / "config").read_text(encoding="utf-8")
    assert "embeds credentials" in caplog.text
    assert "s3cret" not in caplog.text
    # points at the settings and the clean URL, not at putting secrets in the URL
    assert "GIT_TOKEN" in caplog.text
    assert "git remote set-url origin https://example.invalid/g/r.git" in caplog.text
    assert "repository URL" not in caplog.text
    fetch = next(c for c in calls if "fetch" in c["args"])
    assert fetch["helper"] == "https://alice:s3cret@example.invalid\n"
    assert not any("s3cret" in " ".join(c["args"]) for c in calls)


@needs_git
def test_update_keeps_the_stored_secret_when_the_url_names_only_the_user(
    tmp_path, hermetic_git, caplog
):
    """Regression: a user-only URL erased the only copy of the password, then sent none."""
    handler = _handler(tmp_path)
    dest = _clone_with_origin(handler, "r", "https://alice:s3cret@example.invalid/g/r.git")
    calls = _recording(handler, passthrough=True)

    with caplog.at_level(logging.WARNING, logger="graphforge.git.clone"):
        handler.clone_or_update("https://alice@example.invalid/g/r.git", "r")

    assert _git(dest, "config", "--get", "remote.origin.url") == (
        "https://alice:s3cret@example.invalid/g/r.git"
    )
    assert "embeds credentials" in caplog.text
    for command in ("fetch", "pull"):
        call = next(c for c in calls if command in c["args"])
        assert call["helper"] == "https://alice:s3cret@example.invalid\n", f"{command} lost it"
    assert not any("s3cret" in " ".join(c["args"]) for c in calls)


@needs_git
@pytest.mark.parametrize(
    ("url", "warning"),
    [
        ("https://bob@example.invalid/g/r.git", "embeds credentials. Put them in the git settings"),
        (
            "https://bob:pw@example.invalid/g/r.git",
            "embeds credentials. Put them in the git settings",
        ),
        (
            "https://alice:typo@example.invalid/g/r.git",
            "differ from the password in the repository URL, and git sends the stored ones",
        ),
    ],
    ids=["other-user", "other-user-with-password", "other-password"],
)
def test_update_keeps_a_stored_secret_this_run_does_not_bring_back(
    tmp_path, hermetic_git, caplog, url, warning
):
    """Regression: another user in the URL, or a mistyped password, erased the stored one.

    Each counted as bringing the stored credential back, so it was taken out of
    .git/config before anything had shown the new one works. With another user's
    name alone, alice's password was simply gone, and the next run with the plain
    URL had nothing to authenticate with.
    """
    handler = _handler(tmp_path)
    dest = _clone_with_origin(handler, "r", "https://alice:s3cret@example.invalid/g/r.git")
    _recording(handler, passthrough=True)

    with caplog.at_level(logging.INFO, logger="graphforge.git.clone"):
        handler.clone_or_update(url, "r")

    assert _git(dest, "config", "--get", "remote.origin.url") == (
        "https://alice:s3cret@example.invalid/g/r.git"
    )
    assert "Removed the credentials" not in caplog.text
    assert warning in caplog.text
    assert not any(s in caplog.text for s in ("s3cret", "typo", ":pw"))


@needs_git
@pytest.mark.parametrize(
    ("url", "source"),
    [
        ("https://alice@example.invalid/g/r.git", "GIT_PASSWORD in the git settings"),
        ("https://alice:typo@example.invalid/g/r.git", "the password in the repository URL"),
    ],
    ids=["url-names-the-user", "url-password"],
)
def test_the_warning_names_where_the_other_password_comes_from(tmp_path, caplog, url, source):
    """Regression: with GIT_TOKEN set too, GIT_PASSWORD's password was put down to the URL.

    The source was found by comparing with what the settings alone would send,
    which is the token whenever there is one, not GIT_USERNAME's password.
    """
    handler = _handler(tmp_path, token=TOKEN, username="alice", password="new")
    dest = _clone_with_origin(handler, "r", "https://alice:s3cret@example.invalid/g/r.git")
    _recording(handler, passthrough=True)

    with caplog.at_level(logging.WARNING, logger="graphforge.git.clone"):
        handler.clone_or_update(url, "r")

    assert "s3cret" in _git(dest, "config", "--get", "remote.origin.url")
    assert f"embeds credentials that differ from {source}," in caplog.text


@needs_git
def test_update_strips_a_stored_name_when_the_run_logs_in_as_that_user(tmp_path, caplog):
    """A stored URL naming a user and no password holds nothing the run does not bring."""
    handler = _handler(tmp_path)
    dest = _clone_with_origin(handler, "r", "https://alice@example.invalid/g/r.git")
    _recording(handler, passthrough=True)

    with caplog.at_level(logging.INFO, logger="graphforge.git.clone"):
        handler.clone_or_update("https://alice:pw@example.invalid/g/r.git", "r")

    assert _git(dest, "config", "--get", "remote.origin.url") == "https://example.invalid/g/r.git"
    assert "Removed the credentials" in caplog.text
    assert "embeds credentials" not in caplog.text


@needs_git
def test_update_strips_a_lone_user_that_the_url_brings_back_every_run(tmp_path, hermetic_git):
    url = f"https://{URL_TOKEN}@example.invalid/g/r.git"
    handler = _handler(tmp_path)
    dest = _clone_with_origin(handler, "r", url)
    calls = _recording(handler, passthrough=True)

    handler.clone_or_update(url, "r")

    assert _git(dest, "config", "--get", "remote.origin.url") == "https://example.invalid/g/r.git"
    fetch = next(c for c in calls if "fetch" in c["args"])
    assert fetch["helper"] == f"https://{URL_TOKEN}:@example.invalid\n"
    assert "credential.helper=" in fetch["args"], "the user's own helpers could store the token"
    assert not any(URL_TOKEN in " ".join(c["args"]) for c in calls)


@needs_git
@pytest.mark.parametrize(
    ("settings", "userinfo"),
    [({"token": TOKEN}, f"oauth2:{TOKEN}"), ({"username": "ci", "password": "pw"}, "ci:pw")],
    ids=["token", "password"],
)
def test_update_strips_the_settings_credential_that_v0_2_0_stored(
    tmp_path, caplog, settings, userinfo
):
    """v0.2.0 wrote the settings' own credential into every clone's origin URL.

    The settings bring it back on every run, so taking it out loses nothing. It
    used to stay in .git/config for good, with a warning on each run to put it
    in the settings, where it already was.
    """
    handler = _handler(tmp_path, **settings)
    dest = _clone_with_origin(handler, "r", f"https://{userinfo}@example.invalid/g/r.git")
    calls = _recording(handler, passthrough=True)

    with caplog.at_level(logging.INFO, logger="graphforge.git.clone"):
        handler.clone_or_update("https://example.invalid/g/r.git", "r")

    assert _git(dest, "config", "--get", "remote.origin.url") == "https://example.invalid/g/r.git"
    assert "Removed the credentials" in caplog.text
    assert "embeds credentials" not in caplog.text
    fetch = next(c for c in calls if "fetch" in c["args"])
    assert fetch["helper"] == f"https://{userinfo}@example.invalid\n"


@needs_git
def test_a_stored_token_other_than_the_settings_one_is_kept_and_named(tmp_path, caplog):
    """A token rotated since v0.2.0 stored the old one in the origin URL.

    git sends a password in the URL without asking any helper, so the stored
    token is what goes out; it may also be a per-repository one, the only copy.
    It stays, and the warning says it wins over GIT_TOKEN and how to switch.
    """
    rotated = "glpat-ROTATED0000000000"
    handler = _handler(tmp_path, token=rotated)
    dest = _clone_with_origin(handler, "r", f"https://oauth2:{TOKEN}@example.invalid/g/r.git")
    _recording(handler, passthrough=True)

    with caplog.at_level(logging.WARNING, logger="graphforge.git.clone"):
        handler.clone_or_update("https://example.invalid/g/r.git", "r")

    assert TOKEN in _git(dest, "config", "--get", "remote.origin.url")
    assert "differ from GIT_TOKEN" in caplog.text
    assert "git remote set-url origin https://example.invalid/g/r.git" in caplog.text
    assert TOKEN not in caplog.text and rotated not in caplog.text


@needs_git
def test_a_password_for_another_host_leaves_the_stored_secret_alone(tmp_path, caplog):
    """Two repos of the same name share one clone directory, whatever their host.

    The call's password authenticates other.invalid; the stored one is still the
    only thing that authenticates this clone.
    """
    handler = _handler(tmp_path)
    dest = _clone_with_origin(handler, "r", "https://alice:s3cret@example.invalid/g/r.git")
    calls = _recording(handler, passthrough=True)

    with caplog.at_level(logging.WARNING, logger="graphforge.git.clone"):
        handler.clone_or_update("https://bob:pw@other.invalid/g/r.git", "r")

    assert "s3cret" in _git(dest, "config", "--get", "remote.origin.url")
    assert "embeds credentials" in caplog.text
    fetch = next(c for c in calls if "fetch" in c["args"])
    assert fetch["helper"] == "https://alice:s3cret@example.invalid\n", "bob's went to this host"


@needs_git
def test_a_token_from_an_insteadof_rule_is_not_taken_for_a_stored_one(
    tmp_path, hermetic_git, caplog
):
    """`remote get-url` applies insteadOf, so the token shows up without being in .git/config."""
    Path(os.environ["GIT_CONFIG_GLOBAL"]).write_text(
        f'[url "https://{URL_TOKEN}@example.invalid/"]\n\tinsteadOf = https://example.invalid/\n',
        encoding="utf-8",
    )
    handler = _handler(tmp_path)
    dest = _clone_with_origin(handler, "r", "https://example.invalid/g/r.git")
    _recording(handler, passthrough=True)

    # INFO too: a clean origin "stripped" again is a no-op that only the log shows
    with caplog.at_level(logging.INFO, logger="graphforge.git.clone"):
        handler.clone_or_update("https://alice:s3cret@example.invalid/g/r.git", "r")

    assert "embeds credentials" not in caplog.text
    assert "Removed the credentials" not in caplog.text, "an insteadOf token taken for a stored one"
    assert _git(dest, "config", "--get", "remote.origin.url") == "https://example.invalid/g/r.git"
    assert handler.remote_url(str(dest)) == "https://example.invalid/g/r.git"


# -- the graph and the logs ----------------------------------------------------
def _ingestor(tmp_path, writer):
    from graphforge.core.config import GitSettings
    from graphforge.git.ingest import GitIngestor

    return GitIngestor(writer, GitSettings(repo_dir=str(tmp_path / "repos")))


def test_the_graph_stores_the_repository_url_without_credentials(tmp_path, monkeypatch):
    """:Repository.url is on show in the dashboard's label explorer."""
    from graphforge.core.neo4j_writer import Neo4jWriter
    from graphforge.git import history as history_mod

    monkeypatch.setattr(history_mod, "head_commit", lambda path: "")
    out = tmp_path / "out.cypher"
    with Neo4jWriter(settings=None, emit_path=str(out)) as writer:
        ingestor = _ingestor(tmp_path, writer)
        ingestor.handler.resolve = lambda spec, default_branch="main": str(tmp_path)  # type: ignore[method-assign]
        ingestor.ingest_repo(
            {"url": "https://alice:s3cret@example.invalid/g/r.git", "name": "r"},
            with_structure=False,
            with_history=False,
        )
    text = out.read_text(encoding="utf-8")
    assert "s3cret" not in text and "alice:" not in text
    assert "https://example.invalid/g/r.git" in text


@pytest.mark.parametrize(
    ("url", "secret"),
    [
        (f"https://{URL_TOKEN}@example.invalid/g/r.git", URL_TOKEN),
        # Regression: urlsplit raised on the "]", and the URL was logged as it was
        ("https://alice:s3cr]et@example.invalid/g/r.git", "s3cr]et"),
        ("https://alice:s3/cret@example.invalid/g/r.git", "s3/cret"),
    ],
)
def test_a_failed_repo_is_logged_without_its_credentials(tmp_path, caplog, url, secret):
    from graphforge.core.neo4j_writer import Neo4jWriter

    def fail(spec, default_branch="main"):
        raise GitError("clone failed for r: ***")

    with Neo4jWriter(settings=None, emit_path=str(tmp_path / "out.cypher")) as writer:
        ingestor = _ingestor(tmp_path, writer)
        ingestor.handler.resolve = fail  # type: ignore[method-assign]
        with caplog.at_level(logging.WARNING, logger="graphforge.git.ingest"):
            stats = ingestor.ingest([{"url": url}])
    assert stats["failed"] == 1
    assert secret not in caplog.text
    assert "https://example.invalid/g/r.git" in caplog.text


# -- scrubbing and prompts -----------------------------------------------------
@pytest.mark.parametrize("secret", [TOKEN, PASSWORD])
def test_git_output_is_scrubbed_before_it_becomes_an_exception(tmp_path, secret):
    kwargs = {"token": secret} if secret is TOKEN else {"username": "ci", "password": secret}
    handler = _handler(tmp_path, **kwargs)
    noisy = f"fatal: could not read from 'https://oauth2:{secret}@gitlab.example/g/r.git'"
    handler._run = lambda args, cwd=None, timeout=900: (  # type: ignore[method-assign]
        1,
        "",
        noisy,
    )

    with pytest.raises(GitError) as err:
        handler.clone("https://gitlab.example/g/r.git", "repo")
    assert secret not in str(err.value), "the secret survived into the error message"
    assert "***" in str(err.value)


def test_scrub_also_catches_the_url_encoded_form(tmp_path):
    handler = _handler(tmp_path, username="ci", password=PASSWORD)
    encoded = "p%40ss%20word%2Fwith%2Bspecials"
    assert PASSWORD not in handler._scrub(f"remote: rejected {PASSWORD}")
    assert encoded not in handler._scrub(f"url https://ci:{encoded}@x/")


def test_interactive_prompts_are_disabled_so_a_capture_cannot_hang(tmp_path, monkeypatch):
    """stdout/stderr are captured, so a credential prompt would block on nothing."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs.get("env") or {})

        class _P:
            returncode, stdout, stderr = 0, "", ""

        return _P()

    monkeypatch.setattr("graphforge.git.clone.subprocess.run", fake_run)
    _handler(tmp_path).check_git()
    assert captured.get("GIT_TERMINAL_PROMPT") == "0"
