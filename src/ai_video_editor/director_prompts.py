"""Prompts and tool declarations for the Editorial Director agent.

The director is a tool-using ReAct agent that reviews, critiques, and
iteratively refines storyboard outputs. These prompts define its persona,
review rubric, and the Gemini function-calling tool schema.
"""

from .config import ReviewBudget
from .models import EditorialStoryboard
from .storyboard_format import format_duration


DIRECTOR_SYSTEM_PROMPT = """\
You are an editorial director reviewing a video storyboard for VX.
Your job: watch the edit, spot problems, fix them. You are opinionated
about quality — a rough cut that's "good enough to share" is the bar.

## Your Review Process
1. OVERVIEW: Study the contact strip, eval scores, and source material
   digest. Get the big picture — what's in the edit AND what's available.
2. INSPECT: Use screenshot_segment and get_transcript_excerpt to drill
   into anything suspicious. Look for:
   - Cuts that interrupt speech mid-sentence
   - Segments where transcript content doesn't match what's visible
   - Pacing problems (too many slow segments in a row, or too jumpy)
   - Missing must-include moments (check constraint scores)
   - Audio/transition contradictions (j_cut with muted audio, etc.)
   - Weak opening hook or abrupt ending
   - Duplicate visual content across segments
   Use get_full_transcript to understand conversation flow before
   choosing cut points — find natural breaks (pauses, speaker turns),
   not just sentence boundaries.
3. EXPAND: If coverage is below 60% or a constraint needs footage not
   in the storyboard, use get_unused_footage to find candidates from
   source clips. Add footage with edit_timeline(action='add') only when
   it serves the narrative — don't pad for coverage alone.
4. EDIT: Use edit_timeline to modify the storyboard:
   - update: change segment fields (timestamps, description, etc.)
   - add: insert new footage from unused clips
   - remove: delete a segment
   - move: reposition a segment in the timeline
   Rules for editing:
   - Before editing a segment's description, screenshot it first.
   - NEVER remove "Text overlay: ..." content from descriptions.
   - When fixing a constraint match, ADD natural keywords to the
     existing description. Do NOT paste constraint text verbatim.
     Good: "Walking along the ridge with panoramic city views."
     Bad: "Walking along the ridge with the city view all along the hikes."
5. FINALIZE: When the edit is good enough to share, call finalize_review.
   Always call finalize_review before your budget runs out.

## Quality Bar
- Constraints: 100% must be satisfied (hard requirement)
- No mid-sentence cuts (check transcript at cut points)
- Cut points: prefer natural conversation breaks (pauses, speaker turns)
- Visual flow: contact strip should tell a coherent story
- Pacing: variety of segment durations, not all the same length
- Coverage: aim for 60%+ of clips used or explicitly discarded
- Transitions: audio_note must be compatible with transition type

## Budget
{budget}
Be efficient. Don't inspect every segment — focus on problems.
If all scores look good and the contact strip shows coherent flow,
finalize quickly.
"""


def build_system_prompt(budget: ReviewBudget, style_guidelines: str | None = None) -> str:
    """Build the director system prompt with current budget and optional style guidelines."""
    prompt = DIRECTOR_SYSTEM_PROMPT.format(budget=budget.remaining_summary())
    if style_guidelines:
        prompt += (
            "\n\n## Style-Specific Guidelines\n"
            "The storyboard was created with the following style preset. "
            "Your edits MUST remain consistent with these guidelines:\n\n" + style_guidelines
        )
    return prompt


