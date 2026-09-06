# Showcase video generator

Produces `out/graphforge-showcase.mp4`: a narrated walkthrough of the GitHub repository and
the live dashboard, plus two reference images and a short animated intro clip.

What you say and show lives in **`scenes.py`**. `render.py` is the machinery and should not
need editing to change content.

## How alignment works

Narration is synthesised **first** and each clip is measured with ffprobe. The browser then
holds every page on screen for exactly that long, inside one continuous recording, and writes
each scene's **actual** start offset to `out/timeline.json` as it goes. Assembly places each
narration clip at that recorded offset. A slow page load therefore cannot push the audio out
of sync — the audio follows the video that was actually captured.

## Prerequisites

- `ffmpeg` and `ffprobe` on PATH.
- `python -m pip install -r scripts/showcase/requirements.txt` and
  `python -m playwright install chromium`, in the interpreter that has `graphforge` installed.
- For the dashboard scenes: the Neo4j playground up on `127.0.0.1:7687` and an `audit.env`
  at the repo root (see `docs/HARDENING_LOG.md`, rule S-1 — the dashboard is started from
  `audit.env`, never from `.env`).
- For the images: `OPENROUTER_API_KEY` in the **environment** of the shell you run from.
  It is read from the environment only and is never written to disk or logged. Everything
  else renders without it; the image and clip stages just skip.
- For `--publish`: either the `gh` CLI, authenticated, or `GITHUB_TOKEN` (a fine-grained
  token with *contents: write* on the repo). Without either, everything still renders and
  the script prints the manual upload steps.

## Running

```
python scripts/showcase/render.py --dry-run          # list stages and scenes, change nothing
python scripts/showcase/render.py --only preflight   # tools, model availability, ports
python scripts/showcase/render.py                    # full render to out/
python scripts/showcase/render.py --only tts --force # re-synthesise narration after editing text
python scripts/showcase/render.py --publish          # also commit scripts + create the release
```

Stages, in order: `preflight`, `tts`, `record`, `images`, `clip`, `assemble`, `publish`.
Each is idempotent and can be run alone with `--only`, or skipped with `--skip`, so changing
one scene's wording re-renders one mp3 and one recording pass — not everything.

## Outputs (`out/`, gitignored)

| Path | What |
|---|---|
| `audio/NN-<id>.mp3` | one narration clip per scene |
| `frames/NN-<id>.png` | a still of each scene, taken as it was recorded |
| `video/walkthrough.webm` | the raw Playwright screen recording |
| `images/github-social.png`, `images/linkedin-post.png` | generated reference images |
| `reference-clip.mp4` | the short animated clip built from those images |
| `timeline.json` | per-scene narration durations and recorded video offsets |
| `graphforge-showcase.mp4` | the final video: intro clip + narrated walkthrough |

## Models

Only one model ID exists in the project, in `scenes.py` → `MODELS`. Preflight queries
OpenRouter's live catalogue and prints whether each configured ID exists before any request
is made, so a wrong ID fails loudly and early. The catalogue currently has no video-output
model, which is why the intro clip is animated with ffmpeg rather than generated.
