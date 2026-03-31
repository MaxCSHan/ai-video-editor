#!/usr/bin/env python3
"""
vx — AI Video Editor CLI

A production-studio-style command-line tool for AI-powered video storyboarding.

Commands:
    vx new <name> <source>       Create a new project from footage
    vx projects                  List all projects in the library
    vx status [project]          Show detailed project status
    vx preprocess [project]      Run preprocessing only
    vx transcribe [project]      Transcribe audio (mlx-whisper local or Gemini cloud)
    vx analyze [project]         Run AI analysis and generate storyboard
    vx cut [project]             Assemble rough cut video (no LLM)
    vx config [--key value]      Show or update workspace defaults
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from .config import (
    VIDEO_EXTENSIONS,
    Config,
    DEFAULT_CONFIG,
    LIBRARY_DIR,
)
from .storyboard_format import format_duration


# ---------------------------------------------------------------------------
# Project metadata (project.json in each project root)
# ---------------------------------------------------------------------------


def _project_meta_path(project_root: Path) -> Path:
    return project_root / "project.json"


def _read_project_meta(project_root: Path) -> dict | None:
    p = _project_meta_path(project_root)
    if p.exists():
        return json.loads(p.read_text())
    return None


def _write_project_meta(project_root: Path, meta: dict):
    _project_meta_path(project_root).write_text(json.dumps(meta, indent=2))


def _detect_source_type(source: Path) -> str:
    """Detect if source is a directory of clips (editorial) or a single video (descriptive)."""
    if source.is_dir():
        return "editorial"
    if source.is_file() and source.suffix.lower() in VIDEO_EXTENSIONS:
        return "descriptive"
    raise ValueError(f"Source must be a video file or a directory of clips: {source}")


# ---------------------------------------------------------------------------
# Workspace config (~/.vx.json or .vx.json in cwd)
# ---------------------------------------------------------------------------

WORKSPACE_CONFIG_PATH = Path(".vx.json")


def _read_workspace_config() -> dict:
    defaults = {"provider": "gemini", "style": "vlog"}
    if WORKSPACE_CONFIG_PATH.exists():
        stored = json.loads(WORKSPACE_CONFIG_PATH.read_text())
        defaults.update(stored)
    return defaults


def _write_workspace_config(cfg: dict):
    WORKSPACE_CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RED = "\033[31m"
RESET = "\033[0m"


def _check(ok: bool) -> str:
    return f"{GREEN}+{RESET}" if ok else f"{DIM}-{RESET}"


def _tag(text: str, color: str = CYAN) -> str:
    return f"{color}{text}{RESET}"


def _header(text: str):
    print(f"\n{BOLD}{text}{RESET}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_new(args, cfg: Config):
    """Create a new project from footage."""
    name = args.name
    source = Path(args.source).resolve()

    if not source.exists():
        print(f"{RED}Error:{RESET} Source not found: {source}")
        sys.exit(1)

    project_type = _detect_source_type(source)
    ws = _read_workspace_config()
    provider = args.provider or ws["provider"]
    style = args.style or ws["style"]

    project_root = cfg.library_dir / name
    if project_root.exists() and _read_project_meta(project_root):
        print(
            f"{YELLOW}Project '{name}' already exists.{RESET} Use {BOLD}vx analyze {name}{RESET} to re-run."
        )
        sys.exit(1)

    _header(f"Creating {project_type} project: {name}")
    print(f"  Source:   {source}")
    print(f"  Provider: {provider}")
    if project_type == "editorial":
        print(f"  Style:    {style}")

    # Create project structure
    if project_type == "editorial":
        from .editorial_agent import (
            discover_source_clips,
            preprocess_all_clips,
            build_master_manifest,
        )

        ep = cfg.editorial_project(name)
        ep.ensure_dirs()

        clips = discover_source_clips(source)
        if not clips:
            print(f"\n{RED}Error:{RESET} No video files found in {source}")
            sys.exit(1)

        meta = {
            "name": name,
            "type": "editorial",
            "provider": provider,
            "style": style,
            "source_dir": str(source),
            "clip_count": len(clips),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_project_meta(project_root, meta)

        print(f"\n  Found {BOLD}{len(clips)} clips{RESET}")
        for c in clips:
            print(f"    {DIM}{c.name}{RESET}")

        _header("Preprocessing")
        clip_metadata = preprocess_all_clips(clips, ep, cfg.preprocess)
        manifest = build_master_manifest(clip_metadata, ep, name)
        print(f"\n  Total footage: {BOLD}{manifest['total_duration_fmt']}{RESET}")

    else:  # descriptive
        from .preprocess import ingest_source, get_video_info, run_full_preprocess

        pp = cfg.project(name)
        pp.ensure_dirs()

        meta = {
            "name": name,
            "type": "descriptive",
            "provider": provider,
            "source_file": str(source),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_project_meta(project_root, meta)

        _header("Preprocessing")
        result = run_full_preprocess(source, pp, cfg.preprocess)
        info = result["video_info"]
        print(f"  Duration: {BOLD}{format_duration(info['duration_sec'])}{RESET}")
        print(
            f"  Proxy:    {result['proxy_path']} ({result['proxy_path'].stat().st_size / 1024 / 1024:.1f} MB)"
        )
        print(f"  Frames:   {len(result['frames_manifest']['frames'])}")
        print(f"  Scenes:   {len(result['scenes'])}")

    print(
        f"\n{GREEN}Project created.{RESET} Run {BOLD}vx analyze {name}{RESET} to generate the storyboard."
    )


def cmd_projects(args, cfg: Config):
    """List all projects in the library."""
    if not cfg.library_dir.exists():
        print(f"{DIM}No projects yet. Create one with:{RESET} vx new <name> <source>")
        return

    projects = sorted(
        d for d in cfg.library_dir.iterdir() if d.is_dir() and _project_meta_path(d).exists()
    )

    if not projects:
        print(f"{DIM}No projects yet. Create one with:{RESET} vx new <name> <source>")
        return

    _header("Projects")
    print()

    for p in projects:
        meta = _read_project_meta(p)
        ptype = meta.get("type", "?")
        provider = meta.get("provider", "?")
        created = meta.get("created_at", "")[:10]

        # Check for storyboard output
        storyboard_dir = p / "storyboard"
        has_storyboard = any(storyboard_dir.glob("*.md")) if storyboard_dir.exists() else False

        if ptype == "editorial":
            clip_count = meta.get("clip_count", "?")
            label = f"{clip_count} clips"
        else:
            label = "single video"

        status = f"{GREEN}done{RESET}" if has_storyboard else f"{YELLOW}pending{RESET}"

        print(f"  {BOLD}{meta['name']}{RESET}")
        print(
            f"    {_tag(ptype)} {DIM}|{RESET} {label} {DIM}|{RESET} {provider} {DIM}|{RESET} {status} {DIM}|{RESET} {DIM}{created}{RESET}"
        )
        print()


def cmd_status(args, cfg: Config):
    """Show detailed project status."""
    name = args.project or _infer_project(cfg)
    if not name:
        print(f"{RED}Error:{RESET} Specify a project name or run from a project directory.")
        sys.exit(1)

    project_root = cfg.library_dir / name
    meta = _read_project_meta(project_root)
    if not meta:
        print(f"{RED}Error:{RESET} Project '{name}' not found.")
        sys.exit(1)

    ptype = meta["type"]
    versions = meta.get("versions", {})
    _header(f"Project: {name}")
    print(f"  Type:     {_tag(ptype)}")
    print(f"  Provider: {meta.get('provider', '?')}")
    print(f"  Created:  {meta.get('created_at', '?')[:19]}")
    if versions:
        v_parts = [f"{k}: v{v}" for k, v in versions.items()]
        print(f"  Versions: {', '.join(v_parts)}")

    if ptype == "editorial":
        print(f"  Style:    {meta.get('style', '?')}")
        ep = cfg.editorial_project(name)
        clips = ep.discover_clips()

        _header(f"Clips ({len(clips)})")
        for clip_id in clips:
            cp = ep.clip_paths(clip_id)
            cache = cp.cache_status()
            provider = meta.get("provider", "gemini")
            reviewed = cp.has_review(provider)

            has_transcript = cp.has_transcript()
            status_parts = [
                f"proxy:{_check(cache['proxy'])}",
                f"frames:{_check(cache['frames'])}",
                f"scenes:{_check(cache['scenes'])}",
                f"audio:{_check(cache['audio'])}",
                f"transcript:{_check(has_transcript)}",
                f"review:{_check(reviewed)}",
            ]
            print(f"  {clip_id}  {' '.join(status_parts)}")

        # Storyboard status
        _header("Storyboard")
        storyboard_dir = ep.storyboard
        md_files = (
            sorted(storyboard_dir.glob("editorial_*_v*.md")) if storyboard_dir.exists() else []
        )
        if not md_files:
            md_files = (
                list(storyboard_dir.glob("editorial_*.md")) if storyboard_dir.exists() else []
            )
        if md_files:
            for f in md_files:
                if f.is_symlink():
                    continue
                size = f.stat().st_size
                print(f"  {GREEN}{f.name}{RESET}  ({size / 1024:.0f} KB)")
        else:
            print(f"  {DIM}Not generated yet. Run:{RESET} vx analyze {name}")

        # Exports/cuts status
        exports_dir = ep.exports
        if exports_dir.exists():
            cut_dirs = sorted(
                d for d in exports_dir.iterdir() if d.is_dir() and d.name.startswith("v")
            )
            if cut_dirs:
                _header("Exports")
                for d in cut_dirs:
                    has_video = (d / "rough_cut.mp4").exists()
                    has_preview = (d / "preview.html").exists()
                    if has_video and has_preview:
                        status = f"{GREEN}video+preview{RESET}"
                    elif has_preview:
                        status = f"{CYAN}preview{RESET}"
                    else:
                        status = f"{DIM}empty{RESET}"
                    print(f"  {d.name}: {status}")

    else:  # descriptive
        pp = cfg.project(name)
        cache = pp.cache_status()
        _header("Preprocessing")
        for key, ok in cache.items():
            print(f"  {key}: {_check(ok)}")

        _header("Storyboard")
        md_files = list(pp.storyboard.glob("storyboard_*.md")) if pp.storyboard.exists() else []
        if md_files:
            for f in md_files:
                size = f.stat().st_size
                print(f"  {GREEN}{f.name}{RESET}  ({size / 1024:.0f} KB)")
        else:
            print(f"  {DIM}Not generated yet. Run:{RESET} vx analyze {name}")


def cmd_preprocess(args, cfg: Config):
    """Run preprocessing only (no AI analysis)."""
    name = args.project or _infer_project(cfg)
    if not name:
        print(f"{RED}Error:{RESET} Specify a project name.")
        sys.exit(1)

    project_root = cfg.library_dir / name
    meta = _read_project_meta(project_root)
    if not meta:
        print(
            f"{RED}Error:{RESET} Project '{name}' not found. Create it with: vx new {name} <source>"
        )
        sys.exit(1)

    _header(f"Preprocessing: {name}")

    if meta["type"] == "editorial":
        from .editorial_agent import (
            discover_source_clips,
            preprocess_all_clips,
            build_master_manifest,
        )

        ep = cfg.editorial_project(name)
        source_dir = Path(meta["source_dir"])
        clips = discover_source_clips(source_dir)
        clip_metadata = preprocess_all_clips(clips, ep, cfg.preprocess)
        manifest = build_master_manifest(clip_metadata, ep, name)
        print(f"\n  {GREEN}Done.{RESET} {len(clips)} clips, {manifest['total_duration_fmt']} total")
    else:
        from .preprocess import run_full_preprocess

        pp = cfg.project(name)
        source_file = Path(meta["source_file"])
        run_full_preprocess(source_file, pp, cfg.preprocess)
        print(f"\n  {GREEN}Done.{RESET}")


def cmd_transcribe(args, cfg: Config):
    """Run audio transcription on all clips (mlx-whisper or Gemini)."""
    name = args.project or _infer_project(cfg)
    if not name:
        print(f"{RED}Error:{RESET} Specify a project name.")
        sys.exit(1)

    project_root = cfg.library_dir / name
    meta = _read_project_meta(project_root)
    if not meta:
        print(f"{RED}Error:{RESET} Project '{name}' not found.")
        sys.exit(1)

    if meta["type"] != "editorial":
        print(f"{RED}Error:{RESET} 'vx transcribe' is only for editorial projects.")
        sys.exit(1)

    from .editorial_agent import _resolve_transcribe_provider, transcribe_all_clips

    # Override provider from CLI flag if specified
    if args.provider:
        cfg.transcribe.provider = args.provider

    provider = _resolve_transcribe_provider(cfg.transcribe)
    if not provider:
        print(
            f"{RED}Error:{RESET} No transcription provider available.\n"
            f"  Install mlx-whisper: uv pip install -e '.[whisper]'\n"
            f"  Or set GEMINI_API_KEY for cloud transcription."
        )
        sys.exit(1)

    ep = cfg.editorial_project(name)
    clips = ep.discover_clips()
    if not clips:
        print(f"{RED}Error:{RESET} No clips found in project '{name}'.")
        sys.exit(1)

    # Load speaker hints from briefing context if available
    speaker_hints = None
    context_path = project_root / "user_context.json"
    if context_path.exists():
        import json as _json

        ctx = _json.loads(context_path.read_text())
        people = ctx.get("people", "")
        if people:
            speaker_hints = [p.strip() for p in people.split(",") if p.strip()]

    _header(f"Transcribing: {name} ({len(clips)} clips, {provider})")

    transcripts = transcribe_all_clips(
        [{"clip_id": cid} for cid in clips],
        ep,
        cfg.transcribe,
        provider=provider,
        speaker_hints=speaker_hints,
    )

    count = len(transcripts)
    print(f"\n  {GREEN}Done.{RESET} {count}/{len(clips)} clips have speech")

    if getattr(args, "srt", False) and transcripts:
        from .transcribe import generate_srt

        print("\n  Generating SRT subtitles...")
        for clip_id, transcript in transcripts.items():
            clip_paths = ep.clip_paths(clip_id)
            srt_path = clip_paths.audio / f"{clip_id}.srt"
            generate_srt(transcript, srt_path)
            print(f"    {clip_id}: {srt_path.name}")
        print(f"  {GREEN}SRT files generated.{RESET}")


def cmd_analyze(args, cfg: Config):
    """Run AI analysis and generate storyboard."""
    name = args.project or _infer_project(cfg)
    if not name:
        print(f"{RED}Error:{RESET} Specify a project name.")
        sys.exit(1)

    project_root = cfg.library_dir / name
    meta = _read_project_meta(project_root)
    if not meta:
        print(f"{RED}Error:{RESET} Project '{name}' not found.")
        sys.exit(1)

    ws = _read_workspace_config()
    provider = args.provider or meta.get("provider") or ws["provider"]

    _header(f"Analyzing: {name} ({provider})")

    if meta["type"] == "editorial":
        from .editorial_agent import run_editorial_pipeline

        source_dir = Path(meta["source_dir"])
        style = meta.get("style", ws.get("style", "vlog"))

        force = getattr(args, "force", False)
        interactive = not getattr(args, "no_interactive", False)
        output_path = run_editorial_pipeline(
            source_dir=source_dir,
            project_name=name,
            provider=provider,
            style=style,
            cfg=cfg,
            force=force,
            interactive=interactive,
        )
    else:
        # Descriptive pipeline
        pp = cfg.project(name)
        source_files = list(pp.source.glob("*"))
        if not source_files:
            print(f"{RED}Error:{RESET} No source file found. Re-run: vx new {name} <video>")
            sys.exit(1)
        source = source_files[0]

        from .preprocess import create_proxy, extract_frames, detect_scenes, get_video_info

        video_info = get_video_info(source)
        cache = pp.cache_status()

        if provider == "gemini":
            from .gemini_analyze import run_gemini_analysis

            proxy_path = create_proxy(source, pp, cfg.preprocess)
            output_path = run_gemini_analysis(proxy_path, video_info, pp.storyboard, cfg.gemini)
        elif provider == "claude":
            from .claude_analyze import run_claude_analysis

            frames_dir, manifest = extract_frames(source, pp, cfg.preprocess)
            scenes = detect_scenes(source, pp, cfg.preprocess)
            output_path = run_claude_analysis(
                frames_dir, manifest, scenes, video_info, pp.storyboard, cfg.claude
            )
        else:
            print(f"{RED}Error:{RESET} Unknown provider: {provider}")
            sys.exit(1)

    print(f"\n{GREEN}Storyboard ready:{RESET} {BOLD}{output_path}{RESET}")


def cmd_brief(args, cfg: Config):
    """Edit the editorial briefing — opens $EDITOR with a pre-filled template."""
    name = args.project or _infer_project(cfg)
    if not name:
        print(f"{RED}Error:{RESET} Specify a project name.")
        sys.exit(1)

    project_root = cfg.library_dir / name
    meta = _read_project_meta(project_root)
    if not meta or meta["type"] != "editorial":
        print(f"{RED}Error:{RESET} Project '{name}' not found or not editorial.")
        sys.exit(1)

    ep = cfg.editorial_project(name)
    context_path = project_root / "user_context.json"

    # Load Phase 1 reviews for smart template generation
    reviews = []
    for clip_id in ep.discover_clips():
        cp = ep.clip_paths(clip_id)
        for pattern in ["review_*_latest.json", "review_*.json"]:
            found = list(cp.review.glob(pattern))
            if found:
                f = next((x for x in found if not x.is_symlink()), found[0])
                reviews.append(json.loads(f.read_text()))
                break

    ws = _read_workspace_config()
    style = meta.get("style", ws.get("style", "vlog"))

    from .briefing import generate_template, parse_template, open_in_editor

    # If context exists, pre-fill the template with existing answers
    template = generate_template(reviews, style)
    if context_path.exists():
        existing = json.loads(context_path.read_text())
        # Inject existing answers into template
        for key, value in existing.items():
            template = template.replace(f"\n{key}:\n", f"\n{key}: {value}\n")

    _header(f"Editorial Briefing: {name}")
    print(f"  Opening in $EDITOR ({os.environ.get('EDITOR', 'vim')})...")
    print(f"  {DIM}Fill in what you can, save and close.{RESET}\n")

    edited = open_in_editor(template)
    if not edited:
        print(f"  {DIM}No changes.{RESET}")
        return

    answers = parse_template(edited)
    if answers:
        context_path.write_text(json.dumps(answers, indent=2, ensure_ascii=False))
        print(f"  {GREEN}Context saved ({len(answers)} fields):{RESET}")
        for k, v in answers.items():
            print(f"    {CYAN}{k}{RESET}: {v[:80]}{'...' if len(v) > 80 else ''}")
        print(
            f"\n  Now run {BOLD}vx analyze {name}{RESET} to generate the storyboard with this context."
        )
    else:
        print(f"  {DIM}No answers provided.{RESET}")


def _find_storyboard_json(ep) -> Path | None:
    """Find the latest structured storyboard JSON for an editorial project."""
    storyboard_dir = ep.storyboard
    candidates = [
        storyboard_dir / "editorial_gemini_latest.json",
        storyboard_dir / "editorial_claude_latest.json",
    ]
    if storyboard_dir.exists():
        candidates.extend(sorted(storyboard_dir.glob("editorial_*_v*.json"), reverse=True))
    for c in candidates:
        if c.exists():
            return c
    return None


def cmd_preview(args, cfg: Config):
    """Regenerate HTML preview from structured storyboard (no LLM, no ffmpeg)."""
    name = args.project or _infer_project(cfg)
    if not name:
        print(f"{RED}Error:{RESET} Specify a project name.")
        sys.exit(1)

    project_root = cfg.library_dir / name
    meta = _read_project_meta(project_root)
    if not meta:
        print(f"{RED}Error:{RESET} Project '{name}' not found.")
        sys.exit(1)

    if meta["type"] != "editorial":
        print(f"{RED}Error:{RESET} 'vx preview' is only for editorial projects.")
        sys.exit(1)

    ep = cfg.editorial_project(name)

    json_path = _find_storyboard_json(ep)
    if not json_path:
        print(
            f"{RED}Error:{RESET} No structured storyboard JSON found. Run {BOLD}vx analyze {name}{RESET} first."
        )
        sys.exit(1)

    _header(f"Preview: {name}")
    print(f"  Storyboard: {json_path.name}")
    print()

    from .models import EditorialStoryboard
    from .render import render_html_preview
    from .versioning import next_version, versioned_dir, update_latest_symlink

    sb = EditorialStoryboard.model_validate_json(json_path.read_text())
    v = next_version(ep.root, "preview")
    vdir = versioned_dir(ep.exports, v)

    # Find existing rough cut to embed (use latest if available)
    rough_cut_path = None
    latest_export = ep.exports / "latest"
    if latest_export.exists():
        rc = latest_export / "rough_cut.mp4"
        if rc.exists():
            rough_cut_path = rc.resolve()

    html = render_html_preview(
        sb,
        clips_dir=ep.clips_dir,
        output_dir=vdir,
        rough_cut_path=rough_cut_path,
    )
    preview_path = vdir / "preview.html"
    preview_path.write_text(html)
    update_latest_symlink(vdir)

    print(f"  {BOLD}Version:{RESET}    v{v}")
    print(f"  {GREEN}Preview:{RESET}    {preview_path}")


def cmd_cut(args, cfg: Config):
    """Assemble rough cut video from structured storyboard (no LLM needed)."""
    name = args.project or _infer_project(cfg)
    if not name:
        print(f"{RED}Error:{RESET} Specify a project name.")
        sys.exit(1)

    project_root = cfg.library_dir / name
    meta = _read_project_meta(project_root)
    if not meta:
        print(f"{RED}Error:{RESET} Project '{name}' not found.")
        sys.exit(1)

    if meta["type"] != "editorial":
        print(f"{RED}Error:{RESET} 'vx cut' is only for editorial projects.")
        sys.exit(1)

    ep = cfg.editorial_project(name)

    json_path = _find_storyboard_json(ep)
    if not json_path:
        print(
            f"{RED}Error:{RESET} No structured storyboard JSON found. Run {BOLD}vx analyze {name}{RESET} first."
        )
        sys.exit(1)

    _header(f"Rough Cut: {name}")
    print(f"  Storyboard: {json_path.name}")
    print()

    from .rough_cut import run_rough_cut

    result = run_rough_cut(
        storyboard_json_path=json_path,
        editorial_paths=ep,
    )

    v = result.get("version", "?")
    warn_count = len(result.get("warnings", []))
    print()
    print(f"  {BOLD}Version:{RESET}    v{v}")
    if "rough_cut" in result:
        size_mb = result["rough_cut"].stat().st_size / 1024 / 1024
        print(f"  {GREEN}Rough cut:{RESET}  {result['rough_cut']} ({size_mb:.1f} MB)")
    print(f"  {GREEN}Preview:{RESET}    {result['preview']}")
    if warn_count:
        print(f"  {YELLOW}Warnings:{RESET}   {warn_count} issue(s) — see preview for details")
    print(f"\n  Open preview: {DIM}open {result['preview']}{RESET}")


def cmd_config(args, cfg: Config):
    """Show or update workspace defaults."""
    ws = _read_workspace_config()

    # Check for updates
    changed = False
    if args.provider:
        ws["provider"] = args.provider
        changed = True
    if args.style:
        ws["style"] = args.style
        changed = True

    if changed:
        _write_workspace_config(ws)
        print(f"{GREEN}Config updated.{RESET}")

    _header("Workspace Config")
    print(f"  Provider:  {BOLD}{ws['provider']}{RESET}")
    print(f"  Style:     {BOLD}{ws['style']}{RESET}")
    print(f"  Library:   {cfg.library_dir.resolve()}")

    # Show API key status
    _header("API Keys")
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    print(
        f"  GEMINI_API_KEY:    {GREEN}set{RESET}"
        if gemini_key
        else f"  GEMINI_API_KEY:    {RED}not set{RESET}"
    )
    print(
        f"  ANTHROPIC_API_KEY: {GREEN}set{RESET}"
        if anthropic_key
        else f"  ANTHROPIC_API_KEY: {RED}not set{RESET}"
    )

    # Show preprocessing defaults
    _header("Preprocessing")
    pc = cfg.preprocess
    print(
        f"  Proxy:     {pc.proxy_width}x{pc.proxy_height} @ {pc.proxy_fps}fps, CRF {pc.proxy_crf}"
    )
    print(f"  Frames:    every {pc.frame_interval_sec}s @ {pc.frame_width}x{pc.frame_height}")
    print(f"  Scene:     threshold {pc.scene_threshold}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _infer_project(cfg: Config) -> str | None:
    """Try to infer project name from the most recently modified project."""
    if not cfg.library_dir.exists():
        return None
    projects = [
        d for d in cfg.library_dir.iterdir() if d.is_dir() and _project_meta_path(d).exists()
    ]
    if len(projects) == 1:
        return projects[0].name
    return None


# ---------------------------------------------------------------------------
# Main CLI parser
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        prog="vx",
        description="AI Video Editor — production-grade storyboard generation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
{DIM}Examples:{RESET}
  vx new puma-run ~/footage/puma/     Create editorial project from clip folder
  vx new recap video.mp4              Create descriptive project from single video
  vx projects                         List all projects
  vx status puma-run                  Show project status
  vx transcribe puma-run               Transcribe audio (auto-detect provider)
  vx transcribe puma-run --provider gemini   Use Gemini for richer transcripts (speakers, sounds)
  vx transcribe puma-run --srt        Also generate SRT subtitle files
  vx analyze puma-run                 Generate storyboard
  vx analyze puma-run --provider claude
  vx preview puma-run                 Regenerate HTML preview (no LLM, no ffmpeg)
  vx cut puma-run                     Assemble rough cut from structured storyboard (no LLM)
  vx config --provider gemini         Set default provider
""",
    )

    sub = parser.add_subparsers(dest="command", metavar="command")

    # --- new ---
    p_new = sub.add_parser("new", help="Create a new project from footage")
    p_new.add_argument("name", help="Project name (e.g., puma-run, tokyo-trip)")
    p_new.add_argument("source", help="Footage directory (editorial) or video file (descriptive)")
    p_new.add_argument("--provider", choices=["gemini", "claude"], help="AI provider")
    p_new.add_argument("--style", help="Video style for editorial (default: vlog)")

    # --- projects ---
    sub.add_parser("projects", aliases=["ls"], help="List all projects")

    # --- status ---
    p_status = sub.add_parser("status", help="Show detailed project status")
    p_status.add_argument("project", nargs="?", help="Project name (auto-detected if only one)")

    # --- preprocess ---
    p_prep = sub.add_parser("preprocess", aliases=["prep"], help="Run preprocessing only")
    p_prep.add_argument("project", nargs="?", help="Project name")

    # --- transcribe ---
    p_transcribe = sub.add_parser(
        "transcribe", help="Transcribe audio (mlx-whisper local or Gemini cloud)"
    )
    p_transcribe.add_argument("project", nargs="?", help="Project name")
    p_transcribe.add_argument(
        "--provider",
        choices=["mlx", "gemini"],
        help="Transcription provider (default: auto-detect)",
    )
    p_transcribe.add_argument("--srt", action="store_true", help="Also generate SRT subtitle files")

    # --- analyze ---
    p_analyze = sub.add_parser("analyze", aliases=["run"], help="Run AI analysis")
    p_analyze.add_argument("project", nargs="?", help="Project name")
    p_analyze.add_argument("--provider", choices=["gemini", "claude"], help="Override AI provider")
    p_analyze.add_argument(
        "--force", action="store_true", help="Re-run Phase 1 reviews (ignore cache)"
    )
    p_analyze.add_argument(
        "--no-interactive", action="store_true", help="Skip the editorial briefing questions"
    )

    # --- brief ---
    p_brief = sub.add_parser("brief", help="Edit the editorial briefing (opens $EDITOR)")
    p_brief.add_argument("project", nargs="?", help="Project name")

    # --- preview ---
    p_preview = sub.add_parser("preview", help="Regenerate HTML preview (no LLM, no ffmpeg)")
    p_preview.add_argument("project", nargs="?", help="Project name")

    # --- cut ---
    p_cut = sub.add_parser(
        "cut", help="Assemble rough cut video (no LLM — uses structured JSON from analyze)"
    )
    p_cut.add_argument("project", nargs="?", help="Project name")

    # --- config ---
    p_config = sub.add_parser("config", help="Show or update workspace defaults")
    p_config.add_argument("--provider", choices=["gemini", "claude"], help="Default AI provider")
    p_config.add_argument("--style", help="Default video style")

    args = parser.parse_args()
    cfg = DEFAULT_CONFIG

    if not args.command:
        # No subcommand → launch interactive mode
        from .interactive import run_interactive

        run_interactive()
        sys.exit(0)

    commands = {
        "new": cmd_new,
        "projects": cmd_projects,
        "ls": cmd_projects,
        "status": cmd_status,
        "preprocess": cmd_preprocess,
        "prep": cmd_preprocess,
        "transcribe": cmd_transcribe,
        "analyze": cmd_analyze,
        "run": cmd_analyze,
        "brief": cmd_brief,
        "preview": cmd_preview,
        "cut": cmd_cut,
        "config": cmd_config,
    }

    handler = commands.get(args.command)
    if handler:
        handler(args, cfg)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
