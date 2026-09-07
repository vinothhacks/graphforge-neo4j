"""A git secret must never reach argv, the remote URL, a log line or an exception.

Credentials used to be spliced into the remote URL. That put the token in `ps`
output for every local user, wrote it into `.git/config`, and let git echo it
back inside the error text that `GitError` then carried up to the console.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from graphforge.git.clone import GitError, GitHandler

TOKEN = "glpat-NOTAREALTOKEN12345"
PASSWORD = "p@ss word/with+specials"


def _handler(tmp_path, **kwargs) -> GitHandler:
    return GitHandler(str(tmp_path / "repos"), **kwargs)


def test_the_secret_goes_in_a_file_that_is_deleted_afterwards(tmp_path):
    handler = _handler(tmp_path, token=TOKEN)
    with handler._credentials_for("https://gitlab.example/group/repo.git") as flags:
        assert flags[:2] == ["-c", "credential.helper="], "inherited helpers not cleared"
        path = flags[-1].split("--file=", 1)[1]
        assert os.path.exists(path)
        content = Path(path).read_text(encoding="utf-8")
        assert TOKEN in content, "the helper file must actually carry the credential"
        assert content.startswith("https://oauth2:")
        assert "gitlab.example" in content
    assert not os.path.exists(path), "credential file outlived the operation"


def test_a_port_is_kept_so_the_credential_matches_the_remote(tmp_path):
    handler = _handler(tmp_path, username="ci", password=PASSWORD)
    with handler._credentials_for("https://git.example:8443/g/r.git") as flags:
        content = Path(flags[-1].split("--file=", 1)[1]).read_text(encoding="utf-8")
    assert "git.example:8443" in content


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
