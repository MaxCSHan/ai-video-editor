"""Interactive TUI mode — guided video production workflow."""

import json
import os
import subprocess
import sys
from pathlib import Path

import questionary
from questionary import Style

from .config import DEFAULT_CONFIG, LIBRARY_DIR

VX_STYLE = Style(
    [
        ("qmark", "fg:#2ecc71 bold"),
        ("question", "fg:#ffffff bold"),
        ("answer", "fg:#2ecc71"),
        ("pointer", "fg:#2ecc71 bold"),
        ("highlighted", "fg:#2ecc71 bold"),
        ("selected", "fg:#2ecc71"),
        ("instruction", "fg:#666666"),
        ("text", "fg:#aaaaaa"),
    ]
)

BANNER = """
\033[1m  VX — AI Video Editor\033[0m
\033[2m  Turn raw footage into polished vlogs with AI\033[0m
"""


def run_interactive():
    """Main interactive loop."""
    from dotenv import load_dotenv

    load_dotenv()

    print(BANNER)
    cfg = DEFAULT_CONFIG

    while True:
        action = questionary.select(
            "What would you like to do?",
            choices=[
                "New project",
                "Open existing project",
                "Settings",
                questionary.Choice("Quit", value="quit"),
            ],
            style=VX_STYLE,
        ).ask()

        if action is None or action == "quit":
            print("\n  Bye!\n")
            break
        elif action == "New project":
            _new_project_flow(cfg)
        elif action == "Open existing project":
            _open_project_flow(cfg)
        elif action == "Settings":
            _settings_flow(cfg)


