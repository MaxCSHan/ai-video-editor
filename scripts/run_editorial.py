#!/usr/bin/env python3
"""CLI entry point for the editorial storyboard agent (multi-clip workflow).

Usage:
    python scripts/run_editorial.py <project_name> <footage_dir> [--provider gemini|claude] [--style vlog]

Examples:
    python scripts/run_editorial.py puma-run ~/footage/puma-run/
    python scripts/run_editorial.py puma-run ~/footage/puma-run/ --provider claude
    python scripts/run_editorial.py my-trip ~/footage/trip/ --style travel-vlog
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv()

from ai_video_editor.editorial_agent import run_editorial_pipeline


def main():
    parser = argparse.ArgumentParser(
        description="Generate an editorial storyboard from a folder of raw video clips."
    )
    parser.add_argument("project_name", help="Name for this project (e.g., puma-run)")
    parser.add_argument("footage_dir", type=Path, help="Directory containing raw video clips")
    parser.add_argument(
        "--provider", choices=["gemini", "claude"], default="gemini",
        help="AI provider for analysis (default: gemini)"
    )
    parser.add_argument(
        "--style", default="vlog",
        help="Video style for editorial guidance (default: vlog)"
    )

    args = parser.parse_args()

    if not args.footage_dir.is_dir():
        print(f"Error: {args.footage_dir} is not a directory")
        sys.exit(1)

    print(f"Editorial Storyboard Agent")
    print(f"  Project:  {args.project_name}")
    print(f"  Source:   {args.footage_dir}")
    print(f"  Provider: {args.provider}")
    print(f"  Style:    {args.style}")
    print()

    output_path = run_editorial_pipeline(
        source_dir=args.footage_dir,
        project_name=args.project_name,
        provider=args.provider,
        style=args.style,
    )

    print(f"\nDone! Editorial storyboard: {output_path}")


if __name__ == "__main__":
    main()
