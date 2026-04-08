"""User briefing — interactive questionnaire using questionary (prompt_toolkit).

Includes smart briefing: a low-cost LLM quick scan of all proxy videos
that produces an overview, then asks the user targeted questions based
on what the AI actually observed in the footage.
"""

import json
import os
import time
from pathlib import Path

from .config import MODEL_GEMINI_25_FLASH

import questionary
from questionary import Style

_GEMINI_UPLOAD_TIMEOUT_SEC = 300


def _wait_for_gemini_file(video_file, client, timeout_sec: int = _GEMINI_UPLOAD_TIMEOUT_SEC):
    """Poll until Gemini file processing completes, with timeout."""
    start = time.monotonic()
    while video_file.state.name == "PROCESSING":
        if time.monotonic() - start > timeout_sec:
            raise TimeoutError(
                f"Gemini file processing timed out after {timeout_sec}s for {video_file.name}"
            )
        time.sleep(2)
        video_file = client.files.get(name=video_file.name)
    return video_file


# Custom style matching the vx aesthetic
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
        ("separator", "fg:#333333"),
    ]
)


def generate_questions(reviews: list[dict], style: str) -> list[dict]:
    """Generate smart questions based on Phase 1 clip reviews."""
    # Gather detected people
    people_descs = []
    for r in reviews:
        for p in r.get("people", []):
            desc = p.get("description", p.get("label", ""))[:100]
            if desc and desc not in people_descs:
                people_descs.append(desc)

    # Gather highlights
    highlights = []
    for r in reviews:
        for km in r.get("key_moments", []):
            if km.get("editorial_value") == "high":
                highlights.append(f"{r.get('clip_id', '?')}: {km.get('description', '')[:60]}")

    total_dur = sum(r.get("duration_sec", 0) for r in reviews if "duration_sec" in r)

    return {
        "people_detected": people_descs[:8],
        "highlights_detected": highlights[:5],
        "total_minutes": total_dur / 60 if total_dur > 0 else 0,
        "style": style,
    }


def run_briefing(reviews: list[dict], style: str, project_root: Path) -> dict | None:
    """Run the interactive editorial briefing. Returns user context dict."""
    from .versioning import resolve_user_context_path

    context_path = resolve_user_context_path(project_root)

    # Reuse existing context
    if context_path:
        existing = json.loads(context_path.read_text())
        print("\n  Existing user context found:")
        for k, v in existing.items():
            if v:
                print(f"    {k}: {v[:80]}{'...' if len(v) > 80 else ''}")

        action = questionary.select(
            "Use existing context?",
            choices=["Yes, use as-is", "Edit it", "Start fresh", "Skip briefing"],
            style=VX_STYLE,
        ).ask()

        if action is None or action == "Skip briefing":
            return None
        if action == "Yes, use as-is":
            return existing
        if action == "Start fresh":
            pass  # continue to fresh questions
        if action == "Edit it":
            return _edit_existing(existing, project_root)

    info = generate_questions(reviews, style)
    return _ask_questions(info, project_root)


def _ask_questions(info: dict, project_root: Path) -> dict | None:
    """Ask the editorial briefing questions interactively."""
    print("\n  Editorial Briefing")
    print("  Help the AI editor make better decisions. Press Esc to skip any question.\n")

    answers = {}

    # People
    if info["people_detected"]:
        print("  AI detected these people:")
        for d in info["people_detected"]:
            print(f"    - {d}")
        print()

    people = questionary.text(
        "Who are the main people? (names & roles)",
        instruction="(e.g., 'Woman in blue is my sister Amy, man with glasses is my dad')",
        style=VX_STYLE,
    ).ask()
    if people:
        answers["people"] = people

    # Activity
    activity = questionary.text(
        "What was this activity/occasion?",
        instruction="(e.g., 'Family day trip to Hsinchu Science Park')",
        style=VX_STYLE,
    ).ask()
    if activity:
        answers["activity"] = activity

    # Highlights
    if info["highlights_detected"]:
        print("\n  AI-flagged highlights:")
        for h in info["highlights_detected"]:
            print(f"    - {h}")

    highlights = questionary.text(
        "Any must-include moments?",
        instruction="(specific moments the editor should not miss)",
        style=VX_STYLE,
    ).ask()
    if highlights:
        answers["highlights"] = highlights

    # Tone
    tone = questionary.select(
        "Desired tone?",
        choices=[
            "Fun and lighthearted",
            "Cinematic and epic",
            "Chill and relaxed",
            "Warm and nostalgic",
            "Energetic and fast-paced",
            questionary.Choice("Custom...", value="__custom__"),
            questionary.Choice("Skip", value=""),
        ],
        style=VX_STYLE,
    ).ask()
    if tone == "__custom__":
        tone = questionary.text("Describe the tone:", style=VX_STYLE).ask()
    if tone:
        answers["tone"] = tone

    # Avoid
    avoid = questionary.text(
        "Anything to exclude?",
        instruction="(unflattering moments, private conversations, specific clips)",
        style=VX_STYLE,
    ).ask()
    if avoid:
        answers["avoid"] = avoid

    # Duration
    if info["total_minutes"] > 0:
        duration = questionary.text(
            f"Preferred final length? (~{info['total_minutes']:.0f} min raw footage)",
            instruction="(leave empty to let AI decide)",
            style=VX_STYLE,
        ).ask()
        if duration:
            answers["duration"] = duration

    if not answers:
        print("\n  No context provided — proceeding without briefing.")
        return None

    _save_user_context(project_root, answers)
    print(f"\n  Context saved ({len(answers)} fields)")
    return answers