def _new_project_flow(cfg):
    """Guided flow: create project → preprocess → brief → analyze."""
    print()
    name = questionary.text(
        "Project name:",
        instruction="(e.g., family-trip-hsinchu, puma-run)",
        style=VX_STYLE,
    ).ask()
    if not name:
        return

    source = questionary.path(
        "Footage folder (directory containing your raw video clips):",
        only_directories=True,
        style=VX_STYLE,
    ).ask()
    if not source:
        return
    source_path = Path(source.strip().strip("'\"")).expanduser().resolve()

    if not source_path.is_dir():
        print(f"\n  Error: {source_path} is not a directory\n")
        return

    style = questionary.select(
        "Video style:",
        choices=["vlog", "travel-vlog", "family-video", "event-recap", "cinematic", "short-form"],
        style=VX_STYLE,
    ).ask()
    if not style:
        return

    # Read workspace config for provider
    ws_path = Path(".vx.json")
    ws = json.loads(ws_path.read_text()) if ws_path.exists() else {}
    provider = ws.get("provider", "gemini")

    # Check API key
    key_var = "GEMINI_API_KEY" if provider == "gemini" else "ANTHROPIC_API_KEY"
    if not os.environ.get(key_var):
        print(f"\n  Warning: {key_var} not set. Check your .env file.\n")
        if not questionary.confirm("Continue anyway?", default=False, style=VX_STYLE).ask():
            return

    print(f"\n  Creating project: {name}")
    print(f"  Source: {source_path}")
    print(f"  Style: {style}, Provider: {provider}\n")

    from .editorial_agent import (
        discover_source_clips,
        preprocess_all_clips,
        build_master_manifest,
        run_phase1_gemini,
        run_phase1_claude,
        run_phase2,
    )

    ep = cfg.editorial_project(name)
    ep.ensure_dirs()

    # Save project metadata
    from datetime import datetime, timezone

    meta = {
        "name": name,
        "type": "editorial",
        "provider": provider,
        "style": style,
        "source_dir": str(source_path),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (ep.root / "project.json").write_text(json.dumps(meta, indent=2))

    # Discover
    clips = discover_source_clips(source_path)
    if not clips:
        print(f"  No video files found in {source_path}\n")
        return

    print(f"  Found {len(clips)} clips")
    meta["clip_count"] = len(clips)
    (ep.root / "project.json").write_text(json.dumps(meta, indent=2))

    # Preprocess
    print(f"  Preprocessing {len(clips)} clips...\n")
    clip_metadata = preprocess_all_clips(clips, ep, cfg.preprocess)
    manifest = build_master_manifest(clip_metadata, ep, name)
    print(f"\n  Total footage: {manifest['total_duration_fmt']}")

    # Transcription
    _run_transcription(ep, clip_metadata, cfg)

    # Phase 1
    if not questionary.confirm("Run Phase 1 clip reviews?", default=True, style=VX_STYLE).ask():
        print("\n  Skipped. Run 'vx analyze' later.\n")
        return

    print(f"\n  Phase 1: Reviewing clips with {provider}...\n")
    if provider == "gemini":
        reviews = run_phase1_gemini(ep, manifest, cfg.gemini)
    else:
        reviews = run_phase1_claude(ep, manifest, cfg.claude)
    print(f"\n  Reviewed {len(reviews)} clips")

    # Briefing
    from .briefing import run_briefing

    user_context = run_briefing(reviews, style, ep.root)

    # Phase 2
    if not questionary.confirm(
        "Generate editorial storyboard?", default=True, style=VX_STYLE
    ).ask():
        print("\n  Context saved. Run 'vx analyze' later.\n")
        return

    print(f"\n  Phase 2: Generating storyboard...\n")
    output = run_phase2(
        clip_reviews=reviews,
        editorial_paths=ep,
        project_name=name,
        provider=provider,
        gemini_cfg=cfg.gemini,
        claude_cfg=cfg.claude,
        style=style,
        user_context=user_context,
    )

    print(f"\n  Storyboard ready!")
    _project_actions(name, cfg)


def _open_project_flow(cfg):
    """Open an existing project and show actions."""
    if not cfg.library_dir.exists():
        print("\n  No projects yet.\n")
        return

    projects = sorted(
        d.name for d in cfg.library_dir.iterdir() if d.is_dir() and (d / "project.json").exists()
    )
    if not projects:
        print("\n  No projects yet.\n")
        return

    name = questionary.select(
        "Select project:",
        choices=projects + [questionary.Choice("← Back", value="")],
        style=VX_STYLE,
    ).ask()
    if not name:
        return

    _project_actions(name, cfg)


def _project_actions(name, cfg):
    """Show actions for an open project."""
    meta_path = cfg.library_dir / name / "project.json"
    meta = json.loads(meta_path.read_text())
    ep = cfg.editorial_project(name)

    while True:
        # Check state
        has_storyboard = (
            any(ep.storyboard.glob("editorial_*_latest.json")) if ep.storyboard.exists() else False
        )
        has_preview = any(ep.exports.glob("*/preview.html")) if ep.exports.exists() else False
        has_rough_cut = any(ep.exports.glob("*/rough_cut.mp4")) if ep.exports.exists() else False

        choices = []
        if has_preview:
            choices.append("Open preview in browser")
        if has_storyboard:
            choices.append("Regenerate preview")
            choices.append("Assemble rough cut")
        choices.append("Transcribe audio")
        choices.append("Run analysis (Phase 1 + 2)")
        choices.append("Edit briefing")
        choices.append("Show status")
        if has_rough_cut:
            choices.append("Open rough cut video")
        choices.append(questionary.Choice("← Back", value="back"))

        print(f"\n  Project: {name}")
        action = questionary.select(
            "Action:",
            choices=choices,
            style=VX_STYLE,
        ).ask()

        if action is None or action == "back":
            break
        elif action == "Open preview in browser":
            # Find latest preview in exports/
            latest_export = ep.exports / "latest"
            if latest_export.exists():
                preview = latest_export / "preview.html"
                if preview.exists():
                    subprocess.run(["open", str(preview)])
        elif action == "Open rough cut video":
            cuts = sorted(ep.exports.glob("*/rough_cut.mp4"), reverse=True)
            if cuts:
                subprocess.run(["open", str(cuts[0])])
        elif action == "Regenerate preview":
            from .models import EditorialStoryboard
            from .render import render_html_preview
            from .versioning import next_version, versioned_dir, update_latest_symlink

            json_files = sorted(ep.storyboard.glob("editorial_*_latest.json"))
            if json_files:
                sb = EditorialStoryboard.model_validate_json(json_files[0].read_text())
                v = next_version(ep.root, "preview")
                vdir = versioned_dir(ep.exports, v)
                # Embed existing rough cut if available
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
                print(f"\n  Preview v{v} generated: {preview_path}")
                if questionary.confirm("Open preview?", default=True, style=VX_STYLE).ask():
                    subprocess.run(["open", str(preview_path)])
        elif action == "Assemble rough cut":
            from .rough_cut import run_rough_cut

            json_files = sorted(ep.storyboard.glob("editorial_*_latest.json"))
            if json_files:
                print(f"\n  Assembling rough cut...\n")
                result = run_rough_cut(json_files[0], ep)
                print(f"\n  Done! v{result['version']}")
                if questionary.confirm("Open preview?", default=True, style=VX_STYLE).ask():
                    subprocess.run(["open", str(result["preview"])])
        elif action == "Transcribe audio":
            _run_transcription_interactive(name, cfg)
        elif action == "Run analysis (Phase 1 + 2)":
            _run_analyze(name, meta, cfg)
        elif action == "Edit briefing":
            reviews = _load_reviews(ep)
            style = meta.get("style", "vlog")
            from .briefing import run_briefing

            # Delete existing context to force fresh questions
            ctx_path = ep.root / "user_context.json"
            if ctx_path.exists():
                ctx_path.unlink()
            run_briefing(reviews, style, ep.root)
        elif action == "Show status":
            _show_status(name, meta, cfg)


def _run_analyze(name, meta, cfg):
    """Run the full analysis pipeline."""
    from .editorial_agent import (
        discover_source_clips,
        preprocess_all_clips,
        build_master_manifest,
        run_phase1_gemini,
        run_phase1_claude,
        run_phase2,
    )
    from .briefing import run_briefing

    ep = cfg.editorial_project(name)
    provider = meta.get("provider", "gemini")
    style = meta.get("style", "vlog")
    source_dir = Path(meta["source_dir"])

    clips = discover_source_clips(source_dir)
    print(f"\n  {len(clips)} clips, preprocessing...\n")
    clip_metadata = preprocess_all_clips(clips, ep, cfg.preprocess)
    manifest = build_master_manifest(clip_metadata, ep, name)

    # Transcription
    _run_transcription(ep, clip_metadata, cfg)

    print(f"\n  Phase 1: Reviewing clips...\n")
    if provider == "gemini":
        reviews = run_phase1_gemini(ep, manifest, cfg.gemini)
    else:
        reviews = run_phase1_claude(ep, manifest, cfg.claude)

    user_context = run_briefing(reviews, style, ep.root)

    print(f"\n  Phase 2: Generating storyboard...\n")
    run_phase2(
        clip_reviews=reviews,
        editorial_paths=ep,
        project_name=name,
        provider=provider,
        gemini_cfg=cfg.gemini,
        claude_cfg=cfg.claude,
        style=style,
        user_context=user_context,
    )
    print(f"\n  Storyboard ready!")


def _load_reviews(ep):
    """Load all Phase 1 reviews for a project."""
    reviews = []
    for clip_id in ep.discover_clips():
        cp = ep.clip_paths(clip_id)
        for pattern in ["review_*_latest.json", "review_*.json"]:
            found = [
                f
                for f in cp.review.glob(pattern)
                if not f.name.endswith("_latest.json") or f.is_symlink()
            ]
            if found:
                reviews.append(json.loads(found[0].read_text()))
                break
    return reviews


def _run_transcription(ep, clip_metadata, cfg):
    """Run transcription as part of a pipeline flow (non-interactive provider selection)."""
    from .editorial_agent import _resolve_transcribe_provider, transcribe_all_clips

    t_provider = _resolve_transcribe_provider(cfg.transcribe)
    if not t_provider:
        print("\n  Skipping transcription (no provider available)")
        return

    # Load speaker context from briefing if available
    speaker_context = None
    context_path = ep.root / "user_context.json"
    if context_path.exists():
        ctx = json.loads(context_path.read_text())
        speaker_context = ctx.get("people", "") or None

    print(f"\n  Transcribing audio ({t_provider})...\n")
    transcripts = transcribe_all_clips(
        clip_metadata, ep, cfg.transcribe, provider=t_provider, speaker_context=speaker_context
    )
    count = len(transcripts)
    print(f"\n  Transcribed {count}/{len(clip_metadata)} clips with speech")


def _run_transcription_interactive(name, cfg):
    """Run transcription from project actions menu with provider choice."""
    from .editorial_agent import (
        _resolve_transcribe_provider,
        discover_source_clips,
        preprocess_all_clips,
        transcribe_all_clips,
    )

    ep = cfg.editorial_project(name)
    meta = json.loads((ep.root / "project.json").read_text())

    # Let user pick provider
    available = []
    try:
        import mlx_whisper  # noqa: F401

        available.append("mlx (local, fast, no API cost)")
    except ImportError:
        pass
    if os.environ.get("GEMINI_API_KEY"):
        available.append("gemini (cloud, speakers + sound events)")
    if not available:
        print("\n  No transcription provider available.")
        print("  Install mlx-whisper or set GEMINI_API_KEY.\n")
        return

    if len(available) == 1:
        t_provider = available[0].split(" ")[0]
    else:
        choice = questionary.select(
            "Transcription provider:",
            choices=available,
            style=VX_STYLE,
        ).ask()
        if not choice:
            return
        t_provider = choice.split(" ")[0]

    # Build clip metadata from existing clips
    clips = ep.discover_clips()
    if not clips:
        print("\n  No clips found.\n")
        return
    clip_metadata = [{"clip_id": cid} for cid in clips]

    # Check for existing transcripts and offer overwrite
    cached = [cid for cid in clips if ep.clip_paths(cid).has_transcript()]
    if cached:
        print(f"\n  {len(cached)}/{len(clips)} clips already have transcripts.")
        if not questionary.confirm(
            "Overwrite existing transcripts?", default=False, style=VX_STYLE
        ).ask():
            print("  Keeping cached transcripts (only un-transcribed clips will be processed).")
        else:
            for cid in cached:
                audio_dir = ep.clip_paths(cid).audio
                for f in ["transcript.json", "transcript.vtt", "transcript_preview.html"]:
                    p = audio_dir / f
                    if p.exists():
                        p.unlink()
            print(f"  Cleared {len(cached)} cached transcripts.")

    # Load speaker context from briefing
    speaker_context = None
    context_path = ep.root / "user_context.json"
    if context_path.exists():
        ctx = json.loads(context_path.read_text())
        speaker_context = ctx.get("people", "") or None

    print(f"\n  Transcribing {len(clips)} clips ({t_provider})...\n")
    transcripts = transcribe_all_clips(
        clip_metadata, ep, cfg.transcribe, provider=t_provider, speaker_context=speaker_context
    )
    count = len(transcripts)
    print(f"\n  Done. {count}/{len(clips)} clips have speech\n")


def _show_status(name, meta, cfg):
    """Print project status."""
    ep = cfg.editorial_project(name)
    clips = ep.discover_clips()
    provider = meta.get("provider", "gemini")
    print(f"\n  Type: {meta['type']}, Provider: {provider}, Style: {meta.get('style', '?')}")
    print(f"  Clips: {len(clips)}")
    for cid in clips:
        cp = ep.clip_paths(cid)
        cached = [k for k, v in cp.cache_status().items() if v]
        transcribed = "transcribed" if cp.has_transcript() else ""
        reviewed = "reviewed" if cp.has_review(provider) else "pending"
        parts = ", ".join(cached)
        if transcribed:
            parts += f" | {transcribed}"
        parts += f" | {reviewed}"
        print(f"    {cid}: {parts}")

    storyboards = list(ep.storyboard.glob("editorial_*_v*.json")) if ep.storyboard.exists() else []
    if storyboards:
        print(f"  Storyboards: {len(storyboards)}")
        for s in sorted(storyboards):
            print(f"    {s.name}")
    print()


def _settings_flow(cfg):
    """Edit workspace settings."""
    ws_path = Path(".vx.json")
    ws = (
        json.loads(ws_path.read_text())
        if ws_path.exists()
        else {"provider": "gemini", "style": "vlog"}
    )

    provider = questionary.select(
        "Default AI provider:",
        choices=["gemini", "claude"],
        default=ws.get("provider", "gemini"),
        style=VX_STYLE,
    ).ask()
    if provider:
        ws["provider"] = provider

    style = questionary.select(
        "Default video style:",
        choices=["vlog", "travel-vlog", "family-video", "event-recap", "cinematic", "short-form"],
        default=ws.get("style", "vlog"),
        style=VX_STYLE,
    ).ask()
    if style:
        ws["style"] = style

    ws_path.write_text(json.dumps(ws, indent=2) + "\n")
    print(f"\n  Settings saved: provider={ws['provider']}, style={ws['style']}\n")
