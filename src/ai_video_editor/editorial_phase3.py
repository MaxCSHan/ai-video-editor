"""Phase 3 — Visual monologue generation (text overlay planning).

Extracted from editorial_agent.py to reduce god-module complexity.
Generates text overlay plans for silent vlog style presets.
Split pipeline: Call M1 (segment eligibility) → Call M2 (overlay text).
"""

import json
import os
from pathlib import Path

from .infra.atomic_write import atomic_write_text
from .config import (
    ClaudeConfig,
    EditorialProjectPaths,
    GeminiConfig,
)
from .infra.gemini_client import GeminiClient
from .versioning import (
    begin_version,
    commit_version,
    versioned_path,
)


def _load_transcript_for_prompt(clip_paths):
    """Import helper from editorial_agent to avoid circular dependency."""
    from .editorial_agent import _load_transcript_for_prompt as _helper

    return _helper(clip_paths)


# ---------------------------------------------------------------------------


def _run_monologue_split(
    editorial_paths: EditorialProjectPaths,
    provider: str,
    gemini_cfg: GeminiConfig | None = None,
    style_preset=None,
    user_context: dict | None = None,
    tracer=None,
    persona_hint: str | None = None,
    storyboard_path: Path | None = None,
) -> Path:
    """Multi-call Phase 3: Analysis → Creative → Validation.

    Call 1: Segment eligibility analysis + arc planning (which segments get overlays)
    Call 2: Creative text generation within bounded windows
    Call 3: Deterministic validation + assembly into MonologuePlan
    """
    from .models import (
        EditorialStoryboard,
        MonologuePlan,
        MonologueOverlay,
        OverlayPlan,
        OverlayDrafts,
    )
    from .editorial_prompts import (
        build_monologue_call1_prompt,
        build_monologue_call2_prompt,
        validate_monologue_overlays,
    )
    from .briefing import format_brief_for_prompt
    from .tracing import otel_phase_span, traced_gemini_generate, LLMSpinner
    from google.genai import types

    if not style_preset or not style_preset.has_phase3:
        raise ValueError("Phase 3 requires a style preset with has_phase3=True")

    # Load storyboard
    if storyboard_path:
        storyboard_json = storyboard_path
    else:
        storyboard_json = _find_latest_storyboard(editorial_paths, provider)
    if not storyboard_json:
        raise FileNotFoundError(
            f"No storyboard found in {editorial_paths.storyboard}. Run Phase 2 first."
        )
    storyboard = EditorialStoryboard.model_validate_json(storyboard_json.read_text())

    transcripts = _load_all_transcripts_for_monologue(editorial_paths, storyboard)

    user_context_text = None
    if user_context:
        user_context_text = format_brief_for_prompt(user_context, phase="phase2")
    else:
        from .versioning import resolve_user_context_path

        context_path = resolve_user_context_path(editorial_paths.root)
        if context_path:
            ctx = json.loads(context_path.read_text())
            user_context_text = format_brief_for_prompt(ctx, phase="phase2")

    client = GeminiClient.from_env()

    # ── Call 1: Segment analysis & arc planning ────────────────────────────
    call1_prompt = build_monologue_call1_prompt(
        storyboard=storyboard,
        transcripts=transcripts,
        user_context_text=user_context_text,
    )

    seg_count = len(storyboard.segments)
    print(f"  [M1] Analyzing {seg_count} segments for overlay eligibility...")

    with LLMSpinner("Segment analysis (Call M1)", provider=provider):
        with otel_phase_span("phase3_analysis", stage="monologue", provider="gemini", call="M1"):
            response_1 = traced_gemini_generate(
                client.raw,
                model=gemini_cfg.model,
                contents=call1_prompt,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    response_mime_type="application/json",
                    response_schema=OverlayPlan,
                ),
                phase="phase3_analysis",
                tracer=tracer,
                prompt_chars=len(call1_prompt),
            )
    overlay_plan = OverlayPlan.model_validate_json(response_1.text)
    eligible_count = len(overlay_plan.eligible_segments)
    print(
        f"  [M1] {eligible_count}/{seg_count} segments eligible for overlays "
        f"(persona: {overlay_plan.persona_recommendation})"
    )

    if eligible_count == 0:
        raise ValueError("No segments eligible for overlays — all segments contain speech?")

    # ── Call 2: Creative text generation ───────────────────────────────────
    call2_prompt = build_monologue_call2_prompt(
        overlay_plan=overlay_plan,
        storyboard=storyboard,
        persona_hint=persona_hint,
    )

    print(
        f"  [M2] Generating overlay text ({persona_hint or overlay_plan.persona_recommendation})..."
    )

    with LLMSpinner("Creative text (Call M2)", provider=provider):
        with otel_phase_span("phase3_creative", stage="monologue", provider="gemini", call="M2"):
            response_2 = traced_gemini_generate(
                client.raw,
                model=gemini_cfg.model,
                contents=call2_prompt,
                config=types.GenerateContentConfig(
                    temperature=0.8,  # creative writing needs variety
                    response_mime_type="application/json",
                    response_schema=OverlayDrafts,
                ),
                phase="phase3_creative",
                tracer=tracer,
                prompt_chars=len(call2_prompt),
            )
    drafts = OverlayDrafts.model_validate_json(response_2.text)
    print(f"  [M2] Generated {len(drafts.overlays)} overlay drafts")

    # ── Call 3: Deterministic validation (no LLM needed) ──────────────────
    fixed_overlays, fix_log = validate_monologue_overlays(
        overlays=drafts.overlays,
        eligible_segments=overlay_plan.eligible_segments,
        storyboard_segments=storyboard.segments,
    )

    if fix_log:
        print(f"  [M3] Validation fixes ({len(fix_log)}):")
        for f in fix_log[:5]:
            print(f"    {f}")
        if len(fix_log) > 5:
            print(f"    ... and {len(fix_log) - 5} more")

    # Assemble into MonologuePlan
    persona = persona_hint or overlay_plan.persona_recommendation
    monologue_overlays = []
    for i, ov in enumerate(fixed_overlays):
        monologue_overlays.append(
            MonologueOverlay(
                index=i,
                segment_index=ov.segment_index,
                text=ov.text,
                appear_at=ov.appear_at,
                duration_sec=ov.duration_sec,
                note=f"arc: {ov.arc_phase}" if ov.arc_phase else "",
            )
        )

    monologue = MonologuePlan(
        persona=persona,
        persona_description=overlay_plan.persona_rationale,
        tone_mechanics=["lowercase_whisper", "ellipses", "micro_pacing"],
        arc_structure=sorted({es.arc_phase for es in overlay_plan.eligible_segments}),
        overlays=monologue_overlays,
        total_text_time_sec=sum(ov.duration_sec for ov in monologue_overlays),
        pacing_notes=[],
        music_sync_notes=[],
    )

    # ── Version and save ──────────────────────────────────────────────────
    editorial_paths.storyboard.mkdir(parents=True, exist_ok=True)

    storyboard_input = {}
    storyboard_latest = editorial_paths.storyboard / f"editorial_{provider}_latest.json"
    if storyboard_latest.exists():
        import re as _re

        resolved = storyboard_latest.resolve()
        vm = _re.search(r"_v(\d+)\.json$", resolved.name)
        if vm:
            storyboard_input["storyboard"] = f"storyboard:{provider}:v{vm.group(1)}"

    cfg_snap = {"model": gemini_cfg.model, "temperature": gemini_cfg.temperature}
    sb_parent_id = f"sb.{vm.group(1)}" if storyboard_latest.exists() and vm else None

    art_meta = begin_version(
        editorial_paths.root,
        phase="monologue",
        provider=provider,
        inputs=storyboard_input,
        config_snapshot=cfg_snap,
        target_dir=editorial_paths.storyboard,
        parent_id=sb_parent_id,
    )
    v = art_meta.version
    base = f"monologue_{provider}"

    # Save intermediate artifacts
    plan_path = editorial_paths.storyboard / f"overlay_plan_{provider}_v{v}.json"
    atomic_write_text(plan_path, overlay_plan.model_dump_json(indent=2))

    if fix_log:
        fix_path = editorial_paths.storyboard / f"monologue_fixlog_{provider}_v{v}.txt"
        fix_path.write_text("\n".join(fix_log))

    json_path = versioned_path(editorial_paths.storyboard / f"{base}.json", v)
    atomic_write_text(json_path, monologue.model_dump_json(indent=2))
    commit_version(
        editorial_paths.root,
        art_meta,
        output_paths=[json_path, plan_path],
        target_dir=editorial_paths.storyboard,
    )

    overlay_count = len(monologue.overlays)
    text_time = monologue.total_text_time_sec
    print(
        f"  v{v} monologue (split pipeline): {overlay_count} overlays, {text_time:.1f}s text time"
    )
    print(f"    Persona: {monologue.persona}")
    print(f"    Plan:    {plan_path}")
    print(f"    JSON:    {json_path}")
    return json_path


