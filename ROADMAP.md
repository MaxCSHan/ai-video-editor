# VX Roadmap

## Implemented (v0.1.0)

### Core Pipeline
- [x] Multi-clip editorial workflow: folder of raw clips → AI-edited storyboard → rough cut video
- [x] Single-video descriptive mode: one video → shot-by-shot breakdown
- [x] Dual LLM provider support: Gemini (native video) and Claude (frame-based)
- [x] Pydantic `EditorialStoryboard` model as single source of truth (enforced via Gemini structured output)

### Preprocessing
- [x] Parallel ffmpeg preprocessing (4 workers) — proxy, frames, scenes, audio
- [x] Proxy downscaling: 4K → 360p @1fps for fast AI upload (~5-8MB per clip)
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
- [x] Phase 2: Cross-clip editorial assembly — story arc, cast, EDL with precise in/out seconds, pacing, music plan, technical notes
- [x] Clip ID fuzzy resolution (handles LLM abbreviations like `C0073` → `20260330114125_C0073`)
- [x] User briefing context injected into Phase 1 and Phase 2 prompts
- [x] Proxy video concatenation for multi-clip Gemini calls (briefing + Phase 2 visual)

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
- [x] Cast table, story arc cards, music plan, technical notes
- [x] Export adjusted JSON for human-in-the-loop refinement
- [x] Keyboard shortcuts (Space play/pause, Escape close)
- [x] Per-clip transcript preview HTML (video + VTT captions + clickable sidebar)

### Versioning
- [x] Auto-incrementing version per phase (analyze v1, v2...; cut v1, v2...)
- [x] Versioned file outputs: `editorial_gemini_v1.json`, `exports/v1/rough_cut.mp4`
- [x] Latest symlinks: `editorial_gemini_latest.json` always points to newest
- [x] Version counters stored in `project.json`

### Validation & Guardrails
- [x] EDL bounds validation: clamp out-of-bounds timestamps to actual clip duration
- [x] Invalid segment detection (in >= out, missing sources)
- [x] Post-extraction duration check (expected vs actual ±1s tolerance)
- [x] Short segment warnings (<0.5s)
- [x] Warnings section in HTML preview

### Audio Transcription
- [x] Dual-provider transcription: mlx-whisper (local, Apple Silicon) and Gemini (cloud, structured output)
- [x] Gemini transcription with speaker identification, sound event detection, visual context from proxy video
- [x] Dedicated `GeminiTranscript` Pydantic model for structured output (no word-level waste)
- [x] Anti-hallucination prompt: uses visual context, marks silence/music/sound_effect types correctly
- [x] Timestamped transcript JSON per clip (cached, with overwrite prompt)
- [x] Feed transcripts into Phase 1 review (what was said, when, by whom)
- [x] Phase 2 uses dialogue content for narrative decisions
- [x] SRT + WebVTT subtitle generation with speaker prefixes and non-speech markers
- [x] Per-clip transcript preview HTML (video + VTT captions + clickable transcript sidebar)
- [x] Transcript overlay in editorial preview (segment modal shows relevant dialogue, auto-highlights during playback)
- [x] Rich speaker context from `user_context.json` passed as free-form text to Gemini prompt (not comma-split)
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
- [x] AI asks targeted questions based on what it actually saw ("Who is the person in the green shirt?")
- [x] Replaces blind briefing with informed briefing — user responds to specific observations
- [x] `vx brief --scan`: standalone AI-guided briefing with fresh scan
- [x] Interactive TUI: "Edit briefing (AI-guided)" vs "Edit briefing (manual)"
- [x] Auto-used in pipeline when GEMINI_API_KEY available, falls back to manual
- [x] Quick scan uses concat bundles instead of individual videos (fixes Gemini 10-video limit)
- [x] Q&A pairs stored with full question text (not truncated keys) for clean LLM prompt passthrough

### Pipeline Ordering & Context Flow
- [x] Shared Gemini File API cache (`file_cache.py`): upload once, reuse across briefing → transcription → Phase 1 → Phase 2
- [x] Smart briefing runs before transcription (Gemini path): speaker names available for transcription
- [x] Transcription runs before Phase 1: transcripts available for clip review
- [x] User context (people, activity, tone, highlights) injected into Phase 1 prompts
- [x] Manual briefing (non-Gemini path) runs after Phase 1 (depends on review data for smart questions)
- [x] Force re-run option for cached Phase 1 reviews in TUI re-analyze flow

