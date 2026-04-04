# Deep Dive: mazsola2k-ai-video-editor

A comprehensive analysis of the [mazsola2k AI video editor](../mazsola2k-ai-video-editor/) — an AI-powered video editing automation pipeline — from the perspective of product strategy, system architecture, and AI engineering. Written as a reference for the VX project team.

---

## 1. Executive Summary

**What it is:** A 5-stage end-to-end pipeline that transforms long-form raw footage into rendered, uploaded YouTube videos with zero manual editing. Raw clips go in; a published YouTube video comes out.

**Who it's for:** Solo content creators producing repetitive, process-oriented footage (specifically tuned for scale model car building on the ModernHackers YouTube channel). The target user films 30-60+ minutes of workshop footage and needs it compressed into 10-15 minute highlight reels.

**Core value proposition:** Compress 6+ hours of manual editing into ~20 minutes of automated processing. The system watches the footage, decides what's interesting, sets playback speeds, assembles a broadcast-ready timeline with transitions/music/watermarks, renders via DaVinci Resolve, and uploads to YouTube — all from a single command.

**Key differentiator from VX:** Fully local AI stack (no cloud API calls), GPU-first architecture, direct NLE rendering integration via DaVinci Resolve scripting API, and a speed-ramping editing philosophy rather than subclip selection.

---

## 2. Architecture Overview

### 5-Stage Pipeline

```
Stage 1: AI Analysis          (analyze_advanced5.py)
   Raw video → frame sampling → CLIP + ResNet + Qwen2.5-VL → LLM scene analysis
   Output: scene_analysis_*.json

Stage 2: Clip Extraction       (extract_scenes.py)
   scene_analysis JSON → ffmpeg + NVENC → speed-adjusted clips
   Output: ai_clips/{video_stem}/*.mov

Stage 3: Timeline Generation   (export_resolve.py)
   Clips + analysis JSON → FCPXML 1.13 assembly
   Output: timeline_davinci_resolve.fcpxml

Stage 4: Rendering             (render_youtube.py + apply_lut_resolve.py)
   FCPXML → DaVinci Resolve scripting API → H.265 4K MP4
   Output: rendered MP4

Stage 5: YouTube Upload        (upload_youtube.py)
   MP4 + metadata → OAuth 2.0 resumable upload
   Output: published YouTube video
```

**Orchestrator:** `run_pipeline.py` chains all 5 stages. Each stage is also independently runnable.

**Bonus pipeline:** `export_reels.py` + `render_reels.py` generates 9:16 vertical YouTube Shorts (< 59 seconds) from curated clips in `assets/videos-reels/`.

### Data Flow

```
Raw Video Files (MOV/MP4/MKV)
    │
    ├─→ Frame Sampling (every 2 seconds)
    │       │
    │       ├─→ CLIP ViT-B/32 (semantic embeddings, 9 prompt categories)
    │       ├─→ ResNet-50 (2048-dim feature vectors, duplicate detection)
    │       └─→ Qwen2.5-VL-7B (per-frame keyword captions)
    │               │
    │               └─→ metadata_*.json (Pass 1 output)
    │
    ├─→ LLM Scene Analysis (Pass 2)
    │       │
    │       ├─→ Scene boundary detection (timestamp + reason)
    │       ├─→ Scene classification + rating (1-10 → speed assignment)
    │       └─→ Showcase selection (top N diverse highlight moments)
    │               │
    │               └─→ scene_analysis_*.json (final analysis)
    │
    ├─→ FFmpeg Extraction (GPU NVENC)
    │       │
    │       └─→ ai_clips/{stem}/*_scene_*_{speed}x.mov
    │           ai_clips/{stem}/*_showcase_*_{speed}x.mov
    │
    ├─→ FCPXML Timeline Assembly
    │       │
    │       ├─→ Teaser section (best showcases)
    │       ├─→ Intro/outro branding
    │       ├─→ Main content (speed-ramped scenes)
    │       ├─→ Multi-track audio (teaser + background music)
    │       └─→ Watermark overlay
    │               │
    │               └─→ timeline_davinci_resolve.fcpxml
    │
    └─→ DaVinci Resolve → YouTube
```

---

## 3. AI & LLM Strategy

### Model Stack (All Local)