def _edit_existing(existing: dict, project_root: Path) -> dict:
    """Let user edit existing context fields."""
    updated = {}
    for k, v in existing.items():
        if not isinstance(v, str):
            updated[k] = v
            continue
        new_val = questionary.text(
            f"{k}:",
            default=v,
            style=VX_STYLE,
        ).ask()
        updated[k] = new_val if new_val else v

    _save_user_context(project_root, updated)
    print(f"\n  Context updated ({len(updated)} fields)")
    return updated


def _save_user_context(project_root: Path, answers: dict):
    """Save user context with versioning."""
    from .versioning import begin_version, commit_version, versioned_path, update_latest_symlink

    meta = begin_version(
        project_root,
        phase="user_context",
        provider="user",
        target_dir=project_root,
    )
    out = versioned_path(project_root / "user_context.json", meta.version)
    out.write_text(json.dumps(answers, indent=2, ensure_ascii=False))
    update_latest_symlink(out)
    commit_version(project_root, meta, output_paths=[out], target_dir=project_root)


# ---------------------------------------------------------------------------
# Smart briefing — AI-guided context gathering via quick scan
# ---------------------------------------------------------------------------

QUICK_SCAN_PROMPT = """\
You are helping a filmmaker prepare to edit their footage.
Watch all the attached video clips and provide a structured overview.

For each clip: a one-line summary and energy level.
For people: describe their appearance in detail (clothing, features) so the filmmaker can identify them.
For activities: what locations, events, or activities do you observe?
For suggested_questions: ask the filmmaker specific questions that would help an editor, \
based on what you actually see. Focus on identifying people, understanding relationships, \
and clarifying the story context. Ask about specific things you noticed.

Be concise. This is a quick scan, not a detailed review.
"""


def run_quick_scan(
    editorial_paths,
    gemini_model: str = MODEL_GEMINI_25_FLASH,
    tracer=None,
) -> dict | None:
    """Upload all proxy videos and get a quick AI overview in one LLM call.

    Returns QuickScanResult as dict, or None if no proxies or API unavailable.
    """
    from google import genai
    from google.genai import types

    from .models import QuickScanResult
    from .tracing import otel_phase_span, traced_gemini_generate

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("  Skipping quick scan (GEMINI_API_KEY not set)")
        return None

    # Discover all proxy videos
    clips_dir = editorial_paths.clips_dir
    if not clips_dir.exists():
        return None

    clip_ids = sorted(d.name for d in clips_dir.iterdir() if d.is_dir() and (d / "proxy").exists())
    if not clip_ids:
        return None

    # Check cache (versioned → _latest → bare file)
    from .versioning import resolve_quick_scan_path

    cached = resolve_quick_scan_path(editorial_paths.root)
    if cached:
        return json.loads(cached.read_text())

    from .file_cache import load_file_api_cache, get_cached_uri, cache_file_uri
    from .preprocess import concat_proxies, format_concat_timeline

    client = genai.Client(api_key=api_key)

    # Concatenate proxies into bundles (chronological order, ≤40 min each)
    # This avoids Gemini's 10-video-per-prompt limit.
    bundles = concat_proxies(editorial_paths, clip_ids)
    if not bundles:
        return None

    # Upload concat bundles (reuse cached URIs)
    file_cache = load_file_api_cache(editorial_paths)
    video_parts = []
    for i, bundle in enumerate(bundles):
        cache_key = f"_concat_bundle_{i}"
        cached_uri = get_cached_uri(file_cache, cache_key)
        if cached_uri:
            video_parts.append(types.Part.from_uri(file_uri=cached_uri, mime_type="video/mp4"))
            continue

        print(f"  Uploading concat bundle {i + 1}/{len(bundles)}...")
        video_file = client.files.upload(file=str(bundle["path"]))
        video_file = _wait_for_gemini_file(video_file, client)
        if video_file.state.name == "FAILED":
            continue
        cache_file_uri(editorial_paths, cache_key, video_file.uri)
        video_parts.append(types.Part.from_uri(file_uri=video_file.uri, mime_type="video/mp4"))

    if not video_parts:
        return None

    # Build prompt with chronological timeline mapping
    timeline = format_concat_timeline(bundles)
    prompt = (
        QUICK_SCAN_PROMPT
        + "\n\nThe attached video contains all clips concatenated in chronological "
        "shooting order. Each clip has its filename overlaid in the top-left corner.\n"
        f"Timeline:\n{timeline}\n"
    )

    print(f"  Running quick scan ({gemini_model})...")
    with otel_phase_span("briefing_scan", stage="briefing", provider="gemini"):
        response = traced_gemini_generate(
            client,
            model=gemini_model,
            contents=[types.Content(parts=[*video_parts, types.Part.from_text(text=prompt)])],
            config=types.GenerateContentConfig(
                temperature=0.3,
                response_mime_type="application/json",
                response_schema=QuickScanResult,
            ),
            phase="briefing_scan",
            tracer=tracer,
            num_video_files=len(video_parts),
            prompt_chars=len(prompt),
        )

    scan = QuickScanResult.model_validate_json(response.text)
    result = scan.model_dump()

    # Save with versioning
    from .versioning import begin_version, commit_version, versioned_path, update_latest_symlink

    meta = begin_version(
        editorial_paths.root,
        phase="quick_scan",
        provider="gemini",
        config_snapshot={"model": gemini_model},
        target_dir=editorial_paths.root,
    )
    out = versioned_path(editorial_paths.root / "quick_scan.json", meta.version)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    update_latest_symlink(out)
    commit_version(editorial_paths.root, meta, output_paths=[out], target_dir=editorial_paths.root)
    return result


