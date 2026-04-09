# VX Codebase Audit

**Date:** 2026-04-10
**Scope:** Full structural, architectural, and data-flow audit of the VX AI video editor.

---

## Table of Contents

1. [Folder Layout](#1-folder-layout)
2. [Module Inventory](#2-module-inventory)
3. [Data Model Catalog](#3-data-model-catalog)
4. [Pipeline Data Flow](#4-pipeline-data-flow)
5. [Intentional Architectural Patterns](#5-intentional-architectural-patterns)
6. [Emergent & Accidental Patterns](#6-emergent--accidental-patterns)
7. [Concrete Anti-Pattern Evidence](#7-concrete-anti-pattern-evidence)
8. [Design Doc Landscape](#8-design-doc-landscape)
9. [Summary Statistics](#9-summary-statistics)

---

## 1. Folder Layout

```
ai-video-editor/
├── .env / .env.example              # API keys (GEMINI_API_KEY, ANTHROPIC_API_KEY)
├── .vx.json                         # Workspace config (provider, style, locale, setup_complete)
├── pyproject.toml                   # Package metadata, deps, ruff config, entry point: vx
├── uv.lock                          # Dependency lock
├── install.sh                       # One-line macOS/Linux installer
├── CLAUDE.md                        # Claude Code agent guidance
├── README.md                        # User-facing documentation (920+ lines)
├── ROADMAP.md                       # Product roadmap
│
├── src/ai_video_editor/             # Main package — 28 .py files, 25,237 total lines
│   │
│   │  ── ENTRY POINTS & UI ──────────────────────────────────────────────
│   ├── cli.py                  (2752 lines, 37 functions)  argparse dispatch, project CRUD
│   ├── interactive.py          (2824 lines, 33 functions)  questionary TUI, pipeline state machine
│   ├── setup_wizard.py          (326 lines,  8 functions)  first-run wizard
│   ├── i18n/
│   │   ├── __init__.py          (163 lines,  9 functions)  t() lookup, locale detection
│   │   └── locales/
│   │       ├── en.json                                     English (master)
│   │       └── zh-TW.json                                  Traditional Chinese
│   │
│   │  ── CONFIGURATION & DATA MODELS ────────────────────────────────────
│   ├── config.py                (329 lines, 10 classes)    dataclass configs, path builders
│   ├── models.py                (882 lines, 52 classes)    all Pydantic models
│   ├── style_presets.py         (274 lines,  1 class)      StylePreset + Silent Vlog definition
│   │
│   │  ── PREPROCESSING & INGESTION ──────────────────────────────────────
│   ├── preprocess.py            (868 lines, 22 functions)  ffmpeg: proxy, frames, scenes, audio
│   ├── format_analyzer.py       (541 lines,  1 class)      device color profiles, format detection
│   ├── file_cache.py             (57 lines,  4 functions)  Gemini File API URI cache (46h TTL)
│   │
│   │  ── BRIEFING & TRANSCRIPTION ───────────────────────────────────────
│   ├── briefing.py             (1257 lines, 24 functions)  smart briefing + manual questionnaire
│   ├── transcribe.py            (785 lines, 16 functions)  mlx-whisper (local) / Gemini (cloud)
│   │
│   │  ── AI ANALYSIS (PHASES 1–3) ──────────────────────────────────────
│   ├── editorial_agent.py      (3032 lines, 29 functions)  pipeline orchestrator
│   ├── editorial_prompts.py    (1581 lines, 27 functions)  prompt construction
│   ├── gemini_analyze.py         (95 lines,  4 functions)  Gemini provider: video upload
│   ├── claude_analyze.py        (172 lines,  6 functions)  Claude provider: frame images
│   ├── storyboard_format.py      (74 lines,  2 functions)  shared prompt template
│   ├── section_grouping.py      (348 lines,  5 functions)  Timeline Mode: date grouping, merge
│   │
│   │  ── OUTPUT RENDERING & EXPORT ──────────────────────────────────────
│   ├── render.py               (1053 lines, 11 functions)  markdown + self-contained HTML preview
│   ├── rough_cut.py            (1527 lines,  1 class, 24 functions)  3-phase ffmpeg assembly
│   ├── fcpxml_export.py         (921 lines, 17 functions)  FCPXML v1.9 for DaVinci Resolve / FCP
│   │
│   │  ── VERSIONING, TRACING, EVALUATION ────────────────────────────────
│   ├── versioning.py            (797 lines, 38 functions)  two-phase commit, lineage DAG
│   ├── tracing.py               (839 lines,  4 classes, 20 functions)  LLM call logging, cost, retry
│   ├── eval.py                  (456 lines,  2 classes,  9 functions)  deterministic storyboard scoring
│   │
│   │  ── EDITORIAL DIRECTOR (EXPERIMENTAL) ──────────────────────────────
│   ├── editorial_director.py   (1343 lines, 20 functions)  multi-turn agent review loop
│   ├── director_prompts.py      (549 lines,  6 functions)  system prompt + 8 tool definitions
│   ├── director_tools.py       (1115 lines,  1 class, 19 functions)  tool impls, regression guard
│   └── review_display.py        (277 lines,  5 functions)  pretty-print review output
│
├── tests/
│   └── test_multi_call_pipeline.py  (41KB)  fixture-based integration tests
│
├── docs/                         17 design/research/planning documents (see §8)
│   ├── backlog-director-review.md
│   ├── design_fcpxml_export.md
│   ├── design-briefing-and-creative-brief.md
│   ├── design-editorial-director.md
│   ├── design-style-presets-visual-monologue.md
│   ├── design-timeline-mode.md
│   ├── dev-gemini-timestamp-drift.md
│   ├── dev-tracing-review.md
│   ├── feature-preview-editor-mode.md
│   ├── plan-codebase-hardening.md
│   ├── plan-llm-architecture.md
│   ├── product-owner.md
│   ├── prompt-engineering-cookbook.md
│   ├── research-llm-orchestration-and-local-models.md
│   ├── ideas/The-Art-of-the-Silent-Vlog-...md
│   └── refactor_plan/llm-architecture-improvement-plan.md
│
├── example/
│   └── family-hiking-in-Shipai.fcpxml
│
└── library/                          runtime project data (see §4 for full layout)
```

---

## 2. Module Inventory

### 2.1 Size Distribution

| Module | Lines | Functions | Classes | Role |
|--------|------:|----------:|--------:|------|
| editorial_agent.py | 3,032 | 29 | 0 | Pipeline orchestrator (Phase 1 + 2 + 3) |
| interactive.py | 2,824 | 33 | 0 | TUI state machine via questionary |
| cli.py | 2,752 | 37 | 0 | CLI dispatch, project CRUD |
| editorial_prompts.py | 1,581 | 27 | 0 | Prompt construction for all LLM calls |
| rough_cut.py | 1,527 | 24 | 1 | 3-phase ffmpeg assembly + validation |
| editorial_director.py | 1,343 | 20 | 0 | Multi-turn agent review loop |
| briefing.py | 1,257 | 24 | 0 | Smart briefing + manual questionnaire |
| director_tools.py | 1,115 | 19 | 1 | Director tool impls + regression guard |
| render.py | 1,053 | 11 | 0 | Markdown + self-contained HTML |
| fcpxml_export.py | 921 | 17 | 0 | FCPXML v1.9 export |
| models.py | 882 | 0 | 52 | All Pydantic data models |
| preprocess.py | 868 | 22 | 0 | ffmpeg preprocessing |
| tracing.py | 839 | 20 | 4 | LLM call logging, cost tracking |
| versioning.py | 797 | 38 | 0 | Two-phase commit, lineage DAG |
| transcribe.py | 785 | 16 | 0 | mlx-whisper / Gemini transcription |
| director_prompts.py | 549 | 6 | 0 | Director system prompt + tool schema |
| format_analyzer.py | 541 | 9 | 1 | Device color profiles, format analysis |
| eval.py | 456 | 9 | 2 | Deterministic storyboard scoring |
| section_grouping.py | 348 | 5 | 0 | Timeline Mode clip grouping |
| config.py | 329 | 0 | 10 | Dataclass configs + path builders |
| setup_wizard.py | 326 | 8 | 0 | First-run setup wizard |
| style_presets.py | 274 | 2 | 1 | StylePreset model + Silent Vlog |
| review_display.py | 277 | 5 | 0 | Pretty-print director output |
| claude_analyze.py | 172 | 6 | 0 | Claude frame-based provider |
| i18n/__init__.py | 163 | 9 | 0 | Lightweight i18n system |
| gemini_analyze.py | 95 | 4 | 0 | Gemini video upload provider |
| storyboard_format.py | 74 | 2 | 0 | Shared prompt template |
| file_cache.py | 57 | 4 | 0 | Gemini File API URI cache |
| **TOTAL** | **25,237** | **406** | **72** | |

### 2.2 Module Dependency Map

Arrows indicate "imports from". Only intra-package dependencies shown.

```
cli.py ──────────┬──→ config, briefing, interactive, setup_wizard,
                 │    editorial_agent, rough_cut, fcpxml_export,
                 │    models, versioning, eval, i18n, tracing,
                 │    format_analyzer, section_grouping, style_presets,
                 │    editorial_director
                 │
interactive.py ──┼──→ config, models, versioning, i18n, briefing,
                 │    editorial_agent, rough_cut, render, tracing,
                 │    format_analyzer, section_grouping, style_presets,
                 │    editorial_director, eval, preprocess
                 │
editorial_agent.py ──→ config, models, editorial_prompts, preprocess,
                       gemini_analyze, claude_analyze, versioning,
                       tracing, file_cache, briefing, transcribe,
                       section_grouping, style_presets, render,
                       format_analyzer, storyboard_format
                       │
                       ├──→ gemini_analyze ──→ config, storyboard_format, file_cache
                       └──→ claude_analyze ──→ config, storyboard_format, tracing
                       │
editorial_prompts.py ──→ config, models, i18n, storyboard_format
                       │
editorial_director.py ──→ config, models, director_prompts, director_tools,
                          versioning, tracing
                          │
                          ├──→ director_prompts ──→ config, models, storyboard_format
                          └──→ director_tools ──→ config, models, eval, preprocess
                       │
rough_cut.py ──→ config, models, render, preprocess, versioning
render.py ──→ models, config
fcpxml_export.py ──→ config, models
versioning.py ──→ models
tracing.py ──→ config
eval.py ──→ models
```

**Observation:** `editorial_agent.py` is the gravitational center — it imports from 16 other modules. `cli.py` and `interactive.py` are the two entry surfaces, each importing ~15 modules.

---

## 3. Data Model Catalog

### 3.1 All 51 Pydantic Models by Domain

#### Transcription (5 models)

| Model | Line | Key Fields | Notes |
|-------|-----:|------------|-------|
| TranscriptWord | 18 | word, start, end | Word-level timing |
| TranscriptSegment | 24 | start, end, text, words[], speaker, type | type: speech/music/sound_effect/silence |
| Transcript | 33 | segments[], duration_sec, has_speech, speakers[], provider | Canonical format for all providers |
| GeminiTranscriptSegment | 50 | start, end, text, speaker, type | **Gemini response_schema** |
| GeminiTranscript | 61 | language, segments[], speakers[], has_speech | **Gemini response_schema** |

#### Briefing & Creative Direction (9 models)

| Model | Line | Key Fields | Notes |
|-------|-----:|------------|-------|
| PersonSighting | 75 | description, estimated_appearances, role_guess | Quick scan detection |
| ClipSummary | 83 | clip_id, summary, energy | Quick scan per-clip |
| QuickScanResult | 89 | overall_summary, people[], activities[], mood, suggested_questions[], clip_summaries[] | **Gemini response_schema** |
| AudienceSpec | 108 | platform, viewer | Enhanced briefing |
| NarrativeDirection | 121 | story_thesis, story_hook, key_beats[], ending_note, structure | Enhanced briefing |
| StyleDirection | 141 | pacing, music_mood, energy_curve, transitions, visual_tone | Enhanced briefing |
| CreativeBrief | 166 | people, activity, tone, highlights, avoid, duration, context_qa, intent, audience, narrative, style, references, notes, creative_direction_text, brief_version, source, preset_key | Central user context model (17 fields) |
| CreativePreset | 237 | key, label, description, intent, tone, audience, narrative_defaults, style, references, style_preset_key, created_at | Reusable creative template |
| StylePreset | style_presets.py:14 | key, label, description, phase1_supplement, phase2_supplement, has_phase3, phase3_prompt, creator_references | **Only model outside models.py** |

#### Phase 1: Clip Review (7 models)

| Model | Line | Key Fields | Notes |
|-------|-----:|------------|-------|
| ReviewQuality | 269 | overall, stability, lighting, focus, composition | 1-10 numeric scores |
| ReviewPerson | 277 | label, description, role, screen_time_pct, speaking, timestamps[] | Per-person per-clip |
| ReviewKeyMoment | 293 | timestamp, timestamp_sec, description, editorial_value, suggested_use | Highlight extraction |
| ReviewUsableSegment | 304 | in_point, in_sec, out_point, out_sec, duration_sec, description, quality | The foundation of all cuts |
| ReviewDiscardSegment | 314 | in_point, out_point, reason | Explicit rejection |
| ReviewAudio | 322 | has_speech, speech_language, speech_summary, ambient_description, music_potential | Audio analysis |
| ClipReview | 336 | clip_id, summary, quality, content_type[], people[], key_moments[], usable_segments[], discard_segments[], audio, editorial_notes | **Gemini response_schema.** Central Phase 1 output. |

#### Phase 2: Editorial Assembly — Core (6 models)

| Model | Line | Key Fields | Notes |
|-------|-----:|------------|-------|
| CastMember | 363 | name, description, role, appears_in[] | Deduplicated across clips |
| Segment | 372 | index, clip_id, in_sec, out_sec, purpose, description, transition, audio_note, text_overlay | **Central hub model** — referenced by 11 other models |
| DiscardedClip | 414 | clip_id, reason | Explicit exclusion |
| MusicCue | 419 | section, strategy, notes | Music direction |
| StoryArcSection | 428 | title, description, segment_indices[] | Narrative structure |
| EditorialStoryboard | 436 | editorial_reasoning, title, estimated_duration_sec, style, story_concept, cast[], story_arc[], segments[], discarded[], music_plan[], technical_notes, pacing_notes | **Gemini response_schema.** Single source of truth consumed by render, rough cut, FCPXML, eval, director. |

#### Phase 2: Multi-Call Pipeline (9 models)

| Model | Line | Key Fields | Notes |
|-------|-----:|------------|-------|
| PlannedSegment | 488 | clip_id, usable_segment_index, purpose, arc_phase, narrative_role, audio_strategy, is_speech_segment | References Phase 1 usable_segment by index |
| StoryPlan | 509 | title, style, story_concept, cast[], story_arc[], planned_segments[], discarded[], pacing_notes, music_direction, constraint_satisfaction | **Gemini response_schema.** Call 2A.5 output. |
| Section | 540 | section_id, label, clip_ids[], time_range, activity | Date-based grouping unit |
| SectionGroup | 552 | group_id, date, label, sections[] | Per-day grouping |
| SectionNarrative | 561 | section_id, narrative_role, arc_phase, energy, target_duration_sec, section_goal, must_include[], must_exclude[], key_clips[] | Per-section editorial plan |
| SectionPlan | 591 | title, style, story_concept, section_narratives[], hook_section_id, hook_description, pacing_notes, music_direction, constraint_satisfaction | **Gemini response_schema.** Timeline Mode storyline. |
| ScenePlan | 607 | sections[], reasoning | **Gemini response_schema.** Scene grouping strategy. |
| SectionStoryboard | 617 | section_id, segments[], discarded[], cast[], narrative_summary, music_cue, editorial_reasoning | Per-section Phase 2 output |
| HookStoryboard | 634 | segments[], editorial_reasoning, hook_concept | Opening hook output |

#### Phase 3: Visual Monologue (7 models)

| Model | Line | Key Fields | Notes |
|-------|-----:|------------|-------|
| EligibleSegment | 649 | segment_index, segment_duration_sec, arc_phase, intent, preceding_context, following_context, max_overlay_count, notes | Call 1 eligibility analysis |
| OverlayPlan | 668 | persona_recommendation, persona_rationale, eligible_segments[] | **Gemini response_schema.** Call 1 output. |
| OverlayDraft | 683 | segment_index, text, appear_at, duration_sec, word_count, arc_phase | Individual text card |
| OverlayDrafts | 696 | overlays[] | Wrapper for Call 2 |
| TextOverlayStyle | 702 | font, case, size, position, alignment | Rendering config |
| MonologueOverlay | 710 | index, segment_index, text, appear_at, duration_sec, note | Final overlay after validation |
| MonologuePlan | 719 | persona, persona_description, tone_mechanics, arc_structure, overlays[], total_text_time_sec, pacing_notes, music_sync_notes | **Gemini response_schema.** Final Phase 3 output. |

#### Editorial Director (5 models)

| Model | Line | Key Fields | Notes |
|-------|-----:|------------|-------|
| SegmentIssue | 735 | segment_index, dimension, severity, description, suggested_fix | Per-issue diagnostic |
| ReviewVerdict | 749 | passed, scores, issues[], summary | Pass/fail with per-dimension scores |
| ReviewIteration | 760 | turn, tool_name, tool_args, result_summary, cost_usd, duration_sec | Single agent turn |
| SegmentChange | 771 | change_type, segment_index, fields_changed, before, after | Edit audit record |
| ReviewLog | 781 | iterations[], final_verdict, total_turns, total_fixes, total_cost_usd, total_duration_sec, convergence_reason, changes[], eval_before, eval_after | Full review session log |

#### Chat & Session (2 models)

| Model | Line | Key Fields | Notes |
|-------|-----:|------------|-------|
| ChatMessage | 801 | role, text, tool_calls, tool_responses, timestamp | Director conversation turn |
| ChatSession | 811 | session_id, created_at, updated_at, status, storyboard_version, starting_version, provider, messages[], budget_state, total_edits, style_preset | Persistent session state |

#### Versioning & Composition (3 models)

| Model | Line | Key Fields | Notes |
|-------|-----:|------------|-------|
| ArtifactMeta | 832 | artifact_id, phase, provider, version, status, created_at, completed_at, parent_id, inputs{}, clip_id, track, config_snapshot, output_files[], error | Sidecar lineage metadata |
| Composition | 855 | name, storyboard, monologue, created_at, notes | Named version combination |
| CutComposition | 868 | cut_id, created_at, storyboard, monologue, transcription_provider, briefing, style_preset, output_format | Full provenance manifest |

### 3.2 Model Dependency Graph

`Segment` is the single most-referenced model in the codebase — it is the atomic unit of every storyboard, review, cut, and export.

```
                         ┌─────────────────────────────┐
                         │    EditorialStoryboard       │
                         │  (central contract model)    │
                         └─┬───┬───┬───┬───┬───┬───────┘
                           │   │   │   │   │   │
              CastMember ──┘   │   │   │   │   └── Section
              Segment ─────────┘   │   │   │
              StoryArcSection ─────┘   │   │
              DiscardedClip ───────────┘   │
              MusicCue ────────────────────┘

ClipReview ──┬── ReviewQuality
             ├── ReviewPerson
             ├── ReviewKeyMoment
             ├── ReviewUsableSegment     ←── PlannedSegment references by index
             ├── ReviewDiscardSegment
             └── ReviewAudio

StoryPlan ───┬── CastMember, StoryArcSection, PlannedSegment, DiscardedClip
SectionPlan ─┬── SectionNarrative, Section
MonologuePlan ── MonologueOverlay
OverlayPlan ──── EligibleSegment
ReviewLog ────── ReviewIteration, ReviewVerdict, SegmentChange
CreativeBrief ── AudienceSpec, NarrativeDirection, StyleDirection
Transcript ───── TranscriptSegment ── TranscriptWord
ChatSession ──── ChatMessage
```

**Cross-domain bridge points:**
- `Segment.clip_id` → links to `ClipReview.clip_id` → links to `manifest.json` clip entries
- `PlannedSegment.usable_segment_index` → indexes into `ClipReview.usable_segments[]`
- `MonologueOverlay.segment_index` → indexes into `EditorialStoryboard.segments[]`
- `ArtifactMeta.inputs{}` → references upstream artifact IDs (versioning DAG)

---

## 4. Pipeline Data Flow

### 4.1 Phase-by-Phase Flow

```
Raw clips (4K, mixed formats, any codec)
    │
    ▼
[1. Preprocessing] ─── ThreadPoolExecutor, 4 workers, per-clip cached
    │  IN:   Source video files
    │  OUT:  proxy/ (360p 1fps H.264), frames/ (JPEGs), scenes/ (boundaries), audio/ (16kHz WAV)
    │  DISK: clips/<id>/{proxy,frames,scenes,audio}/ + manifest.json
    │  TOOL: ffmpeg subprocess (SW decode, HW encode via VideoToolbox)
    │
    ▼
[2. Format Analysis] ─── deterministic, no LLM
    │  IN:   manifest.json (resolution, codec, fps, orientation per clip)
    │  OUT:  OutputFormat recommendation (target resolution, codec, color space)
    │  DISK: Ephemeral — passed in memory to rough_cut
    │
    ▼
[3. Smart Briefing] ─── 1 Gemini call (quick scan of all proxies)
    │  IN:   All proxy videos → Gemini File API (cached in file_api_cache.json)
    │  LLM:  Gemini Flash, structured output → QuickScanResult
    │  OUT:  QuickScanResult → targeted questions → user answers → CreativeBrief
    │  DISK: quick_scan_v*.json, user_context_v*.json
    │  COST: ~$0.01-0.02 per project
    │
    ▼
[4. Transcription] ─── parallel, per-clip cached
    │  IN:   Proxy video (Gemini path) or WAV (mlx path) + speaker names from briefing
    │  LLM:  Gemini Flash (speaker ID, sound events, 90s chunking) or mlx-whisper (local, free)
    │  OUT:  Transcript model per clip
    │  DISK: transcript_{provider}_v*.json + transcript.vtt + transcript_preview.html
    │  REUSES: Gemini File API URIs from briefing
    │
    ▼
[5. Phase 1: Clip Review] ─── ThreadPoolExecutor, 5 workers, per-clip cached
    │  IN:   Proxy video (Gemini) or frames (Claude) + transcript excerpt + CreativeBrief
    │  LLM:  Gemini (native video + structured output schema) or Claude (base64 frames + text parse)
    │  PROMPT: build_clip_review_prompt() from editorial_prompts.py
    │  OUT:  ClipReview model per clip
    │  DISK: review_{provider}_v*.json + review_{provider}_v*.meta.json
    │  REUSES: Gemini File API URIs
    │
    ▼
[6. Phase 2: Editorial Assembly] ─── sequential, 1–3 LLM calls (Story) or 2+N (Timeline)
    │
    │  STORY MODE (default — split pipeline):
    │  ┌─ Call 2A  (reasoning)    Gemini Pro, temp 0.8
    │  │  IN:  Condensed clip reviews + transcripts + CreativeBrief + optional proxy video bundle
    │  │  OUT: Freeform editorial reasoning prose (~500-2000 tokens)
    │  │
    │  ├─ Call 2A.5 (structuring) Gemini Flash Lite, temp 0.2
    │  │  IN:  Call 2A prose + condensed reviews
    │  │  OUT: StoryPlan (segment refs by usable_segment_index, no timestamps yet)
    │  │
    │  └─ Call 2B  (assembly)     Gemini Flash, temp 0.3
    │     IN:  StoryPlan + usable_segment timestamp windows from Phase 1
    │     OUT: EditorialStoryboard (final segments with precise in_sec/out_sec)
    │
    │  TIMELINE MODE (chronological):
    │  ┌─ Storyline call:  Plan arc across sections, assign constraints per section
    │  ├─ Hook call:       Create 10-15s cinematic opening from best moments
    │  ├─ Per-section calls (N): Each receives only its clips + cumulative narrative summary
    │  └─ Deterministic merge: merge_section_storyboards() re-indexes, unions cast/music
    │
    │  DISK: editorial_{provider}_v*.json + .meta.json + .md + preview.html
    │
    ▼
[7. Phase 3: Monologue] ─── optional, if style preset has_phase3=True (e.g., Silent Vlog)
    │  Call 1: Identify eligible silent segments → OverlayPlan (persona selection + eligibility)
    │  Call 2: Generate text overlays → MonologuePlan (timed text cards, two-breath rule)
    │  DISK: monologue_{provider}_v*.json + .meta.json
    │
    ▼
[8. Editorial Director Review] ─── optional, experimental
    │  Multi-turn Gemini Flash agent loop (up to 50 turns, $0.50 budget)
    │  8 tools: screenshot_segment, get_transcript_excerpt, get_full_transcript,
    │           get_clip_review, run_eval_check, get_unused_footage,
    │           edit_timeline, finalize_review
    │  Regression guard: eval scores checked before/after edits; reverted if scores drop
    │  DISK: Modified EditorialStoryboard + review_log.json
    │
    ▼
[9. Render] ─── deterministic, no LLM
    │  IN:   EditorialStoryboard + optional MonologuePlan
    │  OUT:  preview.html (self-contained: contact strip, timeline visualization, video playback)
    │        preview.md (markdown EDL)
    │
    ▼
[10. Rough Cut] ─── deterministic, 3-phase ffmpeg
    │  Phase A: Per-segment extraction + format normalization
    │           (SW decode, HW encode, forced IDR at frame 0, fps normalization)
    │  Phase B: 3-layer validation (per-segment, cross-segment compatibility, post-concat)
    │  Phase C: Concat demuxer with stream copy
    │  DISK: exports/cuts/cut_NNN/rough_cut.mp4 + composition.json + preview.html
    │
    ▼
[11. FCPXML Export] ─── deterministic, no LLM
       IN:   EditorialStoryboard + manifest.json + source file paths + transcripts
       OUT:  .fcpxml (v1.9, all manifest clips as assets) + timeline_subtitles.srt + per-clip .srt
       DISK: exports/*.fcpxml + exports/subtitles/
```

### 4.2 Data Contract Table

| Phase Transition | Pydantic Model | Disk Artifact | Versioned? |
|-----------------|----------------|---------------|:----------:|
| Preprocessing → downstream | — (dict in manifest.json) | `manifest.json`, per-clip dirs | No |
| Quick Scan → Briefing | `QuickScanResult` | `quick_scan_v*.json` | Yes |
| Briefing → Phase 1, Phase 2 | `CreativeBrief` | `user_context_v*.json` | Yes |
| Transcription → Phase 1, Phase 2, Director | `Transcript` | `transcript_{provider}_v*.json` | Yes |
| Phase 1 → Phase 2 | `ClipReview` (per clip) | `review_{provider}_v*.json` | Yes |
| Phase 2A → 2A.5 | Freeform text (str) | Not persisted | — |
| Phase 2A.5 → 2B | `StoryPlan` | Optionally persisted | — |
| Phase 2 → Phase 3, Render, Cut, FCPXML | `EditorialStoryboard` | `editorial_{provider}_v*.json` | Yes |
| Phase 3 → Cut, Render | `MonologuePlan` | `monologue_{provider}_v*.json` | Yes |
| Director → Cut | Modified `EditorialStoryboard` | Overwrites storyboard JSON | Yes |
| Cut → User | — | `rough_cut.mp4` + `composition.json` | Yes |
| Cross-phase shared | Gemini File API URI | `file_api_cache.json` (46h TTL) | No |
| All LLM calls | — (trace record) | `traces.jsonl` (append-only) | No |
| All versioned outputs | `ArtifactMeta` | `*_v*.meta.json` sidecar | Yes |

### 4.3 Runtime Project Layout

```
library/<project>/
├── project.json                        # {name, type, provider, style_preset, version_counters{}, tracks[]}
├── compositions.json                   # [{name, storyboard, monologue, created_at, notes}]
├── manifest.json                       # [{clip_id, filename, source_path, duration_sec, resolution, fps, creation_time}]
├── user_context_v1.json                # CreativeBrief (versioned)
├── user_context_latest.json            # Symlink → latest version
├── quick_scan_v1.json                  # QuickScanResult (versioned)
├── quick_scan_latest.json              # Symlink → latest
├── file_api_cache.json                 # {clip_id: {uri, cached_at}} — shared, 46h TTL
├── traces.jsonl                        # Append-only: {phase, model, clip_id, tokens, cost, duration, success}
│
├── clips/<clip-id>/
│   ├── source/                         # Original video (symlink or copy)
│   ├── proxy/*.mp4                     # 360p 1fps H.264
│   ├── frames/
│   │   ├── manifest.json               # [{filename, timestamp_sec}]
│   │   └── frame_NNNNN.jpg             # Extracted keyframes
│   ├── scenes/manifest.json            # Scene boundaries
│   ├── audio/
│   │   ├── <clip-id>.wav               # 16kHz mono WAV
│   │   ├── transcript_gemini_v1.json   # Gemini transcript (versioned)
│   │   ├── transcript_mlx_v1.json      # mlx-whisper transcript (versioned)
│   │   ├── transcript_latest.json      # Symlink → latest (any provider)
│   │   ├── transcript.vtt              # WebVTT subtitles
│   │   └── transcript_preview.html     # Video + captions viewer
│   └── review/
│       ├── review_gemini_v1.json       # Phase 1 ClipReview (versioned)
│       ├── review_gemini_v1.meta.json  # ArtifactMeta sidecar
│       └── review_gemini_latest.json   # Symlink → latest
│
├── storyboard/
│   ├── editorial_gemini_v1.json        # Phase 2 EditorialStoryboard
│   ├── editorial_gemini_v1.meta.json   # ArtifactMeta (inputs, config, status)
│   ├── editorial_gemini_v1.md          # Rendered markdown
│   ├── editorial_gemini_latest.json    # Symlink → latest
│   ├── monologue_gemini_v1.json        # Phase 3 MonologuePlan
│   ├── monologue_gemini_v1.meta.json   # ArtifactMeta
│   └── <track>/                        # Experiment track variants
│
└── exports/
    ├── v1/                             # Preview tied to storyboard version
    │   ├── preview.html                # Interactive preview
    │   └── thumbnails/                 # Contact strip images
    ├── cuts/
    │   ├── cut_001/
    │   │   ├── rough_cut.mp4           # Assembled video
    │   │   ├── composition.json        # CutComposition (full provenance)
    │   │   ├── preview.html            # Self-contained preview
    │   │   └── segments/               # Per-segment .mp4 (intermediate)
    │   └── latest -> cut_001/
    ├── <project>.fcpxml                # FCPXML v1.9 for DaVinci Resolve / FCP
    ├── timeline_subtitles.srt          # Timeline-aligned SRT
    └── subtitles/<clip-id>.srt         # Per-clip source-relative SRT
```

---

## 5. Intentional Architectural Patterns

### 5.1 Pydantic as Single Source of Truth

All data structures live in `models.py` (882 lines, 52 classes). `EditorialStoryboard` is the central contract consumed by every downstream stage: render, rough cut, FCPXML export, eval scoring, and the Editorial Director.

Field descriptions serve a dual purpose — they're both documentation and **inline Gemini instructions**. When passed as `response_schema`, Gemini reads field descriptions as generation constraints. This follows the PARSE paper finding of +64.7% accuracy from field-level instructions (documented in `docs/prompt-engineering-cookbook.md`).

10 models are explicitly designed as Gemini response schemas: `GeminiTranscript`, `QuickScanResult`, `ClipReview`, `EditorialStoryboard`, `StoryPlan`, `SectionPlan`, `ScenePlan`, `OverlayPlan`, `MonologuePlan`, and `OverlayDrafts`.

### 5.2 Two-Phase Commit Versioning

Every LLM-generated artifact follows a transactional protocol (`versioning.py`):

1. `begin_version(phase, provider, inputs, config_snapshot)` — reserves version number, writes `.meta.json` with `status: "pending"`
2. Work proceeds (LLM call, parsing, validation)
3. `commit_version(meta, output_paths)` — marks `status: "complete"`, updates `_latest` symlink, increments `project.json` counter
4. On failure: `fail_version(meta, error)` — marks `status: "failed"`, counter/symlink untouched

**Lineage tracking:** Each `.meta.json` records `inputs: {upstream_artifact_id: version}`, enabling full DAG reconstruction. Example: storyboard v3's meta records `{"review:C0059": "rv.2", "review:C0073": "rv.1", "user_context": "uc.2"}`.

**Lineage ID format:** `{stage_code}:{parent_id}.{version}`. Stage codes: `sc` (scan), `uc` (user_context), `tr` (transcript), `rv` (review), `sb` (storyboard), `mn` (monologue).

**Compositions** (`Composition` model) allow mixing versions: storyboard v3 + monologue v1. **Experiment tracks** namespace outputs under `storyboard/<track>/` without disturbing main.

### 5.3 Multi-Call Split Pipeline (Phase 2)

Phase 2 separates three cognitive tasks into distinct LLM calls, each with optimized model/temperature:

| Call | Model | Temp | Purpose | Input | Output |
|------|-------|------|---------|-------|--------|
| 2A | Gemini Pro | 0.8 | Creative reasoning | Full reviews + transcripts + brief | Freeform prose |
| 2A.5 | Flash Lite | 0.2 | Structuring | 2A prose + reviews | `StoryPlan` JSON |
| 2B | Flash | 0.3 | Timestamp assembly | StoryPlan + usable_segment windows | `EditorialStoryboard` JSON |

This design was motivated by the `refactor_plan/llm-architecture-improvement-plan.md` finding that single-call Phase 2 produced either creative-but-imprecise or precise-but-boring results, never both. Separating reasoning from structuring allows different temperature/model tradeoffs for each.

### 5.4 Structural Chronological Enforcement (Timeline Mode)

Timeline Mode (`design-timeline-mode.md`) solves a concrete LLM failure: given 20-50 clips at once, LLMs consistently reorder vlog scenes "creatively," destroying the narrative timeline.

The fix is structural, not prompt-based: clips are grouped by date/time gap (`section_grouping.py`), each section edited independently with only its own clips, then merged deterministically. The LLM never sees all clips simultaneously, so it cannot reorder across sections.

### 5.5 Gemini File API URI Reuse

A single proxy video upload is reused across 4 pipeline phases: briefing quick scan → transcription → Phase 1 review → Phase 2 visual mode. `file_cache.py` stores `{clip_id: {uri, cached_at}}` with a 46h TTL (under Gemini's 48h retention). This saves significant upload time for multi-phase pipelines with many clips.

### 5.6 Deterministic Post-LLM Pipeline

Render, rough cut, FCPXML export, and eval are pure functions of the Pydantic models. Zero LLM calls. If the storyboard JSON is correct, the video is correct. This creates a clean debugging boundary: all quality issues trace back to LLM outputs (Phases 1-3), not rendering logic.

### 5.7 Three-Tier Prompt Constraint Hierarchy

User context is structured into three priority tiers in `format_context_for_prompt()`:

1. **CONSTRAINTS** (non-negotiable): `highlights` → MUST-INCLUDE, `avoid` → MUST-EXCLUDE
2. **DIRECTION** (editorial): tone, narrative structure, audience, duration
3. **PREFERENCES** (best-effort): context Q&A, style notes, references

This hierarchy is enforced by the eval system (`eval.py`) and the Editorial Director (constraint satisfaction is a hard quality gate at 100%).

### 5.8 Locale-Aware LLM Output

When a non-English locale is active, `_output_language_directive()` in `editorial_prompts.py` appends an instruction telling the LLM to produce human-readable fields (descriptions, reasoning, narrative) in the user's language while keeping structural fields (clip IDs, JSON keys, timestamps, model field names) in English. This prevents parser breakage while delivering localized content.

### 5.9 Filesystem as State Machine

All project state is represented by files on disk:
- `project.json` — version counters, tracks, metadata
- `_latest` symlinks — current active version per stage
- `.meta.json` sidecars — lineage, status, config snapshot
- `composition.json` — full provenance manifest per cut
- `traces.jsonl` — append-only LLM call log

No database. This enables `ls`/`cat` debugging, git-friendly backup, and trivial project migration (copy the directory).

### 5.10 Failure-Driven ffmpeg Design

The rough cut pipeline's design choices each trace to a specific real-world failure (documented in README):

| Design Choice | Failure It Prevents |
|---------------|-------------------|
| Software decode, hardware encode | VideoToolbox HW decoder drops frames during fast-seek → 1s freezes |
| Forced IDR keyframe at frame 0 | Concat demuxer hits undecodable P/B-frames at segment boundaries |
| `fps=` filter always applied | Mismatched timebases between source clips → PTS discontinuities |
| `-c:a copy` during concat | A/V sync drift when audio re-encoded but video stream-copied |
| `-avoid_negative_ts make_zero` | Negative timestamps → corrupt moov atom edit lists |
| `-fflags +genpts` | Residual PTS irregularities at segment joins |

---

## 6. Emergent & Accidental Patterns

### 6.1 God Modules

Three files contain 34% of the entire codebase:

| Module | Lines | Responsibilities |
|--------|------:|-----------------|
| `editorial_agent.py` | 3,032 | Clip discovery, preprocessing orchestration, briefing dispatch, transcription dispatch, Phase 1 execution, Phase 2 execution (Story + Timeline), Phase 3 execution, all provider branching, clip ID resolution, visual mode concatenation, retry logic |
| `interactive.py` | 2,824 | TUI state machine, pipeline status display, project selection, section management, briefing UI, output format configuration, versioning/composition UI, style preset selection, experiment track management |
| `cli.py` | 2,752 | argparse with 15+ subcommands, project creation, status display, config management, version listing, composition management, track management, eval batch runner |

**Impact:** Each file mixes 5-10 distinct responsibilities. Refactoring a single concern (e.g., Timeline Mode) requires editing functions scattered across 3000 lines.

### 6.2 Ad-Hoc Provider Dispatch

23 instances of `if provider == "gemini"` / `elif provider == "claude"` string comparisons scattered across 3 files:

- `editorial_agent.py` — 12 instances (lines 291, 340, 1577, 2090, 2123, 2170, 2604, 2627, 2815, 2944, 2954, and within sub-expressions)
- `interactive.py` — 7 instances (lines 878, 998, 1048, 1069, 1905, 1943, 1964)
- `cli.py` — 2 instances (lines 712, 717)

No interface, no strategy pattern, no provider registry. Adding a third provider requires editing all three files and hunting for every branch.

### 6.3 Duplicated Utility Functions

**`_wait_for_gemini_file()` — 4 implementations:**

| Location | Sleep Interval | FAILED Check | Timeout Source |
|----------|:-:|:-:|----------------|
| `editorial_agent.py:51` | 3s | No | `GEMINI_UPLOAD_TIMEOUT_SEC` (300) |
| `briefing.py:22` | 2s | No | `_GEMINI_UPLOAD_TIMEOUT_SEC` (300) |
| `transcribe.py:25` | 3s | Yes | `_GEMINI_UPLOAD_TIMEOUT_SEC` (300) |
| `gemini_analyze.py:24` | 5s | Inline variant | `_GEMINI_UPLOAD_TIMEOUT_SEC` (300) |

The `transcribe.py` version is the most complete (includes FAILED state check). The others silently proceed on failure.

**`_resolve_clip_source()` — 3 implementations:**

| Location | Manifest Lookup | Legacy source/ | Proxy Fallback |
|----------|:-:|:-:|:-:|
| `rough_cut.py:43` | Yes (via source_map) | Yes | Conditional (parameter) |
| `render.py:139` | No | Yes | Always (via helper) |
| `fcpxml_export.py:160` | Yes (via source_map) | Yes | Never (strict) |

Different fallback behavior means the same clip can resolve to different files depending on which stage is looking for it.

**Gemini client creation — 7 locations:**

| Location | Pattern |
|----------|---------|
| `gemini_analyze.py:18` | `get_client()` helper |
| `editorial_agent.py:76` | `_get_gemini_client()` helper |
| `editorial_director.py:265` | Direct `genai.Client()` |
| `editorial_director.py:700` | Direct `genai.Client()` |
| `briefing.py:301` | Direct `genai.Client()` |
| `transcribe.py:283` | Direct `genai.Client()` |
| `transcribe.py:359` | Direct `genai.Client()` |

No singleton, no connection pooling, no shared utility.

### 6.4 Hardcoded Constants (No Configuration Path)

| Constant | Value | File:Line | Notes |
|----------|-------|-----------|-------|
| `MAX_PREPROCESS_WORKERS` | 4 | editorial_agent.py:177 | ffmpeg parallelism |
| `MAX_LLM_WORKERS` | 5 | editorial_agent.py:180 | LLM API parallelism |
| `MAX_TRANSCRIBE_WORKERS_MLX` | 2 | editorial_agent.py:247 | ~3GB RAM per whisper instance |
| `GEMINI_UPLOAD_TIMEOUT_SEC` | 300 | editorial_agent.py:48 | Duplicated in 3 other files |
| `_GEMINI_UPLOAD_TIMEOUT_SEC` | 300 | briefing.py:19 | Duplicate |
| `_GEMINI_UPLOAD_TIMEOUT_SEC` | 300 | transcribe.py:22 | Duplicate |
| `_GEMINI_UPLOAD_TIMEOUT_SEC` | 300 | gemini_analyze.py:21 | Duplicate |
| `MAX_LLM_RETRIES` | 3 | tracing.py:31 | Retry attempts |
| `BASE_RETRY_DELAY_SEC` | 2.0 | tracing.py:32 | Initial backoff |
| `GEMINI_VIDEO_TOKENS_PER_SEC` | 263 | tracing.py:95 | Token estimation |
| `TRANSCRIBE_CHUNK_SEC` | 90 | transcribe.py:45 | Gemini drift mitigation |
| `FILE_API_CACHE_MAX_AGE_SEC` | 165,600 | file_cache.py:12 | 46h (Gemini retains 48h) |
| `MAX_CONCAT_DURATION_SEC` | 2,400 | preprocess.py:614 | 40-min Gemini safety cap |
| `DEFAULT_TRANSITION_SEC` | 1.0 | fcpxml_export.py:26 | Cross-dissolve duration |
| `THUMB_WIDTH` / `THUMB_HEIGHT` | 160/90 | render.py:162-163 | Contact strip thumbnail size |
| `LIVE_PHOTO_MAX_DURATION` | 4.0 | format_analyzer.py:216 | Live Photo detection |
| `_MAX_DIMENSION_DROP` | 0.10 | director_tools.py:415 | Regression guard threshold |
| `_FRAC_LIMIT` | 1,000,000 | fcpxml_export.py:52 | Rational fraction denominator |

None of these are configurable via CLI flags, config files, or environment variables.

### 6.5 Inconsistent Validation Boundaries

**Validated (Pydantic `model_validate_json`):**
- `cli.py:826` — storyboard load
- `rough_cut.py:1362` — storyboard load
- `interactive.py:195` — storyboard load
- `versioning.py:606` — composition load

**Unvalidated (plain `json.loads` → dict):**
- `rough_cut.py:34` — manifest load
- `rough_cut.py:1063` — transcript load
- `rough_cut.py:1316` — project.json load
- `fcpxml_export.py:151` — manifest load
- `editorial_agent.py:108` — manifest load
- `editorial_agent.py:289` — transcript load
- `briefing.py:86` — user context load
- `file_cache.py:20` — file API cache load
- `preprocess.py:74` — ffprobe output
- `preprocess.py:451` — scene manifest load

**Pattern:** Storyboard files are consistently validated; everything else (manifest, transcript, user context, project metadata) is loaded as raw dicts. A schema change or manual edit in these files produces runtime `KeyError`/`TypeError` with no diagnostic message.

### 6.6 Non-Atomic File Writes

All artifact writes use direct `Path.write_text()` with no atomic write pattern (write-to-temp + rename):

- `editorial_agent.py:239` — manifest.json
- `editorial_agent.py:600` — review JSON
- `editorial_agent.py:1124` — scene plan
- `editorial_agent.py:1427` — storyboard JSON
- `editorial_director.py:1177` — storyboard JSON
- `briefing.py:238` — user context
- `briefing.py:997` — creative brief
- `transcribe.py:90` — transcript
- `versioning.py:51` — project.json
- `interactive.py:917` — project.json

A crash mid-write leaves a truncated/corrupt artifact file. The two-phase commit protocol tracks *metadata* atomicity but not *content* atomicity.

### 6.7 Configuration Fragmentation

Settings live in 5 separate locations with no unified resolution:

| Source | Examples | Read By |
|--------|----------|---------|
| `config.py` code defaults | Model names, temperatures, worker counts, fps | Everything |
| `.vx.json` workspace file | provider, style, locale, setup_complete | cli.py, setup_wizard.py |
| `project.json` per-project | type, provider, style_preset, version_counters | versioning.py, cli.py |
| `.env` file | GEMINI_API_KEY, ANTHROPIC_API_KEY | dotenv → os.environ |
| CLI flags | --provider, --visual, --timeline, --force, --max-cost | cli.py argparse |

No schema validation on `.vx.json` or `project.json`. Typos fail silently. No precedence hierarchy documented.

### 6.8 Minimal Test Coverage

Single test file: `tests/test_multi_call_pipeline.py` (41KB). Tests depend on `library/family-hiking-in-Shipai/` existing locally — fail without it. Test scope: prompt building, constraint extraction, formatting. No mocks of LLM calls or ffmpeg. No CI. No unit tests for: versioning protocol, file cache expiration, clip ID resolution, timestamp clamping, FCPXML generation, eval scoring, section grouping, or i18n.

---

## 7. Concrete Anti-Pattern Evidence

### 7.1 Provider Branching — Full Inventory

```
cli.py:712          if provider == "gemini":
cli.py:717          elif provider == "claude":
editorial_agent.py:291   if provider == "gemini":
editorial_agent.py:340   ... if provider == "gemini" else MAX_TRANSCRIBE_WORKERS_MLX
editorial_agent.py:1577  if provider == "gemini":
editorial_agent.py:2090  if provider == "gemini":
editorial_agent.py:2123  elif provider == "claude":
editorial_agent.py:2170  if provider == "gemini" and user_context and gemini_cfg:
editorial_agent.py:2604  if provider == "gemini":
editorial_agent.py:2627  elif provider == "claude":
editorial_agent.py:2815  if provider == "gemini":
editorial_agent.py:2944  if provider == "gemini":
editorial_agent.py:2954  elif provider == "claude":
interactive.py:878       ... if provider == "gemini" else "ANTHROPIC_API_KEY"
interactive.py:998       if provider == "gemini":
interactive.py:1048      if provider == "gemini":
interactive.py:1069      if provider == "gemini":
interactive.py:1905      if provider == "gemini":
interactive.py:1943      if provider == "gemini":
interactive.py:1964      if provider == "gemini":
```

### 7.2 Duplicated Constants — GEMINI_UPLOAD_TIMEOUT_SEC

Four separate definitions of the same 300-second timeout:

```
editorial_agent.py:48    GEMINI_UPLOAD_TIMEOUT_SEC = 300
briefing.py:19           _GEMINI_UPLOAD_TIMEOUT_SEC = 300
transcribe.py:22         _GEMINI_UPLOAD_TIMEOUT_SEC = 300
gemini_analyze.py:21     _GEMINI_UPLOAD_TIMEOUT_SEC = 300
```

Changing the timeout requires updating 4 files. No shared constant.

### 7.3 _wait_for_gemini_file — Behavioral Divergence

The three standalone implementations have subtly different behavior:

```python
# editorial_agent.py:51 — sleeps 3s, NO failure check
def _wait_for_gemini_file(video_file, client, timeout_sec=GEMINI_UPLOAD_TIMEOUT_SEC):
    while video_file.state.name == "PROCESSING":
        if time.monotonic() - start > timeout_sec:
            raise TimeoutError(...)
        time.sleep(3)
        video_file = client.files.get(name=video_file.name)
    return video_file  # ← returns even if state == "FAILED"

# briefing.py:22 — sleeps 2s, NO failure check
def _wait_for_gemini_file(video_file, client, timeout_sec=_GEMINI_UPLOAD_TIMEOUT_SEC):
    while video_file.state.name == "PROCESSING":
        ...
        time.sleep(2)
    return video_file  # ← returns even if state == "FAILED"

# transcribe.py:25 — sleeps 3s, HAS failure check
def _wait_for_gemini_file(video_file, client, label=""):
    while video_file.state.name == "PROCESSING":
        ...
        time.sleep(3)
    if video_file.state.name == "FAILED":   # ← only version with this check
        raise RuntimeError(f"Gemini file processing failed for {label}")
    return video_file
```

`editorial_agent.py` and `briefing.py` silently return a FAILED file object, which downstream code will attempt to use — producing opaque errors.

### 7.4 _resolve_clip_source — Fallback Divergence

```python
# rough_cut.py:43 — conditional proxy fallback
def _resolve_clip_source(clip_id, editorial_paths, source_map=None, proxy_fallback=False):
    # 1. Try source_map (manifest.json source_path)
    # 2. Try legacy source/ dir
    # 3. If proxy_fallback=True: try proxy/*.mp4
    # 4. Return None

# render.py:139 — always falls back to proxy
def _resolve_clip_source(clip_id, clips_dir):
    # 1. Try source/ dir
    # 2. Always fall back to proxy via _resolve_clip_proxy()
    # 3. Return None

# fcpxml_export.py:160 — never falls back to proxy
def _resolve_clip_source(clip_id, editorial_paths, source_map):
    # 1. Try source_map
    # 2. Try legacy source/ dir
    # 3. Return None (no proxy fallback — strict)
```

Same clip, three different resolution outcomes depending on which stage asks.

---

## 8. Design Doc Landscape

### 8.1 Active Design Documents

| Document | Status | Key Insight |
|----------|--------|-------------|
| `design_fcpxml_export.md` | Implemented | 13 documented DaVinci Resolve pitfalls; embedded timecodes critical for Sony; flat timeline (no compound clips) |
| `design-briefing-and-creative-brief.md` | Implemented | Three-tier questioning (context → intent → preferences); CreativeBrief as persistent context model |
| `design-editorial-director.md` | Implemented (experimental) | Visual review default (contact strip <$0.0007); 8 tools; regression guard; budget enforcement |
| `design-style-presets-visual-monologue.md` | Implemented | Phase 1/2 supplements + Phase 3 activation; three monologue personas; two-breath rule |
| `design-timeline-mode.md` | Implemented | Structural chronological enforcement; section-based editing; constraint distribution at planning stage |

### 8.2 Development & Operations Docs

| Document | Key Insight |
|----------|-------------|
| `dev-gemini-timestamp-drift.md` | Progressive clock speedup on long videos; 90s chunking mitigation; model-specific severity |
| `dev-tracing-review.md` | Phoenix + traces.jsonl debugging workflow; red flags for model hallucination |
| `plan-codebase-hardening.md` | 5-batch hardening (XSS, path traversal, subprocess timeouts, error handling, resource leaks) — all complete |
| `plan-llm-architecture.md` | Phoenix integration, retry/resilience, response validation, cost management — phases A-B done |
| `prompt-engineering-cookbook.md` | PARSE paper (+64.7% from field descriptions), context rot (20-50% drop at 100K), lost-in-the-middle effect, temperature calibration |

### 8.3 Strategic & Research Docs

| Document | Key Insight |
|----------|-------------|
| `product-owner.md` | Two personas: casual sharer (MP4) + power editor (FCPXML+Resolve); prioritization scoring formula; anti-goals (won't become GUI editor) |
| `research-llm-orchestration-and-local-models.md` | Local ML candidates on M4 Pro (mlx-whisper proven, CLIP/SigLIP testable, Qwen2.5-VL risky); 9 orchestration techniques |
| `refactor_plan/llm-architecture-improvement-plan.md` | Multi-call Phase 2 design rationale; context compression tiers; evaluation harness design |
| `ideas/The-Art-of-the-Silent-Vlog-...md` | Reference guide: silent vlog aesthetic, three narrative personas, two-breath rule origin |

### 8.4 Planned but Unimplemented

| Document | Feature | Status |
|----------|---------|--------|
| `feature-preview-editor-mode.md` | FastAPI local server for in-browser storyboard editing | Not started |
| `backlog-director-review.md` | 5 design issues: over-deterministic constraints, missing HITL checkpoints, no diagnostic feedback | Backlog |

---

## 9. Summary Statistics

| Metric | Value |
|--------|------:|
| Total Python lines | 25,237 |
| Python modules | 28 |
| Top-level functions | 406 |
| Classes (total) | 72 |
| Pydantic models | 51 (50 in models.py + 1 in style_presets.py) |
| Gemini response_schema models | 10 |
| Dataclass config classes | 10 (in config.py) |
| CLI commands | 15+ |
| LLM call phases | 6 (quick_scan, transcription, Phase 1, Phase 2, Phase 3, Director) |
| Max LLM calls per run | 2+N (Timeline) or 3 (Story) + per-clip Phase 1 + per-clip transcription |
| LLM providers | 2 (Gemini, Claude) |
| Export formats | 4 (MP4, HTML preview, Markdown EDL, FCPXML + SRT) |
| Supported locales | 2 (en, zh-TW) |
| Style presets | 1 (Silent Vlog) |
| Design documents | 17 |
| Test files | 1 (fixture-dependent integration tests) |
| Provider string branches | 23 |
| Duplicated utility functions | 3 (wait_for_gemini_file ×4, resolve_clip_source ×3, gemini_client ×7) |
| Hardcoded magic constants | 18 |
| Non-atomic file writes | 60+ |
| Unvalidated JSON reads | 40+ |