| Model | Role | Size | Runtime |
|-------|------|------|---------|
| **Qwen2.5-VL-7B** | Vision-language: frame captioning + scene reasoning | ~4.7GB (Q4_K_M GGUF) | llama-cpp-python (GPU) |
| **CLIP ViT-B/32** | Semantic similarity: interest/quality scoring | ~350MB | PyTorch (GPU) |
| **ResNet-50** | Feature extraction: duplicate/similarity detection | ~100MB | PyTorch (GPU) |

No cloud API calls. No OpenAI, Gemini, or Claude usage. All inference runs on local NVIDIA GPU with CUDA. Models are loaded once at pipeline start and reused across all videos, with explicit memory cleanup between videos.

### Two-Pass Analysis Design

This is the most interesting architectural decision. Rather than giving an LLM the raw video, the system builds a **metadata layer first**, then lets the LLM reason over summarized data.

**Pass 1 — Feature Collection (compute-heavy, parallelizable):**
- Extract frames at 2-second intervals
- CLIP: Score each frame against 9 semantic prompts (e.g., "applying polishing compound", "close-up macro of surface detail"). Produces per-frame interest/quality vectors.
- ResNet-50: Extract 2048-dim feature vectors. Compute cosine similarity between consecutive frames for motion detection. Flag duplicates (similarity > 0.975 AND motion < 0.03).
- Qwen2.5-VL: Caption every frame as comma-separated keywords (max 40 tokens). Extract keyword features: work-related, tools, parts, quality indicators, negatives.
- Output: `metadata_*.json` with rich per-frame annotations.

**Pass 2 — LLM Reasoning (lightweight, sequential):**
- LLM receives **text summaries** of the metadata, not raw frames.
- Context format: `"{timestamp}s: {caption} (interest={score}, motion={score})"`
- Three targeted LLM calls:
  1. Scene boundary detection (where does the workflow change?)
  2. Per-scene classification and rating (how interesting is this scene, 1-10?)
  3. Showcase selection (pick the N most diverse highlight moments)
- Output: `scene_analysis_*.json` with classified scenes, speed assignments, and showcases.

**Why this matters:** The LLM never processes raw visual data in Pass 2. It operates on pre-digested summaries. This keeps token usage extremely low (~1,000-2,000 tokens per video) and makes LLM calls fast (~1-2 seconds each). The heavy lifting is done by specialized vision models in Pass 1.

### Prompt Engineering

Four distinct prompts, each tightly constrained:

**1. Frame Captioning** (Qwen2.5-VL, per-frame)
- System: "You output ONLY comma-separated keywords describing visible objects, colors, and actions. NO full sentences."
- User: "List comma-separated keywords only. NO sentences. Example: hands, blue gloves, red car, screwdriver, workbench"
- Temperature: 0.3, max_tokens: 40
- Design choice: Keywords, not descriptions. Maximally dense information per token.

**2. Scene Boundary Detection** (Qwen2.5-VL, once per video)
- System: "You are an expert video analyst specializing in hobby workshop videos."
- Provides sampled timeline context (20-second intervals, max 30 samples)
- Asks for max 5 boundary timestamps with reasons
- Temperature: 0.2 (very deterministic), max_tokens: 200

**3. Scene Classification** (Qwen2.5-VL, per scene)
- System: "You are an expert video analyst."
- Provides 5-frame caption sample + precomputed metrics
- Domain-specific guidance: "For model building content, highly value close-up detail shots, hands-on assembly..."
- Temperature: 0.3, max_tokens: 100
- Output format: "Rating: X/10 - reason"

**4. Showcase Selection** (Qwen2.5-VL, once per video)
- System: "You are an expert video curator."
- Asks for diverse, interesting moments demonstrating different aspects
- Temperature: 0.4 (slightly more creative)
- max_tokens: 150

### Context Management

The system is aggressive about keeping context small:
- Hard context limit: 4096 tokens
- Frame captions capped at 40 tokens (keywords only)
- Scene boundary context: 30 samples max, sampled at 20-second intervals
- Scene classification: 5-frame samples per scene
- Showcase selection: 20 uniform samples across entire video

There is no chunking, RAG, or multi-turn conversation. Each LLM call is a single-shot prompt with tightly bounded context.

### Structured Output Parsing

All LLM outputs are parsed with regex — no JSON mode, no Pydantic validation, no structured output schemas:

```python
# Scene boundaries: extract timestamps from "XXs: reason" format
for match in re.finditer(r'(\d+)s:', response):
    ts = int(match.group(1))

# Scene rating: extract X from "X/10" format
rating_match = re.search(r'(\d+)/10', response)

# Showcase timestamps: same pattern as boundaries
for match in re.finditer(r'(\d+)s:', response):
    ts = int(match.group(1))
```

