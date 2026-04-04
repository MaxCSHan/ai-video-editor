"""Audio transcription — local (mlx-whisper) or cloud (Gemini structured output).

Produces timestamped transcript JSON per clip, formatted prompts for LLM injection,
and SRT subtitle files.

Providers:
    mlx    — Local, fast, no API cost. Requires: uv pip install -e ".[whisper]"
    gemini — Cloud, richer output (speaker ID, sound events). Requires: GEMINI_API_KEY
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .config import ProjectPaths, TranscribeConfig

_GEMINI_UPLOAD_TIMEOUT_SEC = 300


def _wait_for_gemini_file(video_file, client, label: str = ""):
    """Poll until Gemini file processing completes, with timeout."""
    start = time.monotonic()
    while video_file.state.name == "PROCESSING":
        if time.monotonic() - start > _GEMINI_UPLOAD_TIMEOUT_SEC:
            raise TimeoutError(
                f"Gemini file processing timed out after {_GEMINI_UPLOAD_TIMEOUT_SEC}s ({label})"
            )
        time.sleep(3)
        video_file = client.files.get(name=video_file.name)
    if video_file.state.name == "FAILED":
        raise RuntimeError(f"Gemini file processing failed for {label}")
    return video_file


if TYPE_CHECKING:
    from .models import GeminiTranscript

# Max chunk duration for Gemini transcription to avoid timestamp drift.
# Gemini's timestamps drift progressively on videos longer than ~3-5 minutes.
TRANSCRIBE_CHUNK_SEC = 90  # 1.5 minutes — Gemini drifts noticeably past ~3 min


def transcribe_clip(
    audio_path: Path,
    clip_paths: ProjectPaths,
    cfg: TranscribeConfig,
) -> dict | None:
    """Transcribe a single clip's audio. Returns transcript dict or None if unavailable.

    Results are versioned to clip_paths.audio / "transcript_mlx_v{N}.json".
    """
    from .versioning import resolve_transcript_path

    cached = resolve_transcript_path(clip_paths.root)
    if cached:
        return json.loads(cached.read_text())

    try:
        import mlx_whisper
    except ImportError:
        return None

    kwargs = {
        "path_or_hf_repo": cfg.model,
        "word_timestamps": cfg.word_timestamps,
    }
    if cfg.language:
        kwargs["language"] = cfg.language

    result = mlx_whisper.transcribe(str(audio_path), **kwargs)

    transcript = _build_transcript(result, audio_path.name, cfg.model)

    from .versioning import begin_version, commit_version, versioned_path, update_latest_symlink

    clip_paths.audio.mkdir(parents=True, exist_ok=True)
    meta = begin_version(
        clip_paths.root,
        phase="transcript",
        provider="mlx",
        config_snapshot={"model": cfg.model},
        target_dir=clip_paths.audio,
    )
    out = versioned_path(clip_paths.audio / "transcript_mlx.json", meta.version)
    out.write_text(json.dumps(transcript, indent=2, ensure_ascii=False))
    update_latest_symlink(out, link_name="transcript_latest.json")
    commit_version(clip_paths.root, meta, output_paths=[out], target_dir=clip_paths.audio)
    return transcript


def _build_transcript(whisper_result: dict, source_audio: str, model: str) -> dict:
    """Transform mlx-whisper output into canonical transcript format."""
    segments = []
    for seg in whisper_result.get("segments", []):
        words = []
        for w in seg.get("words", []):
            words.append(
                {
                    "word": w["word"].strip(),
                    "start": round(w["start"], 3),
                    "end": round(w["end"], 3),
                }
            )
        segments.append(
            {
                "start": round(seg["start"], 3),
                "end": round(seg["end"], 3),
                "text": seg["text"].strip(),
                "words": words,
            }
        )

    full_text = whisper_result.get("text", "").strip()
    language = whisper_result.get("language", "unknown")
    has_speech = bool(full_text and any(s["text"] for s in segments))

    # Estimate duration from last segment end, or 0 if no segments
    duration_sec = segments[-1]["end"] if segments else 0.0

    return {
        "source_audio": source_audio,
        "model": model,
        "language": language,
        "text": full_text,
        "segments": segments,
        "duration_sec": round(duration_sec, 3),
        "has_speech": has_speech,
    }


# ---------------------------------------------------------------------------
# Gemini transcription (cloud, structured output)
# ---------------------------------------------------------------------------

GEMINI_TRANSCRIBE_PROMPT = """\
You are watching a video clip. Transcribe the audio while using the visual context \
to identify who is speaking (match voices to the people you see on screen).

