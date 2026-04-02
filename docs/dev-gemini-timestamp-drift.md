# Gemini Timestamp Drift — Dev Notes

## Problem

Gemini produces progressively drifting timestamps when transcribing or analyzing videos longer than ~3 minutes. The text content is accurate but timestamps overshoot — e.g., a 6:42 video yielded timestamps spanning 10:41 (59% overshoot). The drift is roughly linear and worsens with duration.

This is a well-documented upstream bug affecting multiple Gemini models. The drift pattern varies by model:

| Model | Drift severity | Notes |
|---|---|---|
| Gemini 3 Flash | Catastrophic (-157s on 12min) | 22% clock speedup |
| Gemini 3.1 Pro | Significant (-16s on 12min) | Non-linear, accelerates mid-audio |
| Gemini 3.1 Flash Lite | None | Sub-second alignment throughout |
| Gemini 2.5 Pro | Severe (erratic jumping) | Timestamps can exceed video duration |

Sources:
- [Gemini 3 Flash/3.1 Pro drift bug](https://discuss.ai.google.dev/t/bug-gemini-3-flash-and-3-1-pro-progressive-timestamp-drift-in-audio-transcription/129501)
- [Gemini 2.5 Pro timestamp jumping](https://discuss.ai.google.dev/t/gemini-2-5-pro-severe-timestamp-timecode-jumping-issues-in-video-transcription-need-workarounds/87242)
- [Forced alignment broken on 2.0 production models](https://discuss.ai.google.dev/t/timestamp-generation-forced-alignment-on-2-0-production-models-is-still-broken/79553)
- [SRT transcription suddenly broken](https://discuss.ai.google.dev/t/gemini-api-srt-transcription-suddenly-broken-timestamps-are-wildly-inaccurate-since-yesterday-despite-no-prompt-model-change/111846)
- [YouTube URL timestamp inaccuracy](https://github.com/googleapis/python-genai/issues/1359)

Key findings from research:
- The drift is **not random — it's progressive and roughly linear**, accelerating slightly around the midpoint
- **Text transcription is accurate** — only the timestamps drift. Content is recoverable with correct timing
- YouTube URLs produce worse results than direct file uploads
- Server-side model updates can silently degrade timestamp quality with no API version change
- Gemini's timestamp resolution is limited to **whole seconds** (MM:SS), too coarse for subtitle work
- Drift becomes noticeable around the **5-minute mark** on most models

## Current Mitigation (2026-04-02)

### Transcription Chunking (transcribe.py)

Videos longer than `TRANSCRIBE_CHUNK_SEC` (90s) are split and transcribed per-chunk:

```
Original video (6:42)
    │
    ├─ ffmpeg -ss 0 -t 90 -c copy → chunk_000.mp4 (92s, keyframe-aligned)
    ├─ ffmpeg -ss 90 -t 90 -c copy → chunk_001.mp4 (93s)
    ├─ ffmpeg -ss 180 -t 90 -c copy → chunk_002.mp4 (92s)
    ├─ ... etc
    │
    ▼ Each chunk → separate Gemini API call (0-based timestamps)
    │
    ▼ Merge: offset each chunk's timestamps by cumulative actual duration (via ffprobe)
    │
    ▼ Drift guard: discard segments where seg.start > chunk_duration + 2s
    │
    ▼ Clamp: seg.end = min(seg.end, chunk_duration)
    │
    ▼ Final merged transcript.json with corrected absolute timestamps
```

Key functions:
- `_split_video_chunks()` — ffmpeg `-c copy` split (fast, no re-encode, cuts at keyframes)
- `_transcribe_single_chunk_gemini()` — one Gemini API call per chunk, no file cache (temp files)
- `_merge_chunk_transcripts()` — offset + clamp + concatenate
- `_transcribe_short_clip_gemini()` — original single-call path for short videos (with file cache)

Short videos (<90s) are unaffected — same code path as before, with Gemini File API cache reuse.

### Phase 2 Prompt Hardening (editorial_prompts.py)

Phase 2 (editorial assembly) receives a concatenated proxy video for visual context, but Gemini would hallucinate timestamps from the visual instead of using the structured Phase 1 review data. Three prompt changes:

1. **CRITICAL RULES**: "in_sec/out_sec MUST come from clip reviews or transcripts — do NOT estimate timestamps by watching the video"
2. **Visual section**: Reframed as "ONLY for qualitative visual judgments" with explicit "Do NOT use the video to determine timestamps"
3. **Validation anchor**: "Before outputting each segment, verify in_sec/out_sec fall within a usable_segment from the clip review"

### Phase 2 Model Separation (config.py)

`GeminiConfig.phase2_model` allows using a more capable model for Phase 2 independently:
```python
GeminiConfig(model="gemini-3-flash-preview", phase2_model="gemini-3.1-pro-preview")
```
The `phase2` property falls back to `model` if `phase2_model` is None. File cache is model-agnostic (keyed by clip_id only), so this doesn't break caching.

### Monologue Overlay Positioning (rough_cut.py)

Monologue text overlays are now always rendered at `lower_third` (y=h*0.88-th). Speech captions move to the top (y=h*0.08) only when temporally colliding with a monologue overlay. The `center` position is reserved for future title card / word card implementation.

## Known Remaining Issues

- **Chunk boundary precision**: ffmpeg `-c copy` cuts at keyframes, not exact timestamps. Chunk durations may be ~2s off from the requested 90s. We use ffprobe to get actual duration for offset calculation, so the math is correct.
- **Phase 1 drift on very long clips**: Phase 1 sends the full proxy video to Gemini for per-clip review. Clips longer than ~5 min may have drifted timestamps in `key_moments` and `usable_segments`. Not yet chunked.
- **Phase 2 still prompt-dependent**: The LLM may still hallucinate timestamps beyond the clip duration despite prompt instructions. No hard programmatic clamping in Phase 2 output yet.
- **Transcription chunk size**: 90s is conservative. Could potentially be increased once Gemini improves, or decreased further if drift is still observed within chunks.
- **No overlap at chunk boundaries**: Chunks are split at hard boundaries. Speech crossing a boundary may be cut mid-sentence, with each half transcribed independently. No deduplication or merging of boundary segments.

## Future Work (Candidate Solutions from Research)

### 1. Programmatic Phase 2 Validation (High Priority)
After Phase 2 returns the storyboard, validate all `in_sec`/`out_sec` against actual clip durations and Phase 1 `usable_segments`. Clamp or warn on out-of-bounds timestamps. This is a safety net regardless of prompt quality.

### 2. Phase 1 Chunking
For clips >3 min, chunk Phase 1 analysis similarly to transcription. Each chunk gets its own review, then merge `key_moments` and `usable_segments` with offset correction.

### 3. Whisper + Gemini Hybrid
Use mlx-whisper for word-level timestamps (accurate, local), Gemini for speaker ID and sound event detection only. Merge the two outputs. This eliminates timestamp drift entirely for transcription while keeping Gemini's visual-context speaker identification.

Reference: [WhisperX](https://github.com/m-bain/whisperX) — Whisper + wav2vec2 phoneme alignment + pyannote diarization. LLMs can serve as diarization correction post-processing ([paper](https://arxiv.org/html/2406.04927v1)).

### 4. VAD + Gemini Chunking
Pre-segment audio with Voice Activity Detection (sileroVAD from faster-whisper) into short speech segments (<8s), send each to Gemini for transcription, reassemble with VAD timestamps. Gemini never needs to produce timestamps — it just transcribes each short clip.

Reference: [pyvideotrans approach](https://pyvideotrans.com/blog/gemini-stt-duration) — sileroVAD config: min speech 1ms, max 8 seconds, 200ms min silence, 100ms padding. Batch up to 50 segments per Gemini call.

Caveat: When segments are ~10s, Gemini may split them into multiple sentences with separate timestamps, disrupting the 1:1 segment-to-transcription correspondence.

### 5. Two-Pass Model Strategy
Use Flash Lite (no drift) for timestamp-critical tasks, Pro for text quality. Doubles API cost but gets accurate timing with better language understanding.

### 6. Prompting Best Practices (from community)
- Request MM:SS format explicitly (Gemini 2.0+ was trained on this)
- Place the prompt **after** the video part (better temporal processing)
- Provide speaker metadata upfront (names, descriptions)
- Use low media resolution for longer content (3x longer analysis at lower cost)
- Keep videos **under 2 minutes** for best timestamp accuracy ([Decipher blog](https://getdecipher.com/blog/lessons-from-using-google-gemini-for-video-analysis))

### 7. Monitor Upstream Fixes
Gemini Flash Lite reportedly has no drift. Future model versions may resolve this. The bug has been reported to Google's AI developer forum multiple times.