Simple, deterministic, fragile. No retry logic for malformed outputs. Relies on prompt engineering to ensure consistent formatting.

---

## 4. Video Understanding Approach

### Frame-Based, Not Video-Based

The system never processes full video streams through AI. Instead:

1. **Temporal sampling:** Extract one frame every 2 seconds (configurable). A 60-minute video yields ~1,800 frames.
2. **Per-frame analysis:** Each frame is independently processed by CLIP, ResNet, and Qwen2.5-VL. No temporal modeling within vision models.
3. **Temporal patterns from metadata:** Motion is computed as `1 - cosine_similarity(frame[i], frame[i+1])` using ResNet features. Similarity windows (11-frame, ±5) detect repetitive sequences.
4. **Audio:** Not analyzed. No speech recognition, no sound event detection. Video audio is muted in the final timeline.

### CLIP Semantic Scoring

Nine domain-specific text prompts scored against each frame:
- `"applying polishing compound to model car surface"`
- `"visible reflection improvement and shine"`
- `"close-up macro of surface detail and texture"`
- `"hands working on small scale model parts"`
- `"changing camera angle or perspective shift"`
- `"comparing before and after results"`
- `"repetitive motion or same action repeated"`
- `"static scene with no visible activity"`
- `"blurry out of focus image"`

These produce a 9-dimensional "interest vector" per frame. The first 6 are positive signals; the last 3 are negative.

### Duplicate Detection

Multi-layered approach:
- **Perceptual hashing (dHash):** 64-bit hash per scene for cross-video deduplication
- **Cosine similarity:** ResNet feature vectors between consecutive frames (> 0.975 = duplicate)
- **Hamming distance:** Cross-video scene comparison (threshold: 6 bits)

---

## 5. Scene Intelligence

### Classification System

Scenes are classified into 4 tiers based on LLM rating (1-10):

| Rating | Classification | Speed | Purpose |
|--------|---------------|-------|---------|
| 8-10 | `interesting` | 1.0x | Preserve every detail — peak craftsmanship |
| 5-7 | `moderate` | 2.0x | Standard content — compress slightly |
| 2-4 | `low` | 4.0x | Background/setup — visible but fast |
| 0-1 | `boring` | 6.0x or skip | Filler — excluded if `exclude_boring: true` |

### Speed Ramping Philosophy

This is the project's core editing philosophy and its most distinctive feature. Rather than selecting subclips (VX's approach), it **keeps all footage but adjusts playback speed**. The viewer sees the entire process at varying speeds — boring parts fly by, interesting parts play at full speed.

Implications:
- **No content is lost.** Every moment of the build process is represented.
- **Temporal continuity preserved.** The viewer follows the chronological flow without jump cuts.
- **Compression is continuous, not discrete.** Instead of "in or out," scenes exist on a speed gradient.
- **Target ratio:** ~15% of original duration (configurable via `target_output_ratio`).

### Showcase Detection

The LLM selects 3-8 "showcase" moments — the absolute peak highlights. These are:
- Extracted as 5-second clips at 1.0x speed
- Placed in the teaser section at the start of the video
- Meant to hook the viewer before the main content

### Multi-Video Deduplication

When processing multiple videos from the same project (e.g., Part 1-5 of a build series), perceptual hashing detects and removes repeated scenes across videos. Configurable Hamming distance threshold (default: 6).

---

## 6. Timeline & Rendering

### FCPXML Generation (export_resolve.py — 2,142 lines)

The largest and most complex file in the project. Generates FCPXML 1.13 compatible with DaVinci Resolve.

**Timeline Structure:**

```
[Teaser Section: 30-50s]     ← Top showcase moments + teaser music
[Intro Video: ~10s]          ← Branding (Start-Intro-V3.mkv)
[Main Content: variable]     ← All classified scenes, speed-adjusted
[Closing Photos: 3s each]    ← From assets/photos/
[Teaser Videos: variable]    ← From assets/teaser-videos/
[Outro Video: ~10s]          ← Call-to-action (Finish-Intro-V3.mkv)
```

**Video Tracks:**
- Lane 0: Main video content
- Lane 1: Watermark overlay (QR code, 30% opacity, configurable position)

**Audio Tracks:**
- Lane 1: Teaser music (random WAV from `assets/music-teaser/`, 1s fade)
- Lane 2: Background music (shuffled WAVs from `assets/music-background/`, 3s crossfade)
- Video audio: Muted (-96dB)