def run_monologue(
    editorial_paths: EditorialProjectPaths,
    provider: str,
    gemini_cfg: GeminiConfig | None = None,
    claude_cfg: ClaudeConfig | None = None,
    style_preset=None,
    user_context: dict | None = None,
    tracer=None,
    persona_hint: str | None = None,
    storyboard_path: Path | None = None,
) -> Path:
    """Phase 3: generate Visual Monologue text overlay plan from the editorial storyboard.

    If gemini_cfg.use_split_pipeline is True, delegates to the multi-call pipeline
    (Call M1 analysis → Call M2 creative → deterministic validation).

    Args:
        storyboard_path: Explicit storyboard JSON path for version switching.
                         If None, uses the latest storyboard.
    """
    # Check for split pipeline mode
    if gemini_cfg and gemini_cfg.use_split_pipeline and provider == "gemini":
        return _run_monologue_split(
            editorial_paths=editorial_paths,
            provider=provider,
            gemini_cfg=gemini_cfg,
            style_preset=style_preset,
            user_context=user_context,
            tracer=tracer,
            persona_hint=persona_hint,
            storyboard_path=storyboard_path,
        )

    from .models import EditorialStoryboard, MonologuePlan
    from .editorial_prompts import build_monologue_prompt
    from .briefing import format_brief_for_prompt

    if not style_preset or not style_preset.has_phase3:
        raise ValueError("Phase 3 requires a style preset with has_phase3=True")

    # Load storyboard — explicit path or latest
    if storyboard_path:
        storyboard_json = storyboard_path
    else:
        storyboard_json = _find_latest_storyboard(editorial_paths, provider)
    if not storyboard_json:
        raise FileNotFoundError(
            f"No storyboard found in {editorial_paths.storyboard}. Run Phase 2 first."
        )
    storyboard = EditorialStoryboard.model_validate_json(storyboard_json.read_text())

    # Load transcripts
    transcripts = _load_all_transcripts_for_monologue(editorial_paths, storyboard)

    # Build user context text
    user_context_text = None
    if user_context:
        user_context_text = format_brief_for_prompt(user_context, phase="phase2")
    else:
        from .versioning import resolve_user_context_path

        context_path = resolve_user_context_path(editorial_paths.root)
        if context_path:
            ctx = json.loads(context_path.read_text())
            user_context_text = format_brief_for_prompt(ctx, phase="phase2")

    prompt = build_monologue_prompt(
        storyboard=storyboard,
        phase3_prompt_template=style_preset.phase3_prompt,
        transcripts=transcripts,
        user_context_text=user_context_text,
    )

    # Optionally inject persona hint
    if persona_hint:
        prompt += (
            f"\n\nIMPORTANT: The filmmaker prefers the **{persona_hint}** persona. "
            "Use this persona for the monologue."
        )

    # Log what we're sending
    seg_count = len(storyboard.segments)
    transcript_count = len(transcripts) if transcripts else 0
    prompt_kb = len(prompt) / 1024
    print(
        f"  Storyboard: {seg_count} segments, {transcript_count} transcripts, prompt ~{prompt_kb:.0f}KB"
    )

    from .tracing import LLMSpinner

    if provider == "gemini":
        from google.genai import types
        from .tracing import otel_phase_span, traced_gemini_generate

        client = GeminiClient.from_env()

        with LLMSpinner("Generating visual monologue", provider="gemini"):
            with otel_phase_span("monologue", stage="monologue", provider="gemini"):
                response = traced_gemini_generate(
                    client.raw,
                    model=gemini_cfg.model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=gemini_cfg.temperature,
                        response_mime_type="application/json",
                        response_schema=MonologuePlan,
                    ),
                    phase="monologue",
                    tracer=tracer,
                    prompt_chars=len(prompt),
                )
        monologue = MonologuePlan.model_validate_json(response.text)

    elif provider == "claude":
        import anthropic

        from .tracing import otel_phase_span, traced_claude_generate

        anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
        if not anthropic_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set. Add it to your .env file.")
        client = anthropic.Anthropic(api_key=anthropic_key)
        with LLMSpinner("Generating visual monologue", provider="claude"):
            with otel_phase_span("monologue", stage="monologue", provider="claude"):
                response = traced_claude_generate(
                    client,
                    model=claude_cfg.model,
                    messages=[
                        {
                            "role": "user",
                            "content": prompt
                            + "\n\nRespond ONLY with valid JSON matching the MonologuePlan schema.",
                        }
                    ],
                    max_tokens=claude_cfg.max_tokens * 2,
                    temperature=claude_cfg.temperature,
                    phase="monologue",
                    tracer=tracer,
                    prompt_chars=len(prompt),
                )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        monologue = MonologuePlan.model_validate_json(text)
    else:
        raise ValueError(f"Unknown provider: {provider}")

    # Version and save
    editorial_paths.storyboard.mkdir(parents=True, exist_ok=True)

    # Build lineage: which storyboard was this monologue derived from
    storyboard_input = {}
    storyboard_latest = editorial_paths.storyboard / f"editorial_{provider}_latest.json"
    if storyboard_latest.exists():
        import re as _re

        resolved = storyboard_latest.resolve()
        vm = _re.search(r"_v(\d+)\.json$", resolved.name)
        if vm:
            storyboard_input["storyboard"] = f"storyboard:{provider}:v{vm.group(1)}"

    cfg_snap = {}
    if gemini_cfg:
        cfg_snap = {"model": gemini_cfg.model, "temperature": gemini_cfg.temperature}
    elif claude_cfg:
        cfg_snap = {"model": claude_cfg.model, "temperature": claude_cfg.temperature}

    # Determine storyboard parent_id for lineage-prefixed versioning
    sb_parent_id = None
    if vm:
        sb_version = int(vm.group(1))
        sb_parent_id = f"sb.{sb_version}"

    art_meta = begin_version(
        editorial_paths.root,
        phase="monologue",
        provider=provider,
        inputs=storyboard_input,
        config_snapshot=cfg_snap,
        target_dir=editorial_paths.storyboard,
        parent_id=sb_parent_id,
    )
    v = art_meta.version
    base = f"monologue_{provider}"

    json_path = versioned_path(editorial_paths.storyboard / f"{base}.json", v)
    atomic_write_text(json_path, monologue.model_dump_json(indent=2))
    commit_version(
        editorial_paths.root,
        art_meta,
        output_paths=[json_path],
        target_dir=editorial_paths.storyboard,
    )

    overlay_count = len(monologue.overlays)
    text_time = monologue.total_text_time_sec
    print(f"  v{v} monologue: {overlay_count} overlays, {text_time:.1f}s text time")
    print(f"    Persona: {monologue.persona}")
    print(f"    JSON: {json_path}")
    return json_path


def _find_latest_storyboard(editorial_paths: EditorialProjectPaths, provider: str) -> Path | None:
    """Find the latest storyboard JSON for the given provider.

    Always returns the resolved versioned path (not the _latest symlink).
    """
    from .versioning import resolve_versioned_path

    latest = editorial_paths.storyboard / f"editorial_{provider}_latest.json"
    if latest.exists():
        return resolve_versioned_path(latest)
    # Fallback: try any provider
    for p in ("gemini", "claude"):
        f = editorial_paths.storyboard / f"editorial_{p}_latest.json"
        if f.exists():
            return resolve_versioned_path(f)
    return None


def _load_all_transcripts_for_monologue(
    editorial_paths: EditorialProjectPaths,
    storyboard,
) -> dict[str, str] | None:
    """Load transcripts for clips used in the storyboard."""
    clip_ids = {seg.clip_id for seg in storyboard.segments}
    transcripts = {}
    for clip_id in clip_ids:
        clip_paths = editorial_paths.clip_paths(clip_id)
        text = _load_transcript_for_prompt(clip_paths)
        if text:
            transcripts[clip_id] = text
    return transcripts if transcripts else None