def run_smart_briefing(
    editorial_paths,
    style: str,
    gemini_model: str = MODEL_GEMINI_25_FLASH,
    tracer=None,
) -> dict | None:
    """Run AI-guided briefing: quick scan → show observations → ask targeted questions.

    Supports three briefing depths:
    - Quick (3 questions, ~30s) — people, activity, highlights/avoid
    - Director's (9 questions, ~2 min) — adds intent, audience, tone, pacing
    - Deep (all fields, ~5 min) — full creative brief with narrative and style
    """
    from .versioning import resolve_user_context_path

    context_path = resolve_user_context_path(editorial_paths.root)

    # Reuse existing context
    if context_path:
        existing = json.loads(context_path.read_text())
        print("\n  Existing user context found:")
        for k, v in existing.items():
            if isinstance(v, str) and v:
                print(f"    {k}: {v[:80]}{'...' if len(v) > 80 else ''}")

        action = questionary.select(
            "Use existing context?",
            choices=["Yes, use as-is", "Edit it", "Re-scan and start fresh", "Skip briefing"],
            style=VX_STYLE,
        ).ask()

        if action is None or action == "Skip briefing":
            return None
        if action == "Yes, use as-is":
            return existing
        if action == "Edit it":
            return _edit_existing(existing, editorial_paths.root)
        if action == "Re-scan and start fresh":
            pass  # Quick scan versioning handles this — new scan creates new version

    # Run quick scan
    print("\n  Running AI quick scan of all footage...")
    scan = run_quick_scan(editorial_paths, gemini_model, tracer=tracer)

    if not scan:
        print("  Quick scan unavailable — falling back to standard briefing.")
        return _ask_questions(
            {"people_detected": [], "highlights_detected": [], "total_minutes": 0, "style": style},
            editorial_paths.root,
        )

    # Show scan results
    _display_scan_results(scan)

    # Depth selector
    clip_count = len(scan.get("clip_summaries", []))
    total_raw = sum(
        s.get("duration_sec", 0) for s in scan.get("clip_summaries", []) if "duration_sec" in s
    )
    total_raw_str = f", {total_raw / 60:.0f} min raw" if total_raw > 0 else ""

    print(f"  Your footage: {clip_count} clips{total_raw_str}\n")

    depth = questionary.select(
        "Briefing depth?",
        choices=[
            questionary.Choice("Quick brief      (3 questions, ~30s)", value="quick"),
            questionary.Choice("Director's brief (9 questions, ~2 min)", value="director"),
            questionary.Choice("Deep brief       (all fields, ~5 min)", value="deep"),
            questionary.Choice("Load from file   (creative_brief.md)", value="file"),
            questionary.Choice("Skip briefing", value="skip"),
        ],
        style=VX_STYLE,
    ).ask()

    if depth is None or depth == "skip":
        print("\n  No context provided — proceeding without briefing.")
        return None

    if depth == "file":
        return _brief_from_file(editorial_paths, scan)

    brief = _ask_brief_questions(scan, depth)
    if not brief:
        print("\n  No context provided — proceeding without briefing.")
        return None

    result = brief.model_dump()
    save_creative_brief(editorial_paths.root, brief)
    field_count = sum(
        1 for k, v in result.items() if v and k not in ("brief_version", "source", "preset_key")
    )
    print(f"\n  Creative brief saved ({field_count} fields)")
    return result


