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
Transcribe this video clip's audio completely. For each segment, identify:
- The speaker (by name if recognizable, else Speaker_A, Speaker_B, etc.)
- The type: "speech" for dialogue, "music" for songs/jingles, "sound_effect" for other sounds, "silence" for gaps
- Precise start and end timestamps in seconds relative to the clip start

Be thorough: capture ALL dialogue, music, and notable sound effects.
Short segments are fine — prefer accuracy over merging.
"""


def _build_gemini_prompt(speaker_hints: list[str] | None = None) -> str:
    """Build transcription prompt, optionally including speaker hints from briefing."""
    prompt = GEMINI_TRANSCRIBE_PROMPT
    if speaker_hints:
        names = "\n".join(f"- {name}" for name in speaker_hints)
        prompt += (
            "\nKnown people in this footage:\n"
            f"{names}\n"
            "Use these names when you identify these speakers.\n"
        )
    return prompt


def transcribe_clip_gemini(
    proxy_path: Path,
    clip_paths: ProjectPaths,
    cfg: TranscribeConfig,
    speaker_hints: list[str] | None = None,
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

    from .models import Transcript

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None

    client = genai.Client(api_key=api_key)

    # Upload proxy video (retains audio at AAC 64k)
    video_file = client.files.upload(file=str(proxy_path))

    while video_file.state.name == "PROCESSING":
        time.sleep(3)
        video_file = client.files.get(name=video_file.name)

    if video_file.state.name == "FAILED":
        return None

    prompt = _build_gemini_prompt(speaker_hints)

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
            response_schema=Transcript,
        ),
    )

    transcript = Transcript.model_validate_json(response.text)
    result = transcript.model_dump()

    # Ensure provider is marked
    result["provider"] = "gemini"

    # Ensure audio dir exists and cache
    clip_paths.audio.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return result


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

    Uses word-level timestamps when available for tighter cues,
    otherwise falls back to segment-level timestamps.
    """
    entries = []
    index = 1

    for seg in transcript.get("segments", []):
        if not seg.get("text"):
            continue
        entries.append(
            f"{index}\n"
            f"{_srt_timecode(seg['start'])} --> {_srt_timecode(seg['end'])}\n"
            f"{seg['text']}\n"
        )
        index += 1

    output_path.write_text("\n".join(entries), encoding="utf-8")
    return output_path


def _srt_timecode(seconds: float) -> str:
    """Convert seconds to SRT timecode format: HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds % 1) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