DIRECTOR_CHAT_PROMPT = """\
You are an editorial director helping a filmmaker refine their video storyboard.
The filmmaker will give you editorial direction — requests, feedback, questions.
Follow their lead. You are collaborative, not autonomous.

## How to Work
1. LISTEN: Understand what the filmmaker is asking for.
2. INSPECT: Use tools to study the relevant parts of the storyboard —
   screenshot segments, check transcripts, browse unused footage.
   Show the filmmaker what you find when relevant.
3. PROPOSE: Use propose_edits to describe your planned changes and why.
   ALWAYS propose before editing. Wait for the filmmaker's confirmation.
4. EXECUTE: Only after the filmmaker approves, use edit_timeline to
   make the changes. Then show what changed.
5. ITERATE: Ask if there's anything else to work on.

## Rules
- NEVER call edit_timeline without first calling propose_edits and
  receiving filmmaker confirmation.
- When the filmmaker says "yes", "go ahead", "do it", "approved" —
  that means proceed with the proposed edits.
- When the filmmaker says "no", "cancel", "never mind" — abandon the
  proposal and ask what they'd like instead.
- When editing descriptions, preserve "Text overlay: ..." content.
- When fixing constraints, add natural keywords — don't paste constraint
  text verbatim.
- Be concise in your responses. Show key info, not walls of text.

## MOVE vs UPDATE (critical)
- To RELOCATE a clip to a different position in the timeline: use
  action="move" with segment_index and to_position.
- To CHANGE PROPERTIES of a segment (timestamps, description, purpose):
  use action="update" with segment_index and updated_fields.
- NEVER use "update" to overwrite one segment's metadata with another
  clip's data. That corrupts the timeline. Use "move" instead.

## Quality Bar
- Constraints: 100% must be satisfied (hard requirement)
- No mid-sentence cuts (check transcript at cut points)
- Cut points: prefer natural conversation breaks (pauses, speaker turns)
- Coverage: aim for 60%+ of clips used or explicitly discarded

## Budget
{budget}
"""


def build_chat_system_prompt(budget: ReviewBudget, style_guidelines: str | None = None) -> str:
    """Build the conversational director system prompt."""
    prompt = DIRECTOR_CHAT_PROMPT.format(budget=budget.remaining_summary())
    if style_guidelines:
        prompt += (
            "\n## Style-Specific Guidelines\n"
            "Your edits MUST remain consistent with these guidelines:\n\n" + style_guidelines
        )
    return prompt


def build_initial_message(
    storyboard: EditorialStoryboard,
    eval_summary: str,
    contact_strip_image: bytes | None,
    user_context: dict | None,
    budget: ReviewBudget,
    clip_reviews: list[dict] | None = None,
    filming_timeline: str | None = None,
) -> list[dict]:
    """Build the initial user message content parts for the director.

    Returns a list of content parts (text + optional image) suitable for
    the Gemini API's multimodal content format.
    """
    parts = []

    # 1. Storyboard summary
    seg_count = len(storyboard.segments)
    total_dur = storyboard.total_segments_duration
    arc_names = [a.title for a in storyboard.story_arc] if storyboard.story_arc else ["(none)"]

    summary = (
        f"# Storyboard Review\n\n"
        f"**Title**: {storyboard.title}\n"
        f"**Style**: {storyboard.style}\n"
        f"**Segments**: {seg_count} | **Duration**: {format_duration(total_dur)}\n"
        f"**Story arc**: {' → '.join(arc_names)}\n\n"
        f"**Concept**: {storyboard.story_concept}\n"
    )
    parts.append({"type": "text", "text": summary})

    # 2. Contact strip image (if available)
    if contact_strip_image:
        parts.append({"type": "text", "text": "## Contact Strip (one frame per segment)"})
        parts.append({"type": "image", "data": contact_strip_image})

    # 3. Eval scores
    parts.append({"type": "text", "text": f"## Computable Eval Scores\n\n{eval_summary}"})

    # 4. User constraints
    if user_context:
        constraints = []
        highlights = user_context.get("highlights", "")
        avoid = user_context.get("avoid", "")
        if highlights:
            constraints.append(f"MUST-INCLUDE: {highlights}")
        if avoid:
            constraints.append(f"MUST-EXCLUDE: {avoid}")
        if constraints:
            parts.append(
                {"type": "text", "text": "## Filmmaker Constraints\n\n" + "\n".join(constraints)}
            )

    # 5. Filming timeline (chronological shooting order)
    if filming_timeline:
        parts.append({"type": "text", "text": f"## Filming Timeline\n\n{filming_timeline}"})

    # 6. Source material digest (so director sees the full picture)
    if clip_reviews:
        used_clips = {s.clip_id for s in storyboard.segments}
        used_ranges: dict[str, list[tuple[float, float]]] = {}
        for seg in storyboard.segments:
            used_ranges.setdefault(seg.clip_id, []).append((seg.in_sec, seg.out_sec))

        material_lines = []
        unused_clip_count = 0
        unused_moment_lines = []

        for review in clip_reviews:
            cid = review.get("clip_id", "")
            clip_summary = review.get("summary", "")
            usable_count = len(review.get("usable_segments", []))
            has_speech = review.get("audio", {}).get("has_speech", False)
            in_storyboard = cid in used_clips
            marker = "USED" if in_storyboard else "available"
            if not in_storyboard:
                unused_clip_count += 1
            material_lines.append(
                f"  {cid}: {clip_summary} "
                f"({usable_count} usable, speech={'yes' if has_speech else 'no'}) [{marker}]"
            )

            # Find unused high-value key moments
            for m in review.get("key_moments", []):
                if m.get("editorial_value") != "high":
                    continue
                ts = m.get("timestamp_sec", 0)
                is_used = any(in_s <= ts <= out_s for in_s, out_s in used_ranges.get(cid, []))
                if not is_used:
                    unused_moment_lines.append(
                        f"  {cid} @ {ts:.0f}s: {m.get('description', '')} "
                        f"[{m.get('suggested_use', '')}]"
                    )

        digest = f"## Source Material ({len(clip_reviews)} clips, {unused_clip_count} unused)\n\n"
        digest += "\n".join(material_lines)
        if unused_moment_lines:
            digest += "\n\nUnused HIGH-VALUE moments:\n" + "\n".join(unused_moment_lines[:10])

        parts.append({"type": "text", "text": digest})

    # 7. Segment list
    transcript_lines = []
    for seg in storyboard.segments:
        line = (
            f"Seg {seg.index}: [{seg.clip_id}] {format_duration(seg.in_sec)}-"
            f"{format_duration(seg.out_sec)} | {seg.purpose} | "
            f"{seg.audio_note or 'no audio note'}"
        )
        if seg.description:
            line += f" — {seg.description}"
        transcript_lines.append(line)

    parts.append({"type": "text", "text": "## Segment List\n\n" + "\n".join(transcript_lines)})

    # 8. Budget reminder
    parts.append(
        {"type": "text", "text": f"\n---\n{budget.remaining_summary()}\nBegin your review."}
    )

    return parts


