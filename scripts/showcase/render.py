#!/usr/bin/env python
"""Render the graphforge showcase video. Content lives in scenes.py; see README.md.

Stages, in order. Each is idempotent and can run alone with --only:

  preflight  tools on PATH, model IDs exist in OpenRouter's live catalogue, ports and DB
  tts        narration -> out/audio/*.mp3, measured durations -> out/timeline.json
  record     Playwright drives GitHub and the dashboard, holding each scene for its narration
             and writing the ACTUAL start offset of every scene to the timeline
  images     reference images via OpenRouter (skips cleanly without OPENROUTER_API_KEY)
  clip       ffmpeg pan/zoom intro built from the images and a dashboard frame
  assemble   mux narration at the recorded offsets, prepend the intro, self-check with ffprobe
  publish    (--publish only) commit scripts, push, create the GitHub release with the assets

The OpenRouter key is read from the environment only. It is never written, echoed or logged.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import NoReturn

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE / "out"
sys.path.insert(0, str(HERE))

from scenes import (  # noqa: E402
    BRANCH,
    DASHBOARD_URL,
    GITHUB_REPO,
    IMAGE_PROMPTS,
    MODELS,
    SCENES,
    TITLES,
    UI_PORT,
    VOICE,
    VOICE_RATE,
)

PAD_S = 0.4  # breathing room after each narration clip
STAGES = ["preflight", "tts", "record", "images", "clip", "assemble", "publish"]
RELEASE_TAG = "showcase-v1"
FINAL = OUT / "graphforge-showcase.mp4"


# ----------------------------------------------------------------------------- utils
def log(msg: str) -> None:
    print(f"[showcase] {msg}", flush=True)


def die(msg: str) -> NoReturn:
    print(f"[showcase] ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(2)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    kw.setdefault("check", True)
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    try:
        return subprocess.run(cmd, **kw)
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or exc.stdout or "")[-1500:]
        die(f"command failed ({cmd[0]}):\n{tail}")


def clean_env() -> dict[str, str]:
    """S-1: nothing that names a database may leak into a child process from the shell."""
    return {k: v for k, v in os.environ.items() if not k.startswith(("NEO4J_", "DB_", "GF_"))}


def ffprobe_duration(path: Path) -> float:
    out = run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]
    ).stdout.strip()
    return float(out)


def ffprobe_streams(path: Path) -> list[str]:
    return run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(path),
        ]
    ).stdout.split()


def load_timeline() -> dict:
    p = OUT / "timeline.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_timeline(tl: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "timeline.json").write_text(json.dumps(tl, indent=2), encoding="utf-8")


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def narrated() -> list[tuple[int, dict]]:
    return [(i, s) for i, s in enumerate(SCENES) if s.get("narration")]


def scene_files(i: int, s: dict) -> tuple[Path, Path]:
    stem = f"{i:02d}-{s['id']}"
    return OUT / "audio" / f"{stem}.mp3", OUT / "frames" / f"{stem}.png"


# ------------------------------------------------------------------------- preflight
def stage_preflight(args) -> None:
    problems: list[str] = []
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            problems.append(f"{tool} not on PATH -- install ffmpeg (https://ffmpeg.org)")
    for mod, pipname in (("edge_tts", "edge-tts"), ("playwright", "playwright")):
        try:
            __import__(mod)
        except ImportError:
            problems.append(
                f"python package {pipname} missing -- pip install -r scripts/showcase/requirements.txt"
            )

    have_key = bool(os.environ.get("OPENROUTER_API_KEY"))
    log(f"OPENROUTER_API_KEY in environment: {'yes' if have_key else 'no (image stage will skip)'}")

    # Model IDs against the LIVE catalogue. No key needed for this endpoint.
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/models", headers={"User-Agent": "graphforge-showcase"}
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            ids = {m["id"] for m in json.load(r)["data"]}
        log(f"OpenRouter catalogue: {len(ids)} models")
        for role, mid in MODELS.items():
            if mid is None:
                log(f"  model[{role}] = None (stage uses ffmpeg instead)")
            elif mid in ids:
                log(f"  model[{role}] = {mid}: available")
            else:
                problems.append(
                    f"model[{role}] = {mid!r} is NOT in OpenRouter's catalogue -- fix scenes.MODELS"
                )
    except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
        log(
            f"  could not read the OpenRouter catalogue ({type(exc).__name__}); model IDs unverified"
        )

    if any(s["kind"] == "dashboard" for s in SCENES):
        if not (REPO / "audit.env").exists():
            problems.append(
                "audit.env missing at repo root -- the dashboard is started from it (S-1)"
            )
        if not port_open(7687):
            problems.append("Neo4j not reachable on 127.0.0.1:7687 -- start the playground stack")
        if port_open(UI_PORT):
            problems.append(
                f"port {UI_PORT} is already in use -- stop whatever is on it or change scenes.UI_PORT"
            )

    for p in problems:
        print(f"[showcase]   ! {p}", file=sys.stderr)
    if problems:
        die(f"{len(problems)} preflight problem(s)")
    log("preflight OK")


# ------------------------------------------------------------------------------- tts
async def _synth(text: str, path: Path) -> None:
    import edge_tts

    await edge_tts.Communicate(text, VOICE, rate=VOICE_RATE).save(str(path))


def stage_tts(args) -> None:
    (OUT / "audio").mkdir(parents=True, exist_ok=True)
    tl = load_timeline()
    total = 0.0
    for i, s in narrated():
        mp3, _ = scene_files(i, s)
        if args.force or not mp3.exists():
            asyncio.run(_synth(s["narration"], mp3))
        dur = ffprobe_duration(mp3)
        total += dur
        tl.setdefault(s["id"], {})
        tl[s["id"]].update({"title": s["title"], "audio_s": round(dur, 3)})
        log(f"  {mp3.name:32s} {dur:6.2f}s")
    save_timeline(tl)
    log(f"tts OK: {len(narrated())} clips, {total:.1f}s of narration")


# ---------------------------------------------------------------------------- record
_TITLE_JS = """
(title) => {
  let el = document.getElementById('__gf_title');
  if (!el) {
    el = document.createElement('div');
    el.id = '__gf_title';
    Object.assign(el.style, {
      position: 'fixed', left: '40px', bottom: '40px', zIndex: 2147483647,
      background: 'rgba(15,17,26,0.90)', color: '#fff', padding: '14px 22px',
      borderRadius: '10px', font: '600 26px/1.2 "Segoe UI", system-ui, sans-serif',
      letterSpacing: '0.2px', boxShadow: '0 6px 24px rgba(0,0,0,0.35)', pointerEvents: 'none',
      maxWidth: '60vw'
    });
    document.body.appendChild(el);
  }
  el.textContent = title;
}
"""


def _start_ui() -> subprocess.Popen:
    cmd = [
        sys.executable,
        "-m",
        "graphforge.cli",
        "ui",
        "--env",
        "audit.env",
        "--neo4j-uri",
        "bolt://127.0.0.1:7687",
        "--port",
        str(UI_PORT),
    ]
    proc = subprocess.Popen(
        cmd, cwd=str(REPO), env=clean_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    for _ in range(60):
        if port_open(UI_PORT):
            log(f"dashboard up on {DASHBOARD_URL}")
            return proc
        if proc.poll() is not None:
            die("graphforge ui exited before it started listening")
        time.sleep(0.5)
    proc.terminate()
    die(f"dashboard did not open port {UI_PORT} in 30s")


def _stop_ui(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _perform(page, actions: list[dict]) -> None:
    for a in actions:
        if "wait" in a:
            page.wait_for_timeout(a["wait"])
        elif "top" in a:
            page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
        elif "scroll" in a:
            steps = max(1, int(a.get("steps", 1)))
            for _ in range(steps):
                page.mouse.wheel(0, a["scroll"] / steps)
                page.wait_for_timeout(130)
        elif "scroll_to" in a:
            loc = page.get_by_role(
                "heading", name=re.compile(rf"^\s*{re.escape(a['scroll_to'])}", re.I)
            ).first
            loc.scroll_into_view_if_needed()
            page.mouse.wheel(0, -90)  # keep the heading clear of the top edge
        elif "click" in a:
            page.locator(a["click"]).first.click()
        elif "hover" in a:
            page.locator(a["hover"]).first.hover()
        elif "fill" in a:
            sel, text = a["fill"]
            page.locator(sel).first.fill(text)
        elif "drag" in a:
            sel, dx, dy = a["drag"]
            box = page.locator(sel).first.bounding_box()
            if box:
                cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                page.mouse.move(cx, cy)
                page.mouse.down()
                for k in range(1, 13):
                    page.mouse.move(cx + dx * k / 12, cy + dy * k / 12)
                    page.wait_for_timeout(30)
                page.mouse.up()
        else:
            die(f"unknown action {a!r}")


def stage_record(args) -> None:
    tl = load_timeline()
    missing = [s["id"] for _, s in narrated() if "audio_s" not in tl.get(s["id"], {})]
    if missing:
        die(f"no narration durations for {missing} -- run the tts stage first")
    from playwright.sync_api import sync_playwright

    (OUT / "frames").mkdir(parents=True, exist_ok=True)
    vid_dir = OUT / "video"
    vid_dir.mkdir(parents=True, exist_ok=True)
    for old in vid_dir.glob("*.webm"):
        old.unlink()

    ui = _start_ui() if any(s["kind"] == "dashboard" for s in SCENES) else None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                record_video_dir=str(vid_dir),
                record_video_size={"width": 1920, "height": 1080},
                color_scheme="dark",
            )
            page = ctx.new_page()
            t0 = time.monotonic()  # the recording starts with the page
            current_url = None
            for i, s in enumerate(SCENES):
                if s["kind"] == "title":
                    continue
                dur = tl[s["id"]]["audio_s"]
                nav0 = time.monotonic() - t0
                if s.get("url") and s["url"] != current_url:
                    page.goto(s["url"], wait_until="load", timeout=90_000)
                    current_url = s["url"]
                if s.get("ready"):
                    page.locator(s["ready"]).first.wait_for(timeout=30_000)
                page.wait_for_timeout(400)  # let the first paint settle
                if TITLES:
                    page.evaluate(_TITLE_JS, s["title"])
                # The narration clock starts HERE, on a page that is visible and populated.
                # Everything before it -- browser start-up, the load, the data fetch -- is a
                # silent transition, not narrated dead air. The first cut of this stamped
                # `start` before goto and narrated over black frames and the previous page.
                start = time.monotonic() - t0
                _perform(page, s.get("actions", []))
                _, png = scene_files(i, s)
                page.screenshot(path=str(png))
                spent = (time.monotonic() - t0) - start
                hold = dur + PAD_S - spent
                if hold > 0:
                    page.wait_for_timeout(int(hold * 1000))
                end = time.monotonic() - t0
                tl[s["id"]].update(
                    {
                        "video_start_s": round(start, 3),
                        "video_end_s": round(end, 3),
                        "load_s": round(start - nav0, 3),
                    }
                )
                log(
                    f"  {s['id']:16s} load {start - nav0:5.2f}s  start {start:7.2f}s  "
                    f"narration {dur:6.2f}s  held to {end:7.2f}s"
                )
            video = page.video
            ctx.close()  # flushes the webm to disk
            src = Path(video.path())
            browser.close()
        dst = vid_dir / "walkthrough.webm"
        shutil.move(str(src), str(dst))
        save_timeline(tl)
        log(f"record OK: {dst.name}, {ffprobe_duration(dst):.1f}s")
    finally:
        if ui:
            _stop_ui(ui)


# ---------------------------------------------------------------------------- images
def _openrouter_image(prompt: str) -> bytes:
    key = os.environ["OPENROUTER_API_KEY"]
    body = {
        "model": MODELS["image"],
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["image", "text"],
    }
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": f"https://github.com/{GITHUB_REPO}",
            "X-Title": "graphforge showcase",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            resp = json.load(r)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        die(f"OpenRouter returned HTTP {exc.code} for model {MODELS['image']!r}: {detail}")
    msg = resp["choices"][0]["message"]
    images = msg.get("images") or []
    if not images:
        die(
            f"model {MODELS['image']!r} returned no image (text was: {str(msg.get('content'))[:200]!r})"
        )
    url = images[0]["image_url"]["url"]
    return base64.b64decode(url.split(",", 1)[1])


def stage_images(args) -> None:
    if not os.environ.get("OPENROUTER_API_KEY"):
        log("images SKIPPED: OPENROUTER_API_KEY not set")
        return
    if not MODELS.get("image"):
        log("images SKIPPED: scenes.MODELS['image'] is None")
        return
    (OUT / "images").mkdir(parents=True, exist_ok=True)
    sizes = {"github-social": (1280, 640), "linkedin-post": (1200, 627)}
    for name, prompt in IMAGE_PROMPTS.items():
        final = OUT / "images" / f"{name}.png"
        if final.exists() and not args.force:
            log(f"  {final.name} exists (use --force to regenerate)")
            continue
        raw = OUT / "images" / f"{name}.raw.png"
        raw.write_bytes(_openrouter_image(prompt))
        w, h = sizes[name]
        run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(raw),
                "-vf",
                f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
                str(final),
            ]
        )
        raw.unlink()
        log(f"  {final.name}: {w}x{h} via {MODELS['image']}")
    log("images OK")


# ------------------------------------------------------------------------------ clip
def stage_clip(args) -> None:
    sources: list[Path] = [
        p
        for p in (OUT / "images" / "github-social.png", OUT / "images" / "linkedin-post.png")
        if p.exists()
    ]
    for i, s in enumerate(SCENES):
        if s["id"] == "ui-overview":
            _, png = scene_files(i, s)
            if png.exists():
                sources.append(png)
    if not sources:
        log("clip SKIPPED: no images or frames to animate yet")
        return
    seg_dir = OUT / "video" / "intro"
    seg_dir.mkdir(parents=True, exist_ok=True)
    segs = []
    for n, src in enumerate(sources):
        seg = seg_dir / f"seg{n}.mp4"
        # A single-frame input expanded by zoompan to d=120 frames = 4s @ 30fps. Upscaling to
        # 4K before the pan keeps the motion smooth. anullsrc gives the clip a silent track so
        # it can be concatenated with the narrated walkthrough.
        run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(src),
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo",
                "-filter_complex",
                # Letterbox on a dark ground, never crop: the banner carries its title at
                # the right edge and `increase`+crop cut the last letter off. The zoom is
                # gentle for the same reason -- a 1.30x push-in would crop 23% of the frame.
                "[0:v]scale=3840:2160:force_original_aspect_ratio=decrease,"
                "pad=3840:2160:(ow-iw)/2:(oh-ih)/2:color=0x101218,"
                "zoompan=z='min(zoom+0.0004,1.08)':d=120:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                ":s=1920x1080:fps=30,format=yuv420p[v]",
                "-map",
                "[v]",
                "-map",
                "1:a",
                "-t",
                "4",
                "-c:v",
                "libx264",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-shortest",
                str(seg),
            ]
        )
        segs.append(seg)
    inputs: list[str] = []
    fc = ""
    for n, seg in enumerate(segs):
        inputs += ["-i", str(seg)]
        fc += f"[{n}:v][{n}:a]"
    fc += f"concat=n={len(segs)}:v=1:a=1[v][a]"
    clip = OUT / "reference-clip.mp4"
    run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            *inputs,
            "-filter_complex",
            fc,
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(clip),
        ]
    )
    log(f"clip OK: {clip.name}, {len(segs)} segments, {ffprobe_duration(clip):.1f}s")


# -------------------------------------------------------------------------- assemble
def stage_assemble(args) -> None:
    tl = load_timeline()
    webm = OUT / "video" / "walkthrough.webm"
    if not webm.exists():
        die("no recording at out/video/walkthrough.webm -- run the record stage first")
    clips = [(i, s) for i, s in narrated() if "video_start_s" in tl.get(s["id"], {})]
    if not clips:
        die("timeline has no recorded offsets -- run the record stage first")
    webm_len = ffprobe_duration(webm)
    # Before the first narration there is only browser start-up and the first page load:
    # black frames. Trim to just ahead of it so the video opens on content, and shift every
    # narration offset by the same amount so nothing moves relative to the picture.
    first_start = min(tl[s["id"]]["video_start_s"] for _, s in clips)
    lead = max(0.0, first_start - 0.6)
    video_len = webm_len - lead
    log(f"  trimming {lead:.2f}s of lead-in; walkthrough runs {video_len:.1f}s")

    # Each narration clip is placed at the offset that was MEASURED while recording.
    inputs: list[str] = []
    fc: list[str] = []
    labels = ""
    for n, (i, s) in enumerate(clips, start=1):
        mp3, _ = scene_files(i, s)
        inputs += ["-i", str(mp3)]
        ms = round((tl[s["id"]]["video_start_s"] - lead) * 1000)
        fc.append(f"[{n}:a]adelay={ms}:all=1[a{n}]")
        labels += f"[a{n}]"
    fc.append(f"{labels}amix=inputs={len(clips)}:normalize=0:duration=longest[mix]")
    fc.append(f"[mix]apad=whole_dur={video_len:.3f}[aout]")

    walk = OUT / "video" / "walkthrough.mp4"
    run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-ss",
            f"{lead:.3f}",
            "-i",
            str(webm),
            *inputs,
            "-filter_complex",
            ";".join(fc),
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            "-fps_mode",
            "cfr",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-shortest",
            "-movflags",
            "+faststart",
            str(walk),
        ]
    )
    log(f"  narrated walkthrough: {ffprobe_duration(walk):.1f}s")

    intro = OUT / "reference-clip.mp4"
    if intro.exists():
        run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(intro),
                "-i",
                str(walk),
                "-filter_complex",
                "[0:v]scale=1920:1080,setsar=1,fps=30,format=yuv420p[v0];"
                "[1:v]scale=1920:1080,setsar=1,fps=30,format=yuv420p[v1];"
                "[0:a]aformat=sample_rates=48000:channel_layouts=stereo[a0];"
                "[1:a]aformat=sample_rates=48000:channel_layouts=stereo[a1];"
                "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]",
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-c:v",
                "libx264",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-movflags",
                "+faststart",
                str(FINAL),
            ]
        )
        expected = ffprobe_duration(intro) + video_len
    else:
        shutil.copyfile(walk, FINAL)
        expected = video_len

    # Self-check: one video + one audio stream, and the duration is what the parts add to.
    streams = ffprobe_streams(FINAL)
    got = ffprobe_duration(FINAL)
    ok = sorted(streams) == ["audio", "video"] and abs(got - expected) <= 1.0
    log(
        f"assemble {'OK' if ok else 'FAILED SELF-CHECK'}: {FINAL.name} streams={streams} "
        f"duration={got:.1f}s expected={expected:.1f}s"
    )
    if not ok:
        sys.exit(2)


# --------------------------------------------------------------------------- publish
RELEASE_NOTES = """graphforge showcase video