**Effects & Transitions:**
- 1-second cross-dissolves between clips
- Rotation transforms for portrait clips (270 + 1.78x zoom)
- Audio fade automation via keyframes

**Technical Details:**
- All timings expressed as fractions (e.g., `95/6s` = 15.833s) to avoid floating-point errors
- File URIs are percent-encoded absolute paths
- Frame rates: 23.98fps (1001/24000s) for 4K, 24fps (1/24s) for Shorts
- Audio normalization: ensures 24-bit WAV, strips BWF time_reference metadata that causes silence offset in Resolve

### DaVinci Resolve Integration

The project directly controls DaVinci Resolve via its Python scripting API:
- `render_youtube.py`: Imports FCPXML, sets render parameters (H.265 NVIDIA, 30 Mbps, 3840x2160), triggers render
- `apply_lut_resolve.py`: Applies color grading LUTs (FiLMiC Pro deLOG) to media pool or timeline items
- `render_reels.py`: Same flow but 1080x1920 vertical at 15 Mbps

This is a notable design choice — rendering happens inside a professional NLE, not via raw ffmpeg. The user can optionally open DaVinci Resolve, inspect/tweak the timeline, then render.

---

## 7. Output Pipeline

### YouTube Upload

Full YouTube Data API v3 integration:
- OAuth 2.0 with Brand Account support
- Resumable upload (10MB chunks, max 10 retries with exponential backoff)
- Metadata: title, description, tags, category, privacy status
- Thumbnail: auto-detected from `assets/photo-index/`, resized to 1280x720
- Playlist assignment
- Made-for-kids and synthetic media declarations

### YouTube Shorts / Reels

Separate pipeline:
- `export_reels.py`: Assembles 9:16 vertical timeline from curated clips in `assets/videos-reels/`
- Max 59 seconds (YouTube Shorts requirement)
- Single random music track with 1s fade
- `render_reels.py`: Renders via Resolve at 1080x1920

### Asset System

```
assets/
├── Start-Intro-V3.mkv       # Intro branding
├── Finish-Intro-V3.mkv      # Outro CTA
├── music-background/         # Shuffled background tracks (WAV, 24-bit)
├── music-teaser/             # Teaser section music
├── photos/                   # Closing still images (3s each)
├── photo-index/              # Thumbnail source
├── teaser-videos/            # Highlight clips for teaser section
├── videos-reels/             # YouTube Shorts source clips
└── watermark/qr-code.jpg    # Brand watermark
```

---

## 8. Product Philosophy & UX

### One-Command Automation

`python run_pipeline.py` — point it at a folder of raw footage, get a published YouTube video. The system is designed for zero-intervention operation.

### Configuration-Driven Customization

Everything is controlled via `project_config.json`:
- Source/output paths
- Analysis parameters (sample interval, output ratio, speed multipliers)
- Model selection and quantization
- Export format and codec settings
- Timeline structure (intro/outro clips, watermark, music folders)
- YouTube metadata (title, tags, category, playlist, privacy)
- Reels/Shorts parameters

### Domain Specificity

This is explicitly built for one use case: process-oriented hobby/craft videos. The CLIP prompts reference "polishing compound," "scale model parts," and "surface detail." The LLM prompts say "For model building content, highly value close-up detail shots." The YouTube metadata defaults to "Howto & Style" category.

This tight domain focus allows the system to make strong assumptions about what "interesting" means, which is the opposite of VX's general-purpose editorial approach.

### No Human-in-the-Loop

There is no interactive preview, no approval step, no briefing questionnaire. The system makes all editorial decisions autonomously. The only human touchpoint is optional: opening DaVinci Resolve to inspect the timeline before rendering.

---

## 9. Technical Infrastructure

### GPU Requirements

