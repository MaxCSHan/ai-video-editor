"""Phase 2 — Editorial assembly (Story Mode + Timeline Mode).

Extracted from editorial_agent.py to reduce god-module complexity.
Assembles per-clip reviews into a unified EditorialStoryboard via
multi-call split pipeline (2A reasoning → 2A.5 structuring → 2B assembly).
"""

import json
import os
from pathlib import Path

from .config import (
    ClaudeConfig,
    EditorialProjectPaths,
    GeminiConfig,
    ReviewConfig,
)
from .domain.clip_resolution import resolve_clip_id_refs
from .domain.exceptions import FileUploadError
from .editorial_prompts import build_editorial_assembly_prompt
from .domain.timestamps import clamp_segments_to_usable
from .domain.validation import validate_storyboard
from .file_cache import (
    cache_file_uri,
    get_cached_uri,
    load_file_api_cache,
)
from .infra.gemini_client import GeminiClient
from .versioning import (
    begin_version,
    commit_version,
    versioned_path,
    versioned_dir,
    update_latest_symlink,
)


def _load_transcript_for_prompt(clip_paths):
    """Import helper from editorial_agent to avoid circular dependency."""
    from .editorial_agent import _load_transcript_for_prompt as _helper

    return _helper(clip_paths)


def _load_all_transcripts_for_prompt(editorial_paths, clip_ids):
    """Import helper from editorial_agent to avoid circular dependency."""
    from .editorial_agent import _load_all_transcripts_for_prompt as _helper

    return _helper(editorial_paths, clip_ids)


# Phase 2 visual mode now uses concat_proxies() from preprocess.py instead of
# individual uploads. See run_phase2() below.


def _validate_constraints(
    storyboard,
    user_context: dict,
    client,
    model: str,
    tracer=None,
) -> str | None:
    """Run a cheap LLM validation call to check filmmaker constraint satisfaction.

    Returns the validation report text, or None if validation was skipped.
    Prints constraint violations to the console.
    """
    from .tracing import otel_phase_span, traced_gemini_generate

    constraints = []
    if user_context.get("highlights"):
        constraints.append(f"MUST INCLUDE: {user_context['highlights']}")
    if user_context.get("avoid"):
        constraints.append(f"MUST EXCLUDE: {user_context['avoid']}")
    if not constraints:
        return None

    # Build compact storyboard summary for the validator
    seg_lines = []
    for seg in storyboard.segments:
        seg_lines.append(
            f"  [{seg.index}] {seg.clip_id} {seg.in_sec:.1f}-{seg.out_sec:.1f}s "
            f"({seg.purpose}): {seg.description}"
        )
    seg_summary = "\n".join(seg_lines)

    prompt = (
        "You are reviewing a video storyboard against the filmmaker's constraints.\n\n"
        "CONSTRAINTS:\n"
        + "\n".join(f"  {i + 1}. {c}" for i, c in enumerate(constraints))
        + "\n\nSTORYBOARD SEGMENTS:\n"
        + seg_summary
        + "\n\nFor each constraint, answer:\n"
        "- SATISFIED: YES or NO\n"
        "- EVIDENCE: which segment(s) satisfy it, or what is missing\n"
        "- If NO: suggest a specific fix (which clip/segment to add or remove)\n\n"
        "Be concise. One paragraph per constraint."
    )

    try:
        from google.genai import types

        print(f"  [Validate] Checking constraint satisfaction ({model})...")
        with otel_phase_span("phase2_validation", stage="validation", provider="gemini"):
            response = traced_gemini_generate(
                client.raw,
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.1),
                phase="phase2_validation",
                tracer=tracer,
                prompt_chars=len(prompt),
            )
        report = response.text.strip()

        # Parse for violations and print them
        has_violation = False
        for line in report.split("\n"):
            line_lower = line.strip().lower()
            if "satisfied: no" in line_lower or "no" in line_lower and "must" in line_lower:
                has_violation = True
        if has_violation:
            print("  [Validate] ⚠ Constraint violation(s) detected:")
            for line in report.split("\n"):
                if line.strip():
                    print(f"    {line.strip()}")
        else:
            print("  [Validate] ✓ All constraints satisfied")

        return report
    except Exception as e:
        print(f"  [Validate] Skipped — validation call failed: {e}")
        return None