def _display_scan_results(scan: dict) -> None:
    """Show quick scan results to the user."""
    print(f"\n  {'─' * 60}")
    print("  AI Quick Scan Results")
    print(f"  {'─' * 60}")
    print(f"\n  {scan['overall_summary']}\n")

    if scan.get("people"):
        print("  People spotted:")
        for p in scan["people"]:
            role = f" ({p['role_guess']})" if p.get("role_guess") else ""
            print(f"    - {p['description']}{role}")
        print()

    if scan.get("activities"):
        print("  Activities/locations: " + ", ".join(scan["activities"]))
        print()

    if scan.get("mood"):
        print(f"  Overall mood: {scan['mood']}\n")

    print(f"  {'─' * 60}\n")


def _ask_tone() -> str:
    """Ask tone preference (shared across briefing modes)."""
    tone = questionary.select(
        "Desired tone?",
        choices=[
            "Fun and lighthearted",
            "Cinematic and epic",
            "Chill and relaxed",
            "Warm and nostalgic",
            "Energetic and fast-paced",
            questionary.Choice("Custom...", value="__custom__"),
            questionary.Choice("Skip", value=""),
        ],
        style=VX_STYLE,
    ).ask()
    if tone == "__custom__":
        tone = questionary.text("Describe the tone:", style=VX_STYLE).ask()
    return tone or ""