A narrated walkthrough of the repository and the live dashboard: how the hardening pass was
done, the logic behind each page, and the tech stack. Regenerate it with
`python scripts/showcase/render.py` -- see scripts/showcase/README.md.

Assets: graphforge-showcase.mp4 (the video), github-social.png, linkedin-post.png.
"""


def _git(*a: str) -> str:
    return run(["git", *a], cwd=str(REPO)).stdout.strip()


def stage_publish(args) -> None:
    if not FINAL.exists():
        die("nothing to publish -- run assemble first")
    assets = [FINAL, *sorted((OUT / "images").glob("*.png"))]

    # A release must point at a commit that already contains the scripts that made it. The
    # commit itself is left to a person -- the message deserves one -- so this stage only
    # refuses to publish work that is uncommitted or unpushed.
    paths = [
        "scripts/showcase/README.md",
        "scripts/showcase/requirements.txt",
        "scripts/showcase/scenes.py",
        "scripts/showcase/render.py",
        ".gitignore",
        "README.md",
    ]
    if _git("status", "--porcelain", "--", *paths):
        die("uncommitted showcase changes -- commit and push them before publishing the release")
    if _git("rev-parse", "HEAD") != _git("rev-parse", f"origin/{BRANCH}"):
        die(
            f"HEAD is not pushed to origin/{BRANCH} -- push first so the release tags a public commit"
        )

    gh = shutil.which("gh")
    token = os.environ.get("GITHUB_TOKEN")
    if gh:
        notes = Path(tempfile.mkstemp(suffix=".md")[1])
        notes.write_text(RELEASE_NOTES, encoding="utf-8")
        run(
            [
                gh,
                "release",
                "create",
                RELEASE_TAG,
                *map(str, assets),
                "--repo",
                GITHUB_REPO,
                "--target",
                BRANCH,
                "--title",
                "graphforge showcase video",
                "--notes-file",
                str(notes),
            ]
        )
        notes.unlink()
        log(f"publish OK via gh: https://github.com/{GITHUB_REPO}/releases/tag/{RELEASE_TAG}")
    elif token:
        api = f"https://api.github.com/repos/{GITHUB_REPO}"
        hdr = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "graphforge-showcase",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        body = json.dumps(
            {
                "tag_name": RELEASE_TAG,
                "target_commitish": BRANCH,
                "name": "graphforge showcase video",
                "body": RELEASE_NOTES,
            }
        ).encode()
        req = urllib.request.Request(f"{api}/releases", data=body, headers=hdr, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            rel = json.load(r)
        upload = rel["upload_url"].split("{", 1)[0]
        for a in assets:
            ctype = "video/mp4" if a.suffix == ".mp4" else "image/png"
            req = urllib.request.Request(
                f"{upload}?name={a.name}",
                data=a.read_bytes(),
                headers={**hdr, "Content-Type": ctype},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=600):
                pass
            log(f"  uploaded {a.name}")
        log(f"publish OK via REST: {rel['html_url']}")
    else:
        log("publish: neither `gh` nor GITHUB_TOKEN available. Manual steps:")
        log(f"  1. https://github.com/{GITHUB_REPO}/releases/new?tag={RELEASE_TAG}&target={BRANCH}")
        log("  2. title: graphforge showcase video")
        for a in assets:
            log(f"  3. attach {a}")


# ------------------------------------------------------------------------------ main
LOCAL_ENV = HERE / "local.env"  # gitignored by the repo's *.env rule


def _load_local_env() -> bool:
    """Pick up OPENROUTER_API_KEY from local.env when the shell did not provide it.

    The environment always wins. Only that one key is read, its value is never echoed,
    and the file is meant to be deleted once the images exist -- it is the sole sanctioned
    place for the key on disk, and only because a key on a command line is worse.
    """
    if os.environ.get("OPENROUTER_API_KEY") or not LOCAL_ENV.exists():
        return False
    for line in LOCAL_ENV.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("OPENROUTER_API_KEY="):
            os.environ["OPENROUTER_API_KEY"] = line.split("=", 1)[1].strip().strip("'\"")
            return True
    return False


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--only", choices=STAGES, action="append", help="run only these stage(s)")
    ap.add_argument(
        "--skip", choices=STAGES, action="append", default=[], help="skip these stage(s)"
    )
    ap.add_argument("--publish", action="store_true", help="also run the publish stage")
    ap.add_argument(
        "--force", action="store_true", help="regenerate narration / images that already exist"
    )
    ap.add_argument("--dry-run", action="store_true", help="print the plan and change nothing")
    args = ap.parse_args()
    if _load_local_env():
        log(f"OPENROUTER_API_KEY loaded from {LOCAL_ENV.name} (gitignored; delete it when done)")

    stages = args.only or [s for s in STAGES if s != "publish" or args.publish]
    stages = [s for s in stages if s not in args.skip]

    if args.dry_run:
        print("stages:", " -> ".join(stages))
        print(f"{'#':>2}  {'id':16s} {'kind':10s} {'~words':>6s}  title")
        for i, s in enumerate(SCENES):
            words = len((s.get("narration") or "").split())
            print(f"{i:2d}  {s['id']:16s} {s['kind']:10s} {words:6d}  {s['title']}")
        total = sum(len((s.get("narration") or "").split()) for s in SCENES)
        print(f"~{total} words ~= {total / 150:.1f} min at 150 wpm, before pads")
        return

    for name in stages:
        log(f"--- {name}")
        globals()[f"stage_{name}"](args)
    log("done")


if __name__ == "__main__":
    main()
