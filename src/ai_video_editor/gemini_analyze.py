"""Gemini pattern — upload proxy video and analyze with native video understanding."""

import os
import time
from pathlib import Path

from google import genai
from google.genai import types

from .config import GeminiConfig
from .storyboard_format import build_storyboard_prompt, format_duration


def get_client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY environment variable is not set. See .env.example")
    return genai.Client(api_key=api_key)


_GEMINI_UPLOAD_TIMEOUT_SEC = 300


def upload_video(client: genai.Client, video_path: Path) -> types.File:
    """Upload video to Gemini File API and wait for processing."""
    print(f"  Uploading {video_path.name} ({video_path.stat().st_size / 1024 / 1024:.1f} MB)...")
    video_file = client.files.upload(file=str(video_path))

    start = time.monotonic()
    while video_file.state.name == "PROCESSING":
        if time.monotonic() - start > _GEMINI_UPLOAD_TIMEOUT_SEC:
            raise TimeoutError(
                f"Gemini file processing timed out after {_GEMINI_UPLOAD_TIMEOUT_SEC}s"
            )
        print("  Waiting for Gemini to process video...")
        time.sleep(5)
        video_file = client.files.get(name=video_file.name)

    if video_file.state.name == "FAILED":
        raise RuntimeError(f"Gemini video processing failed: {video_file.state}")

    print("  Video processed successfully.")
    return video_file


def analyze_video(
    client: genai.Client,
    video_file: types.File,
    video_info: dict,
    cfg: GeminiConfig,
) -> str:
    """Send video + storyboard prompt to Gemini and return the markdown response."""
    duration_str = format_duration(video_info["duration_sec"])
    resolution_str = f"{video_info['width']}x{video_info['height']}"

    prompt = build_storyboard_prompt(
        filename=video_info["filename"],
        duration=duration_str,
        resolution=resolution_str,
    )

    print(f"  Analyzing with {cfg.model}...")
    response = client.models.generate_content(
        model=cfg.model,
        contents=[
            types.Content(
                parts=[
                    types.Part.from_uri(file_uri=video_file.uri, mime_type="video/mp4"),
                    types.Part.from_text(text=prompt),
                ]
            )
        ],
        config=types.GenerateContentConfig(
            temperature=cfg.temperature,
        ),
    )
    return response.text


def run_gemini_analysis(
    proxy_path: Path,
    video_info: dict,
    storyboard_dir: Path,
    cfg: GeminiConfig,
) -> Path:
    """Full Gemini pipeline: upload proxy → analyze → write storyboard."""
    client = get_client()
    video_file = upload_video(client, proxy_path)
    storyboard_md = analyze_video(client, video_file, video_info, cfg)

    storyboard_dir.mkdir(parents=True, exist_ok=True)
    output_path = storyboard_dir / "storyboard_gemini.md"
    output_path.write_text(storyboard_md)
    print(f"  Storyboard written to {output_path}")
    return output_path
