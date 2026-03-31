#!/usr/bin/env python3
"""CLI entry point for the Claude storyboard generation pattern.

Usage:
    python scripts/run_claude.py <project_name> <video_path>
    python scripts/run_claude.py test example/test.mp4
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv()

from ai_video_editor.config import DEFAULT_CONFIG
from ai_video_editor.preprocess import extract_frames, detect_scenes, get_video_info, ingest_source
from ai_video_editor.claude_analyze import run_claude_analysis


def main():
    if len(sys.argv) < 3:
        print("Usage: python scripts/run_claude.py <project_name> <video_path>")
        print("Example: python scripts/run_claude.py test example/test.mp4")
        sys.exit(1)

    project_name = sys.argv[1]
    video_path = Path(sys.argv[2])
    if not video_path.exists():
        print(f"Error: {video_path} not found")
        sys.exit(1)

    cfg = DEFAULT_CONFIG
    paths = cfg.project(project_name)
    paths.ensure_dirs()

    cache = paths.cache_status()
    cached_steps = [k for k, v in cache.items() if v]

    if cached_steps:
        print(f"Project '{project_name}' has cached artifacts: {', '.join(cached_steps)}")

    # Ingest
    if cache["source"]:
        source = next(paths.source.iterdir())
        print(f"[1/5] Source already ingested: {source.name}")
    else:
        print(f"[1/5] Ingesting into project '{project_name}'...")
        source = ingest_source(video_path, paths)

    # Video info
    print(f"[2/5] Reading video info: {source.name}")
    video_info = get_video_info(source)
    print(f"  Duration: {video_info['duration_sec']:.1f}s, "
          f"Resolution: {video_info['width']}x{video_info['height']}")

    # Frames
    if cache["frames"]:
        frames_dir, manifest = extract_frames(source, paths, cfg.preprocess)
        print(f"[3/5] Frames cached: {len(manifest['frames'])} frames")
    else:
        print("[3/5] Extracting frames...")
        frames_dir, manifest = extract_frames(source, paths, cfg.preprocess)
        print(f"  Extracted {len(manifest['frames'])} frames")

    # Scenes
    if cache["scenes"]:
        scenes = detect_scenes(source, paths, cfg.preprocess)
        print(f"[4/5] Scenes cached: {len(scenes)} scene changes")
    else:
        print("[4/5] Detecting scene changes...")
        scenes = detect_scenes(source, paths, cfg.preprocess)
        print(f"  Found {len(scenes)} scene changes")

    # Analyze
    print("[5/5] Analyzing with Claude...")
    output_path = run_claude_analysis(
        frames_dir, manifest, scenes, video_info, paths.storyboard, cfg.claude
    )

    print(f"\nDone! Storyboard: {output_path}")
    print(f"Project directory: {paths.root}")


if __name__ == "__main__":
    main()