def _ask_brief_questions(scan: dict, depth: str):
    """Ask briefing questions at the specified depth. Returns CreativeBrief or None."""
    from .models import CreativeBrief, AudienceSpec, NarrativeDirection, StyleDirection

    brief = CreativeBrief(brief_version=2, source="tui")

    # ── Questions shared by all depths ──────────────────────────────────────

    # People
    if scan.get("people"):
        print("  The AI spotted these people in your footage:")
        for i, p in enumerate(scan["people"], 1):
            print(f"    {i}. {p['description']}")
        print()

    people = questionary.text(
        "Who are these people? (names, roles, relationships)",
        instruction="(refer to the descriptions above — tell the AI who each person is)",
        style=VX_STYLE,
    ).ask()
    if people:
        brief.people = people

    # Activity
    activity_hint = ", ".join(scan.get("activities", []))[:80]
    activity = questionary.text(
        "What was this activity/occasion?",
        instruction=f"(AI observed: {activity_hint})" if activity_hint else "",
        style=VX_STYLE,
    ).ask()
    if activity:
        brief.activity = activity

    if depth == "quick":
        # Quick mode: combined highlights/avoid question
        ha = questionary.text(
            "Must-include or exclude anything?",
            instruction="(e.g., 'include sunset at temple; skip first 30s of clip 3')",
            style=VX_STYLE,
        ).ask()
        if ha:
            # Simple heuristic: split on "skip"/"exclude"/"no " keywords
            brief.highlights = ha
        # Quick mode done
        return brief if (brief.people or brief.activity or brief.highlights) else None

    # ── Director's and Deep modes ───────────────────────────────────────────

    # AI-suggested questions (context Q&A)
    if scan.get("suggested_questions"):
        print("\n  The AI has additional questions:")
        qa_pairs = []
        for q in scan["suggested_questions"]:
            answer = questionary.text(q, style=VX_STYLE).ask()
            if answer:
                qa_pairs.append({"question": q, "answer": answer})
        if qa_pairs:
            brief.context_qa = qa_pairs

    # Intent — the single most impactful new question
    intent = questionary.text(
        "What should viewers feel after watching?",
        instruction="(the north star — e.g., 'feel the warmth of a perfect family day')",
        style=VX_STYLE,
    ).ask()
    if intent:
        brief.intent = intent

    # Audience
    audience = questionary.select(
        "Who is this for?",
        choices=[
            questionary.Choice("Friends and family", value="friends_and_family"),
            questionary.Choice("YouTube audience", value="youtube"),
            questionary.Choice("Social media (TikTok/Instagram)", value="social"),
            questionary.Choice("Personal archive", value="personal"),
            questionary.Choice("Skip", value=""),
        ],
        style=VX_STYLE,
    ).ask()
    if audience:
        brief.audience = AudienceSpec(platform=audience)

    # Tone
    tone = _ask_tone()
    if tone:
        brief.tone = tone

    # Pacing
    pacing = questionary.select(
        "Pacing preference?",
        choices=[
            questionary.Choice("Let it breathe (slow, contemplative)", value="slow-contemplative"),
            questionary.Choice("Balanced (natural rhythm)", value="balanced"),
            questionary.Choice("Punchy (fast, energetic)", value="punchy"),
            questionary.Choice("Builds to climax", value="builds-to-climax"),
            questionary.Choice("Skip", value=""),
        ],
        style=VX_STYLE,
    ).ask()
    if pacing:
        if not brief.style:
            brief.style = StyleDirection()
        brief.style.pacing = pacing

    # Highlights
    highlights = questionary.text(
        "Any must-include moments?",
        instruction="(specific moments the editor should not miss)",
        style=VX_STYLE,
    ).ask()
    if highlights:
        brief.highlights = highlights

    # Avoid
    avoid = questionary.text(
        "Anything to exclude?",
        instruction="(unflattering moments, private conversations, specific clips)",
        style=VX_STYLE,
    ).ask()
    if avoid:
        brief.avoid = avoid

    # Duration
    duration = questionary.text(
        "Preferred final length?",
        instruction="(leave empty to let AI decide)",
        style=VX_STYLE,
    ).ask()
    if duration:
        brief.duration = duration

    if depth == "director":
        return brief

    # ── Deep mode only ──────────────────────────────────────────────────────

    print("\n  Extended creative direction (press Enter to skip any question)\n")

    # Story thesis
    thesis = questionary.text(
        "In one sentence, what is this video about?",
        instruction="(the editorial north star — e.g., 'A family rediscovering each other through travel')",
        style=VX_STYLE,
    ).ask()
    if thesis:
        if not brief.narrative:
            brief.narrative = NarrativeDirection()
        brief.narrative.story_thesis = thesis

    # Key beats
    beats_text = questionary.text(
        "Key moments in order? (comma-separated)",
        instruction="(e.g., 'morning departure, discovering the garden, sunset together')",
        style=VX_STYLE,
    ).ask()
    if beats_text:
        if not brief.narrative:
            brief.narrative = NarrativeDirection()
        brief.narrative.key_beats = [b.strip() for b in beats_text.split(",") if b.strip()]

    # Story hook
    hook = questionary.text(
        "What should the opening look like?",
        instruction="(e.g., 'flash-forward to the summit view')",
        style=VX_STYLE,
    ).ask()
    if hook:
        if not brief.narrative:
            brief.narrative = NarrativeDirection()
        brief.narrative.story_hook = hook

    # Ending
    ending = questionary.text(
        "How should it end?",
        instruction="(e.g., 'warm closure', 'bittersweet', 'looking forward')",
        style=VX_STYLE,
    ).ask()
    if ending:
        if not brief.narrative:
            brief.narrative = NarrativeDirection()
        brief.narrative.ending_note = ending

    # Structure
    structure = questionary.select(
        "Narrative structure?",
        choices=[
            questionary.Choice("Chronological (follow the day)", value="chronological"),
            questionary.Choice("Thematic (group by theme)", value="thematic"),
            questionary.Choice("Circular (end where we began)", value="circular"),
            questionary.Choice("Vignettes (independent scenes)", value="vignettes"),
            questionary.Choice("Skip", value=""),
        ],
        style=VX_STYLE,
    ).ask()
    if structure:
        if not brief.narrative:
            brief.narrative = NarrativeDirection()
        brief.narrative.structure = structure

    # Music mood
    music = questionary.select(
        "Music direction?",
        choices=[
            questionary.Choice("Acoustic / indie", value="acoustic"),
            questionary.Choice("Lo-fi / chill beats", value="lo-fi"),
            questionary.Choice("Orchestral / cinematic", value="orchestral"),
            questionary.Choice("Ambient / atmospheric", value="ambient"),
            questionary.Choice("Natural audio only", value="natural-audio-only"),
            questionary.Choice("Custom...", value="__custom__"),
            questionary.Choice("Skip", value=""),
        ],
        style=VX_STYLE,
    ).ask()
    if music == "__custom__":
        music = questionary.text("Describe the music:", style=VX_STYLE).ask()
    if music:
        if not brief.style:
            brief.style = StyleDirection()
        brief.style.music_mood = music

    # Visual tone
    visual = questionary.select(
        "Visual tone?",
        choices=[
            questionary.Choice("Warm (golden, cozy)", value="warm"),
            questionary.Choice("Cool (blue, crisp)", value="cool"),
            questionary.Choice("Cinematic (high contrast)", value="cinematic"),
            questionary.Choice("Bright (saturated, poppy)", value="bright"),
            questionary.Choice("Natural (as shot)", value="natural"),
            questionary.Choice("Skip", value=""),
        ],
        style=VX_STYLE,
    ).ask()
    if visual:
        if not brief.style:
            brief.style = StyleDirection()
        brief.style.visual_tone = visual

    # References
    refs = questionary.text(
        "Style inspiration? (creators, videos, moods)",
        instruction="(e.g., 'Casey Neistat pacing, sueddu visual calm')",
        style=VX_STYLE,
    ).ask()
    if refs:
        brief.references = [r.strip() for r in refs.split(",") if r.strip()]

    # Free notes
    notes = questionary.text(
        "Anything else the editor should know?",
        style=VX_STYLE,
    ).ask()
    if notes:
        brief.notes = notes

    return brief


