# VX Roadmap

## Vision & Strategy

### Mission

**VX eliminates the gap between "raw footage on a hard drive" and "a video worth sharing" for people who shoot trips and events but never edit them.**

The hard part of editing is not cutting — it is editorial judgment: watching all the dailies, understanding what story the footage tells, and making hundreds of decisions about what to keep and in what order. VX automates editorial thinking, not just mechanical assembly.

### Target Users

**Primary: The Prolific Shooter / Daily Recorder** — Shoots 10–40 clips per trip or daily life on Sony/iPhone/GoPro mix. Technically comfortable with CLI tools. Has folders of unedited footage spanning months or years. Pain: organization paralysis — can't get from raw clips to something worth sharing. Success = a rough cut good enough to send to friends and family directly, no NLE required.

**Secondary: The Power Editor** — Same shooting habits but also uses DaVinci Resolve or Final Cut Pro. Wants AI to handle editorial assembly so they can focus on color, sound, and polish. Pain: initial assembly bottleneck. Success = FCPXML import into Resolve that gives a 70% finished timeline ready to refine.

**Tertiary: The Event Videographer** — Shoots 50–100 clips per event, needs quick turnaround. Knows their NLE well. Pain: repetitive structure across events, volume problem.

**Not targeting**: Real-time/live content creators (need GUI, not CLI), professional film editors with scripted content (established assembly workflows), beginners who need editing tutorials (VX assumes editorial taste).

### Strategic Moats

1. **Briefing context injection** — No competitor captures filmmaker intent (people names, relationships, must-include moments) before editorial assembly. This transforms generic AI editing into informed editorial judgment.
2. **FCPXML as universal NLE bridge** — Timeline references original source files, fully editable in Resolve/FCP. Not a rendered video to decompose — a structured timeline to refine.
3. **Structured data-first architecture** — `EditorialStoryboard` Pydantic model is the single source of truth. All outputs (HTML preview, rough cut, FCPXML, SRT) are deterministic renders of this model.
4. **Local-first, bring-your-own-key** — ~$0.50–2.00 per project vs Eddie AI's $21–333/mo subscription. Full cost transparency per API call.
5. **Versioning and iteration** — Every LLM output is versioned with full DAG lineage. Compare storyboard v1 vs v2, rollback, mix versions.

### Anti-Goals

1. **Will not become a GUI editor.** The HTML preview is for review and adjustment. Timeline editing belongs in Resolve/Premiere/FCP.
2. **Will not replace NLEs.** VX complements NLEs — power users refine in Resolve/FCP, casual users share the rough cut directly.
3. **Will not host user data.** Local-first is the architecture, not a temporary limitation. No cloud storage, no accounts, no sharing platform.
4. **Will not generate music or visual effects.** Music *selection* and *placement* from a user's library is in scope. Music *generation* is not. Text overlay *planning* is in scope. Text *rendering* with effects is the NLE's job.

### Rough Cut Quality Target

The rough cut must be **good enough to share directly** for everyday users who want to turn scattered trip clips into something worth sending to friends and family. For the primary persona, the rough cut IS the final output — not a "70% draft that requires NLE polish." Professional users who want further refinement get FCPXML. This dual-output philosophy means rough cut quality (pacing, transitions, audio levels) is a top priority, not a nice-to-have.

### Competitive Positioning

| Tool | Model | Price | Strength | Weakness | VX Differentiator |
|------|-------|-------|----------|----------|-------------------|
| **Eddie AI** | Cloud GUI, raw→rough cut | $21–333/mo | Multicam switching, Premiere/Resolve/FCP export | Cloud-only, expensive, no filmmaker context | Local-first, BYOK, briefing system, 10–100x cheaper |
| **Adobe Quick Cut** | Cloud, Firefly-based | Adobe subscription | Adobe ecosystem integration | Adobe-locked, beginner-targeted | NLE-agnostic, developer-friendly, open |
| **Gling / TimeBolt** | Single-clip cleanup | $10–20/mo | Fast silence/filler removal | No multi-clip assembly, no narrative intelligence | Different problem space — VX does editorial assembly |
| **Descript** | Text-based full editor | $16–40/mo | Compelling text editing paradigm | Tries to replace NLEs, not bridge to them | Complementary, not competing |
| **NLE AI** (Resolve/Premiere/FCP) | Post-assembly enhancement | Varies | Denoising, masking, color, captions | None do editorial assembly | VX fills the assembly gap, NLE AI handles polish |

**Uncontested positions**: CLI/developer AI video editing (zero competitors), multi-clip editorial assembly with narrative intelligence (only Eddie AI, cloud/GUI), FCPXML generation from structured AI storyboard (unique).

---

## Implemented (v0.1.0)

### Core Pipeline
- [x] Multi-clip editorial workflow: folder of raw clips → AI-edited storyboard → rough cut video
- [x] Single-video descriptive mode: one video → shot-by-shot breakdown
- [x] Dual LLM provider support: Gemini (native video) and Claude (frame-based)
- [x] Pydantic `EditorialStoryboard` model as single source of truth (enforced via Gemini structured output)