def build_eval_summary(
    storyboard: EditorialStoryboard,
    clip_reviews: list[dict],
    user_context: dict | None = None,
    transcripts_by_clip: dict | None = None,
) -> str:
    """Build a compact eval summary string from scoring functions."""
    from .eval import (
        score_constraint_satisfaction,
        score_coverage,
        score_speech_cut_safety,
        score_structural_completeness,
        score_timestamp_precision,
    )

    lines = []

    if user_context:
        results = score_constraint_satisfaction(storyboard, user_context)
        if results:
            sat = sum(1 for r in results if r.satisfied)
            lines.append(f"Constraints: {sat}/{len(results)} satisfied")
            for r in results:
                status = "✓" if r.satisfied else "✗"
                lines.append(f"  {status} {r.constraint_type}: {r.text}")
        else:
            lines.append("Constraints: none specified")
    else:
        lines.append("Constraints: no user context available")

    total, valid, clamped, invalid = score_timestamp_precision(storyboard, clip_reviews)
    if total:
        lines.append(f"Timestamps: {valid}/{total} valid ({valid / total:.0%})")
    else:
        lines.append("Timestamps: no segments")

    struct = score_structural_completeness(storyboard)
    lines.append(f"Structure: {struct:.0%}")

    cov = score_coverage(storyboard, clip_reviews)
    lines.append(f"Coverage: {cov:.0%}")

    if transcripts_by_clip:
        rate, unsafe = score_speech_cut_safety(storyboard, transcripts_by_clip)
        lines.append(f"Speech-safe cuts: {rate:.0%} ({len(unsafe)} unsafe)")
    else:
        lines.append("Speech-safe cuts: N/A (no transcripts)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gemini function-calling tool declarations
# ---------------------------------------------------------------------------


def get_tool_declarations() -> list[dict]:
    """Return Gemini-compatible function declarations for all director tools."""
    return [
        {
            "name": "screenshot_segment",
            "description": (
                "Extract a 2x2 thumbnail grid (4 keyframes) from a specific segment. "
                "Use this to visually inspect a segment in detail."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "segment_index": {
                        "type": "integer",
                        "description": "Index of the segment to screenshot (0-based)",
                    },
                },
                "required": ["segment_index"],
            },
        },
        {
            "name": "get_transcript_excerpt",
            "description": (
                "Get transcript text for a clip within a time range. "
                "Returns speaker-labeled lines with timestamps."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "clip_id": {"type": "string", "description": "The clip ID"},
                    "start_sec": {"type": "number", "description": "Start time in seconds"},
                    "end_sec": {"type": "number", "description": "End time in seconds"},
                },
                "required": ["clip_id", "start_sec", "end_sec"],
            },
        },
        {
            "name": "get_full_transcript",
            "description": (
                "Get the FULL transcript for a clip, grouped by speaker turns with "
                "pause markers. Use this to understand conversation flow and find "
                "natural cut points (pauses, speaker transitions)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "clip_id": {"type": "string", "description": "The clip ID"},
                },
                "required": ["clip_id"],
            },
        },
        {
            "name": "get_clip_review",
            "description": (
                "Get the Phase 1 review data for a clip: usable segments, "
                "people detected, quality notes, and editorial notes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "clip_id": {"type": "string", "description": "The clip ID"},
                },
                "required": ["clip_id"],
            },
        },
        {
            "name": "run_eval_check",
            "description": (
                "Run a computable eval dimension. Available: constraint_satisfaction, "
                "timestamp_precision, structural_completeness, speech_cut_safety, coverage."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "dimension": {"type": "string", "description": "Eval dimension name"},
                },
                "required": ["dimension"],
            },
        },
        {
            "name": "get_unused_footage",
            "description": (
                "Browse unused usable segments and key moments not in the storyboard. "
                "Call without clip_id for overview, or with clip_id for one clip's details."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "clip_id": {
                        "type": "string",
                        "description": "Optional: specific clip to inspect",
                    },
                },
                "required": [],
            },
        },
        {
            "name": "edit_timeline",
            "description": (
                "Edit the storyboard timeline. Actions:\n"
                "- update: change properties of a segment IN PLACE "
                "(segment_index + updated_fields). Cannot change clip_id.\n"
                "- add: insert footage from a clip "
                "(clip_id, in_sec, out_sec, position, purpose, description)\n"
                "- remove: delete a segment (segment_index)\n"
                "- move: reposition a segment to a different timeline position "
                "(segment_index, to_position)\n"
                "IMPORTANT: To relocate a clip to a different position, use 'move'. "
                "Do NOT use 'update' to copy one segment's data into another.\n"
                "All edits are reverted if eval scores regress."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "update, add, remove, or move",
                    },
                    "segment_index": {
                        "type": "integer",
                        "description": "Target segment (for update/remove/move)",
                    },
                    "updated_fields": {
                        "type": "object",
                        "description": "Fields to update (for action=update). "
                        "Valid: in_sec, out_sec, purpose, description, "
                        "transition, audio_note, text_overlay. "
                        "Cannot change clip_id — use move or remove+add instead.",
                    },
                    "to_position": {
                        "type": "integer",
                        "description": "New position (for action=move)",
                    },
                    "clip_id": {
                        "type": "string",
                        "description": "Source clip (for action=add)",
                    },
                    "in_sec": {
                        "type": "number",
                        "description": "Start time in clip (for action=add)",
                    },
                    "out_sec": {
                        "type": "number",
                        "description": "End time in clip (for action=add)",
                    },
                    "position": {
                        "type": "integer",
                        "description": "Insert position (for action=add)",
                    },
                    "purpose": {
                        "type": "string",
                        "description": "Segment purpose (for action=add)",
                    },
                    "description": {
                        "type": "string",
                        "description": "Segment description (for action=add)",
                    },
                    "transition": {
                        "type": "string",
                        "description": "Transition type (for action=add, default: cut)",
                    },
                    "audio_note": {
                        "type": "string",
                        "description": "Audio strategy (for action=add)",
                    },
                },
                "required": ["action"],
            },
        },
        {
            "name": "finalize_review",
            "description": (
                "Signal that the review is complete. Call this when the storyboard "
                "meets quality bar or you've made all feasible improvements."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "passed": {
                        "type": "boolean",
                        "description": "True if the storyboard meets the quality bar",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Brief editorial assessment",
                    },
                },
                "required": ["passed", "summary"],
            },
        },
    ]


def get_chat_tool_declarations() -> list[dict]:
    """Tool declarations for conversational director mode.

    Same as auto-review tools, plus propose_edits for plan-then-execute flow.
    """
    base = get_tool_declarations()
    base.append(
        {
            "name": "propose_edits",
            "description": (
                "Propose planned edits for the filmmaker to review BEFORE executing. "
                "ALWAYS call this before edit_timeline in conversational mode. "
                "Describes what you want to change and why. "
                "Wait for filmmaker confirmation before proceeding."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "plan": {
                        "type": "string",
                        "description": "Human-readable description of what you want to do and why",
                    },
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "description": "A planned edit_timeline call. "
                            "Same params as edit_timeline: action + action-specific fields.",
                        },
                        "description": "List of planned edit_timeline calls",
                    },
                },
                "required": ["plan", "edits"],
            },
        }
    )
    return base
