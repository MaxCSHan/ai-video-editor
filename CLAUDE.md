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
2. **Style Preset** (`style_presets.py`) — Optional creative direction (e.g., Silent Vlog). Adds AI guidance to Phase 1/2 prompts and may enable Phase 3 (visual monologue).
3. **Briefing** (`briefing.py`) — AI quick-scan of all proxies (Gemini path) → targeted questions → `user_context.json`. Uploads proxies to Gemini File API and caches URIs (`file_cache.py`) for reuse by downstream stages. Non-Gemini path: manual briefing runs after Phase 1 instead.
4. **Transcription** (`transcribe.py`) — Gemini (speaker ID, sound events) or mlx-whisper (local). Uses speaker names from briefing. Reuses cached Gemini File API URIs. Per-clip cached.
5. **Phase 1** (`editorial_agent.py` → `editorial_prompts.py`) — One LLM call per clip against proxy video (Gemini) or extracted frames (Claude). Receives user context from briefing (people names, intent). Reuses cached Gemini File API URIs. Produces structured review JSON. Parallel (5 workers), cached per clip with versioning.
6. **Phase 2** (`editorial_agent.py` → `editorial_prompts.py`) — Single LLM call. All clip reviews + transcripts + briefing → `EditorialStoryboard` Pydantic model. Visual mode (≤10 clips) attaches proxy videos. Gemini uses `response_schema` for structured output; Claude uses JSON parsing with fence/brace extraction fallback.
7. **Phase 3** (`editorial_agent.py`) — Optional, preset-dependent. Generates text overlay/monologue plan from storyboard + transcripts. One LLM call. Only runs if style preset has `has_phase3=True`.
8. **Render** (`render.py`) — Deterministic: Pydantic model → markdown + self-contained HTML preview (no build step, no framework).
9. **Rough cut** (`rough_cut.py`) — Deterministic: JSON → ffmpeg segment extraction + concatenation. Validates timestamps against actual clip durations (clamps out-of-bounds).

**Key design decisions:**
- `EditorialStoryboard` in `models.py` is the single source of truth. All timestamps in seconds (float), used directly by ffmpeg.
- LLM calls occur in briefing (quick scan), transcription (Gemini), Phase 1, Phase 2, and Phase 3 (optional). Everything else is deterministic.
- Clip IDs may be abbreviated by LLMs (e.g., `C0073` instead of `20260330114125_C0073`). `_resolve_clip_id_refs()` in `editorial_agent.py` handles fuzzy resolution via suffix matching.
- Versioning (`versioning.py`): Full DAG pipeline versioning — every node (quick_scan, user_context, transcription, Phase 1, Phase 2, Phase 3) is versioned with `.meta.json` sidecar files for lineage tracking. Two-phase commit (`begin_version`/`commit_version`/`fail_version`) — failed runs don't pollute `_latest` symlinks. Cuts live in `exports/cuts/cut_NNN/` with `composition.json` provenance manifests, fully decoupled from storyboard version numbers. Compositions (`compositions.json`) allow mixing storyboard + monologue versions. Experiment tracks namespace outputs under `storyboard/<track>/`. Path resolvers: `resolve_versioned_path()`, `resolve_transcript_path()`, `resolve_user_context_path()`, `resolve_quick_scan_path()`. Legacy projects auto-migrated on first access.
- Three LLM providers: Gemini (native video upload, structured output schema), Claude (frame-based with base64 images), and Gemma (local, frame-based via OpenAI-compatible API). Gemma uses the same frame extraction path as Claude but talks to a local server (Ollama etc.) through the `openai` SDK. Provider-specific code in `gemini_analyze.py`, `claude_analyze.py`, and `gemma_analyze.py` (descriptive mode) or branched within `editorial_agent.py` (editorial mode). Gemma uses `top_p`/`top_k` sampling and `<|think|>` reasoning mode for Phase 2/3.

**Project data lives in `library/<project-name>/`** with per-clip subdirectories under `clips/`. See README for full layout.

## Code Conventions

- Python 3.11+, Pydantic for data models, dataclasses for config
- Ruff for linting/formatting, line length 100, target py311
- Entry point: `vx` CLI command → `cli.py:main()` → argparse dispatch or interactive TUI
- Lazy imports for heavy dependencies (google.genai, anthropic) — only imported inside functions that use them
- API keys loaded from `.env` via python-dotenv
