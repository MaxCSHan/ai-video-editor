# VX — Performance & "Should we rewrite it?"

> This answers the question "video editing is heavy — should we rewrite the
> pipeline into a faster language for a pro-level app?" with evidence from the
> codebase, then lists the optimizations that actually move the needle.

## TL;DR

**The orchestration language was never the bottleneck — do not rewrite it.**
~All wall-clock is spent inside **ffmpeg (C)** and **remote LLM APIs**. Python
spends microseconds building commands and parsing JSON next to minute-scale work.
A Rust/Go/Swift port of the orchestrator would not move wall-clock and would
throw away a working, well-tested pipeline.

The leverage is in **smarter ffmpeg orchestration**, **correct caching**, and
putting **realtime work in the native app's AVFoundation/Metal layer** — not a
language port. One real correctness/efficiency bug was found and fixed; two more
orchestration wins are specified; the rest is a ranked roadmap.

## Evidence (from the actual code)

| Claim | Evidence |
|---|---|
| ffmpeg already HW-accelerated | `preprocess.py` probes VideoToolbox (`get_hwaccel_args`) and maps SW→HW encoders (`get_hwenc_codec`: libx264→h264_videotoolbox, libx265→hevc_videotoolbox). `rough_cut.py` encodes with `*_videotoolbox`. |
| Concat is already optimal | `rough_cut.py` uses stream-copy concat (`-c copy`) gated by a per-segment **compatibility matrix** (`_check_segment_compatibility`/`_reencode_segment`) — only mismatched segments re-encode; IDR-keyframe + color handling included. |
| Concurrency is correct for the work | `ThreadPoolExecutor` for preprocess and Phase-1 LLM (`editorial_agent.py`, `editorial_phase1.py`); the GIL is released during subprocess + network I/O, so threads are right and a process/Rust rewrite buys nothing. |
| AI stages are network-bound | Briefing, transcription (Gemini), Phase 1/2/3 are remote LLM calls; latency is API + upload, not Python. Local transcription (`mlx-whisper`) is already Metal-accelerated native. |

**Decision rule for "is a native/compiled rewrite justified?":** only if profiling
shows a **hot, pure-Python, CPU-bound loop**. None was found — the hot loops are
all ffmpeg or network. The one place compiled-native code is genuinely warranted
is **realtime playback/scrubbing/preview compositing**, which is naturally the
SwiftUI app's **AVFoundation/Metal** layer — not a pipeline rewrite.

## The realtime-vs-batch split (the actual "pro-level" architecture)

> Designed in full in [`COMPOSITION-ARCHITECTURE.md`](./COMPOSITION-ARCHITECTURE.md),
> with a 2026 fact-check. The key shift: preview by **composition reference**
> (`AVMutableComposition` + `AVPlayer`) writes **zero files** and is instant —
> replacing the current render-every-segment-then-concat model that produced 814 MB
> of intermediates and ~85 s previews on `myanmar`. Render only on **export**
> (VideoToolbox); hand off to Resolve via OTIO `.otioz` + FCPXML 1.10.

- **Native (Swift / AVFoundation / Metal)** — latency-sensitive realtime: 4K
  playback, frame-accurate scrubbing, instant trim preview, timeline rendering.
  This is where pro-level responsiveness lives, and it's native by nature.
- **Python + ffmpeg(VideoToolbox) + LLM** — throughput batch: preprocess, AI
  analysis, final render/export. Language-agnostic; already C/remote under the hood.
- **FastAPI sidecar** — the typed contract between them.

## Findings & wins

### Fixed: segment cache-key bug *(correctness + efficiency; shipped)*
`rough_cut.py` named cached segments `seg_{index:03d}_{clip_id}{suffix}.mp4` and
reused any existing file (`seg_path.exists()`), but the key included **neither
in/out nor proxy-vs-source mode**. Consequences:
- A **trim** that kept the same index served the **stale** cached segment (wrong
  pixels) — a silent correctness bug, and fatal to any "watch it re-assemble" UX.
- A pure **reorder** re-encoded identical pixels under a new name (wasted work).
- A **proxy** segment could be reused for a **full** render (cross-mode collision).

Fix: `_segment_cache_name()` — a content hash of `(clip_id, in_sec, out_sec,
transition, overlays, captions, color_target, output_format, proxy_mode)`, with
the index NOT in the cache identity. Now a trim invalidates, a reorder reuses,
and proxy/full never collide. Tests: `tests/test_rough_cut_cache.py` (7 passing),
including the verifier-mandated "trim invalidates / reorder reuses" assertions.

### Specified: two orchestration wins (Python; measurable)
1. **Single-decode preprocessing.** `preprocess.py` decodes each 4K source ~4×
   per clip — `generate_proxy`, `extract_frames`, `detect_scenes`, `extract_audio`
   are four independent ffmpeg passes (called sequentially at `preprocess.py`
   ~583–585). Collapsing to one decode + a split `filter_complex`/multi-output
   pass cuts 4K decode work ~4×→1× — the biggest CPU/IO win. Add as a new
   `preprocess_clip_single_pass()` with the existing functions as fallback.
2. **Parallel segment extraction.** `rough_cut.py`'s extraction loop is sequential
   (`for seg in …` → `_extract_segment`, ~line 1187). A bounded, codec-aware
   `ThreadPoolExecutor` (cap concurrent `*_videotoolbox` jobs to respect limited
   HW encode sessions; CPU jobs ≈ cores; preserve order) is a large final-render
   and preview win.

### Roadmap (ranked, with the substrate ties)
3. **Scrubbable playback proxy** (substrate #2): a 540/720p native-fps proxy for
   human playback; keep 360p@1fps for the LLM. Required before any "live trim".
4. **Skip re-validation of unchanged cached segments.** `assemble_rough_cut`
   probes + runs the compatibility matrix over ALL segments on every assembly
   (~N ffprobe calls). For incremental previews, skip re-probing cache hits, or
   the fixed validation cost dominates the "30–60s revision" budget at 50+ segments.
5. **Stage-timing instrumentation.** Per-stage wall-clock recorded alongside
   `traces.jsonl` so before/after wins are provable and the editor status bar can
   show honest timings. (Low-risk; supports every claim above.)
6. **Cost honesty plumbing.** Drive all pre-spend estimates from
   `tracing.estimate_*` and the real PRICING table, parameterized by clip
   duration / segment count / visual-vs-text mode — never hardcoded figures.

## Acceptance criteria (perf gates, when the interactive layer lands)

- **Single-beat re-encode (playback proxy):** p95 < 2.5s. *Conditional on*:
  re-encode reads the playback proxy (never 4K) in the hot path, HW encode, and
  debounce-to-drag-end. Final-resolution source re-encode is the SLOW path and
  belongs only at Promote/Render.
- **Proxy-mode revision (e.g. 6 changed beats):** measured, not assumed; depends
  on near-total cache reuse + skipping re-validation of unchanged segments (#4).
- **Preprocess:** single-decode pass produces byte-equivalent proxy/frames/audio
  vs the multi-pass path (verified via the existing `rough_cut` probe/validate
  layers), just faster.

## What we explicitly will NOT do

- Rewrite the pipeline orchestration into Rust/Go/Swift (no wall-clock benefit;
  discards a working, tested system). Revisit only if profiling later reveals a
  pure-Python CPU-bound hot loop.
- Add a GPU encode session per segment without a cap (VideoToolbox has limited
  concurrent encode sessions — uncapped parallelism degrades, not improves).
