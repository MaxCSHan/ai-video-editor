# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

VX — an AI video editor that turns raw trip footage into polished vlogs. The core pipeline: raw clips -> ffmpeg preprocessing -> per-clip AI review (Phase 1) -> cross-clip editorial assembly (Phase 2) -> interactive HTML preview -> rough cut video. Two modes: editorial (multi-clip, primary) and descriptive (single-video narration).

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
pytest                      # All tests
pytest tests/domain/        # Domain-only (fast, no I/O)
pytest tests/infra/         # Infrastructure (mocked adapters)
```

## Architecture

### Layering

Dependencies point inward. Three layers:

1. **`domain/`** — Pure logic, zero I/O, no third-party imports except Pydantic/stdlib.
   - `exceptions.py` — `VXError` hierarchy (8 classes). All cross-module errors defined here.
   - `ports.py` — Protocol definitions (`Phase1Reviewer`). Target adapter interface, not yet runtime-dispatched.
   - `validation.py` — `validate_clip_review()`, `validate_storyboard()`. Returns (warnings, is_critical).
   - `clip_resolution.py` — `resolve_clip_id_refs()`. Fuzzy suffix matching for LLM-abbreviated clip IDs.
   - `timestamps.py` — `clamp_segments_to_usable()`. Clamps segments to Phase 1 usable bounds.

2. **`infra/`** — Shared adapters wrapping external SDKs. Imports only from `domain/`.
   - `gemini_client.py` — `GeminiClient` (Golden Sample adapter). `from_env()`, `upload_and_wait()`, `generate()`. Translates SDK exceptions to domain exceptions. All new adapters should follow this pattern.

3. **Phase modules + orchestrator** (at package root):
   - `editorial_agent.py` (617 lines) — Pipeline orchestration only. Delegates to phase modules. Re-exports `run_phase1`, `run_phase2`, `run_monologue` for backward compat.
   - `editorial_phase1.py` — `run_phase1()` dispatcher -> `run_phase1_gemini()` / `run_phase1_claude()`. Per-clip review, parallel (5 workers).
   - `editorial_phase2.py` — Multi-call split pipeline: 2A (reasoning, high temp) -> 2A.5 (structuring) -> 2B (timestamp assembly). Story + Timeline modes.
   - `editorial_phase3.py` — Visual monologue generation. Optional, preset-dependent (`has_phase3=True`).

**Dependency rule:** `domain/` imports nothing from `infra/` or phase modules. `infra/` imports only from `domain/`. Phase modules import from both.

`models.py` (Pydantic models) and `config.py` (dataclass configs) remain at package root.

### Pipeline Flow

Each step's output feeds the next:
1. **Preprocessing** (`preprocess.py`) — ffmpeg: 4K->360p proxy, frame extraction, scene detection, audio extraction. Parallel (4 workers), per-clip cached.
2. **Style Preset** (`style_presets.py`) — Optional creative direction (e.g., Silent Vlog). Adds AI guidance to Phase 1/2 prompts and may enable Phase 3.
3. **Briefing** (`briefing.py`) — AI quick-scan of all proxies (Gemini) -> targeted questions -> `user_context.json`. Caches Gemini File API URIs (`file_cache.py`) for reuse downstream.
4. **Transcription** (`transcribe.py`) — Gemini (speaker ID, sound events) or mlx-whisper (local). Per-clip cached.
5. **Phase 1** (`editorial_phase1.py`) — One LLM call per clip. Gemini: native video upload. Claude: base64 frames. Produces `ClipReview` JSON. Parallel, cached per clip with versioning.
6. **Phase 2** (`editorial_phase2.py`) — Multi-call split pipeline for Gemini: Call 2A (freeform reasoning) -> Call 2A.5 (structuring into `StoryPlan`) -> Call 2B (precise timestamp assembly). All reviews + transcripts + briefing -> `EditorialStoryboard`. Visual mode (<=10 clips) attaches proxy videos.
7. **Phase 3** (`editorial_phase3.py`) — Optional. Generates text overlay/monologue plan from storyboard + transcripts. Only runs if style preset has `has_phase3=True`.
8. **Render** (`render.py`) — Deterministic: Pydantic model -> markdown + self-contained HTML preview.
9. **Rough cut** (`rough_cut.py`) — Deterministic: JSON -> ffmpeg segment extraction + concatenation.

### Key Design Decisions

- `EditorialStoryboard` in `models.py` is the single source of truth. All timestamps in seconds (float), used directly by ffmpeg.
- LLM calls occur in briefing, transcription (Gemini), Phase 1, Phase 2, and Phase 3. Everything else is deterministic.
- Clip IDs may be abbreviated by LLMs (e.g., `C0073` instead of `20260330114125_C0073`). `resolve_clip_id_refs()` in `domain/clip_resolution.py` handles fuzzy resolution via suffix matching.
- Adapters translate SDK exceptions to domain exceptions (`VXError` hierarchy in `domain/exceptions.py`). `LLMProviderError` carries `provider` and `phase` context.
- `GeminiClient` in `infra/gemini_client.py` is the Golden Sample adapter — all new adapters follow this pattern: class with `from_env()`, domain exception translation, thin SDK wrapper.
- Versioning (`versioning.py`): Two-phase commit (`begin_version`/`commit_version`/`fail_version`) with `.meta.json` lineage sidecars and `_latest` symlinks.
- Project data lives in `library/<project-name>/` with per-clip subdirectories under `clips/`. See README for full layout.

## Tests

`tests/` mirrors `src/` structure:
- `tests/domain/` — Pure unit tests (validation, clip_resolution, timestamps, ports). Fast, no I/O.
- `tests/infra/` — Adapter tests with full mocking (`test_gemini_client.py`). No network.
- `tests/` root — `test_eval.py` (scoring), `test_versioning.py` (protocol), `test_multi_call_pipeline.py` (integration).

Rule: when extracting logic into `domain/`, add a corresponding test in `tests/domain/`.

## Code Conventions

- Python 3.11+, Pydantic for data models, dataclasses for config
- Ruff for linting/formatting, line length 100, target py311
- Entry point: `vx` CLI command -> `cli.py:main()` -> argparse dispatch or interactive TUI
- Lazy imports for heavy dependencies (google.genai, anthropic) — only imported inside functions that use them
- API keys loaded from `.env` via python-dotenv

### Architectural Invariants

- **`domain/` must not import from `infra/`, phase modules, or any external SDK.** Pure stdlib + Pydantic only.
- **New adapters follow the GeminiClient pattern:** class with `from_env()`, domain exception translation, thin SDK wrapper. See `infra/gemini_client.py`.
- **Exception hierarchy:** All new exceptions subclass `VXError`. LLM errors use `LLMProviderError(provider=, phase=)`. Adapters catch SDK exceptions and translate — entry points never catch SDK errors directly.
- **Phase boundary validation:** After every LLM call that produces a storyboard, call `resolve_clip_id_refs()` then `validate_storyboard()` (both from `domain/`). After clip reviews, call `validate_clip_review()`.
- **No bare `except:` or `except Exception` without re-raise.**
- **Prompts stay as Python functions** in `editorial_prompts.py`. Different cognitive tasks (2A reasoning, 2A.5 structuring, 2B assembly) keep separate prompts — share only formatting helpers.
- **Backward compat re-exports:** `editorial_agent.py` re-exports `run_phase1`, `run_phase2`, `run_monologue`. Don't break these; callers depend on them.

## Reference

Full architecture rationale and target state: `docs/architecture-manifesto.md`. Codebase audit: `docs/audit-codebase.md`.
