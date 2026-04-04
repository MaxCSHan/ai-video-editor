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

    # Auto-connect to Phoenix tracing server if running
    from .tracing import connect_phoenix, get_phoenix_status

    if connect_phoenix():
        _, trace_url = get_phoenix_status()
        print(f"  \033[2mTracing: connected ({trace_url})\033[0m\n")

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
    meta["included_clips"] = [c.stem for c in clips]
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

            print("\n  Phase 3: Visual Monologue")
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
        # Detect offline source drive
        source_dir = meta.get("source_dir", "")
        offline = bool(source_dir) and not Path(source_dir).is_dir()

        # Check state
        has_storyboard = (
            any(ep.storyboard.glob("editorial_*_latest.json")) if ep.storyboard.exists() else False
        )
        has_preview = any(ep.exports.glob("*/preview.html")) if ep.exports.exists() else False
        _cuts_dir = ep.exports / "cuts"
        has_rough_cut = (
            any(_cuts_dir.glob("*/rough_cut*.mp4"))
            if _cuts_dir.exists()
            else any(ep.exports.glob("*/rough_cut*.mp4"))
            if ep.exports.exists()
            else False
        )

        has_monologue = (
            any(ep.storyboard.glob("monologue_*_latest.json")) if ep.storyboard.exists() else False
        )
        has_preset_phase3 = False
        preset_key = meta.get("style_preset")
        if preset_key:
            from .style_presets import get_preset as _get_preset

            _sp = _get_preset(preset_key)
            has_preset_phase3 = _sp.has_phase3 if _sp else False

        has_manifest = ep.master_manifest.exists()

        choices = []
        if has_preview:
            choices.append("Open preview in browser")
        if has_storyboard:
            choices.append("Regenerate preview")
            if offline:
                choices.append("Assemble rough cut (proxy)")
            else:
                choices.append("Assemble rough cut")
            if has_monologue:
                if offline:
                    choices.append("Assemble rough cut with text overlays (proxy)")
                else:
                    choices.append("Assemble rough cut with text overlays")
        if has_storyboard and has_preset_phase3:
            choices.append("Generate visual monologue")
        if not offline:
            choices.append("Manage clips")
        choices.append("Transcribe audio")
        if offline and has_manifest:
            choices.append("Run analysis (Phase 1 + 2)")
        elif not offline:
            choices.append("Run analysis (Phase 1 + 2)")
        choices.append("Edit briefing (AI-guided)")
        choices.append("Edit briefing (manual)")
        choices.append("Set style preset")
        if has_storyboard:
            choices.append("Compose a cut")
            choices.append("Compare versions")
        choices.append("Version history")
        choices.append("Show status")
        if has_rough_cut:
            choices.append("Open rough cut video")
        choices.append(questionary.Choice("← Back", value="back"))

        if offline:
            print(f"\n  Project: {name}  [OFFLINE]")
            print(f"  Source offline: {source_dir}")
        else:
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
            _cd = ep.exports / "cuts"
            cuts = sorted(_cd.glob("*/rough_cut*.mp4"), reverse=True) if _cd.exists() else []
            if not cuts:
                cuts = sorted(ep.exports.glob("*/rough_cut*.mp4"), reverse=True)
            if cuts:
                subprocess.run(["open", str(cuts[0])])
        elif action == "Regenerate preview":
            from .models import EditorialStoryboard
            from .render import render_html_preview
            from .versioning import (
                begin_version,
                commit_version,
                versioned_dir,
                update_latest_symlink,
            )

            json_files = sorted(ep.storyboard.glob("editorial_*_latest.json"))
            if json_files:
                sb = EditorialStoryboard.model_validate_json(json_files[0].read_text())
                art_meta = begin_version(
                    ep.root,
                    phase="preview",
                    provider="render",
                    target_dir=ep.exports,
                )
                v = art_meta.version
                vdir = versioned_dir(ep.exports, v)
                # Embed existing rough cut if available (check cuts/latest first)
                rough_cut_path = None
                for _rc_dir in [ep.exports / "cuts" / "latest", ep.exports / "latest"]:
                    if _rc_dir.exists():
                        rc = _rc_dir / "rough_cut.mp4"
                        if rc.exists():
                            rough_cut_path = rc.resolve()
                            break
                html = render_html_preview(
                    sb,
                    clips_dir=ep.clips_dir,
                    output_dir=vdir,
                    rough_cut_path=rough_cut_path,
                )
                preview_path = vdir / "preview.html"
                preview_path.write_text(html)
                commit_version(
                    ep.root, art_meta, output_paths=[preview_path], target_dir=ep.exports
                )
                update_latest_symlink(vdir)
                print(f"\n  Preview v{v} generated: {preview_path}")
                if questionary.confirm("Open preview?", default=True, style=VX_STYLE).ask():
                    subprocess.run(["open", str(preview_path)])
        elif action in ("Assemble rough cut", "Assemble rough cut (proxy)"):
            from .rough_cut import run_rough_cut

            is_proxy = action.endswith("(proxy)")
            json_files = sorted(ep.storyboard.glob("editorial_*_latest.json"))
            if json_files:
                if is_proxy:
                    print("\n  Assembling PROXY rough cut (source drive offline)...\n")
                else:
                    print("\n  Assembling rough cut...\n")
                result = run_rough_cut(json_files[0], ep, proxy_mode=is_proxy)
                print(f"\n  Done! v{result['version']}")
                if questionary.confirm("Open preview?", default=True, style=VX_STYLE).ask():
                    subprocess.run(["open", str(result["preview"])])
        elif action in (
            "Assemble rough cut with text overlays",
            "Assemble rough cut with text overlays (proxy)",
        ):
            from .rough_cut import run_rough_cut
            from .models import MonologuePlan

            is_proxy = action.endswith("(proxy)")
            json_files = sorted(ep.storyboard.glob("editorial_*_latest.json"))
            mono_files = sorted(ep.storyboard.glob("monologue_*_latest.json"))
            if json_files and mono_files:
                monologue = MonologuePlan.model_validate_json(mono_files[0].read_text())
                label = f"with {len(monologue.overlays)} text overlays"
                if is_proxy:
                    print(f"\n  Assembling PROXY rough cut {label} (source drive offline)...\n")
                else:
                    print(f"\n  Assembling rough cut {label}...\n")
                result = run_rough_cut(json_files[0], ep, monologue=monologue, proxy_mode=is_proxy)
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
                print(f"\n  Phase 3: Visual Monologue ({_sp2.label})")
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

            # Smart briefing versioning creates new versions instead of deleting
            run_smart_briefing(ep, style, gemini_model=cfg.transcribe.gemini_model)
        elif action == "Edit briefing (manual)":
            reviews = _load_reviews(ep)
            style = meta.get("style", "vlog")
            from .briefing import run_briefing

            # run_briefing handles versioning — new version instead of delete
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
        elif action == "Compose a cut":
            _compose_cut_flow(name, ep)
        elif action == "Compare versions":
            _compare_versions_flow(name, ep)
        elif action == "Version history":
            _version_history_flow(name, ep)
        elif action == "Show status":
            _show_status(name, meta, cfg)


