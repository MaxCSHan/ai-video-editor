"""Audio transcription — local (mlx-whisper) or cloud (Gemini structured output).

Produces timestamped transcript JSON per clip, formatted prompts for LLM injection,
and SRT subtitle files.

Providers:
    mlx    — Local, fast, no API cost. Requires: uv pip install -e ".[whisper]"
    gemini — Cloud, richer output (speaker ID, sound events). Requires: GEMINI_API_KEY
"""

import json
import os
import time
from pathlib import Path

from .config import ProjectPaths, TranscribeConfig


def transcribe_clip(
    audio_path: Path,
    clip_paths: ProjectPaths,
    cfg: TranscribeConfig,
) -> dict | None:
    """Transcribe a single clip's audio. Returns transcript dict or None if unavailable.

    Results are cached to clip_paths.audio / "transcript.json".
    """
    transcript_path = clip_paths.audio / "transcript.json"
    if transcript_path.exists():
        return json.loads(transcript_path.read_text())

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
    transcript_path.write_text(json.dumps(transcript, indent=2, ensure_ascii=False))
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


def transcribe_clip_gemini(
    proxy_path: Path,
    clip_paths: ProjectPaths,
    cfg: TranscribeConfig,
    speaker_context: str | None = None,
) -> dict | None:
    """Transcribe a clip via Gemini structured output from its proxy video.

    Uses the same File API upload pattern as Phase 1 clip review.
    Results are cached to clip_paths.audio / "transcript.json".
    """
    transcript_path = clip_paths.audio / "transcript.json"
    if transcript_path.exists():
        return json.loads(transcript_path.read_text())

    from google import genai
    from google.genai import types

    from .models import GeminiTranscript

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None

    client = genai.Client(api_key=api_key)

    # Upload proxy video (retains audio at AAC 64k + visual context)
    video_file = client.files.upload(file=str(proxy_path))

    while video_file.state.name == "PROCESSING":
        time.sleep(3)
        video_file = client.files.get(name=video_file.name)

    if video_file.state.name == "FAILED":
        return None

    prompt = _build_gemini_prompt(speaker_context)

    response = client.models.generate_content(
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
    )

    gemini_result = GeminiTranscript.model_validate_json(response.text)

    # Transform lean Gemini response into canonical transcript.json format
    result = _gemini_to_canonical(gemini_result, cfg.gemini_model)

    # Ensure audio dir exists and cache
    clip_paths.audio.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))

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
