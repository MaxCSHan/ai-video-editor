# VX — AI Video Editor

Turn raw trip footage into polished vlogs with AI. Point at a folder of clips, and VX reviews every clip, identifies people, builds a story arc, and produces an edit plan with precise cut points — then assembles a rough cut video.

## Quick Start

```bash
uv venv && uv pip install -e ".[dev]"
cp .env.example .env   # Add your GEMINI_API_KEY

vx                     # Launch interactive mode
```

## How It Works

```
Raw Clips (folder)
      |
  [Preprocess]     ffmpeg: downscale proxy, extract frames, detect scenes, audio
      |                    (parallel, cached)
      |
  [Briefing]       Interactive questionnaire — name people, describe occasion,
      |            set tone. Feeds context into Phase 2. (optional, skippable)
      |
  [Phase 1]        LLM reviews EACH clip individually
                   Input:  proxy video (Gemini) or frames (Claude)
                   Output: structured JSON — summary, quality, people,
                           key moments, usable/discard segments, audio
                   Cached per clip.
      |
  [Phase 2]        LLM acts as creative editor across ALL clips
                   Input:  Phase 1 reviews + user briefing context
                   Output: Pydantic structured JSON (enforced schema) —
                           story arc, cast, EDL with precise in/out seconds,
                           pacing notes, music plan, technical notes
                   Also renders: markdown + interactive HTML preview
      |
  [Cut]            ffmpeg: extract segments at exact timestamps, concatenate
                   Input:  structured JSON from Phase 2 (no LLM needed)
                   Output: rough_cut.mp4 + HTML preview with embedded video
```

Each phase is **cached and versioned**. Re-running skips completed work.

## CLI

### Interactive mode (recommended)

```bash
vx                                    # Guided workflow with menus and prompts
```

Walks you through: create project → preprocess → briefing → analyze → preview → cut.

### Direct commands (for scripting)

```bash
vx new my-trip ~/footage/             # Create project from footage folder
vx new recap video.mp4                # Single-video descriptive mode
vx analyze my-trip                    # Phase 1 + 2 → structured storyboard
vx analyze my-trip --force            # Re-run Phase 1 reviews
vx analyze my-trip --no-interactive   # Skip briefing questions
vx cut my-trip                        # Assemble rough cut (no LLM)
vx projects                           # List all projects
vx status my-trip                     # Detailed status with versions
vx config --provider gemini           # Set defaults
```

### Interactive HTML preview

The preview (`storyboard/*_preview.html`) is an interactive editor:
- Click timeline segments to open a **video preview modal**
- Embedded proxy video player with **adjustable in/out range handles**
- Drag to adjust cut points, preview the selection
- **Export adjusted JSON** to fine-tune before running `vx cut`

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) or pip
- ffmpeg & ffprobe (`brew install ffmpeg`)
- `GEMINI_API_KEY` and/or `ANTHROPIC_API_KEY` in `.env`

## Setup

```bash
cd ai-video-editor
uv venv && uv pip install -e ".[dev]"
cp .env.example .env
```

## Project Library

```
library/
  my-trip/                              # One project per shoot
    project.json                        # Metadata (type, provider, style)
    user_context.json                   # Briefing answers
    manifest.json                       # Aggregated clip metadata
    clips/
      20260330_C0059/                   # Per-clip (parallel preprocessed, cached)
        source/  proxy/  frames/  scenes/  audio/
        review/review_gemini_v1.json    # Phase 1 review (versioned)
    storyboard/
      editorial_gemini_v1.json          # Phase 2: structured data (primary)
      editorial_gemini_v1.md            # Rendered markdown view
      editorial_gemini_v1_preview.html  # Interactive HTML preview
    exports/
      v1/                              # Versioned rough cuts
        rough_cut.mp4
        preview.html                   # Preview with embedded video
        segments/  thumbnails/
```

## Source Code

```
src/ai_video_editor/
  cli.py               # CLI entry point (vx command)
  interactive.py        # Interactive TUI mode (questionary)
  briefing.py           # Editorial briefing questionnaire
  models.py             # Pydantic models (EditorialStoryboard, Segment, etc.)
  config.py             # Settings, ProjectPaths, EditorialProjectPaths
  preprocess.py         # ffmpeg: proxy, frames, scenes, audio (parallel, cached)
  editorial_prompts.py  # Phase 1 + 2 prompt templates
  editorial_agent.py    # Multi-clip orchestrator
  render.py             # Markdown + HTML rendering from Pydantic models
  rough_cut.py          # Validation + ffmpeg assembly (no LLM)
  versioning.py         # Run versioning with symlinks
  storyboard_format.py  # Descriptive storyboard template
  gemini_analyze.py     # Gemini single-video descriptive analysis
  claude_analyze.py     # Claude single-video descriptive analysis
```

## LLM Calls

| Phase | What | Input | Output | Cached? |
|-------|------|-------|--------|---------|
| 1 | Clip review | Proxy video or frames | JSON: quality, people, segments | Per-clip |
| 2 | Editorial assembly | Phase 1 JSONs + user context | Pydantic JSON: arc, cast, EDL, music | Per-project (versioned) |
| Cut | Assembly | Structured JSON | rough_cut.mp4 | **No LLM** — pure ffmpeg |
