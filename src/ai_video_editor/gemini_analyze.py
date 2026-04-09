"""Gemini descriptive-mode adapter — upload proxy video and analyze with
native video understanding.

Demonstrates the adapter pattern:
- Receives a GeminiClient (injected, not created internally)
- Implements provider-specific logic only
- Returns domain objects or raises domain exceptions
- Minimal direct google.genai imports (types only, lazy)
"""

from pathlib import Path

from .config import GeminiConfig
from .infra.gemini_client import GeminiClient
from .storyboard_format import build_storyboard_prompt, format_duration


def upload_video(client: GeminiClient, video_path: Path):
    """Upload video to Gemini File API and wait for processing."""
    print(f"  Uploading {video_path.name} ({video_path.stat().st_size / 1024 / 1024:.1f} MB)...")
    video_file = client.upload_and_wait(video_path, label=video_path.name)
    print("  Video processed successfully.")
    return video_file


def analyze_video(
    client: GeminiClient,
    video_file,
    video_info: dict,
    cfg: GeminiConfig,
) -> str:
    """Send video + storyboard prompt to Gemini and return the markdown response."""
    from google.genai import types

    duration_str = format_duration(video_info["duration_sec"])
    resolution_str = f"{video_info['width']}x{video_info['height']}"

    prompt = build_storyboard_prompt(
        filename=video_info["filename"],
        duration=duration_str,
        resolution=resolution_str,
    )

    print(f"  Analyzing with {cfg.model}...")
    response = client.generate(
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
    client: GeminiClient | None = None,
) -> Path:
    """Full Gemini pipeline: upload proxy → analyze → write storyboard.

    Args:
        client: Injected GeminiClient. Created from env if not provided
                (backward compatibility during migration).
    """
    if client is None:
        client = GeminiClient.from_env()

    video_file = upload_video(client, proxy_path)
    storyboard_md = analyze_video(client, video_file, video_info, cfg)

    storyboard_dir.mkdir(parents=True, exist_ok=True)
    output_path = storyboard_dir / "storyboard_gemini.md"
    output_path.write_text(storyboard_md)
    print(f"  Storyboard written to {output_path}")
    return output_path
