#!/usr/bin/env python3
"""CLI entry point for the Gemini storyboard generation pattern.

Usage:
    python scripts/run_gemini.py <project_name> <video_path>
    python scripts/run_gemini.py test example/test.mp4
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv()

from ai_video_editor.config import DEFAULT_CONFIG
from ai_video_editor.preprocess import create_proxy, get_video_info, ingest_source
from ai_video_editor.gemini_analyze import run_gemini_analysis


def main():
    if len(sys.argv) < 3:
        print("Usage: python scripts/run_gemini.py <project_name> <video_path>")
        print("Example: python scripts/run_gemini.py test example/test.mp4")
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
        print(f"[1/4] Source already ingested: {source.name}")
    else:
        print(f"[1/4] Ingesting into project '{project_name}'...")
        source = ingest_source(video_path, paths)

    # Video info
    print(f"[2/4] Reading video info: {source.name}")
    video_info = get_video_info(source)
    print(f"  Duration: {video_info['duration_sec']:.1f}s, "
          f"Resolution: {video_info['width']}x{video_info['height']}")

    # Proxy
    if cache["proxy"]:
        proxy_path = next(paths.proxy.glob("*.mp4"))
        proxy_size = proxy_path.stat().st_size / 1024 / 1024
        print(f"[3/4] Proxy cached: {proxy_path.name} ({proxy_size:.1f} MB)")
    else:
        print("[3/4] Creating proxy video...")
        proxy_path = create_proxy(source, paths, cfg.preprocess)
        proxy_size = proxy_path.stat().st_size / 1024 / 1024
        print(f"  Proxy: {proxy_path} ({proxy_size:.1f} MB)")

    # Analyze
    print("[4/4] Analyzing with Gemini...")
    output_path = run_gemini_analysis(proxy_path, video_info, paths.storyboard, cfg.gemini)

    print(f"\nDone! Storyboard: {output_path}")
    print(f"Project directory: {paths.root}")


if __name__ == "__main__":
    main()
