"""Prompt templates for the editorial storyboard agent (multi-clip workflow)."""

import json
import re

from .storyboard_format import format_duration


def _format_clip_reviews_text(
    clip_reviews: list[dict],
    transcripts: dict[str, str] | None = None,
) -> str:
    """Flatten clip review dicts into compact plain-text for the Phase 2 prompt.

    Drops JSON overhead, duplicate timestamp formats (keeps seconds only),
    and discard_segments (Phase 2 only needs usable segments).
    Each clip's transcript (if available) is inlined after its review.
    """
    blocks = []
    for r in clip_reviews:
        cid = r.get("clip_id", "unknown")
        lines = [f"## {cid}"]
        lines.append(r.get("summary", ""))

        # Quality — single line
        q = r.get("quality", {})
        if q:
            parts = [f"{k}={v}" for k, v in q.items()]
            lines.append(f"Quality: {', '.join(parts)}")

        # Content type
        ct = r.get("content_type", [])
        if ct:
            lines.append(f"Content: {', '.join(ct) if isinstance(ct, list) else ct}")

        # People
        for p in r.get("people", []):
            role = p.get("role", "")
            desc = p.get("description", "")
            speaking = " (speaking)" if p.get("speaking") else ""
            pct = p.get("screen_time_pct")
            pct_str = f" {pct:.0%}" if pct else ""
            lines.append(f"  Person: {p.get('label', '?')} — {role}{pct_str}{speaking}: {desc}")

        # Key moments — seconds only
        for km in r.get("key_moments", []):
            ts = km.get("timestamp_sec", 0)
            val = km.get("editorial_value", "")
            use = km.get("suggested_use", "")
            lines.append(f"  @{ts:.1f}s [{val}] {km.get('description', '')} (use: {use})")

        # Usable segments — seconds only
        segs = r.get("usable_segments", [])
        if segs:
            lines.append("Usable segments:")
            for s in segs:
                lines.append(
                    f"  {s.get('in_sec', 0):.1f}s–{s.get('out_sec', 0):.1f}s "
                    f"({s.get('duration_sec', 0):.1f}s, {s.get('quality', '?')}): "
                    f"{s.get('description', '')}"
                )

        # Audio — compact
        a = r.get("audio", {})
        if a:
            parts = []
            if a.get("speech_summary"):
                parts.append(f"speech: {a['speech_summary']}")
            if a.get("ambient_description"):
                parts.append(f"ambient: {a['ambient_description']}")
            if a.get("music_potential"):
                parts.append(a["music_potential"])
            if parts:
                lines.append(f"Audio: {'; '.join(parts)}")

        # Editorial notes
        notes = r.get("editorial_notes", "")
        if notes:
            lines.append(f"Editorial: {notes}")

        # Inline transcript if available
        if transcripts and cid in transcripts:
            lines.append(f"Transcript:\n{transcripts[cid]}")

        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Phase 1 — Per-clip review (structured JSON output)
# ---------------------------------------------------------------------------

_CLIP_REVIEW_HEADER = """\
You are a professional video editor reviewing raw footage from a trip or activity shoot.
Analyze this clip thoroughly and produce a structured review.

Clip: {clip_id} ({filename})
Duration: {duration}
Resolution: {resolution}
"""

_CLIP_REVIEW_INSTRUCTIONS = """\
Be specific with timestamps. Identify EVERY usable and discardable segment.

CRITICAL: Pay close attention to the PEOPLE in the footage. Use consistent labels \
(person_A, person_B, etc.) and describe each person's appearance in enough detail to \
match them across clips. Note who is the main subject (vlogger/host), who are companions, \
and who are bystanders. This is essential for the editor to maintain narrative focus and \
continuity across clips."""

# JSON template for Claude path (no response_schema available)
_CLIP_REVIEW_JSON_TEMPLATE = """
Respond ONLY with a JSON object (no markdown fences, no commentary) using this exact structure:

{{
  "clip_id": "{clip_id}",
  "summary": "2-3 sentence visual summary of the clip",
  "quality": {{
    "overall": "good|fair|poor",
    "stability": "steady|slightly_shaky|very_shaky",
    "lighting": "well_lit|mixed|dark|overexposed",
    "focus": "sharp|soft|out_of_focus",
    "composition": "intentional|casual|accidental"
  }},
  "content_type": ["talking_head", "b_roll", "action", "landscape", "transition", "establishing", "accidental"],
  "people": [
    {{
      "label": "person_A",
      "description": "physical appearance, clothing, distinguishing features",
      "role": "main_subject|companion|bystander|crowd",
      "screen_time_pct": 0.0,
      "speaking": true,
      "timestamps": ["M:SS-M:SS"]
    }}
  ],
  "key_moments": [
    {{
      "timestamp": "M:SS",
      "timestamp_sec": 0.0,
      "description": "what happens",
      "editorial_value": "high|medium|low",
      "suggested_use": "opening_hook|establishing|context|action|reaction|b_roll|cutaway|climax|outro"
    }}
  ],
  "usable_segments": [
    {{
      "in_point": "M:SS",
      "in_sec": 0.0,
      "out_point": "M:SS",
      "out_sec": 0.0,
      "duration_sec": 0.0,
      "description": "what this segment contains",
      "quality": "good|fair"
    }}
  ],
  "discard_segments": [
    {{
      "in_point": "M:SS",
      "out_point": "M:SS",
      "reason": "blurry|shaky|accidental|redundant|boring|lens_cap|out_of_focus"
    }}
  ],
  "audio": {{
    "has_speech": true,
    "speech_language": "language or null",
    "speech_summary": "key things said, or null",
    "ambient_description": "wind, crowd, traffic, silence, etc",
    "music_potential": "good_for_music_bed|needs_music_overlay|has_natural_soundtrack"
  }},
  "editorial_notes": "free-form notes — how this clip might fit into a final edit"
}}"""


