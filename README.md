# VX — AI Video Editor

Turn raw trip footage into polished vlogs with AI.

## Philosophy

This project exists because most people come back from trips with hours of raw clips and never edit them. The gap between "raw footage on a hard drive" and "a video worth sharing" is enormous — it requires the eye of an editor, the patience to review every clip, and the craft to assemble a story.

VX automates the editor's thinking, not just the cutting. Here's what that means:

**An editor's real job is not cutting video.** It's watching all the dailies, understanding what story the footage can tell, identifying the strongest moments, and making hundreds of small decisions about what to keep, what to cut, and in what order. The mechanical act of cutting is the easy part. VX focuses on automating the hard part — the editorial judgment.

**The footage dictates the story.** You don't start with a script and find clips to match it. You start with what you actually shot — shaky B-roll, accidental recordings, someone talking with their mouth full — and find the best possible story within those constraints. The AI reviews every clip the same way an editor would: what's usable? what's the energy? who's in it? where are the moments?

**Context makes the difference.** An editor who knows "that's my sister, this was her first time surfing" makes a fundamentally different video than one who just sees "woman on surfboard." The briefing system exists because the filmmaker's intent and relationships are the single biggest input to editorial quality. The AI can see what's in the frame; only you know why it matters.

**Structure before style.** A good edit follows: hook the viewer, establish context, build through the body, hit a climax, close with an outro. This isn't a formula — it's how stories work. VX produces a story arc with these beats mapped to specific clips and timestamps, because a well-structured 2-minute video beats a meandering 10-minute one every time.

**The output must be usable or it's worthless.** This is automation, not assistance. If the AI produces a storyboard that a human still needs to heavily rework, we've just added a step instead of removing one. Every segment in the EDL has precise in/out timestamps in seconds. The rough cut assembles from these without any human intervention. The HTML preview lets you verify and adjust, but the default should be good enough to share.

**Iterate, don't perfect.** The versioning system exists because the first AI pass won't be the best. Run analyze, review, adjust the briefing, run again. Each version is preserved. The interactive preview lets you nudge cut points without re-running the AI. The goal is convergence: each pass gets closer to what you want.

## How It Works

```
Raw Clips                       You shot 17 clips on your trip.
    │                           4K, handheld, no plan, mixed quality.
    │                           Sony H.264, iPhone HEVC, any mix.
    │
    ▼
┌─────────┐                     ffmpeg downscales each clip to a tiny proxy
│ Ingest  │                     (360p, 1fps, ~5MB). Extracts frames, detects
└────┬────┘                     scene changes, pulls audio. Runs 4 clips in
     │                          parallel. Cached — never re-processed.
     │                          Hardware-accelerated HEVC decode (VideoToolbox).
     ▼
┌──────────┐                    Detects source formats (resolution, codec,
│ Format   │                    aspect ratio, orientation, fps). Filters
│ Analyzer │                    Live Photo .mov files. Recommends output
└────┬─────┘                    format; user picks resolution, codec (H.264/
     │                          H.265), and fit mode (pad/crop) when sources
     │                          are mixed.
     ▼
┌──────────┐                    Optional creative direction. User picks a
│  Style   │                    style preset (e.g., Silent Vlog) that adds
│  Preset  │                    AI guidance to Phase 1/2 prompts and may
└────┬─────┘                    enable Phase 3 (visual monologue).
     │
     ▼
┌──────────┐                    AI quick-scans all footage, then asks
│ Briefing │  (AI-guided)       targeted questions based on what it saw:
└────┬─────┘                    "Who is the person in the green shirt?"
     │                          Saved as context for all downstream stages.
     │                          Uploads proxies to Gemini File API (cached
     │                          for reuse by transcription and Phase 1).
     ▼
┌──────────────┐                AI transcribes each clip's audio via
│ Transcription│                Gemini (speaker ID, sound events) or
└──────┬───────┘                mlx-whisper (local). Uses speaker names
       │                        from briefing for better ID. Cached per
       │                        clip. VTT + preview HTML for verification.
       ▼
┌─────────────┐                 The AI watches each clip's proxy video and
│  Phase 1    │                 produces a structured review: what's in it,
│ Clip Review │                 who appears, quality assessment, which parts
└──────┬──────┘                 are usable vs throwaway, key moments.
       │                        Uses briefing context (people names, intent).
       │                        One LLM call per clip. Cached per clip.
       ▼
┌─────────────┐                 The AI acts as creative editor. It sees ALL
│  Phase 2    │                 clip reviews + transcripts + your briefing
│  Editorial  │                 context. For ≤10 clips, can also see proxy
│  Assembly   │                 videos (--visual). Produces a complete edit
└──────┬──────┘                 plan: story arc, cast, EDL with precise
       │                        in/out timestamps (seconds), pacing, music.
       │                        One LLM call. Structured output (Pydantic).
       │
       ├──→ editorial.json       The structured data. Source of truth.
       ├──→ editorial.md         Human-readable rendered view.
       └──→ preview.html         Interactive: click segments to preview
                                 video, drag to adjust cut points,
                                 export refined JSON.
       │
       ▼
┌─────────────┐                 Optional (if style preset has Phase 3).
│  Phase 3    │                 Generates text overlay/monologue plan from
│  Monologue  │                 the storyboard + transcripts. Produces
└──────┬──────┘                 timed text cards for silent-style vlogs.
       │                        One LLM call. Preset-dependent.
       ▼
┌─────────┐                     Loads the JSON. Validates timestamps against
│   Cut   │                     actual clip durations (clamps out-of-bounds).
│  (ffmpeg)│                     Normalizes each segment to the target format
└────┬────┘                     (scaling, padding, rotation, fps). Concatenates
     │                          into rough_cut.mp4. No LLM call — pure execution.
     │
     └──→ rough_cut.mp4          Watch it. If it's not right, adjust the
          preview.html           preview, export new JSON, re-cut.
```

