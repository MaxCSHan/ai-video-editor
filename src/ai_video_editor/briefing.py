"""User briefing — interactive questionnaire using questionary (prompt_toolkit)."""

import json
from pathlib import Path

import questionary
from questionary import Style

# Custom style matching the vx aesthetic
VX_STYLE = Style([
    ("qmark", "fg:#2ecc71 bold"),
    ("question", "fg:#ffffff bold"),
    ("answer", "fg:#2ecc71"),
    ("pointer", "fg:#2ecc71 bold"),
    ("highlighted", "fg:#2ecc71 bold"),
    ("selected", "fg:#2ecc71"),
    ("instruction", "fg:#666666"),
    ("text", "fg:#aaaaaa"),
    ("separator", "fg:#333333"),
])


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
    lines.append("Use this context to make better editorial decisions. Prioritize the filmmaker's stated preferences.")
    return "\n".join(lines)