def build_clip_review_prompt(
    clip_id: str,
    filename: str,
    duration_sec: float,
    resolution: str,
    transcript_text: str | None = None,
    style_supplement: str | None = None,
    user_context: dict | None = None,
    include_json_template: bool = True,
) -> str:
    fmt = dict(
        clip_id=clip_id,
        filename=filename,
        duration=format_duration(duration_sec),
        resolution=resolution,
    )
    prompt = _CLIP_REVIEW_HEADER.format(**fmt)
    if include_json_template:
        prompt += _CLIP_REVIEW_JSON_TEMPLATE.format(**fmt)
    prompt += "\n\n" + _CLIP_REVIEW_INSTRUCTIONS
    if transcript_text:
        prompt += (
            "\n\nAudio Transcript (from speech-to-text):\n"
            "---\n"
            f"{transcript_text}\n"
            "---\n"
            "Use this transcript to fill in the audio section accurately. "
            "The speech_summary should reflect actual dialogue. "
            "Match speech timestamps to key_moments and usable_segments."
        )
    if user_context:
        from .briefing import format_context_for_prompt

        prompt += (
            "\n\n"
            + format_context_for_prompt(user_context)
            + "\nUse people names in labels and descriptions when you can identify them. "
            "Flag moments the filmmaker marked as highlights."
        )
    if style_supplement:
        prompt += "\n\n" + style_supplement
    return prompt


# ---------------------------------------------------------------------------
# Phase 2 — Editorial assembly (cross-clip creative edit plan)
# ---------------------------------------------------------------------------

EDITORIAL_ASSEMBLY_PROMPT = """\
You are a professional video editor creating an editorial storyboard for a {style} from raw trip/activity footage.

You have reviewed {clip_count} clips totaling {total_duration} of raw footage.

The available clip IDs are: {clip_ids}

Think like an editor who has watched all the dailies:
- What story can you tell with this footage?
- What's the strongest opening hook?
- How to build narrative momentum and emotional arc?
- What footage is redundant or should be cut?
- Where does audio (speech, ambient) drive the edit vs where music should carry it?
- **WHO are the people?** Match person descriptions across clips. Determine who is the main subject. Ensure continuity.
- **Filming timeline matters**: Clips are provided in chronological filming order. For vlogs, the narrative should generally follow this timeline (place A → B → C). Avoid jump-cutting between different time periods unless intentionally used as an opening hook or flashback. Interleave B-roll within each location/period rather than across periods.

RULES:
- Timestamps are in SECONDS (float), used directly by ffmpeg. in_sec/out_sec must come from the clip reviews — never estimate from video.
- clip_id must be EXACT (e.g., "{example_clip_id}") — never abbreviated.
- Include every segment needed for the final cut, in chronological order of the output video.
- Be thorough — a complete edit plan that a human can execute.
"""