## Quick Start

```bash
uv venv && uv pip install -e ".[dev]"
cp .env.example .env   # Add your GEMINI_API_KEY

vx                     # Launch interactive mode
```

## CLI

### Interactive mode (recommended)

```bash
vx                                    # Guided workflow with menus and prompts
```

### Direct commands

```bash
vx new my-trip ~/footage/             # Create project, preprocess clips
vx transcribe my-trip                 # Transcribe audio (auto-detect provider)
vx transcribe my-trip --provider gemini  # Gemini: speaker ID + sound events
vx transcribe my-trip --provider mlx  # mlx-whisper: local, fast, no API cost
vx transcribe my-trip --force --srt   # Overwrite cached + generate SRT/VTT
vx brief my-trip --scan               # AI-guided briefing (quick scan + questions)
vx analyze my-trip                    # Briefing + Phase 1 + Phase 2
vx analyze my-trip --visual           # Phase 2 sees proxy videos (richer edits)
vx analyze my-trip --dry-run          # Estimate token usage and cost
vx analyze my-trip --force            # Re-run Phase 1 reviews from scratch
vx analyze my-trip --no-interactive   # Skip briefing questions
vx cut my-trip                        # Assemble rough cut (no LLM)

vx projects                           # List all projects
vx status my-trip                     # Per-clip cache, versions, LLM usage
vx config --provider gemini           # Set defaults
```

### Interactive HTML preview

The preview (`storyboard/*_preview.html`) is an editing tool:
- Click timeline segments → video preview modal with the clip's proxy
- Draggable in/out range handles to adjust cut points
- Preview the selected range, play the full clip for context
- Export adjusted JSON → feed back into `vx cut`

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
  my-trip/
    project.json                        # Type, provider, style, versions
    user_context.json                   # Briefing answers (people, tone, etc.)
    quick_scan.json                     # AI quick scan results (smart briefing)
    manifest.json                       # Aggregated clip metadata
    traces.jsonl                        # LLM call traces (tokens, cost, timing)
    file_api_cache.json                 # Gemini File API URI cache (for reuse)
    clips/
      20260330_C0059/                   # Per-clip (parallel preprocessed, cached)
        source/  proxy/  frames/  scenes/
        audio/
          *.wav                         # Extracted audio (16kHz mono)
          transcript.json               # Speech-to-text (mlx-whisper or Gemini)
          transcript.vtt                # WebVTT subtitles
          transcript_preview.html       # Video + captions side-by-side viewer
        review/
          review_gemini_v1.json         # Phase 1 review (versioned, cached)
          review_gemini_latest.json     # Symlink → latest version
    storyboard/
      editorial_gemini_v1.json          # Phase 2: structured data (source of truth)
      editorial_gemini_v1.md            # Rendered markdown
    exports/
      v1/                               # Versioned rough cuts
        rough_cut.mp4
        preview.html                    # Interactive preview (with transcript overlay)
        segments/  thumbnails/
