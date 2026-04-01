"""Interactive TUI mode — guided video production workflow."""

import json
import os
import subprocess
from pathlib import Path

import questionary
from questionary import Style

from .config import DEFAULT_CONFIG

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

    # Style preset selection (optional creative direction)
    from .style_presets import list_presets, get_preset

    presets = list_presets()
    preset_choices = [questionary.Choice("None (standard editing)", value=None)]
    for p in presets:
        preset_choices.append(questionary.Choice(f"{p.label} — {p.description}", value=p.key))

    preset_key = questionary.select(
        "Style preset (optional — adds AI creative direction):",
        choices=preset_choices,
        style=VX_STYLE,
    ).ask()

    style_preset = get_preset(preset_key) if preset_key else None
    if style_preset:
        print(f"\n  Preset: {style_preset.label}")
        if style_preset.has_phase3:
            print("  This preset will generate a Visual Monologue (text overlay narrative)")

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
    print(f"  Style: {style}, Provider: {provider}")
    if style_preset:
        print(f"  Preset: {style_preset.label}")
    print()

    from .editorial_agent import (
        discover_source_clips,
        preprocess_all_clips,
        build_master_manifest,
        run_phase1_gemini,
        run_phase1_claude,
        run_phase2,
        _retry_failed_phase1,
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
    if preset_key:
        meta["style_preset"] = preset_key
    (ep.root / "project.json").write_text(json.dumps(meta, indent=2))

    # Discover
    clips = discover_source_clips(source_path)
    if not clips:
        print(f"  No video files found in {source_path}\n")
        return

    print(f"  Found {len(clips)} clips\n")

    # Let user deselect clips they don't want
    selected = questionary.checkbox(
        "Select clips to include:",
        choices=[questionary.Choice(c.name, value=c, checked=True) for c in clips],
        style=VX_STYLE,
    ).ask()
    if selected is None:
        return
    if not selected:
        print("  No clips selected.\n")
        return
    clips = selected

    meta["clip_count"] = len(clips)
    (ep.root / "project.json").write_text(json.dumps(meta, indent=2))

    # Preprocess
    print(f"  Preprocessing {len(clips)} clips...\n")
    clip_metadata = preprocess_all_clips(clips, ep, cfg.preprocess)
    manifest = build_master_manifest(clip_metadata, ep, name)
    print(f"\n  Total footage: {manifest['total_duration_fmt']}")

    # Format analysis + selection
    clip_metadata, output_format = _run_format_selection(clip_metadata, meta, ep)

    # Rebuild manifest if clips were filtered (Live Photos excluded)
    if len(clip_metadata) != manifest["clip_count"]:
        manifest = build_master_manifest(clip_metadata, ep, name)

    from .tracing import ProjectTracer

    tracer = ProjectTracer(ep.root)

    # Resolve style supplements from preset
    p1_supplement = style_preset.phase1_supplement if style_preset else None
    p2_supplement = style_preset.phase2_supplement if style_preset else None

    use_smart_briefing = bool(os.environ.get("GEMINI_API_KEY"))

    # Smart briefing BEFORE transcription (Gemini path) — uploads proxies and
    # populates the shared File API cache, plus gathers user context (speaker names,
    # highlights) that improves transcription and Phase 1 quality.
    user_context = None
    if use_smart_briefing:
        from .briefing import run_smart_briefing

        user_context = run_smart_briefing(
            ep, style, gemini_model=cfg.transcribe.gemini_model, tracer=tracer
        )

    # Transcription (benefits from cached Gemini URIs + speaker context from briefing)
    _run_transcription(ep, clip_metadata, cfg)

    # Phase 1
    if not questionary.confirm("Run Phase 1 clip reviews?", default=True, style=VX_STYLE).ask():
        print("\n  Skipped. Run 'vx analyze' later.\n")
        return

    print(f"\n  Phase 1: Reviewing clips with {provider}...\n")
    if provider == "gemini":
        reviews, failed = run_phase1_gemini(
            ep,
            manifest,
            cfg.gemini,
            tracer=tracer,
            style_supplement=p1_supplement,
            user_context=user_context,
        )
    else:
        reviews, failed = run_phase1_claude(
            ep,
            manifest,
            cfg.claude,
            style_supplement=p1_supplement,
            user_context=user_context,
        )
    reviews, failed = _retry_failed_phase1(
        failed,
        reviews,
        ep,
        manifest,
        provider,
        cfg,
        tracer=tracer,
        style_supplement=p1_supplement,
        user_context=user_context,
    )
    print(f"\n  Reviewed {len(reviews)} clips")

    # Manual briefing AFTER Phase 1 (non-Gemini path) — needs Phase 1 reviews
    # to generate smart questions about detected people and highlights.
    if not use_smart_briefing:
        from .briefing import run_briefing

        user_context = run_briefing(reviews, style, ep.root)

    # Phase 2
    if not questionary.confirm(
        "Generate editorial storyboard?", default=True, style=VX_STYLE
    ).ask():
        print("\n  Context saved. Run 'vx analyze' later.\n")
        return

    # Ask about visual mode
    visual = False
    if provider == "gemini":
        visual = _ask_visual_phase2(ep, reviews)

    print("\n  Phase 2: Generating storyboard...\n")
    run_phase2(
        clip_reviews=reviews,
        editorial_paths=ep,
        project_name=name,
        provider=provider,
        gemini_cfg=cfg.gemini,
        claude_cfg=cfg.claude,
        style=style,
        user_context=user_context,
        tracer=tracer,
        visual=visual,
        style_supplement=p2_supplement,
    )

    # Phase 3 — Visual Monologue (if preset supports it)
    if style_preset and style_preset.has_phase3:
        if questionary.confirm(
            "Generate visual monologue (text overlay plan)?", default=True, style=VX_STYLE
        ).ask():
            from .editorial_agent import run_monologue

            print("\n  Phase 3: Generating visual monologue...\n")
            run_monologue(
                editorial_paths=ep,
                provider=provider,
                gemini_cfg=cfg.gemini,
                claude_cfg=cfg.claude,
                style_preset=style_preset,
                user_context=user_context,
                tracer=tracer,
            )

    tracer.print_summary("Pipeline Total")
    print("\n  Storyboard ready!")
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

        has_monologue = (
            any(ep.storyboard.glob("monologue_*_latest.json")) if ep.storyboard.exists() else False
        )
        has_preset_phase3 = False
        preset_key = meta.get("style_preset")
        if preset_key:
            from .style_presets import get_preset as _get_preset

            _sp = _get_preset(preset_key)
            has_preset_phase3 = _sp.has_phase3 if _sp else False

        choices = []
        if has_preview:
            choices.append("Open preview in browser")
        if has_storyboard:
            choices.append("Regenerate preview")
            choices.append("Assemble rough cut")
            if has_monologue:
                choices.append("Assemble rough cut with text overlays")
        if has_storyboard and has_preset_phase3:
            choices.append("Generate visual monologue")
        choices.append("Manage clips")
        choices.append("Transcribe audio")
        choices.append("Run analysis (Phase 1 + 2)")
        choices.append("Edit briefing (AI-guided)")
        choices.append("Edit briefing (manual)")
        choices.append("Set style preset")
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
                print("\n  Assembling rough cut...\n")
                result = run_rough_cut(json_files[0], ep)
                print(f"\n  Done! v{result['version']}")
                if questionary.confirm("Open preview?", default=True, style=VX_STYLE).ask():
                    subprocess.run(["open", str(result["preview"])])
        elif action == "Assemble rough cut with text overlays":
            from .rough_cut import run_rough_cut
            from .models import MonologuePlan

            json_files = sorted(ep.storyboard.glob("editorial_*_latest.json"))
            mono_files = sorted(ep.storyboard.glob("monologue_*_latest.json"))
            if json_files and mono_files:
                monologue = MonologuePlan.model_validate_json(mono_files[0].read_text())
                print(f"\n  Assembling rough cut with {len(monologue.overlays)} text overlays...\n")
                result = run_rough_cut(json_files[0], ep, monologue=monologue)
                print(f"\n  Done! v{result['version']}")
                if questionary.confirm("Open preview?", default=True, style=VX_STYLE).ask():
                    subprocess.run(["open", str(result["preview"])])
        elif action == "Generate visual monologue":
            from .editorial_agent import run_monologue
            from .style_presets import get_preset as _get_preset2
            from .tracing import ProjectTracer

            _sp2 = _get_preset2(meta.get("style_preset", ""))
            if _sp2:
                provider = meta.get("provider", "gemini")
                tracer = ProjectTracer(ep.root)
                print(f"\n  Generating visual monologue ({_sp2.label})...\n")
                run_monologue(
                    editorial_paths=ep,
                    provider=provider,
                    gemini_cfg=cfg.gemini,
                    claude_cfg=cfg.claude,
                    style_preset=_sp2,
                    tracer=tracer,
                )
                tracer.print_summary("Monologue")
        elif action == "Transcribe audio":
            _run_transcription_interactive(name, cfg)
        elif action == "Run analysis (Phase 1 + 2)":
            _run_analyze(name, meta, cfg)
        elif action == "Edit briefing (AI-guided)":
            style = meta.get("style", "vlog")
            from .briefing import run_smart_briefing

            # Delete cached scan to force fresh scan
            scan_path = ep.root / "quick_scan.json"
            if scan_path.exists():
                scan_path.unlink()
            run_smart_briefing(ep, style, gemini_model=cfg.transcribe.gemini_model)
        elif action == "Edit briefing (manual)":
            reviews = _load_reviews(ep)
            style = meta.get("style", "vlog")
            from .briefing import run_briefing

            # Delete existing context to force fresh questions
            ctx_path = ep.root / "user_context.json"
            if ctx_path.exists():
                ctx_path.unlink()
            run_briefing(reviews, style, ep.root)
        elif action == "Set style preset":
            from .style_presets import list_presets as _list_presets, get_preset as _get_preset3

            current_preset = meta.get("style_preset")
            all_presets = _list_presets()
            preset_choices = [
                questionary.Choice(
                    "None (standard editing)" + (" (current)" if not current_preset else ""),
                    value=None,
                )
            ]
            for p in all_presets:
                label = f"{p.label} — {p.description}"
                if p.key == current_preset:
                    label += " (current)"
                preset_choices.append(questionary.Choice(label, value=p.key))

            new_key = questionary.select(
                "Style preset:",
                choices=preset_choices,
                style=VX_STYLE,
            ).ask()

            if new_key != current_preset:
                if new_key:
                    meta["style_preset"] = new_key
                    sp = _get_preset3(new_key)
                    print(f"\n  Set preset: {sp.label}")
                    if sp.has_phase3:
                        print("  This preset supports Visual Monologue (Phase 3)")
                    print("  Re-run analysis to apply preset creative direction to all phases.")
                else:
                    meta.pop("style_preset", None)
                    print("\n  Removed style preset.")
                (ep.root / "project.json").write_text(json.dumps(meta, indent=2))
        elif action == "Manage clips":
            _manage_clips(name, meta, cfg)
        elif action == "Show status":
            _show_status(name, meta, cfg)


def _manage_clips(name, meta, cfg):
    """Add or remove clips from an existing project."""
    import shutil

    from .editorial_agent import discover_source_clips, preprocess_all_clips, build_master_manifest

    ep = cfg.editorial_project(name)
    source_dir = Path(meta.get("source_dir", ""))
    if not source_dir.is_dir():
        print(f"\n  Source directory not found: {source_dir}\n")
        return

    # All available clips from source directory
    all_source_clips = discover_source_clips(source_dir)
    if not all_source_clips:
        print(f"\n  No video files found in {source_dir}\n")
        return

    # Currently included clip IDs
    current_clip_ids = set(ep.discover_clips())

    # Build checkbox: checked if already in project, unchecked if not
    choices = []
    for clip_file in all_source_clips:
        clip_id = clip_file.stem
        is_included = clip_id in current_clip_ids
        choices.append(questionary.Choice(clip_file.name, value=clip_file, checked=is_included))

    selected = questionary.checkbox(
        f"Select clips to include ({len(current_clip_ids)} currently included):",
        choices=choices,
        style=VX_STYLE,
    ).ask()
    if selected is None:
        return

    selected_ids = {c.stem for c in selected}

    # Determine adds and removes
    to_add = [c for c in selected if c.stem not in current_clip_ids]
    to_remove = current_clip_ids - selected_ids

    if not to_add and not to_remove:
        print("\n  No changes.\n")
        return

    if to_remove:
        print(f"\n  Removing {len(to_remove)} clip(s):")
        for cid in sorted(to_remove):
            clip_dir = ep.clips_dir / cid
            if clip_dir.exists():
                shutil.rmtree(clip_dir)
                print(f"    - {cid}")

    if to_add:
        print(f"\n  Adding {len(to_add)} clip(s), preprocessing...\n")
        preprocess_all_clips(to_add, ep, cfg.preprocess)

    # Rebuild manifest with current clips
    remaining_clips = [c for c in all_source_clips if c.stem in selected_ids]
    build_master_manifest([{"clip_id": c.stem} for c in remaining_clips], ep, name)

    # Update project metadata
    meta["clip_count"] = len(selected_ids)
    (ep.root / "project.json").write_text(json.dumps(meta, indent=2))

    print(f"\n  Project now has {len(selected_ids)} clips.")
    if to_remove:
        print("  Note: re-run analysis to update storyboard.\n")


def _run_analyze(name, meta, cfg):
    """Run the full analysis pipeline."""
    from .editorial_agent import (
        discover_source_clips,
        preprocess_all_clips,
        build_master_manifest,
        run_phase1_gemini,
        run_phase1_claude,
        run_phase2,
        _retry_failed_phase1,
    )
    from .tracing import ProjectTracer

    ep = cfg.editorial_project(name)
    provider = meta.get("provider", "gemini")
    style = meta.get("style", "vlog")
    source_dir = Path(meta["source_dir"])
    tracer = ProjectTracer(ep.root)

    # Resolve style preset
    style_preset = None
    preset_key = meta.get("style_preset")
    if preset_key:
        from .style_presets import get_preset as _gp

        style_preset = _gp(preset_key)
    p1_supplement = style_preset.phase1_supplement if style_preset else None
    p2_supplement = style_preset.phase2_supplement if style_preset else None

    if style_preset:
        print(f"\n  Style preset: {style_preset.label}")

    clips = discover_source_clips(source_dir)
    print(f"\n  {len(clips)} clips, preprocessing...\n")
    clip_metadata = preprocess_all_clips(clips, ep, cfg.preprocess)
    manifest = build_master_manifest(clip_metadata, ep, name)

    # Format analysis + selection
    clip_metadata, output_format = _run_format_selection(clip_metadata, meta, ep)
    if len(clip_metadata) != manifest["clip_count"]:
        manifest = build_master_manifest(clip_metadata, ep, name)

    use_smart_briefing = bool(os.environ.get("GEMINI_API_KEY"))

    # Smart briefing BEFORE transcription (Gemini path)
    user_context = None
    if use_smart_briefing:
        from .briefing import run_smart_briefing

        user_context = run_smart_briefing(
            ep, style, gemini_model=cfg.transcribe.gemini_model, tracer=tracer
        )

    # Transcription (benefits from cached Gemini URIs + speaker context from briefing)
    _run_transcription(ep, clip_metadata, cfg)

    # Check if cached Phase 1 reviews exist — offer to force re-run
    force_phase1 = False
    review_suffix = f"review_{provider}_latest.json"
    has_cached = any(
        (ep.clip_paths(c["clip_id"]).review / review_suffix).exists() for c in manifest["clips"]
    )
    if has_cached:
        force_phase1 = questionary.confirm(
            "Cached Phase 1 reviews found. Re-run from scratch?",
            default=False,
            style=VX_STYLE,
        ).ask()

    print("\n  Phase 1: Reviewing clips...\n")
    if provider == "gemini":
        reviews, failed = run_phase1_gemini(
            ep,
            manifest,
            cfg.gemini,
            force=force_phase1,
            tracer=tracer,
            style_supplement=p1_supplement,
            user_context=user_context,
        )
    else:
        reviews, failed = run_phase1_claude(
            ep,
            manifest,
            cfg.claude,
            force=force_phase1,
            style_supplement=p1_supplement,
            user_context=user_context,
        )
    reviews, failed = _retry_failed_phase1(
        failed,
        reviews,
        ep,
        manifest,
        provider,
        cfg,
        tracer=tracer,
        style_supplement=p1_supplement,
        user_context=user_context,
    )

    # Manual briefing AFTER Phase 1 (non-Gemini path)
    if not use_smart_briefing:
        from .briefing import run_briefing

        user_context = run_briefing(reviews, style, ep.root)

    # Ask about visual mode
    visual = False
    if provider == "gemini":
        visual = _ask_visual_phase2(ep, reviews)

    print("\n  Phase 2: Generating storyboard...\n")
    run_phase2(
        clip_reviews=reviews,
        editorial_paths=ep,
        project_name=name,
        provider=provider,
        gemini_cfg=cfg.gemini,
        claude_cfg=cfg.claude,
        style=style,
        user_context=user_context,
        tracer=tracer,
        visual=visual,
        style_supplement=p2_supplement,
    )

    # Phase 3 — Visual Monologue (if preset supports it)
    if style_preset and style_preset.has_phase3:
        if questionary.confirm(
            "Generate visual monologue (text overlay plan)?", default=True, style=VX_STYLE
        ).ask():
            from .editorial_agent import run_monologue

            print("\n  Phase 3: Generating visual monologue...\n")
            run_monologue(
                editorial_paths=ep,
                provider=provider,
                gemini_cfg=cfg.gemini,
                claude_cfg=cfg.claude,
                style_preset=style_preset,
                user_context=user_context,
                tracer=tracer,
            )

    tracer.print_summary("Analysis Total")
    print("\n  Storyboard ready!")


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


def _run_format_selection(clip_metadata, meta, ep):
    """Analyze source formats, filter Live Photos, let user pick output format.

    Updates meta and writes project.json. Returns (filtered_clip_metadata, OutputFormat).
    """
    from .config import OutputFormat
    from .format_analyzer import (
        analyze_source_formats,
        recommend_output_format,
        build_format_choices,
        format_summary_text,
    )

    analysis = analyze_source_formats(clip_metadata)
    print(f"\n{format_summary_text(analysis, clip_metadata)}\n")

    # Live Photo filtering
    live_ids = analysis["live_photo_ids"]
    if live_ids:
        display = ", ".join(f"{cid}" for cid in live_ids[:6])
        if len(live_ids) > 6:
            display += "..."
        print(f"  Possible Live Photo clips ({len(live_ids)}): {display}\n")

        action = questionary.select(
            "How to handle Live Photo clips?",
            choices=[
                "Include all",
                "Exclude Live Photos",
                "Choose individually",
            ],
            style=VX_STYLE,
        ).ask()

        if action == "Exclude Live Photos":
            clip_metadata = [c for c in clip_metadata if c["clip_id"] not in live_ids]
            print(f"  Excluded {len(live_ids)} Live Photos, {len(clip_metadata)} clips remain\n")
            # Re-analyze without live photos
            analysis = analyze_source_formats(clip_metadata)
        elif action == "Choose individually":
            keep = questionary.checkbox(
                "Select Live Photo clips to keep:",
                choices=[
                    questionary.Choice(
                        f"{cid} ({next((c['duration_sec'] for c in clip_metadata if c['clip_id'] == cid), 0):.1f}s)",
                        value=cid,
                        checked=False,
                    )
                    for cid in live_ids
                ],
                style=VX_STYLE,
            ).ask()
            if keep is None:
                keep = []
            exclude = set(live_ids) - set(keep)
            if exclude:
                clip_metadata = [c for c in clip_metadata if c["clip_id"] not in exclude]
                print(f"  Excluded {len(exclude)} Live Photos, {len(clip_metadata)} clips remain\n")
                analysis = analyze_source_formats(clip_metadata)

    # Format recommendation
    recommended, rationale = recommend_output_format(analysis)
    print(f"  {rationale}\n")

    if analysis["has_mixed_resolutions"] or analysis["has_mixed_aspects"]:
        # Mixed sources — let user choose
        choices = build_format_choices(analysis)
        choice_labels = [c["label"] for c in choices]
        selected = questionary.select(
            "Output format:",
            choices=choice_labels,
            style=VX_STYLE,
        ).ask()
        if selected:
            chosen = next(c for c in choices if c["label"] == selected)
            output_format = OutputFormat(
                width=chosen["width"],
                height=chosen["height"],
                fps=chosen["fps"],
                orientation=chosen["orientation"],
                label=selected.replace(" (recommended)", ""),
            )
        else:
            output_format = recommended
    else:
        # Uniform — confirm recommendation
        if not questionary.confirm(
            f"Use {recommended.label} @ {recommended.fps}fps?",
            default=True,
            style=VX_STYLE,
        ).ask():
            choices = build_format_choices(analysis)
            choice_labels = [c["label"] for c in choices]
            selected = questionary.select(
                "Output format:",
                choices=choice_labels,
                style=VX_STYLE,
            ).ask()
            if selected:
                chosen = next(c for c in choices if c["label"] == selected)
                output_format = OutputFormat(
                    width=chosen["width"],
                    height=chosen["height"],
                    fps=chosen["fps"],
                    orientation=chosen["orientation"],
                    label=selected.replace(" (recommended)", ""),
                )
            else:
                output_format = recommended
        else:
            output_format = recommended

    # Fit mode
    if analysis["has_mixed_aspects"]:
        fit = questionary.select(
            "How to handle different aspect ratios?",
            choices=[
                questionary.Choice(
                    "Pad (black bars, preserve full frame)",
                    value="pad",
                ),
                questionary.Choice(
                    "Crop to fill (no bars, may lose edges)",
                    value="crop",
                ),
            ],
            style=VX_STYLE,
        ).ask()
        if fit:
            output_format.fit_mode = fit

    # Codec
    codec = questionary.select(
        "Output codec:",
        choices=[
            questionary.Choice(
                "Auto (hardware-accelerated on Apple Silicon, software fallback)", value="auto"
            ),
            questionary.Choice(
                "H.264 software (libx264, universal compatibility)", value="libx264"
            ),
            questionary.Choice("H.265 software (libx265, smaller files)", value="libx265"),
        ],
        style=VX_STYLE,
    ).ask()
    if codec:
        output_format.codec = codec

    # Persist
    meta["output_format"] = output_format.to_dict()
    (ep.root / "project.json").write_text(json.dumps(meta, indent=2))
    print(
        f"\n  Output format: {output_format.label}, {output_format.width}x{output_format.height}"
        f" @ {output_format.fps}fps, {output_format.codec}, fit={output_format.fit_mode}\n"
    )

    return clip_metadata, output_format


def _ask_visual_phase2(ep, reviews):
    """Show cache status and ask about visual Phase 2. Returns bool.

    Visual mode is only available when clip count <= 10 (Gemini limit: 10 videos
    per prompt). For larger projects, attaching a subset would bias the edit toward
    those clips, so we skip visual mode entirely and rely on text reviews.
    """
    from .file_cache import load_file_api_cache, get_cached_uri
    from .editorial_agent import MAX_VISUAL_VIDEOS

    unique_ids = list(dict.fromkeys(r.get("clip_id", "") for r in reviews))
    total = len(unique_ids)

    if total > MAX_VISUAL_VIDEOS:
        print(
            f"\n  Skipping visual Phase 2: {total} clips exceeds Gemini limit "
            f"of {MAX_VISUAL_VIDEOS} videos per request."
        )
        print("  Phase 2 will use text reviews + transcripts (no video attachment).")
        return False

    cache = load_file_api_cache(ep)
    cached_count = sum(1 for cid in unique_ids if get_cached_uri(cache, cid))

    print(f"\n  Visual Phase 2: {total} clips")
    if cached_count == total:
        print(f"  All {cached_count} proxy URIs cached (no upload needed)")
    elif cached_count > 0:
        print(f"  {cached_count} cached, {total - cached_count} need upload")

    return questionary.confirm(
        "Include proxy videos in Phase 2? (AI sees footage, better edits)",
        default=False,
        style=VX_STYLE,
    ).ask()


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
        transcribe_all_clips,
    )

    ep = cfg.editorial_project(name)
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

    # LLM usage
    from .tracing import load_all_traces, summarize_traces

    traces = load_all_traces(ep.root)
    if traces:
        ts = summarize_traces(traces)
        print(
            f"  LLM Usage: {ts['calls']} calls | "
            f"{ts['total_tokens']:,} tokens | "
            f"~${ts['estimated_cost_usd']:.4f}"
        )
        for phase, ps in ts.get("by_phase", {}).items():
            print(
                f"    {phase}: {ps['calls']} calls, "
                f"{ps['total_tokens']:,} tokens, "
                f"~${ps['estimated_cost_usd']:.4f}"
            )
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