def build_editorial_assembly_prompt(
    project_name: str,
    clip_reviews: list[dict],
    style: str,
    clip_count: int,
    total_duration_sec: float,
    transcripts: dict[str, str] | None = None,
    visual_timeline: list[dict] | None = None,
    style_supplement: str | None = None,
    filming_timeline: str | None = None,
    user_context_text: str | None = None,
) -> str:
    clip_ids = [r.get("clip_id", "unknown") for r in clip_reviews]
    example_clip_id = clip_ids[0] if clip_ids else "vid_001"
    prompt = EDITORIAL_ASSEMBLY_PROMPT.format(
        project_name=project_name,
        clip_count=clip_count,
        total_duration=format_duration(total_duration_sec),
        clip_ids=", ".join(clip_ids),
        example_clip_id=example_clip_id,
        style=style,
    )

    # 1. User context — filmmaker's intent, people, preferences (read first)
    if user_context_text:
        prompt += "\n\n" + user_context_text

    # 2. Style-specific guidelines
    if style_supplement:
        prompt += "\n\n" + style_supplement

    # 3. Filming timeline
    if filming_timeline:
        prompt += (
            "\n\nFilming Timeline (chronological shooting order):\n"
            f"{filming_timeline}\n\n"
            "The clip reviews below are sorted in this filming order."
        )

    # 4. Clip reviews (with inline transcripts)
    reviews_text = _format_clip_reviews_text(clip_reviews, transcripts)
    prompt += "\n\n---\nClip Reviews:\n\n" + reviews_text + "\n---"

    if transcripts:
        prompt += (
            "\n\nTranscripts are included inline under each clip review. "
            "Use them for editorial decisions:\n"
            "- Identify dialogue-driven segments that should be preserved intact\n"
            "- Find natural speech breaks for cut points\n"
            "- Use dialogue content to drive narrative arc and story concept\n"
            "- Note where speech and visuals complement or contrast each other\n\n"
            "TIMESTAMP VALIDATION: Before outputting each segment, verify that in_sec and "
            "out_sec fall within a usable_segment from the clip review for that clip_id. "
            "Never reference a timestamp beyond the clip's duration."
        )

    # 5. Visual reference (Gemini only)
    if visual_timeline:
        from .preprocess import format_concat_timeline

        timeline_text = format_concat_timeline(visual_timeline)
        total_clips = sum(len(b["clips"]) for b in visual_timeline)
        prompt += (
            f"\n\nProxy videos for all {total_clips} clips are attached as a concatenated "
            "video in chronological shooting order. Each clip has its filename overlaid.\n"
            f"Timeline:\n{timeline_text}\n\n"
            "Use the attached video ONLY for qualitative visual judgments:\n"
            "- Assess energy, pacing, and composition\n"
            "- Verify which moments have the strongest visual impact\n"
            "- Match people across clips by their actual appearance\n"
            "- Judge transitions based on visual continuity\n\n"
            "CRITICAL: Do NOT use the video to determine timestamps. "
            "The video timeline may not match the actual clip timecodes. "
            "ALL in_sec/out_sec values MUST come from the clip reviews and transcripts. "
            "Each clip's usable_segments define the valid timestamp ranges — stay within them."
        )

    prompt += (
        "\n\nNow produce the EditorialStoryboard. "
        "Use the editorial_reasoning field to think through your editorial decisions "
        f"before filling in the segments for a compelling {style}."
    )
    return prompt


# ---------------------------------------------------------------------------
# Phase 3 — Visual Monologue (text overlay generation)
# ---------------------------------------------------------------------------


def build_monologue_prompt(
    storyboard,
    phase3_prompt_template: str,
    transcripts: dict[str, str] | None = None,
    user_context_text: str | None = None,
) -> str:
    """Build the Phase 3 prompt from a storyboard and the preset's phase3_prompt template."""
    # Build segments table
    rows = ["| # | Clip | In | Out | Dur | Purpose | Description | Audio |"]
    rows.append("|---|------|-----|-----|-----|---------|-------------|-------|")
    for seg in storyboard.segments:
        rows.append(
            f"| {seg.index} | {seg.clip_id} | {seg.in_sec:.1f}s | {seg.out_sec:.1f}s "
            f"| {seg.duration_sec:.1f}s | {seg.purpose} | {seg.description} "
            f"| {seg.audio_note or '-'} |"
        )
    segments_table = "\n".join(rows)

    # Build cast summary
    cast_lines = []
    for c in storyboard.cast:
        cast_lines.append(f"- **{c.name}** ({c.role}): {c.description}")
    cast_text = "\n".join(cast_lines) if cast_lines else "(no cast identified)"

    # Build story arc summary
    arc_lines = []
    for arc in storyboard.story_arc:
        indices = ", ".join(str(i) for i in arc.segment_indices) if arc.segment_indices else "-"
        arc_lines.append(f"- **{arc.title}** (segments {indices}): {arc.description}")
    arc_text = "\n".join(arc_lines) if arc_lines else "(no story arc defined)"

    # Build transcripts section
    if transcripts:
        sections = []
        for clip_id, text in transcripts.items():
            sections.append(f"### {clip_id}\n{text}")
        transcripts_text = "\n\n".join(sections)
    else:
        transcripts_text = "(no transcripts available — assume mostly ambient audio)"

    prompt = phase3_prompt_template.format(
        title=storyboard.title,
        duration=format_duration(storyboard.estimated_duration_sec),
        style=storyboard.style,
        story_concept=storyboard.story_concept,
        cast=cast_text,
        story_arc=arc_text,
        segments_table=segments_table,
        transcripts=transcripts_text,
        user_context=user_context_text or "(no filmmaker context provided)",
    )
    return prompt


# ---------------------------------------------------------------------------
# JSON parsing helper
# ---------------------------------------------------------------------------


def parse_clip_review(response_text: str) -> dict:
    """Extract a JSON object from an AI response, handling code fences and surrounding text."""
    # Try direct parse first
    text = response_text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code fence
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Try finding first { ... } block
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not extract JSON from response:\n{text[:500]}")