```

## Source Code

```
src/ai_video_editor/
  cli.py               # CLI entry point (vx command)
  interactive.py        # Interactive TUI (questionary/prompt_toolkit)
  briefing.py           # Editorial briefing + AI-guided smart briefing (quick scan)
  transcribe.py         # Audio transcription (mlx-whisper local + Gemini cloud)
  tracing.py            # LLM call tracing (tokens, cost, timing per API call)
  models.py             # Pydantic models (EditorialStoryboard, Transcript, etc.)
  config.py             # Settings, paths, provider configs, OutputFormat
  preprocess.py         # ffmpeg: proxy, frames, scenes, audio (parallel, cached, hwaccel)
  format_analyzer.py    # Source format detection, Live Photo filter, output recommendation
  editorial_prompts.py  # Phase 1 + 2 prompt engineering
  editorial_agent.py    # Multi-clip orchestrator (transcribe, review, assemble)
  render.py             # Markdown + interactive HTML from Pydantic models
  rough_cut.py          # Validation + format-normalized ffmpeg assembly (no LLM)
  versioning.py         # Run versioning with symlinks
```

## LLM Calls

| What | Input | Output | LLM? |
|------|-------|--------|------|
| Preprocess | Raw 4K clips | Proxy, frames, scenes, audio | No — ffmpeg |
| Transcribe | Proxy video or WAV | transcript.json + VTT + preview | Yes — per clip, cached (Gemini or mlx-whisper) |
| Quick Scan | All proxy videos | Quick overview for smart briefing | Yes — one call (Gemini) |
| Briefing | AI scan + user answers | user_context.json | Interactive (AI-guided or manual) |
| Phase 1 | Proxy video + transcript | Structured review JSON | Yes — per clip, cached |
| Phase 2 | Reviews + transcripts + briefing (+ videos with --visual) | EditorialStoryboard (Pydantic JSON) | Yes — one call |
| Render | Structured JSON | Markdown + HTML preview (with transcript overlay) | No — templates |
| Cut | Structured JSON | rough_cut.mp4 | No — ffmpeg |

## Rough Cut: Decode → Encode → Concat Pipeline

The rough cut stage (`rough_cut.py`) is a pure ffmpeg pipeline — no LLM calls. It takes the `EditorialStoryboard` JSON and produces a playable `rough_cut.mp4`. The pipeline has three phases, each with specific design decisions learned through real-world testing with mixed 4K footage.

### Phase A: Segment Extraction (decode + encode)

Each segment is extracted from its source clip, re-encoded, and format-normalized:

```
ffmpeg -y
  -ss {in_sec} -i {source} -t {duration}       # Fast-seek + trim
  -vf "scale=...,pad=...,fps={target_fps},..."  # Format normalization + overlays
  -c:v h264_videotoolbox -q:v 65 -allow_sw 1    # HW encode (macOS)
  -force_key_frames "expr:eq(n,0)"               # IDR at frame 0
  -pix_fmt yuv420p                               # iPhone compatibility
  -c:a aac -b:a 128k -ar 48000 -ac 2            # Audio normalization
  -movflags +faststart                           # Seekable output
  seg_NNN.mp4