def _brief_from_file(editorial_paths, scan: dict | None = None) -> dict | None:
    """Load a creative direction file — any .md, freeform text passed through to LLM."""
    brief_md_path = editorial_paths.root / "creative_brief.md"

    if not brief_md_path.exists():
        # Generate a lightweight guide (not a form) to help the user get started
        md_content = generate_creative_brief_md(scan=scan)
        brief_md_path.write_text(md_content, encoding="utf-8")
        print(f"  Generated guide: {brief_md_path}")

    editor = os.environ.get("EDITOR", "vim")
    print(f"  Opening {brief_md_path.name} in {editor}...")
    os.system(f'{editor} "{brief_md_path}"')

    if not brief_md_path.exists():
        return None

    text = brief_md_path.read_text(encoding="utf-8")
    brief = parse_creative_brief_md(text)
    if not brief:
        return None

    result = brief.model_dump()
    save_creative_brief(editorial_paths.root, brief)
    print("  Creative direction loaded from file")
    return result


# ---------------------------------------------------------------------------
# File-based creative direction — freeform passthrough to LLM
# ---------------------------------------------------------------------------


def generate_creative_brief_md(scan: dict | None = None) -> str:
    """Generate a lightweight guide to help the creator get started.

    This is NOT a form to fill out — it's inspiration. The creator can write
    anything they want in any format. The entire text is passed as-is to the
    LLM, which extracts what it needs.
    """
    lines = ["# Creative Direction\n"]
    lines.append("Write your vision for the edit below. Any format works — prose,")
    lines.append("bullet points, stream of consciousness. The AI editor will read")
    lines.append("this and use it to guide every creative decision.\n")

    # Show AI observations as context
    if scan:
        lines.append("---")
        lines.append("**What the AI saw in your footage:**\n")
        if scan.get("overall_summary"):
            lines.append(f"{scan['overall_summary']}\n")
        if scan.get("people"):
            lines.append("**People spotted:**")
            for p in scan["people"]:
                role = f" ({p['role_guess']})" if p.get("role_guess") else ""
                lines.append(f"- {p['description']}{role}")
            lines.append("")
        if scan.get("activities"):
            lines.append(f"**Activities/locations:** {', '.join(scan['activities'])}\n")
        if scan.get("mood"):
            lines.append(f"**Overall mood:** {scan['mood']}\n")
        lines.append("---\n")

    lines.append("## Your vision\n")
    lines.append("Some things you might want to cover (but don't have to):\n")
    lines.append("- What should viewers feel after watching?")
    lines.append("- Who are the people in the footage?")
    lines.append("- What's the story you want to tell?")
    lines.append("- What moments must be included? What should be cut?")
    lines.append("- What's the vibe — pacing, music, energy?")
    lines.append("- Any style references (creators, films, moods)?")
    lines.append("- Target length?\n")
    lines.append("Delete everything above and write freely, or just start below:\n")
    lines.append("")

    return "\n".join(lines)


def parse_creative_brief_md(text: str):
    """Parse a creative direction file — freeform passthrough.

    Strips the template boilerplate if present, stores the raw creative text
    in creative_direction_text. The LLM extracts what it needs.

    Returns CreativeBrief or None if the file is empty.
    """
    import re

    from .models import CreativeBrief

    # Strip HTML comments (if any remain from old templates)
    cleaned = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)

    # Strip the heading line
    cleaned = re.sub(r"^#\s+Creative (?:Direction|Brief)\s*\n", "", cleaned, flags=re.MULTILINE)

    # Strip the template boilerplate guide text (everything before user content)
    # Look for the marker that ends the guide section
    marker = "Delete everything above and write freely"
    if marker in cleaned:
        cleaned = cleaned.split(marker, 1)[1]

    # Clean up
    cleaned = cleaned.strip()

    if not cleaned:
        return None

    return CreativeBrief(
        brief_version=2,
        source="file",
        creative_direction_text=cleaned,
    )


def format_context_for_prompt(user_context: dict) -> str:
    """Format user context into a text block for LLM prompts (Phase 1, 2, 3).

    Separates hard constraints (must-include, must-exclude) from soft preferences
    (tone, duration, etc.) to improve LLM instruction-following.
    """
    if not user_context:
        return ""

    # Keys that become hard constraints vs soft preferences
    constraint_keys = {"highlights", "avoid"}
    constraint_labels = {
        "highlights": "MUST INCLUDE",
        "avoid": "MUST EXCLUDE",
    }
    preference_labels = {
        "people": "People in the footage",
        "activity": "Activity/occasion",
        "tone": "Desired tone",
        "duration": "Duration preference",
    }

    constraints = []
    preferences = []
    qa_items = []

    for key, value in user_context.items():
        if not value:
            continue
        if key == "context_qa":
            for qa in value:
                qa_items.append(f"- **Q: {qa['question']}** → {qa['answer']}")
        elif key.startswith("context_"):
            preferences.append(f"- **Additional context**: {value}")
        elif key in constraint_keys:
            label = constraint_labels[key]
            constraints.append(f"- {label}: {value}")
        else:
            label = preference_labels.get(key, key)
            preferences.append(f"- **{label}**: {value}")

    lines = []

    if constraints:
        lines.append(
            "FILMMAKER CONSTRAINTS (non-negotiable — violating these makes the edit unusable):"
        )
        lines.extend(constraints)
        lines.append(
            "- If you cannot satisfy a constraint, you MUST explain why in editorial_reasoning."
        )
        lines.append("")

    if preferences or qa_items:
        lines.append("FILMMAKER PREFERENCES (guide your creative choices):")
        lines.extend(preferences)
        lines.extend(qa_items)
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Creative Brief — enhanced loading, saving, and prompt formatting
# ---------------------------------------------------------------------------