def _run_phase2_sections(
    clip_reviews: list[dict],
    editorial_paths: EditorialProjectPaths,
    project_name: str,
    provider: str,
    gemini_cfg: GeminiConfig | None = None,
    claude_cfg: ClaudeConfig | None = None,
    style: str = "vlog",
    user_context: dict | None = None,
    tracer=None,
    visual: bool = False,
    style_supplement: str | None = None,
    review_config: "ReviewConfig | None" = None,
    interactive: bool = False,
) -> Path:
    """Section-based Phase 2: Group → Storyline → Hook → Per-Section → Merge.

    Divide & Conquer pipeline that enforces chronological section order
    while allowing aesthetic freedom within each section.
    """
    from google.genai import types

    from .briefing import format_brief_for_prompt
    from .editorial_prompts import (
        build_hook_prompt,
        build_narrative_planner_prompt,
        build_scene_planner_prompt,
        build_section_storyboard_prompt,
        extract_cast_from_reviews,
    )
    from .models import (
        HookStoryboard,
        ScenePlan,
        SectionPlan,
        SectionStoryboard,
    )
    from .render import render_html_preview, render_markdown
    from .section_grouping import (
        build_section_groups_from_scene_plan,
        format_sections_for_display,
        group_clips_by_date,
        merge_section_storyboards,
    )
    from .tracing import LLMSpinner, otel_phase_span, traced_gemini_generate
    from .versioning import (
        begin_version,
        commit_version,
        current_version,
        update_latest_symlink,
        versioned_dir,
        versioned_path,
    )

    client = GeminiClient.from_env()
    gemini_cfg = gemini_cfg or GeminiConfig()

    # Format brief WITHOUT constraints for section calls (constraints distributed by storyline)
    section_context_text = (
        format_brief_for_prompt(user_context, phase="phase2", skip_constraints=True)
        if user_context
        else None
    )
    # Full brief WITH constraints for the storyline planner
    full_context_text = (
        format_brief_for_prompt(user_context, phase="phase2") if user_context else None
    )

    # Extract highlights/avoid for structured constraint distribution
    _highlights = ""
    _avoid = ""
    if user_context:
        if hasattr(user_context, "highlights"):
            _highlights = user_context.highlights or ""
            _avoid = user_context.avoid or ""
        elif isinstance(user_context, dict):
            _highlights = user_context.get("highlights", "")
            _avoid = user_context.get("avoid", "")

    # ── Date grouping (deterministic — the only hard boundary) ─────────────
    print("  [Dates] Grouping clips by date...")
    manifest_file = editorial_paths.master_manifest
    if not manifest_file.exists():
        raise RuntimeError(f"No manifest found: {manifest_file}")
    manifest_data = json.loads(manifest_file.read_text())

    date_groups = group_clips_by_date(manifest_data, clip_reviews)
    total_clips = sum(len(cid) for g in date_groups for s in g.sections for cid in [s.clip_ids])
    print(f"  [Dates] {len(date_groups)} days, {total_clips} clips")

    editorial_paths.storyboard.mkdir(parents=True, exist_ok=True)

    # ── Pre-processing ────────────────────────────────────────────────────
    cast = extract_cast_from_reviews(clip_reviews)
    reviews_by_id = {r.get("clip_id", ""): r for r in clip_reviews}
    all_transcripts = _load_all_transcripts_for_prompt(clip_reviews, editorial_paths)

    # ── Scene Planner LLM call (groups clips into scenes within each date) ──
    print("  [Scene Planner] Identifying scenes within each date...")
    scene_prompt = build_scene_planner_prompt(
        date_groups=date_groups,
        clip_reviews=clip_reviews,
    )

    with LLMSpinner("Scene planning", provider=provider):
        with otel_phase_span(
            "phase2_scene_planner", stage="storyboard", provider="gemini", call="scene"
        ):
            response_scene = traced_gemini_generate(
                client.raw,
                model=gemini_cfg.phase2,
                contents=scene_prompt,
                config=types.GenerateContentConfig(
                    temperature=gemini_cfg.phase2_temperature,
                    response_mime_type="application/json",
                    response_schema=ScenePlan,
                ),
                phase="phase2_scene_planner",
                tracer=tracer,
                prompt_chars=len(scene_prompt),
            )
    scene_plan = ScenePlan.model_validate_json(response_scene.text)
    section_groups = build_section_groups_from_scene_plan(date_groups, scene_plan)

    total_sections = sum(len(g.sections) for g in section_groups)
    print(f"  [Scene Planner] {total_sections} scenes identified")
    print(format_sections_for_display(section_groups, clip_reviews))

    # Save scene plan artifact
    scene_plan_path = editorial_paths.storyboard / "scene_plan_latest.json"
    scene_plan_path.write_text(scene_plan.model_dump_json(indent=2))

    # ── HITL 1: Review scene grouping ─────────────────────────────────────
    if interactive:
        try:
            import questionary

            if not questionary.confirm("Proceed with these scenes?", default=True).ask():
                print(
                    "  Scene grouping not approved."
                    " Edit sections and re-run: vx sections <project> --regroup"
                )
                # Save what we have so user can edit, then exit
                sections_json = [g.model_dump() for g in section_groups]
                sections_path = editorial_paths.storyboard / "sections_latest.json"
                sections_path.write_text(json.dumps(sections_json, indent=2))
                return sections_path
        except (ImportError, EOFError):
            pass  # Non-interactive environment

    # Save confirmed sections
    sections_json = [g.model_dump() for g in section_groups]
    sections_path = editorial_paths.storyboard / "sections_latest.json"
    sections_path.write_text(json.dumps(sections_json, indent=2))

    # ── Narrative Planner LLM call (arc + constraint distribution) ────────
    print(f"  [Narrative] Planning story arc across {total_sections} scenes...")
    narrative_prompt = build_narrative_planner_prompt(
        section_groups=section_groups,
        clip_reviews=clip_reviews,
        user_context_text=full_context_text,
        style=style,
        cast=cast,
        highlights=_highlights,
        avoid=_avoid,
    )

    with LLMSpinner("Narrative planning", provider=provider):
        with otel_phase_span(
            "phase2_narrative_planner", stage="storyboard", provider="gemini", call="narrative"
        ):
            response_narr = traced_gemini_generate(
                client.raw,
                model=gemini_cfg.phase2,
                contents=narrative_prompt,
                config=types.GenerateContentConfig(
                    temperature=gemini_cfg.phase2_temperature,
                    response_mime_type="application/json",
                    response_schema=SectionPlan,
                ),
                phase="phase2_narrative_planner",
                tracer=tracer,
                prompt_chars=len(narrative_prompt),
            )
    section_plan = SectionPlan.model_validate_json(response_narr.text)
    print(f'  [Narrative] "{section_plan.title}" — hook from {section_plan.hook_section_id}')

    # Display narrative plan
    for sn in section_plan.section_narratives:
        goal_str = f" — {sn.section_goal[:60]}" if sn.section_goal else ""
        constraints_str = f" [must: {', '.join(sn.must_include)}]" if sn.must_include else ""
        print(f"    {sn.section_id}: {sn.arc_phase}, {sn.energy}{goal_str}{constraints_str}")

    # ── HITL 2: Review narrative plan ─────────────────────────────────────
    if interactive:
        try:
            import questionary

            if not questionary.confirm("Proceed with this narrative plan?", default=True).ask():
                print("  Narrative plan not approved. Adjust briefing and re-run.")
                return editorial_paths.storyboard / "sections_latest.json"
        except (ImportError, EOFError):
            pass

    # Save narrative plan artifact
    storyline_path = editorial_paths.storyboard / "storyline_latest.json"
    storyline_path.write_text(section_plan.model_dump_json(indent=2))

    # ── Opening Hook LLM call ─────────────────────────────────────────────
    print("  [Hook] Creating opening hook from best clips...")
    high_value_clips = [
        r
        for r in clip_reviews
        if any(km.get("editorial_value") == "high" for km in r.get("key_moments", []))
    ]
    if not high_value_clips:
        high_value_clips = clip_reviews[:5]  # fallback: first 5 clips

    hook_prompt = build_hook_prompt(
        high_value_clips=high_value_clips,
        section_plan=section_plan,
        cast=cast,
        style=style,
    )

    with LLMSpinner("Opening hook", provider=provider):
        with otel_phase_span("phase2_hook", stage="storyboard", provider="gemini", call="hook"):
            response_hook = traced_gemini_generate(
                client.raw,
                model=gemini_cfg.phase2b,
                contents=hook_prompt,
                config=types.GenerateContentConfig(
                    temperature=gemini_cfg.phase2b_temperature,
                    response_mime_type="application/json",
                    response_schema=HookStoryboard,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                    max_output_tokens=65536,
                ),
                phase="phase2_hook",
                tracer=tracer,
                prompt_chars=len(hook_prompt),
            )
    hook_sb = HookStoryboard.model_validate_json(response_hook.text)
    hook_dur = sum(s.duration_sec for s in hook_sb.segments)
    print(f"  [Hook] {len(hook_sb.segments)} segments, {hook_dur:.1f}s")

    # ── Per-section storyboarding (sequential) ────────────────────────────
    narrative_by_id = {sn.section_id: sn for sn in section_plan.section_narratives}
    cumulative_narratives: list[tuple[str, str]] = []
    section_storyboards: list[SectionStoryboard] = []

    flat_sections = [(group, section) for group in section_groups for section in group.sections]

    for idx, (group, section) in enumerate(flat_sections, 1):
        narrative = narrative_by_id.get(section.section_id)
        section_reviews = [reviews_by_id[cid] for cid in section.clip_ids if cid in reviews_by_id]

        # Prepare section transcripts
        section_transcripts = None
        if all_transcripts:
            section_transcripts = {
                cid: all_transcripts[cid] for cid in section.clip_ids if cid in all_transcripts
            }

        prompt = build_section_storyboard_prompt(
            section=section,
            section_clip_reviews=section_reviews,
            section_narrative=narrative,
            transcripts=section_transcripts,
            cumulative_narratives=cumulative_narratives if cumulative_narratives else None,
            cast=cast,
            style=style,
            user_context_text=section_context_text,  # constraints-free, goals come from narrative
            style_supplement=style_supplement,
        )

        label = section.label or section.section_id
        print(f"  [Section {idx}/{len(flat_sections)}] {label}...")

        with LLMSpinner(f"Section: {label}", provider=provider):
            with otel_phase_span(
                f"phase2_section_{section.section_id}",
                stage="storyboard",
                provider="gemini",
                call=f"section_{idx}",
            ):
                response_sec = traced_gemini_generate(
                    client.raw,
                    model=gemini_cfg.phase2b,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=gemini_cfg.phase2b_temperature,
                        response_mime_type="application/json",
                        response_schema=SectionStoryboard,
                        thinking_config=types.ThinkingConfig(thinking_budget=0),
                        max_output_tokens=65536,
                    ),
                    phase=f"phase2_section_{section.section_id}",
                    tracer=tracer,
                    prompt_chars=len(prompt),
                )

        ssb = SectionStoryboard.model_validate_json(response_sec.text)
        section_storyboards.append(ssb)
        cumulative_narratives.append((section.section_id, ssb.narrative_summary))

        seg_dur = sum(s.duration_sec for s in ssb.segments)
        print(f"    ✓ {len(ssb.segments)} segments, {seg_dur:.1f}s")

    # ── Programmatic merge ────────────────────────────────────────────────
    print("  [Merge] Combining sections into final storyboard...")
    storyboard = merge_section_storyboards(
        hook=hook_sb,
        section_storyboards=section_storyboards,
        section_plan=section_plan,
        section_groups=section_groups,
    )

    total_dur = sum(s.duration_sec for s in storyboard.segments)
    print(f"  [Merge] {len(storyboard.segments)} segments, {total_dur:.1f}s total")

    # ── Resolve clip IDs and validate ─────────────────────────────────────
    known_clip_ids = {r["clip_id"] for r in clip_reviews}
    resolve_clip_id_refs(storyboard, known_clip_ids)

    # Auto-clamp timestamps
    fix_log = clamp_segments_to_usable(storyboard, reviews_by_id)

    val_warnings, val_critical = validate_storyboard(storyboard, clip_reviews)
    if fix_log:
        print(f"  [Fix] Auto-clamped {len(fix_log)} timestamps:")
        for f in fix_log[:5]:
            print(f"    {f}")
    if val_warnings:
        for w in val_warnings[:5]:
            print(f"  WARN: {w}")

    # ── Editorial Director review ─────────────────────────────────────────
    if review_config and review_config.enabled:
        from .editorial_director import run_editorial_review
        from .review_display import print_turn

        print(
            f"  [Director] Starting editorial review ({review_config.model}, "
            f"up to {review_config.max_turns} turns, "
            f"{review_config.wall_clock_timeout_sec:.0f}s timeout)..."
        )
        storyboard, _review_log = run_editorial_review(
            storyboard=storyboard,
            clip_reviews=clip_reviews,
            user_context=user_context,
            clips_dir=editorial_paths.clips_dir,
            review_config=review_config,
            tracer=tracer,
            interactive=interactive,
            turn_callback=print_turn,
            style_guidelines=style_supplement,
        )
        print(
            f"  [Director] Review complete: {_review_log.convergence_reason} "
            f"({_review_log.total_turns} turns, {_review_log.total_fixes} fixes, "
            f"${_review_log.total_cost_usd:.3f}, {_review_log.total_duration_sec:.1f}s)"
        )

    # ── Version and save outputs ──────────────────────────────────────────
    editorial_paths.storyboard.mkdir(parents=True, exist_ok=True)

    review_inputs = {}
    for r in clip_reviews:
        cid = r.get("clip_id", "")
        review_inputs[f"review:{cid}"] = cid
    from .versioning import resolve_user_context_path as _rucp_sec

    _uc_sec = _rucp_sec(editorial_paths.root)
    if _uc_sec and "_v" in _uc_sec.name:
        import re as _re_sec

        _um_sec = _re_sec.search(r"_v(\d+)\.", _uc_sec.name)
        if _um_sec:
            review_inputs["user_context"] = f"user_context:user:v{_um_sec.group(1)}"
    cfg_snap = {
        "model_scene": gemini_cfg.phase2,
        "model_narrative": gemini_cfg.phase2,
        "model_hook": gemini_cfg.phase2b,
        "model_section": gemini_cfg.phase2b,
        "pipeline": "sections",
        "temperature": gemini_cfg.phase2_temperature,
    }

    rv_version = current_version(editorial_paths.root, f"review_{provider}")
    if rv_version == 0:
        rv_version = current_version(editorial_paths.root, "review")
    review_parent_id = f"rv.{rv_version}" if rv_version > 0 else None

    art_meta = begin_version(
        editorial_paths.root,
        phase="storyboard",
        provider=provider,
        inputs=review_inputs,
        config_snapshot=cfg_snap,
        target_dir=editorial_paths.storyboard,
        parent_id=review_parent_id,
    )
    v = art_meta.version
    base = f"editorial_{provider}"

    # Primary: structured JSON
    json_path = versioned_path(editorial_paths.storyboard / f"{base}.json", v)
    json_path.write_text(storyboard.model_dump_json(indent=2))
    update_latest_symlink(json_path)

    # Rendered: markdown
    md_path = versioned_path(editorial_paths.storyboard / f"{base}.md", v)
    md_path.write_text(render_markdown(storyboard))
    update_latest_symlink(md_path)

    # Rendered: HTML preview
    export_dir = versioned_dir(editorial_paths.exports, v)
    html = render_html_preview(
        storyboard,
        clips_dir=editorial_paths.clips_dir,
        output_dir=export_dir,
    )
    preview_path = export_dir / "preview.html"
    preview_path.write_text(html)
    update_latest_symlink(export_dir)

    # Save fix log
    if fix_log:
        fix_path = editorial_paths.storyboard / f"fixlog_{provider}_v{v}.txt"
        fix_path.write_text("\n".join(fix_log))

    commit_version(
        editorial_paths.root,
        art_meta,
        output_paths=[json_path, md_path, preview_path],
        target_dir=editorial_paths.storyboard,
    )

    print(f"  v{v} outputs (section pipeline):")
    print(f"    Sections:  {sections_path}")
    print(f"    Storyline: {storyline_path}")
    print(f"    JSON:      {json_path}")
    print(f"    MD:        {md_path}")
    print(f"    Preview:   {preview_path}")
    return json_path