```

**Key decisions:**

- **Software decode, hardware encode.** The command intentionally omits `-hwaccel videotoolbox` for decoding. VideoToolbox hardware decoder drops frames when fast-seeking (`-ss` before `-i`), producing corrupt segments with ~1-second freezes. Software decode is reliable and fast enough since each segment only decodes a few seconds of source. Hardware encoding (`h264_videotoolbox` / `hevc_videotoolbox`) is still used for output.

- **`-ss` before `-i` (input seeking).** Seeks to the nearest keyframe before `in_sec`, then decodes forward to the exact frame. Much faster than output seeking (`-ss` after `-i`) for large 4K source files.

- **`-force_key_frames "expr:eq(n,0)"`** guarantees an IDR keyframe at frame 0 of every segment. Without this, VideoToolbox may start with P/B-frames, and the concat demuxer (which uses stream copy) would hit undecodable frames at segment boundaries — causing freezes.

- **`fps=` filter always applied** (via `-vf`), even when source FPS matches target. This normalizes the timebase across all segments. Without it, different source clips can produce segments with mismatched timebases (e.g., 1/600 vs 1/48000), causing PTS discontinuities during concat.

- **Full re-encode per segment** (not stream copy). This is necessary because segments need format normalization (resolution, fps, rotation, padding, text overlays). The cost is acceptable: each segment is only a few seconds long.

### Phase B: Validation (3-layer)

Before concatenation, segments pass through a validation pipeline:

1. **Per-segment** — ffprobe each segment: has video + audio, correct codec/resolution/fps, duration matches expected (±1s).
2. **Cross-segment compatibility** — compares all segments for matching video codec, resolution, pixel format, fps, audio sample rate, and channel count. Mismatched segments are automatically re-encoded to match the majority.
3. **Post-concat integrity** — validates the final rough cut: file size sanity, has both streams, duration matches (±2s), seek tests at start and midpoint.

### Phase C: Concatenation (stream copy)

All validated segments are joined using the ffmpeg concat demuxer:

```
ffmpeg -y
  -fflags +genpts                    # Regenerate PTS as safety net
  -f concat -safe 0                  # Concat demuxer, allow absolute paths
  -avoid_negative_ts make_zero       # Shift negative timestamps to zero
  -i concat_list.txt
  -c:v copy                          # Video: stream copy (no re-encode)
  -c:a copy                          # Audio: stream copy (already normalized)
  -movflags +faststart               # Seekable output
  rough_cut.mp4
```

**Key decisions:**

- **Stream copy for both video and audio.** Since Phase A already normalizes all segments to matching format, codec, resolution, fps, and audio parameters, no re-encoding is needed during concat. This makes concatenation fast (seconds, not minutes).

- **`-avoid_negative_ts make_zero`** prevents negative timestamps at segment boundaries from producing invalid moov atom edit lists.

- **`-fflags +genpts`** regenerates presentation timestamps from frame rate as a safety net for any residual PTS irregularities.

- **Concat demuxer** (not concat filter or protocol). The demuxer reads a text file listing segment paths and joins them without re-encoding. This is the right choice when segments are already format-normalized.

### What went wrong (and why these choices exist)

These design decisions were not obvious upfront. They emerged from debugging real failures:

| Symptom | Root cause | Fix |
|---------|-----------|-----|
| ~1s video freezes at segment boundaries | VideoToolbox HW decoder drops frames during fast-seek (`-ss` before `-i`) | Remove `-hwaccel videotoolbox` from decode; keep HW encode only |
| ~1s freezes at concat joins | Segments missing IDR keyframe at frame 0; concat `-c:v copy` hits undecodable P/B-frames | Add `-force_key_frames "expr:eq(n,0)"` to segment extraction |
| PTS discontinuities / cranky playback | Different source clips produce segments with mismatched timebases | Always apply `fps=` filter to normalize timebase, even when FPS matches |
| A/V sync drift at segment boundaries | Audio re-encoded during concat while video was stream-copied | Switch concat audio to `-c:a copy` (audio already normalized in Phase A) |
| "moov atom may be corrupt" false positives | Seek test too tight (10s timeout for 2GB+ files) and imprecise diagnostics | Scale timeout with file size, test both start and midpoint, report specific failure mode |