def load_creative_brief(project_root: Path):
    """Load creative brief from project, handling legacy user_context.json format.

    Returns a CreativeBrief instance, or None if no user context exists.
    """
    from .models import CreativeBrief
    from .versioning import resolve_user_context_path

    path = resolve_user_context_path(project_root)
    if not path:
        return None
    data = json.loads(path.read_text())

    # Legacy format: flat dict without brief_version
    if "brief_version" not in data:
        known_fields = set(CreativeBrief.model_fields.keys())
        return CreativeBrief(
            **{k: v for k, v in data.items() if k in known_fields},
            brief_version=1,
        )
    # Enhanced format: full CreativeBrief
    return CreativeBrief.model_validate(data)


def save_creative_brief(project_root: Path, brief) -> Path:
    """Save creative brief with versioning. Returns the output path."""
    from .versioning import begin_version, commit_version, versioned_path, update_latest_symlink

    meta = begin_version(
        project_root,
        phase="user_context",
        provider="user",
        target_dir=project_root,
    )
    out = versioned_path(project_root / "user_context.json", meta.version)
    out.write_text(json.dumps(brief.model_dump(), indent=2, ensure_ascii=False))
    update_latest_symlink(out)
    commit_version(project_root, meta, output_paths=[out], target_dir=project_root)
    return out


def format_brief_for_prompt(brief, phase: str = "phase2") -> str:
    """Format a CreativeBrief into a three-tier prompt block.

    Hierarchy:
      1. CONSTRAINTS (non-negotiable) — highlights, avoid
      2. CREATIVE DIRECTION (strong guidance) — intent, audience, narrative, style
      3. PREFERENCES (soft hints) — people, activity, tone, duration, Q&A

    Falls back to format_context_for_prompt() for legacy (v1) briefs.

    Args:
        brief: CreativeBrief instance (or dict for legacy).
        phase: "phase1" or "phase2" — controls which fields are included.
    """
    # Dict path — upgrade v2 dicts to CreativeBrief, delegate v1 to legacy function
    if isinstance(brief, dict):
        if brief.get("brief_version", 1) >= 2:
            from .models import CreativeBrief

            return format_brief_for_prompt(CreativeBrief.model_validate(brief), phase=phase)
        return format_context_for_prompt(brief)

    # v1 brief with no enhanced fields — use legacy formatting
    if not brief.has_creative_direction():
        return format_context_for_prompt(brief.to_legacy_dict())

    lines: list[str] = []

    # --- Tier 1: CONSTRAINTS ---
    constraints = []
    if brief.highlights:
        constraints.append(f"- MUST INCLUDE: {brief.highlights}")
    if brief.avoid:
        constraints.append(f"- MUST EXCLUDE: {brief.avoid}")

    if constraints:
        lines.append(
            "FILMMAKER CONSTRAINTS (non-negotiable — violating these makes the edit unusable):"
        )
        lines.extend(constraints)
        lines.append(
            "- If you cannot satisfy a constraint, you MUST explain why in editorial_reasoning."
        )
        lines.append("")

    # --- Tier 2: CREATIVE DIRECTION ---
    # Freeform passthrough: if the filmmaker provided a raw creative direction
    # document, inject it as-is. The LLM extracts what it needs.
    if brief.creative_direction_text:
        lines.append(
            "CREATIVE DIRECTION (the filmmaker's vision for this edit — "
            "read carefully and let it guide every editorial decision):"
        )
        lines.append(brief.creative_direction_text.strip())
        lines.append("")
    else:
        # Structured fields from TUI input
        direction = []

        if brief.intent:
            direction.append(
                f"- NORTH STAR: The viewer should {brief.intent}. "
                "Every editorial decision must serve this intent."
            )

        if brief.audience and (brief.audience.platform or brief.audience.viewer):
            parts = []
            if brief.audience.platform:
                parts.append(brief.audience.platform)
            if brief.audience.viewer:
                parts.append(f"for {brief.audience.viewer}")
            direction.append(
                f"- AUDIENCE: {' '.join(parts)}. Tailor hooks, pacing, and content density."
            )

        if brief.narrative:
            n = brief.narrative
            if n.story_thesis:
                direction.append(f"- STORY THESIS: {n.story_thesis}")
            if n.structure:
                direction.append(f"- STRUCTURE: {n.structure}")
            # Key beats and full narrative details only for Phase 2
            if phase == "phase2":
                if n.key_beats:
                    beats = "\n".join(f"  {i}. {b}" for i, b in enumerate(n.key_beats, 1))
                    direction.append(f"- KEY NARRATIVE BEATS (in suggested order):\n{beats}")
                if n.story_hook:
                    direction.append(f"- OPENING: {n.story_hook}")
                if n.ending_note:
                    direction.append(f"- ENDING: {n.ending_note}")

        if brief.style:
            s = brief.style
            if s.pacing:
                direction.append(f"- PACING: {s.pacing}")
            if s.music_mood:
                direction.append(f"- MUSIC: {s.music_mood}")
            if s.energy_curve:
                direction.append(f"- ENERGY ARC: {s.energy_curve}")
            # Transitions and visual tone only for Phase 2
            if phase == "phase2":
                if s.transitions:
                    direction.append(f"- TRANSITIONS: {s.transitions}")
                if s.visual_tone:
                    direction.append(f"- VISUAL TONE: {s.visual_tone}")

        if brief.references:
            direction.append(f"- REFERENCE STYLE: {', '.join(brief.references)}")

        if direction:
            lines.append(
                "CREATIVE DIRECTION (strong guidance — the filmmaker's vision for this edit):"
            )
            lines.extend(direction)
            lines.append("")

    # --- Tier 3: PREFERENCES ---
    preferences = []
    if brief.people:
        preferences.append(f"- **People in the footage**: {brief.people}")
    if brief.activity:
        preferences.append(f"- **Activity/occasion**: {brief.activity}")
    if brief.tone:
        preferences.append(f"- **Desired tone**: {brief.tone}")
    if brief.duration:
        preferences.append(f"- **Duration preference**: {brief.duration}")
    if brief.notes:
        preferences.append(f"- **Additional notes**: {brief.notes}")

    qa_items = []
    if brief.context_qa:
        for qa in brief.context_qa:
            qa_items.append(f"- **Q: {qa['question']}** → {qa['answer']}")

    if preferences or qa_items:
        lines.append("FILMMAKER PREFERENCES (guide your creative choices):")
        lines.extend(preferences)
        lines.extend(qa_items)
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Creative presets — user-defined reusable creative direction templates
# ---------------------------------------------------------------------------