def _run_phase2_split(
    clip_reviews: list[dict],
    editorial_paths: EditorialProjectPaths,
    project_name: str,
    provider: str,
    gemini_cfg: GeminiConfig | None = None,
    claude_cfg: ClaudeConfig | None = None,
    style: str = "vlog",
    user_context: dict | None = None,
    tracer=None,
    visual: bool = False,
    style_supplement: str | None = None,
    review_config: "ReviewConfig | None" = None,
    interactive: bool = False,
) -> Path:
    """Multi-call Phase 2: Reasoning → Structuring → Assembly → Validation.

    Call 2A:   Freeform editorial reasoning (no schema, high temp)
    Call 2A.5: Faithful structuring into StoryPlan JSON (cheap model)
    Call 2B:   Precise timestamp assembly within bounded windows (low temp)
    """
    from .models import EditorialStoryboard, StoryPlan
    from .render import render_markdown, render_html_preview
    from .briefing import format_brief_for_prompt
    from .editorial_prompts import (
        extract_cast_from_reviews,
        condense_clip_for_planning,
        trim_transcript_to_usable,
        build_phase2a_reasoning_prompt,
        build_phase2a_structuring_prompt,
        build_phase2b_assembly_prompt,
    )
    from .tracing import otel_phase_span, traced_gemini_generate, LLMSpinner

    total_duration = sum(
        sum(seg.get("duration_sec", 0) for seg in r.get("usable_segments", []))
        for r in clip_reviews
    )
    if total_duration == 0:
        total_duration = sum(r.get("duration_sec", 0) for r in clip_reviews if "duration_sec" in r)

    # Sort clip reviews chronologically
    filming_timeline = None
    manifest_file = editorial_paths.master_manifest
    if manifest_file.exists():
        manifest_data = json.loads(manifest_file.read_text())
        creation_times = {
            c["clip_id"]: c.get("creation_time") for c in manifest_data.get("clips", [])
        }
        clip_reviews.sort(
            key=lambda r: (creation_times.get(r.get("clip_id"), "") or "", r.get("clip_id", ""))
        )
        timeline_lines = []
        for i, r in enumerate(clip_reviews, 1):
            cid = r.get("clip_id", "unknown")
            ct = creation_times.get(cid)
            timeline_lines.append(f"  {i}. {cid} — filmed {ct}" if ct else f"  {i}. {cid}")
        filming_timeline = "\n".join(timeline_lines)

    # ── Pre-processing (deterministic) ──────────────────────────────────────
    print("  [2A] Pre-processing: deduplicating cast, condensing clips...")
    cast = extract_cast_from_reviews(clip_reviews)

    # Enable editorial hints when creative brief has explicit narrative beats
    _has_key_beats = False
    if user_context:
        from .models import CreativeBrief

        if isinstance(user_context, CreativeBrief):
            _has_key_beats = bool(user_context.narrative and user_context.narrative.key_beats)
        elif isinstance(user_context, dict):
            _narrative = user_context.get("narrative")
            if isinstance(_narrative, dict):
                _has_key_beats = bool(_narrative.get("key_beats"))
    condensed = [
        condense_clip_for_planning(r, include_editorial_hints=_has_key_beats) for r in clip_reviews
    ]

    all_transcripts = _load_all_transcripts_for_prompt(clip_reviews, editorial_paths)
    trimmed_transcripts = None
    if all_transcripts:
        trimmed_transcripts = {}
        for r in clip_reviews:
            cid = r.get("clip_id", "")
            if cid in all_transcripts:
                trimmed_transcripts[cid] = trim_transcript_to_usable(
                    all_transcripts[cid],
                    r.get("usable_segments", []),
                )

    user_context_text = (
        format_brief_for_prompt(user_context, phase="phase2") if user_context else None
    )

    # ── Call 2A: Freeform editorial reasoning ───────────────────────────────
    reasoning_prompt = build_phase2a_reasoning_prompt(
        clip_reviews=clip_reviews,
        style=style,
        total_duration_sec=total_duration,
        cast=cast,
        condensed_clips=condensed,
        transcripts=trimmed_transcripts,
        filming_timeline=filming_timeline,
        user_context_text=user_context_text,
        user_context=user_context,
        style_supplement=style_supplement,
    )

    clip_ids = [c["clip_id"] for c in condensed]

    if provider == "gemini":
        from google.genai import types

        client = GeminiClient.from_env()

        # Visual mode: attach proxy videos to Call 2A
        # Use all proxy clips from disk (same set as briefing/quick scan) so that
        # concat bundle composition matches and Gemini File API URIs get cache hits.
        video_parts = []
        if visual:
            from .preprocess import concat_proxies

            all_clip_ids = sorted(
                d.name
                for d in editorial_paths.clips_dir.iterdir()
                if d.is_dir() and (d / "proxy").exists()
            )
            bundles = concat_proxies(editorial_paths, all_clip_ids)
            if bundles:
                file_cache = load_file_api_cache(editorial_paths)
                cached_count = 0
                for i, bundle in enumerate(bundles):
                    cache_key = f"_concat_bundle_{i}"
                    cached_uri = get_cached_uri(file_cache, cache_key)
                    if cached_uri:
                        video_parts.append(
                            types.Part.from_uri(file_uri=cached_uri, mime_type="video/mp4")
                        )
                        cached_count += 1
                        continue
                    print(f"  Uploading concat bundle {i + 1}/{len(bundles)}...")
                    try:
                        video_file = client.upload_and_wait(
                            Path(bundle["path"]), label=f"bundle_{i + 1}"
                        )
                    except FileUploadError:
                        print(f"  WARNING: bundle {i + 1} upload failed")
                        continue
                    cache_file_uri(editorial_paths, cache_key, video_file.uri)
                    video_parts.append(
                        types.Part.from_uri(file_uri=video_file.uri, mime_type="video/mp4")
                    )
                if cached_count:
                    print(
                        f"  Concat cached: {cached_count}/{len(bundles)} bundle(s)"
                        " reused from Gemini"
                    )

        mode_label = "visual" if video_parts else "text-only"
        print(f"  [2A] Generating editorial plan ({provider}, {mode_label}, freeform)...")

        if video_parts:
            contents_2a = [
                types.Content(parts=[*video_parts, types.Part.from_text(text=reasoning_prompt)])
            ]
        else:
            contents_2a = reasoning_prompt

        with LLMSpinner("Editorial reasoning (Call 2A)", provider=provider):
            with otel_phase_span(
                "phase2a_reasoning", stage="storyboard", provider="gemini", call="2A"
            ):
                response_2a = traced_gemini_generate(
                    client.raw,
                    model=gemini_cfg.phase2,
                    contents=contents_2a,
                    config=types.GenerateContentConfig(
                        temperature=gemini_cfg.phase2_temperature,
                        # No response_schema — freeform text output
                    ),
                    phase="phase2a_reasoning",
                    tracer=tracer,
                    prompt_chars=len(reasoning_prompt),
                    num_video_files=len(video_parts),
                )
        editorial_plan_text = response_2a.text
        print(f"  [2A] Editorial plan: {len(editorial_plan_text)} chars")

        # ── Call 2A.5: Structuring (cheap model) ───────────────────────────
        print(f"  [2A.5] Structuring plan into StoryPlan JSON ({gemini_cfg.structuring_model})...")
        structuring_prompt = build_phase2a_structuring_prompt(
            editorial_plan_text=editorial_plan_text,
            clip_ids=clip_ids,
        )

        with LLMSpinner("Plan structuring (Call 2A.5)", provider=provider):
            with otel_phase_span(
                "phase2a_structuring", stage="storyboard", provider="gemini", call="2A.5"
            ):
                response_2a5 = traced_gemini_generate(
                    client.raw,
                    model=gemini_cfg.structuring_model,
                    contents=structuring_prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.2,
                        response_mime_type="application/json",
                        response_schema=StoryPlan,
                        thinking_config=types.ThinkingConfig(thinking_budget=0),
                        max_output_tokens=65536,
                    ),
                    phase="phase2a_structuring",
                    tracer=tracer,
                    prompt_chars=len(structuring_prompt),
                )
        story_plan = StoryPlan.model_validate_json(response_2a5.text)

        # ── Checkpoint: validate plan ──────────────────────────────────────
        plan_issues = []
        known_ids = {r["clip_id"] for r in clip_reviews}
        for ps in story_plan.planned_segments:
            if ps.clip_id not in known_ids:
                plan_issues.append(f"Unknown clip_id in plan: {ps.clip_id}")
            else:
                review = next((r for r in clip_reviews if r["clip_id"] == ps.clip_id), None)
                if review:
                    n_segs = len(review.get("usable_segments", []))
                    if ps.usable_segment_index >= n_segs:
                        plan_issues.append(
                            f"Segment index {ps.usable_segment_index} out of range "
                            f"for {ps.clip_id} (has {n_segs} usable segments)"
                        )
        if plan_issues:
            for issue in plan_issues:
                print(f"  PLAN WARN: {issue}")

        # ── Call 2B: Precise assembly ──────────────────────────────────────
        selected_ids = {ps.clip_id for ps in story_plan.planned_segments}
        selected_reviews = [r for r in clip_reviews if r["clip_id"] in selected_ids]
        selected_transcripts = (
            {k: v for k, v in all_transcripts.items() if k in selected_ids}
            if all_transcripts
            else None
        )

        assembly_prompt = build_phase2b_assembly_prompt(
            story_plan=story_plan,
            clip_reviews=selected_reviews,
            transcripts=selected_transcripts,
            style=style,
        )

        print(
            f"  [2B] Assembling storyboard ({len(selected_reviews)} clips, "
            f"{len(story_plan.planned_segments)} segments)..."
        )
        with LLMSpinner("Precise assembly (Call 2B)", provider=provider):
            with otel_phase_span(
                "phase2b_assembly", stage="storyboard", provider="gemini", call="2B"
            ):
                response_2b = traced_gemini_generate(
                    client.raw,
                    model=gemini_cfg.phase2b,
                    contents=assembly_prompt,
                    config=types.GenerateContentConfig(
                        temperature=gemini_cfg.phase2b_temperature,
                        response_mime_type="application/json",
                        response_schema=EditorialStoryboard,
                        thinking_config=types.ThinkingConfig(thinking_budget=0),
                        max_output_tokens=65536,
                    ),
                    phase="phase2b_assembly",
                    tracer=tracer,
                    prompt_chars=len(assembly_prompt),
                )
        storyboard = EditorialStoryboard.model_validate_json(response_2b.text)

    else:
        raise ValueError(
            f"Split pipeline not yet implemented for provider: {provider}. "
            "Use provider='gemini' or set use_split_pipeline=False."
        )

    # ── Validate & fix ─────────────────────────────────────────────────────
    known_clip_ids = {r["clip_id"] for r in clip_reviews}
    resolve_clip_id_refs(storyboard, known_clip_ids)

    # Clamp timestamps to usable segment bounds
    reviews_by_id = {r["clip_id"]: r for r in clip_reviews}
    fix_log = clamp_segments_to_usable(storyboard, reviews_by_id)

    val_warnings, val_critical = validate_storyboard(storyboard, clip_reviews)
    if fix_log:
        print(f"  [Fix] Auto-clamped {len(fix_log)} timestamps:")
        for f in fix_log[:5]:
            print(f"    {f}")
    if val_warnings:
        for w in val_warnings[:5]:
            print(f"  WARN: {w}")
    if tracer and tracer.traces:
        tracer.traces[-1].validation_warnings = val_warnings + fix_log

    # ── Constraint validation call (cheap LLM check) ──────────────────────
    constraint_report = None
    if user_context and (user_context.get("highlights") or user_context.get("avoid")):
        constraint_report = _validate_constraints(
            storyboard=storyboard,
            user_context=user_context,
            client=client,
            model=gemini_cfg.structuring_model,
            tracer=tracer,
        )

    # ── Editorial Director review (enabled by default) ────────────────────
    if review_config and review_config.enabled:
        from .editorial_director import run_editorial_review

        print(
            f"  [Director] Starting editorial review ({review_config.model}, "
            f"up to {review_config.max_turns} turns, "
            f"{review_config.wall_clock_timeout_sec:.0f}s timeout)..."
        )
        storyboard, _review_log = run_editorial_review(
            storyboard=storyboard,
            clip_reviews=clip_reviews,
            user_context=user_context,
            clips_dir=editorial_paths.clips_dir,
            review_config=review_config,
            tracer=tracer,
            interactive=interactive,
            style_guidelines=style_supplement,
        )
        print(
            f"  [Director] Review complete: {_review_log.convergence_reason} "
            f"({_review_log.total_turns} turns, {_review_log.total_fixes} fixes, "
            f"${_review_log.total_cost_usd:.3f}, {_review_log.total_duration_sec:.1f}s)"
        )

    # ── Version and save outputs ───────────────────────────────────────────
    editorial_paths.storyboard.mkdir(parents=True, exist_ok=True)

    review_inputs = {}
    for r in clip_reviews:
        cid = r.get("clip_id", "")
        review_inputs[f"review:{cid}"] = cid
    from .versioning import resolve_user_context_path as _rucp2

    _uc2 = _rucp2(editorial_paths.root)
    if _uc2 and "_v" in _uc2.name:
        import re as _re3

        _um2 = _re3.search(r"_v(\d+)\.", _uc2.name)
        if _um2:
            review_inputs["user_context"] = f"user_context:user:v{_um2.group(1)}"
    cfg_snap = {
        "model_2a": gemini_cfg.phase2,
        "model_2a5": gemini_cfg.structuring_model,
        "model_2b": gemini_cfg.phase2b,
        "temperature": gemini_cfg.phase2_temperature,
    }

    from .versioning import current_version

    rv_version = current_version(editorial_paths.root, f"review_{provider}")
    if rv_version == 0:
        rv_version = current_version(editorial_paths.root, "review")
    review_parent_id = f"rv.{rv_version}" if rv_version > 0 else None

    art_meta = begin_version(
        editorial_paths.root,
        phase="storyboard",
        provider=provider,
        inputs=review_inputs,
        config_snapshot=cfg_snap,
        target_dir=editorial_paths.storyboard,
        parent_id=review_parent_id,
    )
    v = art_meta.version
    base = f"editorial_{provider}"

    # Save Call 2A freeform plan (human-readable debug artifact)
    plan_txt_path = editorial_paths.storyboard / f"editorial_plan_{provider}_v{v}.txt"
    plan_txt_path.write_text(editorial_plan_text)

    # Save StoryPlan intermediate
    plan_json_path = editorial_paths.storyboard / f"storyplan_{provider}_v{v}.json"
    plan_json_path.write_text(story_plan.model_dump_json(indent=2))

    # Save fix log if any
    if fix_log:
        fix_path = editorial_paths.storyboard / f"fixlog_{provider}_v{v}.txt"
        fix_path.write_text("\n".join(fix_log))

    # Save constraint validation report
    if constraint_report:
        report_path = editorial_paths.storyboard / f"constraint_check_{provider}_v{v}.txt"
        report_path.write_text(constraint_report)

    # Primary: structured JSON
    json_path = versioned_path(editorial_paths.storyboard / f"{base}.json", v)
    json_path.write_text(storyboard.model_dump_json(indent=2))
    update_latest_symlink(json_path)

    # Rendered: markdown
    md_path = versioned_path(editorial_paths.storyboard / f"{base}.md", v)
    md_path.write_text(render_markdown(storyboard))
    update_latest_symlink(md_path)

    # Rendered: HTML preview
    export_dir = versioned_dir(editorial_paths.exports, v)
    html = render_html_preview(
        storyboard,
        clips_dir=editorial_paths.clips_dir,
        output_dir=export_dir,
    )
    preview_path = export_dir / "preview.html"
    preview_path.write_text(html)
    update_latest_symlink(export_dir)

    commit_version(
        editorial_paths.root,
        art_meta,
        output_paths=[json_path, md_path, preview_path, plan_txt_path, plan_json_path],
        target_dir=editorial_paths.storyboard,
    )

    print(f"  v{v} outputs (split pipeline):")
    print(f"    Plan:      {plan_txt_path}")
    print(f"    StoryPlan: {plan_json_path}")
    print(f"    JSON:      {json_path}")
    print(f"    MD:        {md_path}")
    print(f"    Preview:   {preview_path}")
    return json_path


