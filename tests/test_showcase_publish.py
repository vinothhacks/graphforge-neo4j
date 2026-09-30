"""scripts/showcase/render.py's publish stage, with gh mocked.

The stage created a GitHub release through gh and then crashed on Windows: the
notes temp file's descriptor was never closed, so unlinking it raised
PermissionError [WinError 32], and a failed release leaked the file. It also
skipped gh whenever any configured host was logged out, even with github.com
logged in, because a bare ``gh auth status`` checks every host.
"""

from __future__ import annotations

import importlib.util
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

RENDER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "showcase" / "render.py"
FAKE_GH = "C:/fake/gh.exe"
# Every configured host logged in, so the notes tests pass or fail on the notes alone,
# whatever form the gh auth check takes.
ALL_HOSTS = ("github.com", "ghe.example.invalid")


def _load():
    spec = importlib.util.spec_from_file_location("showcase_render", RENDER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


render = _load()


class FakeGh:
    """Stands in for subprocess.run. `logged_in` is the set of hosts gh is authenticated to;
    one extra configured host (ghe.example.invalid) is always logged out, so a bare
    `gh auth status` fails the way real gh does."""

    def __init__(self, logged_in=("github.com",), release_fails=False):
        self.logged_in = set(logged_in)
        self.release_fails = release_fails
        self.release_calls = []
        self.notes_seen = None

    def __call__(self, cmd, **kw):
        assert cmd[0] == FAKE_GH, cmd
        if cmd[1:3] == ["auth", "status"]:
            if "--hostname" in cmd:
                host = cmd[cmd.index("--hostname") + 1]
                ok = host in self.logged_in
            else:
                ok = self.logged_in >= {"github.com", "ghe.example.invalid"}
            return SimpleNamespace(returncode=0 if ok else 1, stdout="", stderr="")
        assert cmd[1:3] == ["release", "create"], cmd
        self.release_calls.append((cmd, kw))
        src = cmd[cmd.index("--notes-file") + 1]
        # Capture the notes the way gh would read them: stdin for "-", else the file.
        self.notes_seen = kw.get("input") if src == "-" else Path(src).read_text("utf-8")
        if self.release_fails:
            raise subprocess.CalledProcessError(1, cmd, output="", stderr="HTTP 422")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.fixture
def env(tmp_path, monkeypatch):
    out = tmp_path / "out"
    (out / "images").mkdir(parents=True)
    final = out / "graphforge-showcase.mp4"
    final.write_bytes(b"\0")
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    monkeypatch.setattr(render, "OUT", out)
    monkeypatch.setattr(render, "FINAL", final)
    monkeypatch.setattr(render, "_git", lambda *a: "" if a[0] == "status" else "abc123")
    monkeypatch.setattr(render.shutil, "which", lambda name: FAKE_GH if name == "gh" else None)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    # Any temp file the stage makes lands here, so leaks are visible.
    monkeypatch.setattr(tempfile, "tempdir", str(tmpdir))
    return SimpleNamespace(tmpdir=tmpdir, monkeypatch=monkeypatch)


def _install(env, fake):
    env.monkeypatch.setattr(render.subprocess, "run", fake)


# Release notes go through stdin, no temp file ----------------------------------------------
def test_successful_release_does_not_crash_and_leaves_no_temp_file(env):
    fake = FakeGh(logged_in=ALL_HOSTS)
    _install(env, fake)
    render.stage_publish(None)  # must not raise PermissionError [WinError 32] on Windows
    assert len(fake.release_calls) == 1
    assert fake.notes_seen == render.RELEASE_NOTES
    assert list(env.tmpdir.iterdir()) == []


def test_failed_release_leaves_no_temp_file(env):
    fake = FakeGh(logged_in=ALL_HOSTS, release_fails=True)
    _install(env, fake)
    with pytest.raises(SystemExit):
        render.stage_publish(None)
    assert fake.notes_seen == render.RELEASE_NOTES
    assert list(env.tmpdir.iterdir()) == []  # no notes file left behind by the failure


# gh auth status is scoped to github.com -----------------------------------------------------
def test_gh_used_when_github_com_logged_in_but_another_host_is_not(env, capsys):
    fake = FakeGh(logged_in=("github.com",))
    _install(env, fake)
    render.stage_publish(None)
    assert len(fake.release_calls) == 1, capsys.readouterr().out  # the logged-out host is ignored


def test_gh_skipped_when_github_com_itself_logged_out(env, capsys):
    fake = FakeGh(logged_in=("ghe.example.invalid",))
    _install(env, fake)
    render.stage_publish(None)
    assert fake.release_calls == []
    assert "Manual steps" in capsys.readouterr().out


def test_release_targets_the_host_that_was_checked(env):
    fake = FakeGh()
    _install(env, fake)
    render.stage_publish(None)
    cmd, _ = fake.release_calls[0]
    assert cmd[cmd.index("--repo") + 1] == f"github.com/{render.GITHUB_REPO}"
