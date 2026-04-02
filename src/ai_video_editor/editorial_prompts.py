"""Prompt templates for the editorial storyboard agent (multi-clip workflow)."""

import json
import re

from .storyboard_format import format_duration


# ---------------------------------------------------------------------------
# Phase 1 — Per-clip review (structured JSON output)
# ---------------------------------------------------------------------------

CLIP_REVIEW_PROMPT = """\
You are a professional video editor reviewing raw footage from a trip or activity shoot.
Analyze this clip thoroughly and produce a structured review as a JSON object.

Clip: {clip_id} ({filename})
Duration: {duration}
Resolution: {resolution}

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
      "description": "physical appearance, clothing, distinguishing features (e.g., 'man in blue PUMA shirt with bib #30860')",
      "role": "main_subject|companion|bystander|crowd",
      "screen_time_pct": 0.0,
      "speaking": true,
      "timestamps": ["M:SS-M:SS", "M:SS-M:SS"]
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
}}

Be specific with timestamps. Identify EVERY usable and discardable segment.

CRITICAL: Pay close attention to the PEOPLE in the footage. Use consistent labels (person_A, person_B, etc.) and describe each person's appearance in enough detail to match them across clips. Note who is the main subject (vlogger/host), who are companions, and who are bystanders. This is essential for the editor to maintain narrative focus and continuity across clips.
"""


def build_clip_review_prompt(
    clip_id: str,
    filename: str,
    duration_sec: float,
    resolution: str,
    transcript_text: str | None = None,
    style_supplement: str | None = None,
    user_context: dict | None = None,
) -> str:
    prompt = CLIP_REVIEW_PROMPT.format(
        clip_id=clip_id,
        filename=filename,
        duration=format_duration(duration_sec),
        resolution=resolution,
    )
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

Here are the structured clip reviews:

{clip_reviews_json}

---

Now produce an EDITORIAL STORYBOARD as structured JSON — a creative assembly plan for a compelling {style}.

Think like an editor who has watched all the dailies:
- What story can you tell with this footage?
- What's the strongest opening hook?
- How to build narrative momentum and emotional arc?
- What footage is redundant or should be cut?
- Where does audio (speech, ambient) drive the edit vs where music should carry it?
- **WHO are the people?** Match person descriptions across clips. Determine who is the main subject. Ensure continuity.

CRITICAL RULES for the structured output:
- All timestamps must be in SECONDS (float) — these will be used directly by ffmpeg
- in_sec and out_sec MUST come from the clip reviews or transcripts above — do NOT estimate timestamps by watching the video
- clip_id MUST be the EXACT clip_id from the reviews above (e.g., "{example_clip_id}"). Do NOT abbreviate or shorten clip IDs.
- in_sec and out_sec are relative to the START of each clip
- Each clip's maximum duration is listed in its review — NEVER use timestamps that exceed it
- Include every segment needed for the final cut, in chronological order of the output video
- Be thorough — a complete edit plan that a human can execute
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
) -> str:
    reviews_json = json.dumps(clip_reviews, indent=2, ensure_ascii=False)
    clip_ids = [r.get("clip_id", "unknown") for r in clip_reviews]
    example_clip_id = clip_ids[0] if clip_ids else "vid_001"
    prompt = EDITORIAL_ASSEMBLY_PROMPT.format(
        project_name=project_name,
        clip_count=clip_count,
        total_duration=format_duration(total_duration_sec),
        clip_reviews_json=reviews_json,
        clip_ids=", ".join(clip_ids),
        example_clip_id=example_clip_id,
        style=style,
    )
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
            "ALL in_sec/out_sec values MUST come from the clip reviews and transcripts above. "
            "Each clip's usable_segments define the valid timestamp ranges — stay within them."
        )
    if transcripts:
        sections = []
        for clip_id, text in transcripts.items():
            sections.append(f"### {clip_id}\n{text}")
        prompt += (
            "\n\n---\n\n"
            "Audio Transcripts (speech-to-text):\n\n" + "\n\n".join(sections) + "\n\n---\n\n"
            "Use these transcripts for editorial decisions:\n"
            "- Identify dialogue-driven segments that should be preserved intact\n"
            "- Find natural speech breaks for cut points\n"
            "- Use dialogue content to drive narrative arc and story concept\n"
            "- Note where speech and visuals complement or contrast each other\n\n"
            "TIMESTAMP VALIDATION: Before outputting each segment, verify that in_sec and "
            "out_sec fall within a usable_segment from the clip review for that clip_id. "
            "Never reference a timestamp beyond the clip's duration."
        )
    if style_supplement:
        prompt += "\n\n" + style_supplement
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
