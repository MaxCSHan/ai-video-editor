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
    context_path = project_root / "user_context.json"

    # Reuse existing context
    if context_path.exists():
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
            return _edit_existing(existing, context_path)

    info = generate_questions(reviews, style)
    return _ask_questions(info, context_path)


def _ask_questions(info: dict, context_path: Path) -> dict | None:
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

    context_path.write_text(json.dumps(answers, indent=2, ensure_ascii=False))
    print(f"\n  Context saved ({len(answers)} fields)")
    return answers


def _edit_existing(existing: dict, context_path: Path) -> dict:
    """Let user edit existing context fields."""
    updated = {}
    for k, v in existing.items():
        new_val = questionary.text(
            f"{k}:",
            default=v,
            style=VX_STYLE,
        ).ask()
        updated[k] = new_val if new_val else v

    context_path.write_text(json.dumps(updated, indent=2, ensure_ascii=False))
    print(f"\n  Context updated ({len(updated)} fields)")
    return updated


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

    # Check cache
    scan_path = editorial_paths.root / "quick_scan.json"
    if scan_path.exists():
        return json.loads(scan_path.read_text())

    from .file_cache import load_file_api_cache, get_cached_uri, cache_file_uri

    client = genai.Client(api_key=api_key)

    # Upload all proxies (reuse cached URIs, cache new uploads for downstream stages)
    file_cache = load_file_api_cache(editorial_paths)
    print(f"  Uploading {len(clip_ids)} proxy videos for quick scan...")
    video_parts = []
    for cid in clip_ids:
        proxy_dir = clips_dir / cid / "proxy"
        proxy_files = list(proxy_dir.glob("*_proxy.mp4"))
        if not proxy_files:
            continue

        cached_uri = get_cached_uri(file_cache, cid)
        if cached_uri:
            video_parts.append(types.Part.from_uri(file_uri=cached_uri, mime_type="video/mp4"))
            continue

        video_file = client.files.upload(file=str(proxy_files[0]))
        while video_file.state.name == "PROCESSING":
            time.sleep(2)
            video_file = client.files.get(name=video_file.name)
        if video_file.state.name == "FAILED":
            continue
        cache_file_uri(editorial_paths, cid, video_file.uri)
        video_parts.append(types.Part.from_uri(file_uri=video_file.uri, mime_type="video/mp4"))

    if not video_parts:
        return None

    # Build prompt with clip IDs for reference
    clip_list = "\n".join(f"- Clip {i + 1}: {cid}" for i, cid in enumerate(clip_ids))
    prompt = QUICK_SCAN_PROMPT + f"\n\nClip IDs (in order of the attached videos):\n{clip_list}\n"

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

    # Cache the scan
    scan_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return result


def run_smart_briefing(
    editorial_paths,
    style: str,
    gemini_model: str = "gemini-2.5-flash",
    tracer=None,
) -> dict | None:
    """Run AI-guided briefing: quick scan → show observations → ask targeted questions."""
    context_path = editorial_paths.root / "user_context.json"

    # Reuse existing context
    if context_path.exists():
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
            return _edit_existing(existing, context_path)
        if action == "Re-scan and start fresh":
            # Delete cached scan to force re-run
            scan_path = editorial_paths.root / "quick_scan.json"
            if scan_path.exists():
                scan_path.unlink()

    # Run quick scan
    print("\n  Running AI quick scan of all footage...")
    scan = run_quick_scan(editorial_paths, gemini_model, tracer=tracer)

    if not scan:
        print("  Quick scan unavailable — falling back to standard briefing.")
        return _ask_questions(
            {"people_detected": [], "highlights_detected": [], "total_minutes": 0, "style": style},
            context_path,
        )

    # Show scan results
    print(f"\n  {'─' * 60}")
    print(f"  AI Quick Scan Results")
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

    # AI-suggested questions
    if scan.get("suggested_questions"):
        print("\n  The AI has additional questions:")
        for q in scan["suggested_questions"]:
            answer = questionary.text(
                q,
                style=VX_STYLE,
            ).ask()
            if answer:
                # Store under a descriptive key
                key = q[:40].lower().replace(" ", "_").replace("?", "").strip("_")
                answers[f"context_{key}"] = answer

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

    context_path.write_text(json.dumps(answers, indent=2, ensure_ascii=False))
    print(f"\n  Context saved ({len(answers)} fields)")
    return answers


def format_context_for_prompt(user_context: dict) -> str:
    """Format user context into a text block for the Phase 2 prompt."""
    if not user_context:
        return ""

    lines = ["The filmmaker provided the following context:"]
    label_map = {
        "people": "People in the footage",
        "activity": "Activity/occasion",
        "highlights": "Must-include moments",
        "tone": "Desired tone",
        "avoid": "Things to avoid/exclude",
        "duration": "Duration preference",
    }
    for key, value in user_context.items():
        label = label_map.get(key, key)
        lines.append(f"- **{label}**: {value}")

    lines.append("")
    lines.append(
        "Use this context to make better editorial decisions. Prioritize the filmmaker's stated preferences."
    )
    return "\n".join(lines)
