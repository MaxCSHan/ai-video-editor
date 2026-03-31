# AI Video Editor — Storyboard Generator

Turn unplanned trip footage into a polished vlog edit plan using AI.

## How It Works

The editorial pipeline has **4 phases**, each involving an LLM call with a distinct job:

```
Raw Clips (folder)
      |
      v
  [Preprocess]  ←  ffmpeg: proxy, frames, scenes, audio (cached)
      |
      v
  [Phase 1]     ←  LLM reviews EACH clip individually
  Per-clip         Input:  proxy video (Gemini) or frames (Claude)
  Review           Output: structured JSON — summary, quality, people,
                           key moments, usable/discard segments, audio
                   Cached per clip.
      |
      v
  [Phase 2]     ←  LLM acts as creative editor across ALL clips
  Editorial        Input:  all Phase 1 review JSONs (text only)
  Storyboard       Output: editorial markdown — story arc, cast, EDL table,
                           pacing notes, music plan, technical notes
      |
      v
  [Phase 3]     ←  LLM converts editorial plan to precise machine data
  Structured       Input:  editorial markdown + proxy videos (visual grounding)
  EDL              Output: structured JSON (enforced schema) — segments with
                           exact in_sec/out_sec, purposes, transitions, cast
      |
      v
  [Execute]     ←  ffmpeg: extract segments, concatenate rough cut
  Rough Cut        Output: rough_cut.mp4 + timeline HTML preview
```

**Each phase is cached.** Re-running skips completed work and jumps to the next phase.

## CLI — `vx`

```bash
vx new puma-run ~/footage/puma/       # Create project, preprocess all clips
vx analyze puma-run                   # Phase 1 + Phase 2 → editorial storyboard
vx cut puma-run                       # Phase 3 + assembly → rough cut + preview

vx projects                           # List all projects
vx status puma-run                    # Per-clip cache/review status
vx config                             # Show defaults, API key status
vx config --provider claude           # Change default AI provider
```

### Full workflow example

```bash
# Setup
uv venv && uv pip install -e ".[dev]"
cp .env.example .env  # Add GEMINI_API_KEY

# Create project from raw footage folder
vx new puma-run ~/footage/puma-night-run/

# Generate editorial storyboard (Phase 1 + 2)
vx analyze puma-run

# Review the storyboard
cat library/puma-run/storyboard/editorial_gemini.md

# Generate structured EDL + rough cut video + HTML preview (Phase 3 + assembly)
vx cut puma-run

# Open the visual timeline preview
open library/puma-run/exports/preview.html

# Watch the AI-assembled rough cut
open library/puma-run/exports/rough_cut.mp4
```

### Descriptive mode (single video)

```bash
vx new recap video.mp4                # Auto-detects single file → descriptive mode
vx analyze recap                      # Shot-by-shot description of the video
```

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) or pip
- ffmpeg & ffprobe (`brew install ffmpeg`)
- API key: `GEMINI_API_KEY` and/or `ANTHROPIC_API_KEY`

## Setup

```bash
cd ai-video-editor
uv venv && uv pip install -e ".[dev]"
cp .env.example .env
# Edit .env with your API keys
```

## Project Library

```
library/
  puma-run/                           # Editorial project
    project.json                      # Project metadata (type, provider, style)
    manifest.json                     # Aggregated clip metadata
    clips/
      vid_001/                        # Per-clip preprocessing (all cached)
        source/vid_001.mp4            #   Original footage
        proxy/vid_001_proxy.mp4       #   360x240 @1fps proxy for AI
        frames/manifest.json          #   Frames every 5s + index
        scenes/manifest.json          #   Scene-change keyframes
        audio/vid_001.wav             #   Extracted audio
        review/review_gemini.json     #   Phase 1: clip review (cached)
    storyboard/
      editorial_gemini.md             # Phase 2: editorial storyboard
    exports/
      edl.json                        # Phase 3: structured EDL
      rough_cut.mp4                   # Assembled rough cut
      preview.html                    # HTML timeline preview
      thumbnails/                     # Per-segment thumbnails
      segments/                       # Individual extracted segments
```

## Source Code

```
src/ai_video_editor/
  cli.py                # Unified CLI (vx command)
  config.py             # Settings, ProjectPaths, EditorialProjectPaths
  preprocess.py         # ffmpeg: proxy, frames, scenes, audio (cached)
  editorial_prompts.py  # Phase 1 + Phase 2 prompt templates
  editorial_agent.py    # Multi-clip orchestrator (Phase 1 + 2)
  rough_cut.py          # Phase 3 structured EDL + ffmpeg assembly + HTML preview
  storyboard_format.py  # Descriptive storyboard template
  gemini_analyze.py     # Gemini descriptive analysis
  claude_analyze.py     # Claude descriptive analysis
```

## LLM Calls Summary

| Phase | What | Input | Output | Cached? |
|-------|------|-------|--------|---------|
| 1 | Clip review | Proxy video or frames | JSON: quality, people, segments, audio | Per-clip |
| 2 | Editorial assembly | All Phase 1 JSONs (text) | Markdown: story arc, EDL, pacing, music | Per-project |
| 3 | Structured EDL | Editorial MD + proxy videos | JSON: precise in/out timestamps, transitions | Per-project |