### Preprocessing
- [x] Parallel ffmpeg preprocessing (4 workers) — proxy, frames, scenes, audio
- [x] Proxy downscaling: 4K → 360p @1fps for fast AI upload (~5–8MB per clip)
- [x] Aspect-ratio-preserving proxy scaling (`scale=360:-2` — no distortion for 4:3 iPhone footage)
- [x] Frame extraction at configurable intervals (default 5s)
- [x] Scene-change detection via ffmpeg scene filter
- [x] Audio extraction (16kHz mono WAV)
- [x] Per-clip caching — skip already-processed clips on re-run
- [x] macOS resource fork filtering (`._` files)
- [x] Hardware-accelerated HEVC decode via VideoToolbox on macOS (all ffmpeg calls)
- [x] Rotation detection and correction (ffprobe side_data + tags.rotate)

### Format-Aware Pipeline
- [x] Enhanced metadata extraction: rotation, orientation, aspect ratio, resolution class, FPS, HDR detection
- [x] Source format analysis: groups clips by resolution/aspect/codec, detects mixed sources
- [x] iPhone Live Photo detection and optional filtering (short duration + 4:3 heuristic)
- [x] Output format recommendation with interactive TUI selection (resolution, codec, fit mode)
- [x] User-selectable fit mode: pad (black bars, preserve full frame) or crop (fill frame)
- [x] User-selectable output codec: H.264 or H.265
- [x] Normalized segment extraction: adaptive ffmpeg filter chain per segment (rotation → scale → pad/crop → fps)
- [x] Cross-orientation handling: portrait clips centered in landscape canvas with pillarboxing
- [x] Stream-copy concat (segments pre-normalized to uniform format)

### AI Analysis
- [x] Phase 1: Per-clip LLM review — quality, people, key moments, usable/discard segments, audio
- [x] Phase 2: Cross-clip editorial assembly — story arc, cast, EDL with precise in/out seconds, pacing, technical notes
- [x] Clip ID fuzzy resolution (handles LLM abbreviations like `C0073` → `20260330114125_C0073`)
- [x] User briefing context injected into Phase 1 and Phase 2 prompts
- [x] Proxy video concatenation for multi-clip Gemini calls (briefing + Phase 2 visual)
- [x] Multi-call Phase 2 pipeline (Call 2A reasoning → Call 2A.5 structuring → Call 2B assembly) — built, opt-in via `use_split_pipeline`
- [x] Style preset system with creative direction injection into Phase 1/2 prompts
- [x] Phase 3 (optional, preset-dependent): Visual monologue / text overlay plan generation

> **Note**: The `MusicCue` model and `music_plan` field exist in the storyboard schema but are **not populated by the LLM** — the model has only `section`, `strategy`, `notes` fields with no timeline data. Music integration is tracked in Phase 2 below.

> **Note**: The `editorial_reasoning` field is captured by the LLM but **not rendered** in the HTML preview or markdown output. Surfacing this is tracked in Phase 0 below.

### Interactive TUI
- [x] `vx` (no args) launches guided interactive mode via questionary/prompt_toolkit
- [x] Main menu: New project / Open existing / Settings / Quit
- [x] New project flow: name → footage folder → clip selection → style → preset → preprocess → briefing → transcribe → review → analyze
- [x] Project actions menu: preview, cut, re-analyze, transcribe, manage clips, edit briefing (AI-guided/manual), status
- [x] Editorial briefing: AI-guided (quick scan + targeted questions) or manual (smart hints from Phase 1)
- [x] Transcription with provider selection (mlx/gemini) and overwrite prompt for cached transcripts
- [x] Visual Phase 2 opt-in prompt during analysis
- [x] LLM usage tracing with cost summary after pipeline runs
- [x] Status display includes transcript status and cumulative LLM cost
- [x] Tone selector: preset choices + custom option

### CLI (Direct Commands)
- [x] `vx new <name> <source>` — auto-detect editorial vs descriptive
- [x] `vx transcribe` — audio transcription with `--provider gemini|mlx`, `--force`, `--srt`
- [x] `vx brief` — edit briefing context in $EDITOR; `--scan` for AI-guided briefing
- [x] `vx analyze` — Phase 1 + 2, with `--force`, `--no-interactive`, `--visual`, `--dry-run` flags
- [x] `vx cut` — load structured JSON, validate, ffmpeg assembly (no LLM)
- [x] `vx projects` / `vx ls` — list all projects with status
- [x] `vx status` — per-clip cache/review/transcript status, LLM usage summary
- [x] `vx preprocess` / `vx prep` — preprocessing only
- [x] `vx config` — show/set defaults, API key status

