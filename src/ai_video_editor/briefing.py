"""User briefing — interactive questionnaire using questionary (prompt_toolkit).

Includes smart briefing: a low-cost LLM quick scan of all proxy videos
that produces an overview, then asks the user targeted questions based
on what the AI actually observed in the footage.
"""

import json
import os
import time
from pathlib import Path

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
    gemini_model: str = "gemini-2.5-flash",
    tracer=None,
) -> dict | None:
    """Upload all proxy videos and get a quick AI overview in one LLM call.

    Returns QuickScanResult as dict, or None if no proxies or API unavailable.
    """
    from google import genai
    from google.genai import types

    from .models import QuickScanResult
    from .tracing import traced_gemini_generate

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
    gemini_model: str = "gemini-2.5-flash",
    tracer=None,
) -> dict | None:
    """Run AI-guided briefing: quick scan → show observations → ask targeted questions."""
    from .versioning import resolve_user_context_path

    context_path = resolve_user_context_path(editorial_paths.root)

    # Reuse existing context
    if context_path:
        existing = json.loads(context_path.read_text())
        print("\n  Existing user context found:")
        for k, v in existing.items():
            if v:
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

    # Ask targeted questions based on scan
    answers = {}

    # People — show AI observations and ask for identification
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
        answers["people"] = people

    # Activity — pre-fill from scan
    activity_hint = ", ".join(scan.get("activities", []))[:80]
    activity = questionary.text(
        "What was this activity/occasion?",
        instruction=f"(AI observed: {activity_hint})" if activity_hint else "",
        style=VX_STYLE,
    ).ask()
    if activity:
        answers["activity"] = activity

    # AI-suggested questions — store full Q&A pairs so downstream prompts see the question
    if scan.get("suggested_questions"):
        print("\n  The AI has additional questions:")
        qa_pairs = []
        for q in scan["suggested_questions"]:
            answer = questionary.text(
                q,
                style=VX_STYLE,
            ).ask()
            if answer:
                qa_pairs.append({"question": q, "answer": answer})
        if qa_pairs:
            answers["context_qa"] = qa_pairs

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

    # Highlights
    highlights = questionary.text(
        "Any must-include moments?",
        instruction="(specific moments the editor should not miss)",
        style=VX_STYLE,
    ).ask()
    if highlights:
        answers["highlights"] = highlights

    # Avoid
    avoid = questionary.text(
        "Anything to exclude?",
        instruction="(unflattering moments, private conversations, specific clips)",
        style=VX_STYLE,
    ).ask()
    if avoid:
        answers["avoid"] = avoid

    if not answers:
        print("\n  No context provided — proceeding without briefing.")
        return None

    _save_user_context(editorial_paths.root, answers)
    print(f"\n  Context saved ({len(answers)} fields)")
    return answers


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