def run_phase2(
    clip_reviews: list[dict],
    editorial_paths: EditorialProjectPaths,
    project_name: str,
    provider: str,
    gemini_cfg: GeminiConfig | None = None,
    claude_cfg: ClaudeConfig | None = None,
    style: str = "vlog",
    user_context: dict | None = None,
    tracer=None,
    visual: bool = False,
    style_supplement: str | None = None,
    review_config: "ReviewConfig | None" = None,
    interactive: bool = False,
) -> Path:
    """Phase 2: produce structured EditorialStoryboard + render markdown and HTML preview.

    If gemini_cfg.use_split_pipeline is True, delegates to the multi-call pipeline
    (Call 2A reasoning → Call 2A.5 structuring → Call 2B assembly).
    """
    # Check for Timeline Mode (scene-by-scene chronological assembly)
    _timeline = gemini_cfg and gemini_cfg.use_timeline_mode and provider == "gemini"
    if not _timeline and user_context is not None:
        # Auto-detect from Creative Brief narrative structure
        _brief = user_context
        if hasattr(_brief, "narrative") and _brief.narrative:
            if getattr(_brief.narrative, "structure", "") == "chronological":
                _timeline = True
    if _timeline:
        return _run_phase2_sections(
            clip_reviews=clip_reviews,
            editorial_paths=editorial_paths,
            project_name=project_name,
            provider=provider,
            gemini_cfg=gemini_cfg,
            claude_cfg=claude_cfg,
            style=style,
            user_context=user_context,
            tracer=tracer,
            visual=visual,
            style_supplement=style_supplement,
            review_config=review_config,
            interactive=interactive,
        )

    # Check for split pipeline mode
    if gemini_cfg and gemini_cfg.use_split_pipeline and provider == "gemini":
        return _run_phase2_split(
            clip_reviews=clip_reviews,
            editorial_paths=editorial_paths,
            project_name=project_name,
            provider=provider,
            gemini_cfg=gemini_cfg,
            claude_cfg=claude_cfg,
            style=style,
            user_context=user_context,
            tracer=tracer,
            visual=visual,
            style_supplement=style_supplement,
            review_config=review_config,
            interactive=interactive,
        )

    from .models import EditorialStoryboard
    from .render import render_markdown, render_html_preview
    from .briefing import format_brief_for_prompt

    total_duration = sum(
        sum(seg.get("duration_sec", 0) for seg in r.get("usable_segments", []))
        for r in clip_reviews
    )
    if total_duration == 0:
        total_duration = sum(r.get("duration_sec", 0) for r in clip_reviews if "duration_sec" in r)

    # Sort clip reviews in chronological filming order
    filming_timeline = None
    manifest_file = editorial_paths.master_manifest
    if manifest_file.exists():
        manifest_data = json.loads(manifest_file.read_text())
        creation_times = {
            c["clip_id"]: c.get("creation_time") for c in manifest_data.get("clips", [])
        }
        clip_reviews.sort(
            key=lambda r: (creation_times.get(r.get("clip_id"), "") or "", r.get("clip_id", ""))
        )
        timeline_lines = []
        for i, r in enumerate(clip_reviews, 1):
            cid = r.get("clip_id", "unknown")
            ct = creation_times.get(cid)
            timeline_lines.append(f"  {i}. {cid} — filmed {ct}" if ct else f"  {i}. {cid}")
        filming_timeline = "\n".join(timeline_lines)

    # Load transcripts for all clips
    transcripts = _load_all_transcripts_for_prompt(clip_reviews, editorial_paths)

    # Resolve visual mode: concatenate proxies into bundles for Gemini.
    # Uses concat_proxies() to avoid the 10-video-per-prompt limit.
    # Use all proxy clips from disk (same set as briefing/quick scan) so that
    # concat bundle composition matches and Gemini File API URIs get cache hits.
    visual_timeline = None
    video_parts = []
    if visual and provider == "gemini":
        from google.genai import types
        from .preprocess import concat_proxies

        all_clip_ids = sorted(
            d.name
            for d in editorial_paths.clips_dir.iterdir()
            if d.is_dir() and (d / "proxy").exists()
        )
        bundles = concat_proxies(editorial_paths, all_clip_ids)

        if bundles:
            client = GeminiClient.from_env()
            file_cache = load_file_api_cache(editorial_paths)
            cached_count = 0

            for i, bundle in enumerate(bundles):
                cache_key = f"_concat_bundle_{i}"
                cached_uri = get_cached_uri(file_cache, cache_key)
                if cached_uri:
                    video_parts.append(
                        types.Part.from_uri(file_uri=cached_uri, mime_type="video/mp4")
                    )
                    cached_count += 1
                    continue

                print(f"  Uploading concat bundle {i + 1}/{len(bundles)}...")
                try:
                    video_file = client.upload_and_wait(
                        Path(bundle["path"]), label=f"bundle_{i + 1}"
                    )
                except FileUploadError:
                    print(f"  WARNING: bundle {i + 1} upload failed")
                    continue
                cache_file_uri(editorial_paths, cache_key, video_file.uri)
                video_parts.append(
                    types.Part.from_uri(file_uri=video_file.uri, mime_type="video/mp4")
                )

            if cached_count:
                print(
                    f"  Concat cached: {cached_count}/{len(bundles)} bundle(s) reused from Gemini"
                )
            visual_timeline = bundles

    user_context_text = (
        format_brief_for_prompt(user_context, phase="phase2") if user_context else None
    )

    prompt = build_editorial_assembly_prompt(
        project_name=project_name,
        clip_reviews=clip_reviews,
        style=style,
        clip_count=len(clip_reviews),
        total_duration_sec=total_duration,
        transcripts=transcripts,
        visual_timeline=visual_timeline,
        style_supplement=style_supplement,
        filming_timeline=filming_timeline,
        user_context_text=user_context_text,
    )

    p2_model = gemini_cfg.phase2 if gemini_cfg else None
    mode_label = "visual" if visual else "text-only"
    print(f"  Generating editorial storyboard ({provider}, {mode_label})...")

    if provider == "gemini":
        if not visual_timeline:
            from google.genai import types

            client = GeminiClient.from_env()

        from .tracing import otel_phase_span, traced_gemini_generate

        # Build contents: text-only or multipart with concat video bundles
        num_videos = len(video_parts)
        if visual_timeline and video_parts:
            contents = [types.Content(parts=[*video_parts, types.Part.from_text(text=prompt)])]
        else:
            contents = prompt

        with otel_phase_span("phase2", stage="storyboard", provider="gemini"):
            response = traced_gemini_generate(
                client.raw,
                model=p2_model,
                contents=contents,
                config=types.GenerateContentConfig(
                    temperature=gemini_cfg.phase2_temperature,
                    response_mime_type="application/json",
                    response_schema=EditorialStoryboard,
                    max_output_tokens=65536,
                ),
                phase="phase2",
                tracer=tracer,
                prompt_chars=len(prompt),
                num_video_files=num_videos,
            )
        storyboard = EditorialStoryboard.model_validate_json(response.text)

    elif provider == "claude":
        import anthropic

        from .tracing import otel_phase_span, traced_claude_generate

        anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
        if not anthropic_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set. Add it to your .env file.")
        client = anthropic.Anthropic(api_key=anthropic_key)
        with otel_phase_span("phase2", stage="storyboard", provider="claude"):
            response = traced_claude_generate(
                client,
                model=claude_cfg.model,
                messages=[
                    {
                        "role": "user",
                        "content": prompt
                        + "\n\nRespond ONLY with valid JSON matching the EditorialStoryboard schema.",
                    }
                ],
                max_tokens=claude_cfg.max_tokens * 2,
                temperature=claude_cfg.phase2_temperature,
                phase="phase2",
                tracer=tracer,
                prompt_chars=len(prompt),
            )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        storyboard = EditorialStoryboard.model_validate_json(text)
    else:
        raise ValueError(f"Unknown provider: {provider}")

    # Resolve abbreviated clip IDs (e.g., LLM returns "C0073" but clip_id is "20260330114125_C0073")
    known_clip_ids = {r["clip_id"] for r in clip_reviews}
    resolve_clip_id_refs(storyboard, known_clip_ids)

    # Validate storyboard quality
    val_warnings, val_critical = validate_storyboard(storyboard, clip_reviews)
    if val_warnings:
        for w in val_warnings[:5]:
            print(f"  WARN: {w}")
        if len(val_warnings) > 5:
            print(f"  ... and {len(val_warnings) - 5} more warnings")
    if tracer and tracer.traces:
        tracer.traces[-1].validation_warnings = val_warnings
    if val_critical:
        print("  Critical storyboard issues detected — consider re-running with --force")

    # Constraint validation (cheap LLM check — works for both single and split pipelines)
    if provider == "gemini" and user_context and gemini_cfg:
        if user_context.get("highlights") or user_context.get("avoid"):
            if not visual_timeline:
                from google.genai import types  # noqa: F811

                client = GeminiClient.from_env()
            _validate_constraints(
                storyboard=storyboard,
                user_context=user_context,
                client=client,
                model=gemini_cfg.structuring_model,
                tracer=tracer,
            )

    # Editorial Director review (enabled by default)
    if review_config and review_config.enabled:
        from .editorial_director import run_editorial_review

        print(
            f"  [Director] Starting editorial review ({review_config.model}, "
            f"up to {review_config.max_turns} turns, "
            f"{review_config.wall_clock_timeout_sec:.0f}s timeout)..."
        )
        storyboard, _review_log = run_editorial_review(
            storyboard=storyboard,
            clip_reviews=clip_reviews,
            user_context=user_context,
            clips_dir=editorial_paths.clips_dir,
            review_config=review_config,
            tracer=tracer,
            interactive=interactive,
            style_guidelines=style_supplement,
        )
        print(
            f"  [Director] Review complete: {_review_log.convergence_reason} "
            f"({_review_log.total_turns} turns, {_review_log.total_fixes} fixes, "
            f"${_review_log.total_cost_usd:.3f}, {_review_log.total_duration_sec:.1f}s)"
        )

    # Version and save outputs
    editorial_paths.storyboard.mkdir(parents=True, exist_ok=True)

    # Build lineage: record which review versions and user context were used
    review_inputs = {}
    for r in clip_reviews:
        cid = r.get("clip_id", "")
        review_inputs[f"review:{cid}"] = cid
    # Add user_context lineage
    from .versioning import resolve_user_context_path as _rucp2

    _uc2 = _rucp2(editorial_paths.root)
    if _uc2 and "_v" in _uc2.name:
        import re as _re3

        _um2 = _re3.search(r"_v(\d+)\.", _uc2.name)
        if _um2:
            review_inputs["user_context"] = f"user_context:user:v{_um2.group(1)}"
    cfg_snap = {}
    if gemini_cfg:
        cfg_snap = {"model": gemini_cfg.phase2, "temperature": gemini_cfg.temperature}
    elif claude_cfg:
        cfg_snap = {"model": claude_cfg.model, "temperature": claude_cfg.temperature}

    # Determine parent_id from review version for lineage-prefixed versioning
    from .versioning import current_version

    rv_version = current_version(editorial_paths.root, f"review_{provider}")
    if rv_version == 0:
        rv_version = current_version(editorial_paths.root, "review")
    review_parent_id = f"rv.{rv_version}" if rv_version > 0 else None

    art_meta = begin_version(
        editorial_paths.root,
        phase="storyboard",
        provider=provider,
        inputs=review_inputs,
        config_snapshot=cfg_snap,
        target_dir=editorial_paths.storyboard,
        parent_id=review_parent_id,
    )
    v = art_meta.version
    base = f"editorial_{provider}"

    # 1. Primary: structured JSON
    json_path = versioned_path(editorial_paths.storyboard / f"{base}.json", v)
    json_path.write_text(storyboard.model_dump_json(indent=2))
    update_latest_symlink(json_path)

    # 2. Rendered: markdown
    md_path = versioned_path(editorial_paths.storyboard / f"{base}.md", v)
    md_path.write_text(render_markdown(storyboard))
    update_latest_symlink(md_path)

    # 3. Rendered: HTML preview (in versioned exports dir)
    export_dir = versioned_dir(editorial_paths.exports, v)
    html = render_html_preview(
        storyboard,
        clips_dir=editorial_paths.clips_dir,
        output_dir=export_dir,
    )
    preview_path = export_dir / "preview.html"
    preview_path.write_text(html)
    update_latest_symlink(export_dir)

    commit_version(
        editorial_paths.root,
        art_meta,
        output_paths=[json_path, md_path, preview_path],
        target_dir=editorial_paths.storyboard,
    )

    print(f"  v{v} outputs:")
    print(f"    JSON:    {json_path}")
    print(f"    MD:      {md_path}")
    print(f"    Preview: {preview_path}")
    return json_path