### Improved Phase 1 (Holistic Context)
- [x] Transcripts injected into Phase 1 prompt (what was said, when, by whom)
- [x] Pass user_context.json into each Phase 1 review (filmmaker's intent, people names)
- [x] Briefing Q&A context formatted with full question text for clean LLM consumption

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

### Housekeeping
- [x] `ingest_source()` uses symlink instead of copying 4K files
- [x] Original source path stored in clip metadata (`manifest.json`); `rough_cut.py` resolves from manifest with legacy fallback
- [x] Per-clip `storyboard/` and `exports/` dirs no longer created — `ProjectPaths.ensure_dirs()` trimmed to per-clip concerns only
- [x] Consolidated HTML preview into `exports/vN/preview.html` — removed duplicate from `storyboard/`
- [x] Cut writes into the analyze version's export dir (derived from storyboard JSON filename) instead of maintaining a separate version counter
- [x] `manifest.json` enriched with format metadata: rotation, orientation, aspect ratio, resolution class, FPS (float), HDR flag
- [x] `OutputFormat` persisted in `project.json` — rough cut loads automatically, backward-compatible default when absent

---

## In Progress

### Real-World Testing
- [ ] Test with diverse footage types (indoor, outdoor, action, talking head)
- [ ] Test with 30+ clips to validate scaling
- [ ] Test with both Gemini and Claude providers end-to-end
- [ ] Validate proxy quality is sufficient for accurate AI scene understanding

### Storyboard Quality & Evaluation
- [x] Evaluation harness for storyboard quality scoring
- [ ] A/B testing framework for prompt changes
- [ ] Regression test suite across project types

---

## Planned — Near Term

### Music-Aware Storyboard Planning

The current `MusicCue` model exists but is **never populated by the LLM** — it's a structural artifact. The LLM prompt asks about music direction but has no output fields to capture it. This section makes music a first-class citizen in editorial planning.

**Phase 1: Music library ingest & analysis**
- [ ] Music library folder specification in project config (or briefing)
- [ ] Audio analysis per track: BPM detection, key estimation, mood classification, duration, waveform energy curve
- [ ] Store music metadata in `manifest.json` or dedicated `music_manifest.json`
- [ ] `vx music` CLI command to manage music library per project

**Phase 2: Music-informed storyboard construction**
- [ ] Inject music library metadata into Phase 2 prompt (available tracks with BPM, mood, duration)
- [ ] LLM selects music tracks and assigns them to story arc sections
- [ ] Populate `MusicCue` with timeline-anchored fields: `start_sec`, `end_sec`, `asset_path`, `crossfade_sec`
- [ ] LLM aligns segment durations with musical phrases (e.g., 4-bar boundaries at given BPM)
- [ ] Beat-aware cut points: suggest segment transitions on downbeats when `audio_note="music_bed"`

**Phase 3: Music in FCPXML export**
- [ ] Export music tracks as audio-only `<asset>` elements (no `hasVideo`)
- [ ] Place music clips on a dedicated audio lane (nested `<asset-clip>` with `lane` attribute)
- [ ] Volume automation keyframes for fade-in/out and ducking under dialogue
- [ ] Crossfade between music cues at section boundaries

**Data model changes needed:**
```
MusicCue (enhanced):
  + start_sec: float          # Timeline position where music starts
  + end_sec: float            # Timeline position where music ends
  + asset_path: str           # Path to music file
  + crossfade_sec: float      # Transition duration to next cue
  + volume_db: float          # Target volume level
  + duck_under_dialogue: bool # Auto-lower when speech detected

MusicTrackMeta (new):
  + path: str
  + duration_sec: float
  + bpm: float | None
  + key: str | None           # e.g., "C major", "A minor"
  + mood: list[str]           # e.g., ["upbeat", "energetic"]
  + energy_curve: list[float] # Normalized energy per second
```

### B-Roll & Multi-Track Video

The current model labels segments with `purpose="b_roll"` but places them on the same single track as primary footage. Real B-roll should be on a separate video lane that overlays the primary track — exactly how DaVinci Resolve handles it with `lane` attributes on nested `<asset-clip>` elements.

**Phase 1: Track assignment in storyboard model**
- [ ] Add `lane: int = 0` to `Segment` model (0 = primary spine, 1+ = overlay lanes)
- [ ] Add `opacity: float = 1.0` for compositing control
- [ ] LLM assigns B-roll segments to lane 1 (overlay) instead of lane 0 (primary)
- [ ] Primary track maintains dialogue continuity; B-roll lane handles visual variety

**Phase 2: Multi-track FCPXML export**
- [ ] B-roll segments exported as nested `<asset-clip>` inside the primary clip's element, with `lane="1"`
- [ ] Support `<adjust-conform type="fit"/>` for resolution mismatches between primary and B-roll
- [ ] Resolve imports B-roll on V2 track, freely adjustable

**Phase 3: J-cut / L-cut audio**
- [ ] Add `audioStart` / `audioDuration` attributes to asset-clips for decoupled audio-video timing
- [ ] J-cut: next clip's audio starts before its video (audio leads)
- [ ] L-cut: current clip's audio continues over next clip's video (audio trails)
- [ ] LLM specifies J/L-cut timing in segments with `transition="j_cut"` or `transition="l_cut"`

**Data model changes needed:**
```
Segment (enhanced):
  + lane: int = 0             # 0 = primary spine, 1+ = overlay
  + opacity: float = 1.0      # For compositing (B-roll overlay transparency)
  + audio_offset_sec: float = 0.0  # For J/L-cut: negative = audio leads, positive = audio trails
```

### FCPXML Export Versioning

- [ ] Versioned exports: `exports/fcpxml/editorial_gemini_v1.fcpxml` with auto-increment
- [ ] `_latest` symlink for convenience
- [ ] `.meta.json` sidecar: storyboard version, timestamp, segment count, clip count, timeline duration, format
- [ ] Reuse `begin_version()` / `commit_version()` from `versioning.py`

### FCPXML Round-Trip (Re-Import from Resolve)

Enable users to edit in Resolve and bring changes back to VX for AI-assisted iteration.

- [ ] Parse FCPXML v1.9 files exported from DaVinci Resolve
- [ ] Detect changes vs original export: added/removed/reordered clips, adjusted in/out points, new transitions
- [ ] Generate a diff report: "You trimmed segment 3 by 2s, removed segment 7, added a new clip from C0004"
- [ ] Update `EditorialStoryboard` from the imported FCPXML (user-confirmed)
- [ ] Enable "AI re-review": LLM sees user's Resolve edits and suggests further improvements
- [ ] Track export → Resolve edit → re-import as a versioned workflow with lineage

### Transcription & Timestamp Precision
- [ ] Improve transcription timestamp accuracy (chunked processing, cross-validation)
- [ ] Better speaker diarization for multi-person conversations
- [ ] Phase 2 timestamp validation against clip review usable_segments

### Preview UI Improvements
- [ ] Drag-to-reorder segments in the timeline
- [ ] Add/remove segments from the UI
- [ ] Side-by-side comparison of different versions
- [ ] Waveform visualization for audio-driven editing decisions
- [ ] Multi-track timeline preview (primary + B-roll lanes)
- [ ] Music track visualization with beat markers

---

## Planned — Medium Term

### Multi-Track Audio Assembly

Builds on the music-aware storyboard and B-roll track foundations. Goal: produce a multi-track rough cut with properly mixed audio, not just a single-track concat.

- [ ] Audio lane structure: A1 (dialogue), A2 (music), A3 (ambient/SFX)
- [ ] Parse `audio_note` per segment to route audio: `preserve_dialogue` → A1, `music_bed` → duck A1 under A2, `ambient` → A3
- [ ] ffmpeg `filter_complex` assembly for multi-track audio mixing
- [ ] Volume automation from `MusicCue` data (fade-in/out, ducking curves)
- [ ] Voice-over tracks: one clip's audio continues while visuals switch to B-roll
- [ ] Hard constraint: when returning to audio source clip's own visuals, audio and video must be frame-aligned
- [ ] Multi-track FCPXML export with audio lanes matching the rough cut mix
- [ ] `vx cut --multi-track` opt-in flag

### Smart Clip Selection
- [x] Auto-detect iPhone Live Photo .mov files (short duration + 4:3 heuristic) with optional filtering
- [ ] Auto-detect and filter accidental recordings (lens cap, pocket footage, < 2s clips)
- [ ] Quality scoring: sharpness, stability, exposure, composition
- [ ] Duplicate/similar clip detection (same scene from multiple takes)
- [ ] Auto-group clips by location/time (GPS metadata, filename timestamps)

### Phase 1 with Cross-Clip Awareness
- [ ] Single-pass multi-clip review via concat video (all clips in one LLM call)
- [ ] Clip list overview in each review so the agent knows the full shooting context
- [ ] Cross-clip people matching from the start (not just in Phase 2)

### Template System
- [ ] Predefined editing templates: "travel vlog", "event recap", "day-in-the-life"
- [ ] Templates define: pacing rules, typical arc structure, music placement patterns
- [ ] Music mood profiles per template (which energy curves work for which template)
- [ ] User-created custom templates saved per project or globally

---

## Planned — Long Term

### Automated Music Selection & Sync
- [ ] Mood-based music suggestion from free-license libraries (matching footage energy to track mood)
- [ ] Auto-sync cuts to beat (beat detection + segment boundary alignment)
- [ ] Fade in/out and ducking automation (keyframe generation from speech detection)
- [ ] Multi-section music arrangement: different tracks per story arc section with crossfades

### Multi-Camera / Multi-Source
- [ ] Sync clips from multiple cameras by audio fingerprinting
- [ ] Phone + action cam + drone footage unified timeline
- [ ] Split-screen / picture-in-picture support via lane-based compositing

### AI Iteration Loop
- [ ] User feedback on rough cut → AI adjusts editorial plan
- [ ] "Make the intro shorter", "Use more of clip 5" as natural language commands
- [ ] Conversational editing: chat with the AI editor about the cut
- [ ] Integrate with FCPXML round-trip: user edits in Resolve, AI reviews and suggests improvements

### Cloud & Collaboration
- [ ] Remote project storage (S3/GCS)
- [ ] Share preview links (HTML preview as hosted page)
- [ ] Multi-user review: comments/annotations on timeline segments

### Advanced NLE Features
- [ ] Speed ramps (slow motion, time lapse) via FCPXML `<timeMap>` (reference: mazsola2k implementation)
- [ ] Basic color grading presets (LUT references in FCPXML)
- [ ] Watermark overlay on dedicated video lane with `<adjust-blend>`
- [ ] Text overlay rendering (titles, lower thirds, captions) — burn-in via ffmpeg or FCPXML text elements

---

## Architecture Notes

### Design Principles
1. **Structured data first**: Pydantic models are the source of truth. Markdown and HTML are rendered views.
2. **LLM calls are minimal and traced**: Transcribe + Phase 1 + Phase 2 + optional quick scan. Every call logs tokens, cost, and timing to `traces.jsonl`.
3. **Cache everything**: Preprocessing, transcription, Phase 1 reviews, concat bundles, Gemini File API URIs. Re-runs are fast.
4. **Context flows downstream**: Briefing → Transcription → Phase 1 → Phase 2 → Phase 3. Each stage benefits from all prior context.
5. **Version everything**: Every LLM output is versioned. Compare, rollback, iterate.
6. **Human-in-the-loop**: AI proposes, user adjusts via interactive preview or DaVinci Resolve, then execute.
7. **No build step**: HTML preview is self-contained. No React, no bundling, opens in any browser.
8. **Cost visibility**: Dry-run estimation before committing, per-phase cost breakdown in status, cumulative tracking across runs.
9. **Respect API limits**: Proxy concatenation bypasses Gemini's 10-video limit. Chronological ordering aids editorial judgment.
10. **NLE-native output**: FCPXML is the bridge between AI-assembled storyboards and professional editing. Design data models with NLE export in mind — every editorial concept should map cleanly to FCPXML elements.

### Data Model Evolution Path

The `EditorialStoryboard` model needs to evolve to support multi-track workflows. The progression:

```
Current: Single-track linear timeline
  Segment: clip_id, in_sec, out_sec, purpose, transition, audio_note

Phase 1: B-roll overlay support
  Segment: + lane (0=primary, 1+=overlay), + opacity

Phase 2: Music-aware planning
  MusicCue: + start_sec, end_sec, asset_path, crossfade_sec, volume_db
  MusicTrackMeta: BPM, key, mood, duration, energy_curve

Phase 3: Multi-track audio
  Segment: + audio_offset_sec (J/L-cut timing)
  AudioTrack: lane, source_type, volume_automation

Phase 4: Full NLE interop
  SpeedRamp, ColorGrade, TextOverlay models
  FCPXML round-trip (import + diff + re-export)
```

### Tech Stack
- **Python 3.11+** with Pydantic for data models
- **ffmpeg/ffprobe** for all video processing (+ audio analysis for music BPM/energy)
- **questionary** (prompt_toolkit) for interactive TUI
- **Gemini API** (structured output, native video) as primary provider
- **Claude API** (frame-based analysis) as alternative provider
- **uv** for fast dependency management