The project is GPU-mandatory:
- NVIDIA GPU with NVENC (encoding)
- CUDA 12.4 (inference)
- Custom GCC 12.3.0 compilation required on Fedora 43 (CUDA doesn't support GCC 15)
- ~4.5GB VRAM for Qwen2.5-VL-7B (Q4_K_M)
- Batch processing: CLIP (32 images/batch), ResNet (64 images/batch)

### Performance Characteristics

For a 60-minute source video:

| Stage | Time | Notes |
|-------|------|-------|
| Frame extraction | ~30s | FFmpeg, 2s intervals = ~1,800 frames |
| CLIP analysis | ~1-2 min | GPU batch processing |
| ResNet features | ~1-2 min | GPU batch processing |
| Qwen captioning | ~5-8 min | ~1.2 frames/sec (rate-limiting factor) |
| LLM scene analysis | ~5-10s | 3 lightweight prompts |
| Clip extraction | 5-8 min | NVENC hardware encoding |
| Timeline generation | < 1s | XML synthesis |
| Resolve rendering | 3-5 min | 4K H.265 @ 30 Mbps |
| YouTube upload | 2-4 min | Bandwidth-dependent |
| **Total** | **~20-25 min** | |

### Dependencies

- **Python:** 3.9+, PyTorch + CUDA, transformers, llama-cpp-python, clip-anytorch
- **System:** FFmpeg (NVENC), DaVinci Resolve 20.x, NVIDIA CUDA Toolkit
- **Storage:** ~20GB per project (models + clips + render)

### Error Handling & Fallbacks

Graceful degradation at every level:
- Qwen unavailable → captions set to None, analysis continues
- LLM unavailable → rule-based scene detection (motion + CLIP thresholds)
- CLIP unavailable → hardcoded semantic interest = 0.5
- ResNet unavailable → basic histogram features
- Failed videos are skipped, reported, and processing continues

---

## 10. Comparative Analysis with VX

| Dimension | mazsola2k | VX |
|-----------|-----------|-----|
| **AI stack** | Local only (Qwen2.5-VL, CLIP, ResNet) | Cloud APIs (Gemini, Claude) |
| **Video understanding** | Frame sampling (2s intervals) → per-frame analysis | Proxy video upload (Gemini native) or frame extraction (Claude) |
| **Editing philosophy** | Speed ramping (keep all footage, vary speed) | Subclip selection (choose moments, cut the rest) |
| **LLM context** | Minimal (~2K tokens). LLM sees text summaries of pre-computed metrics, not visual data. | Rich. LLM sees actual video/frames + transcripts + user context. |
| **Audio** | Muted entirely. Background music overlaid. | Transcribed. Speech content informs editorial decisions. |
| **Human-in-the-loop** | None (fully autonomous) | Briefing questionnaire, interactive preview, iterative refinement |
| **Output format** | FCPXML → DaVinci Resolve → rendered MP4 | HTML preview → rough cut MP4 (and future NLE export) |
| **NLE integration** | Deep (Resolve scripting API, LUT application, automated rendering) | None currently (standalone rough cut) |
| **Structured output** | Regex parsing of free-text responses | Pydantic models, JSON schemas, Gemini response_schema |
| **Multi-clip** | Supports multi-video with cross-video deduplication | Core design (editorial storyboard across clips) |
| **Content domain** | Narrow (hobby/craft process videos) | General (travel vlogs, any multi-clip project) |
| **Versioning** | None (overwrite in place) | Full DAG versioning with lineage tracking |
| **Caching** | Per-video JSON files (metadata, scene analysis) | Per-clip cached with version-aware invalidation |
| **Cost per run** | $0 (all local) | $0.10-$2.00+ (cloud API calls) |
| **Hardware requirement** | NVIDIA GPU mandatory | Any machine (cloud inference) |
| **Transcription** | None | Gemini (speaker ID) or mlx-whisper (local) |
| **User context** | Config file only | AI-driven briefing (Gemini quick-scan → targeted questions) |
| **Style presets** | None (single hardcoded style) | Extensible preset system (Silent Vlog, etc.) |

### Key Architectural Tradeoffs

**mazsola2k's strengths:**
- Zero API cost, zero latency concerns, full privacy
- Deep NLE integration (the only project that actually renders inside a professional editor)
- Speed ramping preserves temporal continuity — no jump cuts
- Two-pass architecture cleanly separates compute-heavy feature extraction from lightweight reasoning
- Robust GPU pipeline (NVENC encoding is very fast)

**mazsola2k's limitations:**
- No audio understanding — speech, music, ambient sound are all ignored
- No user context — the system can't adapt to "focus on the engine detail" vs "focus on the painting"
- Domain-locked — the CLIP prompts and LLM guidance are hardcoded for model car building
- Regex parsing is fragile — no structured output guarantees
- 4096-token context limit constrains reasoning depth
- No iterative refinement — one shot, take it or leave it
- Single model (Qwen2.5-VL-7B) handles both captioning and reasoning — no model specialization

**VX's strengths:**
- Rich video understanding (native video upload to Gemini, full transcription)
- User briefing system captures intent and context
- General-purpose (works across content domains)
- Structured output with Pydantic validation and retry logic
- Versioning and lineage tracking for iterative workflows
- Style presets for different editing aesthetics

**VX's limitations:**
- Cloud dependency (cost, latency, rate limits)
- No NLE integration yet (rough cut only)
- No speed ramping (binary in/out selection)
- Preview is HTML-based, not a professional editing environment

---

## 11. Takeaways & Lessons

### 1. Two-Pass Analysis is a Strong Pattern
Separating feature extraction (compute-heavy, parallelizable) from reasoning (lightweight, sequential) is elegant. VX could adopt this for scenarios where we want to reduce API costs: run CLIP/ResNet locally as a first pass, then send only the summary to Gemini/Claude for editorial reasoning. This would reduce token usage and enable offline-first workflows.

### 2. Speed Ramping as an Editing Primitive
Speed ramping is a genuinely different philosophy from subclip selection. For process-oriented content (cooking, crafting, building), it preserves narrative continuity while still achieving high compression ratios. VX could offer this as a style preset option — a "process timelapse" mode where all footage is kept but boring parts are accelerated.

### 3. FCPXML / NLE Integration is Valuable
Generating FCPXML that imports cleanly into DaVinci Resolve is a significant capability. This bridges the gap between "AI rough cut" and "professional edit." VX should pursue FCPXML/AAF export as a first-class output format, allowing users to take AI-assembled timelines into their NLE for final polish.

### 4. Local Models Have a Place
For repetitive, domain-specific analysis (captioning frames with keywords, scoring visual quality), local models like CLIP and small VLMs are cost-effective and fast. VX could use local CLIP scoring as a pre-filter before sending clips to expensive cloud APIs — skip clearly boring content before it ever reaches Gemini.

### 5. Audio Matters
The most significant gap in mazsola2k's approach is ignoring audio entirely. For vlogs and most video content, speech, music, and sound events carry critical editorial information. VX's transcription pipeline is a major advantage.

### 6. Domain Specificity vs. Generality
mazsola2k's tight domain focus (9 hardcoded CLIP prompts for model car building) enables strong defaults but limits applicability. VX's general-purpose approach with user briefing is more flexible but requires more user input. The ideal may be VX's approach with optional domain-specific prompt packs.

### 7. Configuration Ergonomics
mazsola2k's single `project_config.json` controlling everything from model selection to YouTube metadata is clean but rigid. VX's separation of concerns (project config, style presets, user context) is more modular and scales better.

### 8. Structured Output Robustness
Regex parsing of LLM output is a significant fragility. Any model update or temperature change can break parsing. VX's use of Pydantic models with Gemini's `response_schema` and Claude's JSON extraction with fallbacks is more production-grade. This is a clear area where mazsola2k would benefit from upgrading.

### 9. The Rendering Gap
VX produces a rough cut MP4 and an HTML preview. mazsola2k produces a complete, rendered, uploaded YouTube video with color grading, watermarks, and multi-track audio. Closing this rendering gap — either through deeper ffmpeg assembly or NLE integration — should be a VX priority.

### 10. Evaluation is Missing from Both
Neither project has systematic evaluation of AI editorial quality. How do you know the LLM picked the right scenes? How do you measure whether speed assignments are correct? Both projects would benefit from evaluation frameworks — even simple ones like user satisfaction ratings per output or A/B testing of classification thresholds.

---

## Appendix: File Reference

| File | Lines | Purpose |
|------|-------|---------|
| `analyze_advanced5.py` | ~1,500 | Core AI analysis engine (CLIP, ResNet, Qwen2.5-VL, LLM scene analysis) |
| `extract_scenes.py` | ~400 | FFmpeg clip extraction with speed ramping |
| `export_resolve.py` | ~2,100 | FCPXML 1.13 timeline generation (largest file) |
| `run_pipeline.py` | ~750 | End-to-end orchestrator (Stages 1-5) |
| `render_youtube.py` | ~290 | DaVinci Resolve rendering (4K) |
| `render_reels.py` | ~270 | DaVinci Resolve rendering (Shorts) |
| `export_reels.py` | ~440 | Vertical FCPXML timeline for Shorts |
| `upload_youtube.py` | ~640 | YouTube upload with OAuth 2.0 |
| `apply_lut_resolve.py` | ~370 | LUT application via Resolve API |
| `project_config.json` | ~170 | All configuration settings |
| `tools/` | — | GPU setup utilities (GCC compilation, CUDA patching) |