### Interactive HTML Preview
- [x] Clickable color-coded timeline (purpose-based colors)
- [x] Segment detail modal with embedded proxy video player
- [x] Draggable in/out range handles for cut point adjustment
- [x] Preview selected range / play full clip buttons
- [x] Transcript overlay in segment modal: shows dialogue for the segment's time range, clickable lines seek video, auto-highlights during playback
- [x] Cast table, story arc cards, technical notes
- [x] Export adjusted JSON for human-in-the-loop refinement
- [x] Keyboard shortcuts (Space play/pause, Escape close)
- [x] Per-clip transcript preview HTML (video + VTT captions + clickable sidebar)

### Versioning
- [x] Full DAG pipeline versioning — every node (quick_scan, user_context, transcription, Phase 1, Phase 2, Phase 3) is versioned
- [x] Two-phase commit (`begin_version`/`commit_version`/`fail_version`) — failed runs don't pollute `_latest` symlinks
- [x] `.meta.json` sidecar files for lineage tracking
- [x] Cuts in `exports/cuts/cut_NNN/` with `composition.json` provenance manifests
- [x] Compositions (`compositions.json`) allow mixing storyboard + monologue versions
- [x] Experiment tracks namespace outputs under `storyboard/<track>/`
- [x] Path resolvers: `resolve_versioned_path()`, `resolve_transcript_path()`, etc.

### Validation & Guardrails
- [x] EDL bounds validation: clamp out-of-bounds timestamps to actual clip duration
- [x] Invalid segment detection (in >= out, missing sources)
- [x] Post-extraction duration check (expected vs actual ±1s tolerance)
- [x] Short segment warnings (<0.5s)
- [x] Warnings section in HTML preview

### Audio Transcription
- [x] Dual-provider transcription: mlx-whisper (local, Apple Silicon) and Gemini (cloud, structured output)
- [x] Gemini transcription with speaker identification, sound event detection, visual context from proxy video
- [x] Dedicated `GeminiTranscript` Pydantic model for structured output
- [x] Anti-hallucination prompt: uses visual context, marks silence/music/sound_effect types correctly
- [x] Timestamped transcript JSON per clip (cached, with overwrite prompt)
- [x] Feed transcripts into Phase 1 review and Phase 2 narrative decisions
- [x] SRT + WebVTT subtitle generation with speaker prefixes and non-speech markers
- [x] Per-clip transcript preview HTML (video + VTT captions + clickable transcript sidebar)
- [x] Transcript overlay in editorial preview (segment modal shows relevant dialogue)
- [x] Rich speaker context from `user_context.json` passed to Gemini prompt
- [x] `vx transcribe` CLI with `--provider gemini|mlx`, `--force`, `--srt` flags
- [x] Auto-provider detection: mlx if installed → gemini if API key set → skip

### LLM Call Tracing & Cost Management
- [x] `tracing.py` module: records tokens, cost, timing for every Gemini API call
- [x] `traced_gemini_generate()` wrapper extracts `response.usage_metadata` automatically
- [x] Cost estimation table for Gemini and Claude models (per 1M tokens)
- [x] Append-only `traces.jsonl` per project — full audit trail of API calls
- [x] `vx analyze --dry-run`: estimate tokens and cost per phase before committing
- [x] `vx status` shows cumulative LLM usage with per-phase breakdown
- [x] Pipeline prints cost summary after completion

### Visual Phase 2 (Proxy Videos in Editorial Assembly)
- [x] `vx analyze --visual`: uploads all proxy videos to Phase 2 Gemini call
- [x] Phase 2 LLM sees actual footage for visual judgments (energy, composition, continuity)
- [x] File API URI caching from Phase 1 uploads (90-min TTL, avoids redundant uploads)
- [x] Phase 2 prompt enhanced with visual context instructions when videos attached
- [x] Dry-run shows cost comparison: text-only vs visual mode
- [x] Proxy concatenation: proxies concatenated into ≤40 min bundles with filename overlay, bypassing Gemini 10-video limit
- [x] Chronological ordering: clips sorted by creation_time metadata for natural vlog narrative
- [x] Concat bundles cached on disk and shared between briefing quick_scan and Phase 2

### Smart Briefing (AI-Guided Context Gathering)
- [x] Quick scan: single Gemini call watches all proxy videos, produces structured overview
- [x] `QuickScanResult` Pydantic model: people sightings, activities, mood, suggested questions
- [x] AI asks targeted questions based on what it actually saw
- [x] `vx brief --scan`: standalone AI-guided briefing with fresh scan
- [x] Interactive TUI: "Edit briefing (AI-guided)" vs "Edit briefing (manual)"
- [x] Auto-used in pipeline when GEMINI_API_KEY available, falls back to manual
- [x] Quick scan uses concat bundles instead of individual videos (fixes Gemini 10-video limit)
- [x] Q&A pairs stored with full question text for clean LLM prompt passthrough

### Pipeline Ordering & Context Flow
- [x] Shared Gemini File API cache (`file_cache.py`): upload once, reuse across briefing → transcription → Phase 1 → Phase 2
- [x] Smart briefing runs before transcription (Gemini path): speaker names available for transcription
- [x] Transcription runs before Phase 1: transcripts available for clip review
- [x] User context (people, activity, tone, highlights) injected into Phase 1 prompts
- [x] Manual briefing (non-Gemini path) runs after Phase 1 (depends on review data for smart questions)
- [x] Force re-run option for cached Phase 1 reviews in TUI re-analyze flow

