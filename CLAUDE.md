# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

VX — an AI video editor that turns raw trip footage into polished vlogs. The core pipeline: raw clips → ffmpeg preprocessing → per-clip AI review (Phase 1) → cross-clip editorial assembly (Phase 2) → interactive HTML preview → rough cut video. Two modes: editorial (multi-clip, primary) and descriptive (single-video narration).

## Commands

```bash
# Setup
uv venv && uv pip install -e ".[dev]"

# Run
vx                          # Interactive TUI mode
vx new my-trip ~/footage/   # Create project from clip folder
vx analyze my-trip          # Phase 1 + Phase 2 (AI calls)
vx cut my-trip              # Assemble rough_cut.mp4 (no AI)

# Dev
ruff check src/             # Lint
ruff format src/            # Format
pytest                      # Tests (no test suite yet)
```

## Architecture

**Pipeline flow** (each step's output feeds the next):
1. **Preprocessing** (`preprocess.py`) — ffmpeg: 4K→360p proxy, frame extraction, scene detection, audio extraction. Parallel (4 workers), per-clip cached.
2. **Phase 1** (`editorial_agent.py` → `editorial_prompts.py`) — One LLM call per clip against proxy video (Gemini) or extracted frames (Claude). Produces structured review JSON. Parallel (5 workers), cached per clip with versioning.
3. **Briefing** (`briefing.py`) — Optional interactive questionnaire injected into Phase 2 prompt as user context.
4. **Phase 2** (`editorial_agent.py` → `editorial_prompts.py`) — Single LLM call. All clip reviews + briefing → `EditorialStoryboard` Pydantic model. Gemini uses `response_schema` for structured output; Claude uses JSON parsing with fence/brace extraction fallback.
5. **Render** (`render.py`) — Deterministic: Pydantic model → markdown + self-contained HTML preview (no build step, no framework).
6. **Rough cut** (`rough_cut.py`) — Deterministic: JSON → ffmpeg segment extraction + concatenation. Validates timestamps against actual clip durations (clamps out-of-bounds).

**Key design decisions:**
- `EditorialStoryboard` in `models.py` is the single source of truth. All timestamps in seconds (float), used directly by ffmpeg.
- LLM calls are isolated to Phase 1 and Phase 2 only. Everything else is deterministic.
- Clip IDs may be abbreviated by LLMs (e.g., `C0073` instead of `20260330114125_C0073`). `_resolve_clip_id_refs()` in `editorial_agent.py` handles fuzzy resolution via suffix matching.
- Versioning (`versioning.py`): auto-incrementing versions with `_latest` symlinks. Version counters stored in `project.json`.
- Two LLM providers: Gemini (native video upload, structured output schema) and Claude (frame-based with base64 images). Provider-specific code in `gemini_analyze.py` and `claude_analyze.py` (descriptive mode) or branched within `editorial_agent.py` (editorial mode).

**Project data lives in `library/<project-name>/`** with per-clip subdirectories under `clips/`. See README for full layout.

## Code Conventions

- Python 3.11+, Pydantic for data models, dataclasses for config
- Ruff for linting/formatting, line length 100, target py311
- Entry point: `vx` CLI command → `cli.py:main()` → argparse dispatch or interactive TUI
- Lazy imports for heavy dependencies (google.genai, anthropic) — only imported inside functions that use them
- API keys loaded from `.env` via python-dotenv
