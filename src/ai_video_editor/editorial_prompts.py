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
    clip_id: str, filename: str, duration_sec: float, resolution: str
) -> str:
    return CLIP_REVIEW_PROMPT.format(
        clip_id=clip_id,
        filename=filename,
        duration=format_duration(duration_sec),
        resolution=resolution,
    )


# ---------------------------------------------------------------------------
# Phase 2 — Editorial assembly (cross-clip creative edit plan)
# ---------------------------------------------------------------------------

EDITORIAL_ASSEMBLY_PROMPT = """\
You are a professional video editor creating an editorial storyboard for a {style} from raw trip/activity footage.

You have reviewed {clip_count} clips totaling {total_duration} of raw footage.

Here are the structured clip reviews:

{clip_reviews_json}

---

Now produce an EDITORIAL STORYBOARD — a creative assembly plan that a human editor can follow to cut this footage into a compelling {style}. Think like an editor who has watched all the dailies and is now mapping out the edit.

Consider:
- What story can you tell with this footage?
- What's the strongest opening hook?
- How to build narrative momentum?
- Where are the natural emotional beats?
- What footage is redundant or should be cut?
- Where does the audio (speech, ambient) drive the edit vs where music should carry it?
- **WHO are the people?** Match person descriptions across clips to identify the same individuals. Determine who is the main subject/vlogger and build the narrative around them. Ensure continuity — don't jump between clips featuring different people without context.

Use this EXACT markdown format:

# Editorial Storyboard: {project_name}
**Raw footage**: {clip_count} clips, {total_duration} total
**Estimated final cut**: [your suggested duration]
**Style**: {style}

## Cast
[Identify each person who appears across the clips. Match person labels from different clip reviews that refer to the same individual. Note who is the main subject/vlogger.]

| Person | Description | Role | Appears In |
|--------|-------------|------|------------|
| Eric (person_A) | Man in blue PUMA shirt, bib #30860 | Main subject / vlogger | vid_001, vid_003, vid_005, ... |
| ... | ... | ... | ... |

## Story Concept
[2-3 sentences: what is this video about? What story are we telling? What's the emotional arc?]

## Story Arc

### Opening Hook (0:00 - ~0:XX)
[What grabs the viewer in the first 5-10 seconds? Be specific about which clip and timestamp.]

### Introduction (~0:XX - ~X:XX)
[Set up the context — where, who, what. Which clips establish the setting and characters?]

### Body (~X:XX - ~X:XX)
[The main content. Break into sub-sections if the activity has natural phases.]

### Climax (~X:XX - ~X:XX)
[The peak moment or payoff — the best footage.]

### Outro (~X:XX - ~X:XX)
[Wrap-up, reflection, or call to action.]

## Edit Decision List (EDL)

| # | Clip | In | Out | Dur | Purpose | Description | Transition |
|---|------|----|-----|-----|---------|-------------|------------|
| 1 | clip_id | M:SS | M:SS | Xs | hook | Brief description | Cut |
| 2 | clip_id | M:SS | M:SS | Xs | establish | Brief description | Dissolve |
[... continue for every segment in the final cut ...]

## Discarded Clips
| Clip | Duration | Reason |
|------|----------|--------|
| clip_id | M:SS | Why this clip was not used |

## Pacing Notes
- **0:00-0:30**: [pacing guidance — fast cuts, slow, etc.]
- **0:30-2:00**: [...]
[... continue for major sections ...]

## Music & Audio Plan
| Section | Time Range | Audio Strategy | Notes |
|---------|------------|---------------|-------|
| Opening | 0:00-0:15 | Upbeat music | No dialogue, let visuals + music hook |
| Intro | 0:15-1:00 | Lower music, dialogue | Speaker sets up the activity |
[... continue ...]

## Technical Notes
[Color grading suggestions, aspect ratio, speed ramps, text overlays, etc.]
"""


def build_editorial_assembly_prompt(
    project_name: str,
    clip_reviews: list[dict],
    style: str,
    clip_count: int,
    total_duration_sec: float,
) -> str:
    reviews_json = json.dumps(clip_reviews, indent=2, ensure_ascii=False)
    return EDITORIAL_ASSEMBLY_PROMPT.format(
        project_name=project_name,
        clip_count=clip_count,
        total_duration=format_duration(total_duration_sec),
        clip_reviews_json=reviews_json,
        style=style,
    )


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
