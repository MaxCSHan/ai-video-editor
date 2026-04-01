"""Claude pattern — frame-based analysis using images sent to the Claude API."""

import base64
import os
from pathlib import Path

import anthropic

from .config import ClaudeConfig
from .storyboard_format import build_storyboard_prompt, format_duration


def get_client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set. See .env.example")
    return anthropic.Anthropic(api_key=api_key)


def _load_image_b64(path: Path) -> str:
    return base64.standard_b64encode(path.read_bytes()).decode("utf-8")


def _build_image_content(image_path: Path, timestamp_fmt: str) -> list[dict]:
    """Build a Claude API content block for one image with its timestamp label."""
    return [
        {"type": "text", "text": f"[{timestamp_fmt}]"},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": _load_image_b64(image_path),
            },
        },
    ]


def analyze_frame_batch(
    client: anthropic.Anthropic,
    frames_dir: Path,
    batch_frames: list[dict],
    video_info: dict,
    batch_index: int,
    total_batches: int,
    cfg: ClaudeConfig,
) -> str:
    """Analyze a batch of frames (up to max_images_per_batch). Returns text analysis."""
    content = []
    for frame in batch_frames:
        image_path = frames_dir / frame["file"]
        content.extend(_build_image_content(image_path, frame["timestamp_fmt"]))

    time_start = batch_frames[0]["timestamp_fmt"]
    time_end = batch_frames[-1]["timestamp_fmt"]

    content.append(
        {
            "type": "text",
            "text": (
                f"These are {len(batch_frames)} frames from a {format_duration(video_info['duration_sec'])} video, "
                f"covering {time_start} to {time_end} (batch {batch_index + 1}/{total_batches}).\n\n"
                "For this segment, describe each distinct shot/scene you observe:\n"
                "- Timestamp range\n"
                "- Shot type (wide/medium/close-up/etc.)\n"
                "- Visual description (setting, subjects, action)\n"
                "- Camera movement\n"
                "- Notable audio cues (if apparent from visual context)\n\n"
                "Group consecutive similar frames into single shots. Be specific about what changes between shots."
            ),
        }
    )

    response = client.messages.create(
        model=cfg.model,
        max_tokens=cfg.max_tokens,
        temperature=cfg.temperature,
        messages=[{"role": "user", "content": content}],
    )
    return response.content[0].text


def synthesize_storyboard(
    client: anthropic.Anthropic,
    batch_analyses: list[str],
    video_info: dict,
    scenes: list[dict],
    cfg: ClaudeConfig,
) -> str:
    """Combine batch analyses into a unified storyboard markdown."""
    duration_str = format_duration(video_info["duration_sec"])
    resolution_str = f"{video_info['width']}x{video_info['height']}"

    storyboard_template = build_storyboard_prompt(
        filename=video_info["filename"],
        duration=duration_str,
        resolution=resolution_str,
    )

    scene_timestamps = ", ".join(s["timestamp_fmt"] for s in scenes) if scenes else "N/A"

    prompt = (
        "You are combining batch analyses of a video into a single unified storyboard.\n\n"
        f"Scene change boundaries detected at: {scene_timestamps}\n\n"
        "Here are the per-segment analyses:\n\n"
    )
    for i, analysis in enumerate(batch_analyses):
        prompt += f"--- Segment {i + 1} ---\n{analysis}\n\n"

    prompt += (
        "\nNow produce the FINAL unified storyboard markdown. "
        "Merge overlapping segments, resolve any conflicts, and use the format below.\n\n"
        f"{storyboard_template}"
    )

    response = client.messages.create(
        model=cfg.model,
        max_tokens=cfg.max_tokens * 2,
        temperature=cfg.temperature,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def run_claude_analysis(
    frames_dir: Path,
    frames_manifest: dict,
    scenes: list[dict],
    video_info: dict,
    storyboard_dir: Path,
    cfg: ClaudeConfig,
) -> Path:
    """Full Claude pipeline: batch analyze frames → synthesize → write storyboard."""
    client = get_client()
    all_frames = frames_manifest["frames"]

    batches = [
        all_frames[i : i + cfg.max_images_per_batch]
        for i in range(0, len(all_frames), cfg.max_images_per_batch)
    ]

    print(f"  Analyzing {len(all_frames)} frames in {len(batches)} batch(es)...")
    batch_analyses = []
    for i, batch in enumerate(batches):
        print(f"  Batch {i + 1}/{len(batches)} ({len(batch)} frames)...")
        analysis = analyze_frame_batch(client, frames_dir, batch, video_info, i, len(batches), cfg)
        batch_analyses.append(analysis)

    print("  Synthesizing final storyboard...")
    storyboard_md = synthesize_storyboard(client, batch_analyses, video_info, scenes, cfg)

    storyboard_dir.mkdir(parents=True, exist_ok=True)
    output_path = storyboard_dir / "storyboard_claude.md"
    output_path.write_text(storyboard_md)
    print(f"  Storyboard written to {output_path}")
    return output_path