Rules:
- Only transcribe speech you can ACTUALLY HEAR. Do NOT invent or guess dialogue.
- When no one is speaking, use type "silence", "music", or "sound_effect" as appropriate. \
NEVER fabricate speech during quiet moments.
- If you hear speech but cannot make out the words, use type "speech" with text "[inaudible]".
- Identify speakers by matching their voice to the person visible on screen. \
If you cannot identify them, use Speaker_A, Speaker_B, etc. consistently.
- Timestamps in seconds relative to clip start. Sentence-level granularity is sufficient.
- For music segments, include the song name if recognizable.
- For sound effects, briefly describe the sound (e.g., "door slam", "crowd cheering").
"""


def _build_gemini_prompt(speaker_context: str | None = None) -> str:
    """Build transcription prompt, optionally including speaker context from briefing."""
    prompt = GEMINI_TRANSCRIBE_PROMPT
    if speaker_context:
        prompt += (
            "\nContext about the people in this footage (from the filmmaker):\n"
            f"{speaker_context}\n\n"
            "Use the names and descriptions above to identify speakers accurately. "
            "Match voices to the people described.\n"
        )
    return prompt


def _split_video_chunks(proxy_path: Path, chunk_sec: float = TRANSCRIBE_CHUNK_SEC) -> list[Path]:
    """Split a video into ≤chunk_sec chunks for accurate Gemini transcription.

    Returns list of chunk paths. If the video is already short enough, returns [proxy_path].
    Chunks are written to a _transcribe_chunks/ subdir alongside the proxy.
    """
    from .preprocess import get_video_duration

    duration = get_video_duration(proxy_path)
    if duration <= chunk_sec:
        return [proxy_path]

    chunk_dir = proxy_path.parent / "_transcribe_chunks"
    chunk_dir.mkdir(exist_ok=True)

    chunks: list[Path] = []
    offset = 0.0
    idx = 0
    while offset < duration:
        chunk_path = chunk_dir / f"chunk_{idx:03d}.mp4"
        cmd = [
            "ffmpeg",
            "-y",
            "-v",
            "quiet",
            "-ss",
            str(offset),
            "-i",
            str(proxy_path),
            "-t",
            str(chunk_sec),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(chunk_path),
        ]
        subprocess.run(cmd, check=True)
        chunks.append(chunk_path)
        offset += chunk_sec
        idx += 1

    return chunks


def _merge_chunk_transcripts(
    chunks: list[tuple[float, float, "GeminiTranscript"]],
) -> "GeminiTranscript":
    """Merge transcripts from sequential chunks into one.

    Each tuple is (offset_sec, chunk_duration_sec, transcript) where offset_sec
    is the chunk's start time in the original video. Segments with timestamps
    exceeding the chunk duration are discarded (Gemini drift protection).
    """
    from .models import GeminiTranscript, GeminiTranscriptSegment

    all_segments: list[GeminiTranscriptSegment] = []
    all_speakers: set[str] = set()
    language = "unknown"
    has_speech = False
    dropped_count = 0

    for offset_sec, chunk_dur, transcript in chunks:
        language = transcript.language
        if transcript.has_speech:
            has_speech = True
        all_speakers.update(transcript.speakers)

        for seg in transcript.segments:
            # Discard segments where Gemini's timestamps drifted past the chunk boundary
            if seg.start > chunk_dur + 2.0:
                dropped_count += 1
                continue
            # Clamp end to chunk duration
            clamped_end = min(seg.end, chunk_dur)
            all_segments.append(
                GeminiTranscriptSegment(
                    start=round(seg.start + offset_sec, 3),
                    end=round(clamped_end + offset_sec, 3),
                    text=seg.text,
                    speaker=seg.speaker,
                    type=seg.type,
                )
            )

    if dropped_count > 0:
        total_segs = sum(len(t.segments) for _, _, t in chunks)
        print(f"  Transcript merge: dropped {dropped_count}/{total_segs} drifted segments")

    return GeminiTranscript(
        language=language,
        segments=all_segments,
        speakers=sorted(all_speakers),
        has_speech=has_speech,
    )


def _transcribe_short_clip_gemini(
    proxy_path: Path,
    clip_id: str,
    cfg: TranscribeConfig,
    speaker_context: str | None,
    tracer,
    editorial_paths,
) -> "GeminiTranscript":
    """Transcribe a short clip (≤TRANSCRIBE_CHUNK_SEC) via Gemini with file cache support."""
    from google import genai
    from google.genai import types

    from .models import GeminiTranscript
    from .tracing import traced_gemini_generate

    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    # Check file cache before uploading (may already be cached by briefing)
    cached_uri = None
    if editorial_paths:
        from .file_cache import cache_file_uri, get_cached_uri, load_file_api_cache

        file_cache = load_file_api_cache(editorial_paths)
        cached_uri = get_cached_uri(file_cache, clip_id)

    if cached_uri:
        file_uri = cached_uri
    else:
        video_file = client.files.upload(file=str(proxy_path))
        video_file = _wait_for_gemini_file(video_file, client, clip_id)
        file_uri = video_file.uri
        if editorial_paths:
            cache_file_uri(editorial_paths, clip_id, file_uri)

    prompt = _build_gemini_prompt(speaker_context)

    response = traced_gemini_generate(
        client,
        model=cfg.gemini_model,
        contents=[
            types.Content(
                parts=[
                    types.Part.from_uri(file_uri=file_uri, mime_type="video/mp4"),
                    types.Part.from_text(text=prompt),
                ]
            )
        ],
        config=types.GenerateContentConfig(
            temperature=0.2,
            response_mime_type="application/json",
            response_schema=GeminiTranscript,
        ),
        phase="transcribe",
        clip_id=clip_id,
        tracer=tracer,
        num_video_files=1,
        prompt_chars=len(prompt),
    )

    gemini_result = getattr(response, "parsed", None)
    if gemini_result is None:
        raw_text = (response.text or "").strip()
        print(f"  WARN [{clip_id}] Gemini returned malformed JSON, attempting recovery...")
        print(f"  Raw response ({len(raw_text)} chars):\n{raw_text[:500]}")
        decoder = json.JSONDecoder()
        parsed, end_idx = decoder.raw_decode(raw_text)
        trailing = raw_text[end_idx:].strip()
        if trailing:
            print(f"  Discarded trailing data ({len(trailing)} chars): {trailing[:200]}")
        gemini_result = GeminiTranscript.model_validate(parsed)

    return gemini_result


def _transcribe_single_chunk_gemini(
    chunk_path: Path,
    cfg: TranscribeConfig,
    speaker_context: str | None,
    tracer,
    clip_id_tag: str,
    chunk_label: str,
) -> "GeminiTranscript":
    """Transcribe a single video chunk via Gemini. Returns parsed GeminiTranscript."""
    from google import genai
    from google.genai import types

    from .models import GeminiTranscript
    from .tracing import traced_gemini_generate

    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    video_file = client.files.upload(file=str(chunk_path))
    video_file = _wait_for_gemini_file(video_file, client, chunk_label)

    prompt = _build_gemini_prompt(speaker_context)

    response = traced_gemini_generate(
        client,
        model=cfg.gemini_model,
        contents=[
            types.Content(
                parts=[
                    types.Part.from_uri(file_uri=video_file.uri, mime_type="video/mp4"),
                    types.Part.from_text(text=prompt),
                ]
            )
        ],
        config=types.GenerateContentConfig(
            temperature=0.2,
            response_mime_type="application/json",
            response_schema=GeminiTranscript,
        ),
        phase="transcribe",
        clip_id=f"{clip_id_tag}/{chunk_label}",
        tracer=tracer,
        num_video_files=1,
        prompt_chars=len(prompt),
    )

    gemini_result = getattr(response, "parsed", None)
    if gemini_result is None:
        raw_text = (response.text or "").strip()
        print(f"  WARN [{clip_id_tag}/{chunk_label}] malformed JSON, attempting recovery...")
        print(f"  Raw response ({len(raw_text)} chars):\n{raw_text[:500]}")
        decoder = json.JSONDecoder()
        parsed, end_idx = decoder.raw_decode(raw_text)
        trailing = raw_text[end_idx:].strip()
        if trailing:
            print(f"  Discarded trailing data ({len(trailing)} chars): {trailing[:200]}")
        gemini_result = GeminiTranscript.model_validate(parsed)

    return gemini_result


def transcribe_clip_gemini(
    proxy_path: Path,
    clip_paths: ProjectPaths,
    cfg: TranscribeConfig,
    speaker_context: str | None = None,
    tracer=None,
    editorial_paths=None,
) -> dict | None:
    """Transcribe a clip via Gemini structured output from its proxy video.

    For videos longer than TRANSCRIBE_CHUNK_SEC (1.5 min), splits into chunks and
    transcribes each separately to avoid Gemini's progressive timestamp drift,
    then merges with corrected offsets.

    Results are cached to clip_paths.audio / "transcript.json".
    If editorial_paths is provided, reuses/populates the shared Gemini File API cache.
    """
    from .versioning import resolve_transcript_path

    cached = resolve_transcript_path(clip_paths.root)
    if cached:
        return json.loads(cached.read_text())

    from .preprocess import get_video_duration

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None

    clip_id = proxy_path.stem.replace("_proxy", "")

    # Split into chunks if video is long enough to cause timestamp drift
    chunks = _split_video_chunks(proxy_path)

    if len(chunks) == 1 and chunks[0] == proxy_path:
        # Short video — single-call path (uses file cache for efficiency)
        gemini_result = _transcribe_short_clip_gemini(
            proxy_path, clip_id, cfg, speaker_context, tracer, editorial_paths
        )
    else:
        # Long video — chunked transcription
        print(f"  [{clip_id}] Splitting into {len(chunks)} chunks for transcription...")
        chunk_dir = proxy_path.parent / "_transcribe_chunks"
        try:
            chunk_results: list[tuple[float, float, GeminiTranscript]] = []
            offset = 0.0
            for i, chunk_path in enumerate(chunks):
                chunk_label = f"chunk_{i:03d}"
                chunk_dur = get_video_duration(chunk_path)
                print(
                    f"  [{clip_id}] Transcribing {chunk_label}"
                    f" (offset {offset:.1f}s, {chunk_dur:.1f}s)..."
                )
                result = _transcribe_single_chunk_gemini(
                    chunk_path, cfg, speaker_context, tracer, clip_id, chunk_label
                )
                chunk_results.append((offset, chunk_dur, result))
                offset += chunk_dur

            gemini_result = _merge_chunk_transcripts(chunk_results)
        finally:
            # Clean up chunk files even on error
            if chunk_dir.exists():
                for f in chunk_dir.iterdir():
                    f.unlink()
                chunk_dir.rmdir()

    # Transform lean Gemini response into canonical transcript.json format
    result = _gemini_to_canonical(gemini_result, cfg.gemini_model)

    # Save with versioning
    from .versioning import begin_version, commit_version, versioned_path, update_latest_symlink

    clip_paths.audio.mkdir(parents=True, exist_ok=True)
    meta = begin_version(
        clip_paths.root,
        phase="transcript",
        provider="gemini",
        clip_id=clip_id,
        config_snapshot={"model": cfg.gemini_model},
        target_dir=clip_paths.audio,
    )
    out = versioned_path(clip_paths.audio / "transcript_gemini.json", meta.version)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    update_latest_symlink(out, link_name="transcript_latest.json")
    commit_version(clip_paths.root, meta, output_paths=[out], target_dir=clip_paths.audio)

    # Generate VTT + preview HTML alongside the transcript
    vtt_path = clip_paths.audio / "transcript.vtt"
    generate_vtt(result, vtt_path)

    preview_path = clip_paths.audio / "transcript_preview.html"
    generate_transcript_preview(
        clip_id=proxy_path.stem.replace("_proxy", ""),
        proxy_path=proxy_path,
        transcript=result,
        vtt_path=vtt_path,
        output_path=preview_path,
    )

    return result


def _gemini_to_canonical(gemini, model: str) -> dict:
    """Transform GeminiTranscript into canonical transcript.json dict."""
    segments = []
    for seg in gemini.segments:
        segments.append(
            {
                "start": round(seg.start, 3),
                "end": round(seg.end, 3),
                "text": seg.text,
                "speaker": seg.speaker,
                "type": seg.type,
            }
        )

    # Build full text from speech segments only
    speech_texts = [s.text for s in gemini.segments if s.type == "speech" and s.text]
    full_text = " ".join(speech_texts)

    duration_sec = segments[-1]["end"] if segments else 0.0

    return {
        "model": model,
        "language": gemini.language,
        "text": full_text,
        "segments": segments,
        "duration_sec": round(duration_sec, 3),
        "has_speech": gemini.has_speech,
        "speakers": gemini.speakers,
        "provider": "gemini",
    }


# ---------------------------------------------------------------------------
# Prompt formatting (for Phase 1/2 injection)
# ---------------------------------------------------------------------------


def format_transcript_for_prompt(transcript: dict, max_chars: int = 3000) -> str:
    """Format transcript as readable text for LLM prompt injection.

    For Gemini transcripts (with speakers/types):
        [0:05] [MUSIC]
        [1:03] Jinx: Tu es vraiment irrécupérable.
        [1:42] [sound_effect: explosion]

    For mlx-whisper transcripts (speech only):
        [0:05] Welcome to the race!

    Truncates to max_chars if needed.
    """
    if not transcript.get("has_speech"):
        return "(no speech detected)"

    lines = []
    for seg in transcript.get("segments", []):
        start = seg["start"]
        mins = int(start // 60)
        secs = int(start % 60)
        ts = f"[{mins}:{secs:02d}]"

        seg_type = seg.get("type", "speech")
        speaker = seg.get("speaker")
        text = seg.get("text", "")

        if seg_type == "music":
            lines.append(f"{ts} [MUSIC: {text}]" if text else f"{ts} [MUSIC]")
        elif seg_type == "sound_effect":
            lines.append(f"{ts} [sound_effect: {text}]" if text else f"{ts} [sound_effect]")
        elif seg_type == "silence":
            continue  # skip silence segments in prompts
        elif speaker:
            lines.append(f"{ts} {speaker}: {text}")
        else:
            lines.append(f"{ts} {text}")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars].rsplit("\n", 1)[0] + "\n... (transcript truncated)"
    return text


def generate_srt(transcript: dict, output_path: Path) -> Path:
    """Generate SRT subtitle file from transcript.

    Includes speaker prefixes and non-speech markers when available.
    """
    entries = []
    index = 1

    for seg in transcript.get("segments", []):
        text = seg.get("text", "")
        if not text:
            continue

        seg_type = seg.get("type", "speech")
        speaker = seg.get("speaker")

        if seg_type == "music":
            cue_text = f"\u266a {text} \u266a" if text else "\u266a Music \u266a"
        elif seg_type == "sound_effect":
            cue_text = f"[{text}]"
        elif seg_type == "silence":
            continue
        elif speaker:
            cue_text = f"{speaker}: {text}"
        else:
            cue_text = text

        entries.append(
            f"{index}\n{_srt_timecode(seg['start'])} --> {_srt_timecode(seg['end'])}\n{cue_text}\n"
        )
        index += 1

    output_path.write_text("\n".join(entries), encoding="utf-8")
    return output_path


def generate_vtt(transcript: dict, output_path: Path) -> Path:
    """Generate WebVTT subtitle file from transcript.

    Includes speaker prefixes and non-speech markers for use with HTML5 <video> <track>.
    """
    lines = ["WEBVTT", ""]

    for seg in transcript.get("segments", []):
        text = seg.get("text", "")
        if not text:
            continue

        seg_type = seg.get("type", "speech")
        speaker = seg.get("speaker")

        if seg_type == "music":
            cue_text = f"\u266a {text} \u266a" if text else "\u266a Music \u266a"
        elif seg_type == "sound_effect":
            cue_text = f"[{text}]" if text else "[sound effect]"
        elif seg_type == "silence":
            continue
        elif speaker:
            cue_text = f"{speaker}: {text}"
        else:
            cue_text = text

        lines.append(f"{_vtt_timecode(seg['start'])} --> {_vtt_timecode(seg['end'])}")
        lines.append(cue_text)
        lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


def _vtt_timecode(seconds: float) -> str:
    """Convert seconds to WebVTT timecode format: MM:SS.mmm"""
    m = int(seconds // 60)
    s = int(seconds % 60)
    ms = int(round((seconds % 1) * 1000))
    return f"{m:02d}:{s:02d}.{ms:03d}"


def generate_transcript_preview(
    clip_id: str,
    proxy_path: Path,
    transcript: dict,
    vtt_path: Path,
    output_path: Path,
) -> Path:
    """Generate self-contained HTML preview: video with captions + clickable transcript."""
    segments_json = json.dumps(transcript.get("segments", []))
    proxy_rel = os.path.relpath(proxy_path, output_path.parent)
    vtt_rel = os.path.relpath(vtt_path, output_path.parent)

    html = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Transcript: {clip_id}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, system-ui, sans-serif; background: #1a1a1a; color: #e0e0e0; }}
.container {{ display: flex; height: 100vh; }}
.video-panel {{ flex: 1; display: flex; flex-direction: column; padding: 16px; }}
.video-panel h2 {{ margin-bottom: 8px; font-size: 14px; color: #888; }}
video {{ width: 100%; max-height: 60vh; background: #000; border-radius: 8px; }}
.info {{ margin-top: 12px; font-size: 12px; color: #666; }}
.transcript-panel {{ width: 380px; border-left: 1px solid #333; overflow-y: auto; padding: 16px; }}
.transcript-panel h2 {{ margin-bottom: 12px; font-size: 14px; color: #888; }}
.seg {{ padding: 8px 10px; margin-bottom: 4px; border-radius: 6px; cursor: pointer; font-size: 13px;
        line-height: 1.5; transition: background 0.15s; }}
.seg:hover {{ background: #2a2a2a; }}
.seg.active {{ background: #2d4a2d; }}
.seg-time {{ color: #666; font-size: 11px; font-family: monospace; margin-right: 8px; }}
.seg-speaker {{ color: #4fc3f7; font-weight: 600; }}
.seg-type {{ color: #888; font-style: italic; }}
.seg-music {{ color: #ce93d8; }}
.seg-sfx {{ color: #ffb74d; }}
</style>
</head>
<body>
<div class="container">
  <div class="video-panel">
    <h2>{clip_id}</h2>
    <video id="vid" controls>
      <source src="{proxy_rel}" type="video/mp4">
      <track id="captions" kind="captions" src="{vtt_rel}" srclang="en" label="Transcript" default>
    </video>
    <div class="info">
      Speakers: {", ".join(transcript.get("speakers", [])) or "N/A"} |
      Language: {transcript.get("language", "?")} |
      Provider: {transcript.get("provider", "?")}
    </div>
  </div>
  <div class="transcript-panel">
    <h2>Transcript</h2>
    <div id="segments"></div>
  </div>
</div>
<script>
const segments = {segments_json};
const vid = document.getElementById('vid');
const container = document.getElementById('segments');

function fmtTime(s) {{
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return m + ':' + String(sec).padStart(2, '0');
}}

segments.forEach((seg, i) => {{
  const div = document.createElement('div');
  div.className = 'seg';
  div.dataset.index = i;

  let content = '<span class="seg-time">' + fmtTime(seg.start) + '</span>';
  if (seg.type === 'music') {{
    content += '<span class="seg-music">\\u266a ' + (seg.text || 'Music') + ' \\u266a</span>';
  }} else if (seg.type === 'sound_effect') {{
    content += '<span class="seg-sfx">[' + (seg.text || 'sound effect') + ']</span>';
  }} else if (seg.type === 'silence') {{
    content += '<span class="seg-type">[silence]</span>';
  }} else {{
    if (seg.speaker) content += '<span class="seg-speaker">' + seg.speaker + ':</span> ';
    content += seg.text;
  }}
  div.innerHTML = content;

  div.onclick = () => {{ vid.currentTime = seg.start; vid.play(); }};
  container.appendChild(div);
}});

vid.ontimeupdate = () => {{
  const t = vid.currentTime;
  document.querySelectorAll('.seg').forEach(el => {{
    const i = parseInt(el.dataset.index);
    const seg = segments[i];
    const active = t >= seg.start && t < seg.end;
    el.classList.toggle('active', active);
    if (active) el.scrollIntoView({{ block: 'nearest', behavior: 'smooth' }});
  }});
}};
</script>
</body>
</html>"""

    output_path.write_text(html, encoding="utf-8")
    return output_path


def _srt_timecode(seconds: float) -> str:
    """Convert seconds to SRT timecode format: HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds % 1) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