_PRESETS_DIR = Path.home() / ".vx" / "presets"


def _ensure_presets_dir() -> Path:
    """Create the presets directory if it doesn't exist."""
    _PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    return _PRESETS_DIR


def list_creative_presets() -> list[dict]:
    """List all user-defined creative presets."""
    from .models import CreativePreset

    if not _PRESETS_DIR.exists():
        return []
    presets = []
    for f in sorted(_PRESETS_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            preset = CreativePreset.model_validate(data)
            presets.append(preset.model_dump())
        except Exception:
            continue
    return presets


def get_creative_preset(key: str):
    """Load a creative preset by key. Returns CreativePreset or None."""
    from .models import CreativePreset

    path = _PRESETS_DIR / f"{key}.json"
    if not path.exists():
        return None
    return CreativePreset.model_validate_json(path.read_text())


def save_creative_preset(preset) -> Path:
    """Save a creative preset to ~/.vx/presets/{key}.json."""
    _ensure_presets_dir()
    path = _PRESETS_DIR / f"{preset.key}.json"
    path.write_text(json.dumps(preset.model_dump(), indent=2, ensure_ascii=False))
    return path


def extract_preset_from_project(project_root: Path, preset_key: str, label: str = ""):
    """Extract a reusable creative preset from a project's creative brief.

    Copies non-project-specific fields (intent, tone, audience, style, references)
    and strips project-specific fields (people, activity, highlights, avoid, context_qa).
    """
    from .models import CreativePreset

    brief = load_creative_brief(project_root)
    if not brief:
        return None

    preset = CreativePreset(
        key=preset_key,
        label=label or preset_key.replace("-", " ").replace("_", " ").title(),
        intent=brief.intent,
        tone=brief.tone,
        audience=brief.audience,
        narrative_defaults=brief.narrative,
        style=brief.style,
        references=brief.references,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )
    return preset


def apply_preset_to_brief(preset, brief=None):
    """Apply a creative preset's defaults to a brief. Returns a new CreativeBrief."""
    from .models import CreativeBrief

    if brief is None:
        brief = CreativeBrief(brief_version=2, source="preset")

    if preset.intent and not brief.intent:
        brief.intent = preset.intent
    if preset.tone and not brief.tone:
        brief.tone = preset.tone
    if preset.audience and not brief.audience:
        brief.audience = preset.audience
    if preset.narrative_defaults and not brief.narrative:
        brief.narrative = preset.narrative_defaults
    if preset.style and not brief.style:
        brief.style = preset.style
    if preset.references and not brief.references:
        brief.references = preset.references
    brief.preset_key = preset.key
    return brief