### DaVinci Resolve / NLE Integration
- [x] FCPXML v1.9 export for DaVinci Resolve and Final Cut Pro (`fcpxml_export.py`)
- [x] Embedded timecode probing (Sony XAVC tmcd track → asset `start` attribute)
- [x] Auto-detect timeline format from dominant source clip resolution/fps
- [x] All manifest clips exported as assets (full raw footage in Media Pool)
- [x] Timeline-aligned SRT subtitle export alongside FCPXML
- [x] Per-clip SRT files for source-relative reference
- [x] TUI integration ("Export to DaVinci Resolve") with error handling and progress feedback
- [x] CLI: `vx export-xml` with `--storyboard`, `--composition`, `--output`, `--no-srt` flags
- [x] 10 documented pitfalls in `docs/design_fcpxml_export.md` (Resolve 20 compatibility)

### Storyboard Quality & Evaluation
- [x] Evaluation harness for storyboard quality scoring (`eval.py`)
- [x] `score_storyboard()`: constraint satisfaction, timestamp precision, structural completeness, coverage

### Housekeeping
- [x] `ingest_source()` uses symlink instead of copying 4K files
- [x] Original source path stored in clip metadata (`manifest.json`); `rough_cut.py` resolves from manifest with legacy fallback
- [x] Consolidated HTML preview into `exports/vN/preview.html`
- [x] Cut writes into the analyze version's export dir
- [x] `manifest.json` enriched with format metadata: rotation, orientation, aspect ratio, resolution class, FPS (float), HDR flag
- [x] `OutputFormat` persisted in `project.json` — rough cut loads automatically, backward-compatible default when absent

---

## Roadmap Phases

### Critical Path

```
Phase 0: Quality Foundation
    ↓
Phase 0.5: Editorial Director (AI Self-Review Loop)
    ↓
Phase 1: B-Roll Lanes & Multi-Track Video
    ↓
Phase 2: Music Integration
    ↓
Phase 3: Multi-Track Audio & J/L-Cuts
    ↓
Phase 4: FCPXML Round-Trip & Editor Mode
    ↓
Phase 5: Scale & Polish
```

Each phase has explicit prerequisites and exit criteria. The phase gate rule: **all exit criteria must pass before starting the next phase.** No phase scores below the previous evaluation baseline.

---

### Phase 0: Quality Foundation

**Goal**: Make the existing pipeline reliably good before adding new capabilities.

**User impact**: Better storyboard quality on every run. Faster iteration on prompt improvements. Confidence that the pipeline works at scale.

**Deliverables**:

- [ ] **Enable split pipeline as default** — The multi-call Phase 2 pipeline (reasoning → structuring → assembly) is built and tested but disabled (`use_split_pipeline=False`). Validate on all library projects, then make it the default for Gemini.
- [ ] **Fix MusicCue dead code** — Add music direction output fields to the Phase 2 prompt so the LLM actually populates `music_plan` with section/strategy/notes. (Timeline fields like `start_sec` wait for Phase 2.)
- [ ] **Evaluation baseline** — Run `score_storyboard()` on all library projects across all versions. Establish baseline scores for constraint satisfaction, timestamp precision, structural quality, and coverage.
- [ ] **A/B testing harness** — Run the same project through two pipeline configurations (e.g., single-call vs split), score both, diff the results automatically.
- [ ] **Surface editorial reasoning** — Render the `editorial_reasoning` field in HTML preview and markdown output. Users should see *why* the AI made its decisions.
- [ ] **Real-world testing** — Test with diverse footage types (indoor, outdoor, action, talking head), 30+ clip projects, and both Gemini and Claude providers end-to-end.

**Exit criteria**: Split pipeline is default. Eval baseline established across all library projects. MusicCue is populated with direction data. Editorial reasoning visible in preview.

---

### Phase 0.5: Editorial Director (AI Self-Review Loop)

**Goal**: Add a tool-using ReAct agent that reviews, critiques, and iteratively refines storyboard outputs with multimodal verification. The director sees the edit visually (contact strip thumbnails), cross-checks transcripts against images, spots mid-sentence cuts, and applies targeted segment-level fixes — not full regeneration.

**User impact**: Storyboards are automatically quality-checked before the user sees them. Mid-sentence cuts, constraint violations, transcript mismatches, and pacing problems are caught and fixed. The rough cut is closer to "good enough to share" on the first try.

**Prerequisites**: Phase 0 complete (eval baseline established, split pipeline default).

**Deliverables**:

- [ ] **Review rubric** — 7 dimensions: constraint satisfaction, timestamp precision, structural completeness, speech cut safety (computable), narrative flow, segment coherence, transcription coherence (agent-judged via multimodal LLM)
- [ ] **Director tools** — 8 tools for the agent: `screenshot_segment` (2x2 thumbnail grid), `get_transcript_excerpt`, `get_clip_review`, `run_eval_check` (inspect); `apply_segment_fix`, `delete_segment`, `reorder_segments` (fix); `finalize_review` (control)
- [ ] **Contact strip** — One midpoint thumbnail per segment composited into a single overview image. Always-on multimodal (cost: ~$0.001 per review via Gemini Flash)
- [ ] **Cross-modal transcript verification** — Agent sees thumbnails alongside transcript text to spot speaker misattribution, content mismatches, and hallucinated dialogue
- [ ] **Speech cut safety** — Computable check that segment boundaries don't cut mid-sentence (cross-reference transcripts with cut points)
- [ ] **Agent harness** — Tool-using loop with budget tracking (max turns, max fixes, max cost), micro-compact context management, regression protection on fixes, model-controlled termination
- [ ] **CLI flags** — `--no-review`, `--review-budget`, `--review-max-turns`
- [ ] **TUI progress** — Per-turn display showing what the director is inspecting and fixing

**Cost**: ~$0.02–0.06 per project (5-15% of pipeline cost). Eliminates $0.10–0.50 of manual re-runs.

**Design doc**: `docs/design-editorial-director.md`

**Exit criteria**: Director completes review in ≤15 turns on all library projects. Speech cut safety catches known mid-sentence cuts. Cross-modal verification catches injected transcript mismatches. Cost stays within $0.06 per project.

---

### Phase 1: B-Roll Lanes & Multi-Track Video

**Goal**: Add lane support to the Segment model, teach the LLM to assign B-roll to an overlay lane, and export multi-track FCPXML. This is the single most impactful architectural change — it unlocks the entire multi-track workflow.

**User impact**: B-roll clips appear on a separate video track (V2) in DaVinci Resolve. Users can freely adjust, extend, or replace B-roll without disturbing the primary dialogue track. The primary track maintains dialogue continuity; the overlay track handles visual variety.

**Prerequisites**: Phase 0 complete.

**Deliverables**:

- [ ] **Segment model extension** — Add `lane: int = 0` (0 = primary spine, 1+ = overlay) and `opacity: float = 1.0` for compositing control. Backward-compatible: existing storyboards default to lane 0.
- [ ] **Phase 2 prompt update** — Teach the LLM: segments with `purpose="b_roll"` or `purpose="cutaway"` should use `lane=1`. Include lane assignment rules in the editorial prompt.
- [ ] **FCPXML multi-track export** — B-roll segments exported as nested `<asset-clip>` inside the spine clip's element, with `lane="1"`. Support `<adjust-conform type="fit"/>` for resolution mismatches. Resolve imports B-roll on V2 track.
- [ ] **HTML preview: multi-track visualization** — Show primary and B-roll tracks as stacked lanes in the timeline view. Color-code by lane.
- [ ] **FCPXML export versioning** — Versioned exports: `exports/fcpxml/editorial_gemini_v1.fcpxml` with auto-increment and `_latest` symlink. `.meta.json` sidecar with storyboard version, timestamp, segment count, timeline duration, format.

**Data model changes**:
```python
class Segment(BaseModel):
    # ... existing fields ...
    lane: int = Field(default=0, description="0 = primary spine, 1+ = overlay lane")
    opacity: float = Field(default=1.0, description="Compositing opacity for overlay segments")
```

**Exit criteria**: A project with B-roll segments produces FCPXML that imports into Resolve with B-roll on V2. Eval scores do not regress on primary track quality.

---

### Phase 2: Music Integration

**Goal**: Make music a first-class citizen in the editorial pipeline. The user provides a music library; the LLM selects tracks and places them on the timeline; FCPXML exports them as audio-only assets on a dedicated lane.

**User impact**: Open the FCPXML in Resolve and music tracks are already placed, roughly timed to story arc sections, with fade-in/out markers. The rough cut MP4 includes background music at appropriate levels. The user adjusts timing and levels rather than starting from scratch.

**Prerequisites**: Phase 1 complete (lane model working, multi-track FCPXML proven).

**Deliverables**:

- [ ] **Music library ingest** — `vx music add <folder>` registers a music library folder. Per-track audio analysis: BPM detection, duration, waveform energy curve. Optional user-provided mood tags. Store in `music_manifest.json`.
- [ ] **Enhanced MusicCue model** — Replace placeholder fields with timeline-anchored data: `start_sec`, `end_sec`, `asset_path`, `crossfade_sec`, `volume_db`, `duck_under_dialogue`.
- [ ] **New MusicTrackMeta model** — `path`, `duration_sec`, `bpm`, `key`, `mood` (list), `energy_curve` (list of floats).
- [ ] **Music-informed Phase 2 prompt** — Inject available music tracks (with BPM, mood, duration) into the Phase 2 prompt. LLM selects tracks, assigns them to story arc sections, populates `MusicCue` with timeline positions. Beat-aware cut suggestions when `audio_note="music_bed"`.
- [ ] **FCPXML audio lane export** — Music tracks as audio-only `<asset>` elements (no `hasVideo`). Placed on a dedicated audio lane (nested `<asset-clip>` with `lane` attribute). Volume automation keyframes for fade-in/out.
- [ ] **Rough cut music mixing** — Include music tracks in the rough cut MP4 via ffmpeg `filter_complex`. Ducking under dialogue segments.
- [ ] **HTML preview: music visualization** — Music track shown as a separate lane below the video timeline. Beat markers when BPM data is available.
- [ ] `vx music` CLI command to manage the music library per project.