def _compose_cut_flow(name, ep):
    """Guided flow: pick storyboard + monologue → save composition → optionally assemble."""
    from datetime import datetime, timezone
    from .versioning import list_artifacts, save_composition, list_compositions
    from .models import Composition, EditorialStoryboard
    from .storyboard_format import format_duration

    storyboards = [
        a for a in list_artifacts(ep.root) if a.phase == "storyboard" and a.status == "complete"
    ]
    if not storyboards:
        print("\n  No storyboards found. Run analysis first.")
        return

    # Pick storyboard
    sb_choices = []
    for sb in storyboards:
        # Load summary info
        label = f"{sb.artifact_id}"
        try:
            sb_path = ep.storyboard / [f for f in sb.output_files if f.endswith(".json")][0]
            if sb_path.exists():
                data = EditorialStoryboard.model_validate_json(sb_path.read_text())
                dur = format_duration(data.total_segments_duration)
                label += f"  ({len(data.segments)} segments, {dur})"
        except Exception:
            pass
        sb_choices.append(questionary.Choice(label, value=sb))

    selected_sb = questionary.select(
        "Pick storyboard version:",
        choices=sb_choices,
        style=VX_STYLE,
    ).ask()
    if not selected_sb:
        return

    # Pick monologue (optional)
    monologues = [
        a for a in list_artifacts(ep.root) if a.phase == "monologue" and a.status == "complete"
    ]
    selected_mono = None
    if monologues:
        mono_choices = [questionary.Choice("None (no text overlays)", value=None)]
        for m in monologues:
            mono_choices.append(questionary.Choice(f"{m.artifact_id}  (v{m.version})", value=m))

        selected_mono = questionary.select(
            "Include monologue?",
            choices=mono_choices,
            style=VX_STYLE,
        ).ask()

    # Name the composition
    existing = list_compositions(ep.root)
    default_name = f"comp-{len(existing) + 1}"
    comp_name = questionary.text(
        "Composition name:",
        default=default_name,
        style=VX_STYLE,
    ).ask()
    if not comp_name:
        return

    comp = Composition(
        name=comp_name,
        storyboard=selected_sb.artifact_id,
        monologue=selected_mono.artifact_id if selected_mono else None,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    save_composition(ep.root, comp)
    mono_str = f" + {selected_mono.artifact_id}" if selected_mono else ""
    print(f"\n  Saved: {comp_name} = {selected_sb.artifact_id}{mono_str}")

    # Offer to assemble now
    if questionary.confirm("Assemble rough cut now?", default=True, style=VX_STYLE).ask():
        from .versioning import resolve_artifact_path

        sb_path = resolve_artifact_path(ep.root, comp.storyboard)
        if sb_path:
            from .rough_cut import run_rough_cut
            from .models import MonologuePlan

            monologue_obj = None
            if comp.monologue:
                mono_path = resolve_artifact_path(ep.root, comp.monologue)
                if mono_path:
                    monologue_obj = MonologuePlan.model_validate_json(mono_path.read_text())

            result = run_rough_cut(
                storyboard_json_path=sb_path,
                editorial_paths=ep,
                monologue=monologue_obj,
            )
            v = result.get("version", "?")
            print(f"\n  Rough cut v{v} assembled.")
            if questionary.confirm("Open preview?", default=True, style=VX_STYLE).ask():
                subprocess.run(["open", str(result["preview"])])


def _compare_versions_flow(name, ep):
    """Side-by-side comparison of two storyboard versions."""
    from .versioning import list_artifacts
    from .models import EditorialStoryboard
    from .storyboard_format import format_duration

    storyboards = [
        a for a in list_artifacts(ep.root) if a.phase == "storyboard" and a.status == "complete"
    ]
    if len(storyboards) < 2:
        print("\n  Need at least 2 storyboard versions to compare.")
        return

    sb_choices = [questionary.Choice(f"{sb.artifact_id}", value=sb) for sb in storyboards]

    sb_a = questionary.select("First version:", choices=sb_choices, style=VX_STYLE).ask()
    if not sb_a:
        return
    sb_b = questionary.select(
        "Second version:",
        choices=[c for c in sb_choices if c.value != sb_a],
        style=VX_STYLE,
    ).ask()
    if not sb_b:
        return

    # Load both storyboards
    try:
        path_a = ep.storyboard / [f for f in sb_a.output_files if f.endswith(".json")][0]
        path_b = ep.storyboard / [f for f in sb_b.output_files if f.endswith(".json")][0]
        data_a = EditorialStoryboard.model_validate_json(path_a.read_text())
        data_b = EditorialStoryboard.model_validate_json(path_b.read_text())
    except Exception as e:
        print(f"\n  Error loading storyboards: {e}")
        return

    print(f"\n  Comparing {sb_a.artifact_id} vs {sb_b.artifact_id}:")
    print(f"  {'':36s} {'v' + str(sb_a.version):>10s}  {'v' + str(sb_b.version):>10s}")
    print(f"  {'Segments':36s} {len(data_a.segments):>10d}  {len(data_b.segments):>10d}")
    print(
        f"  {'Duration':36s} {format_duration(data_a.total_segments_duration):>10s}  {format_duration(data_b.total_segments_duration):>10s}"
    )

    clips_a = {s.clip_id for s in data_a.segments}
    clips_b = {s.clip_id for s in data_b.segments}
    only_a = clips_a - clips_b
    only_b = clips_b - clips_a
    if only_a:
        print(f"  Only in v{sb_a.version}: {', '.join(sorted(only_a)[:5])}")
    if only_b:
        print(f"  Only in v{sb_b.version}: {', '.join(sorted(only_b)[:5])}")
    if not only_a and not only_b:
        print("  Same clips used in both versions")

    discarded_a = len(data_a.discarded)
    discarded_b = len(data_b.discarded)
    if discarded_a != discarded_b:
        print(f"  {'Discarded clips':36s} {discarded_a:>10d}  {discarded_b:>10d}")


def _version_history_flow(name, ep):
    """Show version history for all phases."""
    from datetime import datetime
    from .versioning import list_artifacts, list_compositions

    artifacts = list_artifacts(ep.root, include_failed=True)

    if not artifacts:
        from .versioning import all_versions

        versions = all_versions(ep.root)
        if versions:
            print("\n  Legacy versioning (no artifact metadata):")
            for phase, v in sorted(versions.items()):
                print(f"    {phase}: v{v}")
        else:
            print("\n  No versions found.")
        return

    phases = {}
    for art in artifacts:
        phases.setdefault(art.phase, []).append(art)

    phase_labels = {
        "storyboard": "Storyboards (Phase 2)",
        "monologue": "Monologues (Phase 3)",
        "cut": "Rough Cuts",
        "preview": "Previews",
    }

    for phase, arts in phases.items():
        label = phase_labels.get(phase, phase.title())
        print(f"\n  {label}:")
        for art in arts:
            status = {"complete": "OK", "failed": "FAIL", "pending": "..."}.get(art.status, "?")
            try:
                ts = datetime.fromisoformat(art.created_at)
                ts_str = ts.strftime("%m-%d %H:%M")
            except Exception:
                ts_str = ""

            lineage = ""
            if art.inputs:
                lineage_parts = [f"{v}" for v in art.inputs.values() if v]
                if lineage_parts:
                    lineage = f"  <- {', '.join(lineage_parts[:3])}"

            print(f"    {art.artifact_id}  [{status}]  {ts_str}{lineage}")

    comps = list_compositions(ep.root)
    if comps:
        print("\n  Compositions:")
        for c in comps:
            mono_part = f" + {c.monologue}" if c.monologue else ""
            print(f"    {c.name}: {c.storyboard}{mono_part}")


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

    # Read existing manifest to preserve full clip metadata
    existing_clips_by_id = {}
    if ep.master_manifest.exists():
        old_manifest = json.loads(ep.master_manifest.read_text())
        for c in old_manifest.get("clips", []):
            existing_clips_by_id[c["clip_id"]] = c

    new_clip_metadata = []
    if to_add:
        print(f"\n  Adding {len(to_add)} clip(s), preprocessing...\n")
        new_clip_metadata = preprocess_all_clips(to_add, ep, cfg.preprocess)
    new_clips_by_id = {c["clip_id"]: c for c in new_clip_metadata}

    # Rebuild manifest with current clips in source order
    remaining_clips = [c for c in all_source_clips if c.stem in selected_ids]
    merged_metadata = []
    for c in remaining_clips:
        cid = c.stem
        if cid in new_clips_by_id:
            merged_metadata.append(new_clips_by_id[cid])
        elif cid in existing_clips_by_id:
            merged_metadata.append(existing_clips_by_id[cid])
    build_master_manifest(merged_metadata, ep, name)

    # Update project metadata
    meta["clip_count"] = len(selected_ids)
    meta["included_clips"] = sorted(selected_ids)
    (ep.root / "project.json").write_text(json.dumps(meta, indent=2))

    print(f"\n  Project now has {len(selected_ids)} clips.")
    if to_remove:
        print("  Note: re-run analysis to update storyboard.\n")


def _run_analyze(name, meta, cfg):
    """Run the full analysis pipeline."""
    from .editorial_agent import (
        discover_source_clips,
        discover_clips_from_manifest,
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
    offline = not source_dir.is_dir()
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

    if offline:
        # Offline mode: load clip list from manifest instead of scanning source dir
        print("\n  OFFLINE MODE: Source drive unavailable, skipping preprocessing.")
        print("  Using cached project data.\n")
        clip_metadata, manifest = discover_clips_from_manifest(ep)
        if not clip_metadata:
            print("  No manifest found — run analysis with source drive connected first.\n")
            return
        # Filter to included clips
        included = meta.get("included_clips")
        if included:
            included_set = set(included)
            clip_metadata = [c for c in clip_metadata if c["clip_id"] in included_set]
        print(f"  {len(clip_metadata)} clips (from cached manifest)\n")
    else:
        all_clips = discover_source_clips(source_dir)
        # Only process clips included in the project (respects manage-clips changes)
        included = meta.get("included_clips")
        if included:
            included_set = set(included)
            clips = [c for c in all_clips if c.stem in included_set]
        else:
            clips = all_clips
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

            print("\n  Phase 3: Visual Monologue")
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
    """Show concat status and ask about visual Phase 2. Returns bool.

    Proxies are concatenated into bundles (≤40 min each) to work around Gemini's
    10-video-per-prompt limit. Works for any number of clips.
    """
    unique_ids = list(dict.fromkeys(r.get("clip_id", "") for r in reviews))
    total = len(unique_ids)

    # Check if concat bundles already exist
    concat_dir = ep.root / "concat_proxies"
    has_concat = concat_dir.exists() and list(concat_dir.glob("bundle_*.mp4"))

    print(f"\n  Visual Phase 2: {total} clips (concatenated for Gemini)")
    if has_concat:
        print("  Concat bundles cached (no rebuild needed)")

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
    from .versioning import resolve_user_context_path

    speaker_context = None
    _uc_path = resolve_user_context_path(ep.root)
    if _uc_path:
        ctx = json.loads(_uc_path.read_text())
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
                for f in [
                    "transcript.json",
                    "transcript_latest.json",
                    "transcript.vtt",
                    "transcript_preview.html",
                ]:
                    p = audio_dir / f
                    if p.exists() or p.is_symlink():
                        p.unlink()
            print(f"  Cleared {len(cached)} cached transcripts.")

    # Load speaker context from briefing
    from .versioning import resolve_user_context_path

    speaker_context = None
    _uc_path = resolve_user_context_path(ep.root)
    if _uc_path:
        ctx = json.loads(_uc_path.read_text())
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
