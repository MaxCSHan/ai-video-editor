"""Audio transcription via mlx-whisper (Apple Silicon optimized).

Produces timestamped transcript JSON per clip, formatted prompts for LLM injection,
and SRT subtitle files. Requires the optional 'whisper' dependency:
    uv pip install -e ".[whisper]"
"""

import json
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


def format_transcript_for_prompt(transcript: dict, max_chars: int = 3000) -> str:
    """Format transcript as readable text for LLM prompt injection.

    Returns lines like: [0:05] Welcome to the race!
    Truncates to max_chars if needed.
    """
    if not transcript.get("has_speech"):
        return "(no speech detected)"

    lines = []
    for seg in transcript.get("segments", []):
        start = seg["start"]
        mins = int(start // 60)
        secs = int(start % 60)
        lines.append(f"[{mins}:{secs:02d}] {seg['text']}")

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