**Data model changes**:
```python
class MusicCue(BaseModel):  # enhanced
    section: str
    strategy: str
    notes: str = ""
    start_sec: float        # Timeline position where music starts
    end_sec: float          # Timeline position where music ends
    asset_path: str         # Path to music file
    crossfade_sec: float = 0.0
    volume_db: float = -12.0
    duck_under_dialogue: bool = True

class MusicTrackMeta(BaseModel):  # new
    path: str
    duration_sec: float
    bpm: float | None = None
    key: str | None = None        # e.g., "C major", "A minor"
    mood: list[str] = []          # e.g., ["upbeat", "energetic"]
    energy_curve: list[float] = []  # Normalized energy per second
```

**Exit criteria**: A project with a music library produces FCPXML where music tracks are placed on audio lanes in Resolve. Rough cut MP4 includes background music at appropriate levels. Music timing roughly aligns with story arc sections.

---

### Phase 3: Multi-Track Audio & J/L-Cuts

**Goal**: Produce a properly mixed audio layout with dialogue, music, and ambient tracks separated. Add J-cut and L-cut support for professional-feeling transitions.

**User impact**: Open the FCPXML in Resolve and audio is already separated into dialogue (A1), music (A2), and ambient (A3) lanes. J-cuts and L-cuts provide seamless audio transitions. The rough cut MP4 has better audio continuity.

**Prerequisites**: Phase 2 complete (music on audio lanes, MusicCue populated).

**Deliverables**:

- [ ] **Segment model extension for J/L-cuts** — Add `audio_offset_sec: float = 0.0` to Segment. Negative = audio leads video (J-cut: next clip's audio starts before its video). Positive = audio trails (L-cut: current clip's audio continues over next clip's video).
- [ ] **Audio lane routing in FCPXML** — Route `audio_note` per segment: `preserve_dialogue` → A1, `music_bed` → A2 (duck A1), `ambient` → A3. `audioStart`/`audioDuration` attributes on asset-clips for decoupled audio-video timing.
- [ ] **Voice-over support** — When one clip's audio continues while visuals switch to B-roll, model as audio from clip A on A1 while video switches to clip B. Hard constraint: when returning to source clip's own visuals, audio and video must be frame-aligned.
- [ ] **Phase 2 prompt: J/L-cut instructions** — Teach the LLM when to use `transition="j_cut"` or `transition="l_cut"` with appropriate `audio_offset_sec` values.
- [ ] **ffmpeg multi-track rough cut** — `vx cut --multi-track` opt-in flag. `filter_complex` assembly for multi-track audio mixing with volume automation from MusicCue data.

**Data model changes**:
```python
class Segment(BaseModel):
    # ... existing + Phase 1 fields ...
    audio_offset_sec: float = Field(
        default=0.0,
        description="Audio offset for J/L-cuts. Negative = audio leads (J-cut), positive = audio trails (L-cut)"
    )
```

**Exit criteria**: FCPXML imports into Resolve with separated audio lanes and J/L-cut timing. Eval scores do not regress.

---

### Phase 4: FCPXML Round-Trip & Editor Mode

**Goal**: Close the loop between VX and DaVinci Resolve. Users edit in Resolve, export FCPXML, bring changes back into VX for AI-assisted iteration. Also ship an in-browser editor for quick adjustments without touching Resolve.

**User impact**: The workflow becomes iterative — VX proposes → user edits in Resolve → VX reads back changes → AI suggests improvements → repeat. The in-browser editor makes quick reordering and cut adjustments possible without opening any NLE.

**Prerequisites**: Phase 3 complete (stable multi-track FCPXML format to round-trip).

**Deliverables**:

- [ ] **FCPXML parser** — Parse FCPXML v1.9 files exported from DaVinci Resolve. Extract clips, in/out points, track assignments, transitions, audio routing.
- [ ] **Diff engine** — Compare imported FCPXML against original VX export. Detect: added/removed/reordered clips, adjusted in/out points, new transitions, track changes. Generate human-readable diff report.
- [ ] **Storyboard update from FCPXML** — Apply Resolve edits back to `EditorialStoryboard` model. User-confirmed merge (not automatic overwrite).
- [ ] **AI re-review** — LLM sees user's Resolve edits as a diff and suggests further improvements. "You trimmed segment 3 by 2s — the adjacent segments could use tighter pacing too."
- [ ] **Export → edit → re-import versioned workflow** — Track lineage across round-trips.
- [ ] **Editor mode** — `vx edit <project>` launches a local server. API endpoints for save-to-disk, recut trigger, proxy video serving. Drag-to-reorder segments. Save/recut buttons in the preview.

**Exit criteria**: User can export FCPXML from VX, edit in Resolve, re-import, and see a diff. Editor mode serves the preview with save-to-disk and drag-to-reorder.

---

### Phase 5: Scale & Polish

**Goal**: Handle larger projects (50+ clips), improve accuracy across the board, and add quality-of-life features.

**Deliverables**:

- [ ] **Smart clip selection** — Auto-detect accidental recordings (lens cap, pocket footage, <2s clips). Quality scoring: sharpness, stability, exposure. Duplicate/similar clip detection (same scene, multiple takes).
- [ ] **Phase 1 with cross-clip awareness** — Single-pass multi-clip review via concat video for projects with 20+ clips. Each review knows the full shooting context.
- [ ] **Template system** — Predefined editing templates: "travel vlog", "event recap", "day-in-the-life". Templates define pacing rules, typical arc structure, music placement patterns, mood profiles. User-created custom templates saved per project or globally.
- [ ] **Transcription improvements** — Better timestamp accuracy (chunked processing, cross-validation). Improved speaker diarization for multi-person conversations.
- [ ] **Side-by-side version comparison** — Compare different storyboard versions in the HTML preview with diff highlighting.
- [ ] **Waveform visualization** — Audio waveforms in the HTML preview for audio-driven editing decisions.

**Exit criteria**: Successful processing of a 50+ clip project. Template system produces measurably different outputs for different templates.

---

## Deferred (Acknowledged, Not Scheduled)

These are real capabilities that are explicitly deferred — either because they have low strategic priority relative to the critical path, contradict core principles, or are premature given the current product maturity.

| Capability | Reason Deferred |
|-----------|-----------------|
| **Multi-camera sync** (audio fingerprinting, split-screen) | Requires a fundamentally different data model. Defer until demand is clear from event videographer persona. |
| **Cloud & collaboration** (remote storage, shared previews) | Contradicts local-first principle. Defer indefinitely. |
| **Speed ramps / time-lapse** via FCPXML `<timeMap>` | NLE feature. Only add if FCPXML can carry the metadata natively and demand justifies it. |
| **Color grading presets** (LUT references) | NLE's job. VX stops at editorial structure. |
| **Watermark overlay** | NLE's job. |
| **Conversational editing** ("Make the intro shorter" as natural language) | Premature — get one-shot storyboard quality right first. Revisit after Phase 4 round-trip proves the iteration model. |
| **Automated music selection** from free-license libraries | A product unto itself. VX supports user-provided libraries; automated curation is out of scope. |
| **Text overlay rendering** (burn-in via ffmpeg) | Planning is in scope (Phase 3 monologue). Rendering belongs in the NLE or as a future ffmpeg post-process. |

---

## Prioritization Framework

### Scoring Criteria

Each feature is scored 1–5 on four dimensions:

| Dimension | Weight | 1 (Low) | 5 (High) |
|-----------|--------|---------|----------|
| **User Impact** | 3x | Incremental nice-to-have | Solves a blocking pain point, changes the workflow |
| **Strategic Alignment** | 2x | Tangential, any tool could do it | Deepens a moat, widens the competitive gap |
| **Dependency Position** | 2x | Leaf node, nothing depends on it | Foundation, multiple features build on it |
| **Technical Feasibility** | 1x | Requires new research, uncertain approach | Clear implementation path, existing patterns |

**Score** = (User Impact × 3) + (Strategic Alignment × 2) + (Dependency Position × 2) + (Technical Feasibility × 1)

Range: 8–40.

### Applied Scores

| Feature | Impact | Strategy | Depend. | Feasib. | **Score** | Phase |
|---------|:------:|:--------:|:-------:|:-------:|:---------:|:-----:|
| FCPXML multi-track (B-roll) | 5 | 5 | 4 | 4 | **37** | 1 |
| Enable split pipeline default | 4 | 4 | 5 | 5 | **35** | 0 |
| Segment lane model | 4 | 4 | 5 | 4 | **34** | 1 |
| Eval baseline + A/B harness | 3 | 5 | 5 | 4 | **33** | 0 |
| FCPXML audio lanes (music) | 4 | 5 | 3 | 3 | **30** | 2 |
| Music-informed storyboard | 4 | 4 | 3 | 3 | **29** | 2 |
| FCPXML round-trip import | 4 | 5 | 2 | 2 | **28** | 4 |
| Fix MusicCue population | 3 | 3 | 4 | 5 | **28** | 0 |
| Music library ingest | 3 | 3 | 4 | 3 | **26** | 2 |
| Editor mode (local server) | 4 | 3 | 1 | 4 | **26** | 4 |
| J/L-cut model + export | 3 | 4 | 2 | 3 | **24** | 3 |
| Template system | 3 | 3 | 1 | 4 | **23** | 5 |
| FCPXML export versioning | 2 | 2 | 3 | 5 | **21** | 1 |
| Multi-track audio mix | 3 | 3 | 2 | 2 | **21** | 3 |
| Smart clip selection | 3 | 2 | 1 | 3 | **20** | 5 |
| Multi-camera sync | 2 | 3 | 1 | 1 | **16** | Deferred |

**Why this ordering**: Phase 0 items score highest on dependency position (everything downstream benefits) and feasibility (code is already built). FCPXML multi-track B-roll is the highest user-impact item because it transforms the Resolve import from single-track to professional multi-track. Music follows B-roll because the lane model must exist before music can be placed on audio lanes. Round-trip is Phase 4 because the multi-track format must be stable before it's worth parsing back.

---

## Evaluation Framework

### Storyboard Quality Metrics (per-run, via `eval.py`)

| Metric | Target | How Measured |
|--------|--------|-------------|
| Constraint satisfaction rate | ≥ 90% | Must-include items present, must-exclude items absent |
| Timestamp precision rate | ≥ 85% | Segment timestamps within Phase 1 usable_segments bounds |
| Clip ID resolution rate | 100% | All segment clip_ids resolve after fuzzy matching |
| Structural completeness | All fields | editorial_reasoning, story_arc, cast, discarded all present |
| Duration accuracy | Within 15% | Sum of segment durations vs estimated_duration_sec |
| Coverage ratio | ≥ 50% | (Clips used + clips explicitly discarded) / total clips |
| Duplicate segment indices | 0 | No non-unique Segment.index values |

### User Success Metrics (per-project)

| Metric | Target | How Measured |
|--------|--------|-------------|
| Time to first rough cut | < 30 min human time | Wall clock from `vx new` to `rough_cut.mp4` minus AI wait |
| Iteration count | 1–3 analyze runs | Version counter in `project.json` |
| FCPXML adoption rate | > 50% of projects | Presence of `.fcpxml` in exports directory |
| API cost per project | < $2 for 15 clips | `traces.jsonl` cumulative cost |

### Phase Gate Criteria

Before moving from Phase N to Phase N+1:

1. **All Phase N exit criteria are met** (defined in each phase above)
2. **Eval scores have not regressed** on existing library projects
3. **At least one new real-world project** has been processed through the updated pipeline
4. **No critical bugs** in the new functionality (FCPXML import errors, crashes on valid input, data loss)
5. **Pipeline reliability** < 5% failure rate across runs

---

## Architecture Notes

### Data Model Evolution Path

The `EditorialStoryboard` model evolves incrementally across phases. Each phase adds fields with backward-compatible defaults so existing storyboards continue to work.

```
Current (v0.1.0): Single-track linear timeline
  Segment: clip_id, in_sec, out_sec, purpose, transition, audio_note, text_overlay

Phase 1: B-roll overlay support
  Segment: + lane (0=primary, 1+=overlay), + opacity

Phase 2: Music-aware planning
  MusicCue: + start_sec, end_sec, asset_path, crossfade_sec, volume_db, duck_under_dialogue
  MusicTrackMeta (new): BPM, key, mood, duration, energy_curve

Phase 3: Multi-track audio
  Segment: + audio_offset_sec (J/L-cut timing)

Phase 4: Full NLE interop
  FCPXML parser, diff engine, round-trip versioning
```

### Design Principles

1. **Structured data first**: Pydantic models are the source of truth. Markdown and HTML are rendered views.
2. **LLM calls are minimal and traced**: Every call logs tokens, cost, and timing to `traces.jsonl`.
3. **Cache everything**: Preprocessing, transcription, Phase 1 reviews, concat bundles, Gemini File API URIs. Re-runs are fast.
4. **Context flows downstream**: Briefing → Transcription → Phase 1 → Phase 2 → Phase 3. Each stage benefits from all prior context.
5. **Version everything**: Every LLM output is versioned. Compare, rollback, iterate.
6. **Human-in-the-loop**: AI proposes, user adjusts via interactive preview or DaVinci Resolve, then execute.
7. **No build step**: HTML preview is self-contained. No React, no bundling, opens in any browser.
8. **Cost visibility**: Dry-run estimation before committing, per-phase cost breakdown in status, cumulative tracking across runs.
9. **Respect API limits**: Proxy concatenation bypasses Gemini's 10-video limit. Chronological ordering aids editorial judgment.
10. **NLE-native output**: FCPXML is the bridge between AI-assembled storyboards and professional editing. Design data models with NLE export in mind — every editorial concept should map cleanly to FCPXML elements.
11. **Rough cut is shareable**: For the primary persona, the rough cut is the final output. Quality (pacing, transitions, audio levels) must meet a "proud to share" bar.

### Tech Stack

- **Python 3.11+** with Pydantic for data models
- **ffmpeg/ffprobe** for all video processing (+ audio analysis for music BPM/energy)
- **questionary** (prompt_toolkit) for interactive TUI
- **Gemini API** (structured output, native video) as primary provider
- **Claude API** (frame-based analysis) as alternative provider
- **uv** for fast dependency management
