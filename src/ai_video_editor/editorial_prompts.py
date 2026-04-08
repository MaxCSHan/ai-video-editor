"""Prompt templates for the editorial storyboard agent (multi-clip workflow)."""

import json
import re

from .storyboard_format import format_duration


# ---------------------------------------------------------------------------
# Pre-processing helpers for multi-call Phase 2 pipeline
# ---------------------------------------------------------------------------


def extract_cast_from_reviews(clip_reviews: list[dict]) -> list[dict]:
    """Deduplicate people across all clip reviews into a single cast list.

    Groups by normalized label, merges descriptions, tracks which clips each
    person appears/speaks in. Replaces the per-review people arrays with a
    single cast reference to reduce prompt token usage.
    """
    cast_by_label: dict[str, dict] = {}
    for review in clip_reviews:
        clip_id = review.get("clip_id", "")
        for person in review.get("people", []):
            label = person.get("label", "unknown").strip().lower()
            if label not in cast_by_label:
                cast_by_label[label] = {
                    "label": person.get("label", label),
                    "description": person.get("description", ""),
                    "role": person.get("role", ""),
                    "appears_in": [],
                    "speaking_in": [],
                }
            entry = cast_by_label[label]
            if clip_id not in entry["appears_in"]:
                entry["appears_in"].append(clip_id)
            if person.get("speaking") and clip_id not in entry["speaking_in"]:
                entry["speaking_in"].append(clip_id)
            # Keep the longest description
            desc = person.get("description", "")
            if len(desc) > len(entry["description"]):
                entry["description"] = desc
    return list(cast_by_label.values())


def condense_clip_for_planning(review: dict, include_editorial_hints: bool = False) -> dict:
    """Reduce a clip review to planning-essential fields only.

    Strips people arrays (replaced by cast reference), strips full editorial
    notes, keeps: clip_id, duration, content_type, cast_present, speakers,
    key_moments (compact), usable_segments (with index), audio_summary.

    Args:
        include_editorial_hints: When True, passes through Phase 1 editorial_notes
            as ``editorial_hints``. Enable when the creative brief has explicit
            narrative beats — the hints become informational rather than anchoring.
    """
    quality = review.get("quality", {})
    condensed = {
        "clip_id": review.get("clip_id"),
        "total_usable_sec": sum(
            s.get("duration_sec", 0) for s in review.get("usable_segments", [])
        ),
        "content_type": review.get("content_type", []),
        "cast_present": [p.get("label") for p in review.get("people", [])],
        "speakers": [p.get("label") for p in review.get("people", []) if p.get("speaking")],
        "quality_overall": quality.get("overall", "fair"),
        "quality_composition": quality.get("composition", "casual"),
        "key_moments": [
            {
                "at": km.get("timestamp_sec"),
                "value": km.get("editorial_value"),
                "use": km.get("suggested_use"),
                "what": km.get("description"),
            }
            for km in review.get("key_moments", [])
        ],
        "usable_segments": [
            {
                "index": i,
                "in_sec": s.get("in_sec"),
                "out_sec": s.get("out_sec"),
                "duration_sec": s.get("duration_sec"),
                "description": s.get("description"),
            }
            for i, s in enumerate(review.get("usable_segments", []))
        ],
        "audio_summary": review.get("audio", {}).get("speech_summary"),
        "has_speech": review.get("audio", {}).get("has_speech", False),
    }
    if include_editorial_hints and review.get("editorial_notes"):
        condensed["editorial_hints"] = review["editorial_notes"]
    return condensed


def trim_transcript_to_usable(
    transcript_text: str,
    usable_segments: list[dict],
) -> str:
    """Keep only transcript lines whose timestamps fall within usable segment ranges."""
    if not transcript_text or not usable_segments:
        return transcript_text or ""
    usable_ranges = [(s.get("in_sec", 0), s.get("out_sec", 0)) for s in usable_segments]
    kept = []
    for line in transcript_text.split("\n"):
        ts = _extract_transcript_timestamp(line)
        if ts is None:
            kept.append(line)  # header lines, non-timestamped lines
        elif any(start - 1.0 <= ts <= end + 1.0 for start, end in usable_ranges):
            kept.append(line)
    return "\n".join(kept)


def _extract_transcript_timestamp(line: str) -> float | None:
    """Extract seconds from a transcript line like '[0:35]' or '[1:02:15]'."""
    m = re.match(r"\[(\d+):(\d+)(?::(\d+))?\]", line.strip())
    if not m:
        return None
    if m.group(3):  # H:MM:SS
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    return int(m.group(1)) * 60 + int(m.group(2))


def _format_condensed_clips(condensed_clips: list[dict]) -> str:
    """Format condensed clip summaries into compact text for Call 2A."""
    blocks = []
    for c in condensed_clips:
        lines = [f"## {c['clip_id']}"]
        lines.append(
            f"Usable: {c['total_usable_sec']:.0f}s | Content: {', '.join(c['content_type']) if c['content_type'] else 'unknown'}"
        )
        if c.get("cast_present"):
            lines.append(f"Cast: {', '.join(c['cast_present'])}")
        if c.get("speakers"):
            lines.append(f"Speakers: {', '.join(c['speakers'])}")
        if c.get("has_speech") and c.get("audio_summary"):
            lines.append(f"Speech: {c['audio_summary']}")
        for km in c.get("key_moments", []):
            lines.append(f"  @{km['at']:.1f}s [{km['value']}] {km['what']} (use: {km['use']})")
        segs = c.get("usable_segments", [])
        if segs:
            lines.append("Usable segments:")
            for s in segs:
                lines.append(
                    f"  [{s['index']}] {s['in_sec']:.1f}s–{s['out_sec']:.1f}s "
                    f"({s['duration_sec']:.1f}s): {s['description']}"
                )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _format_cast_list(cast: list[dict]) -> str:
    """Format deduplicated cast into compact text."""
    lines = []
    for c in cast:
        appears = len(c.get("appears_in", []))
        speaks = len(c.get("speaking_in", []))
        parts = [f"**{c['label']}** ({c['role']})"]
        if c.get("description"):
            parts.append(c["description"])
        parts.append(f"Appears in {appears} clips")
        if speaks:
            parts.append(f"speaks in {speaks}")
        lines.append("- " + " — ".join(parts))
    return "\n".join(lines)


def resolve_constraints_to_clips(
    user_context: dict,
    clip_reviews: list[dict],
) -> str:
    """Fuzzy-match user constraint mentions against Phase 1 clip data.

    Resolves free-text like "the sunset at the temple" to specific clip IDs
    and key_moment timestamps, so the LLM doesn't have to search through
    all reviews to find the right moment.

    Returns enhanced constraint text with clip references appended.
    """
    highlights = user_context.get("highlights", "")
    avoid = user_context.get("avoid", "")
    if not highlights and not avoid:
        return ""

    # Build a searchable index of key moments and summaries
    index: list[tuple[str, str, str]] = []  # (clip_id, text, reference)
    for r in clip_reviews:
        cid = r.get("clip_id", "")
        summary = r.get("summary", "")
        if summary:
            index.append((cid, summary.lower(), f"{cid} summary: {summary[:100]}"))
        for km in r.get("key_moments", []):
            desc = km.get("description", "")
            ts = km.get("timestamp_sec", 0)
            val = km.get("editorial_value", "")
            if desc:
                index.append(
                    (
                        cid,
                        desc.lower(),
                        f"{cid} @{ts:.1f}s [{val}]: {desc[:80]}",
                    )
                )
        for seg in r.get("usable_segments", []):
            desc = seg.get("description", "")
            if desc:
                index.append(
                    (
                        cid,
                        desc.lower(),
                        f"{cid} {seg.get('in_sec', 0):.1f}-{seg.get('out_sec', 0):.1f}s: {desc[:80]}",
                    )
                )

    def _find_matches(query: str, max_results: int = 3) -> list[str]:
        """Simple keyword overlap matching."""
        query_words = set(query.lower().split())
        # Remove common stop words
        stop = {"the", "a", "an", "at", "in", "on", "to", "of", "and", "is", "my", "we", "i"}
        query_words -= stop
        if not query_words:
            return []

        scored = []
        for cid, text, ref in index:
            text_words = set(text.split())
            overlap = len(query_words & text_words)
            if overlap > 0:
                scored.append((overlap, ref))
        scored.sort(key=lambda x: -x[0])
        return [ref for _, ref in scored[:max_results]]

    lines = []
    if highlights:
        # Split highlights by commas or common delimiters to match individually
        parts = [p.strip() for p in re.split(r"[,;]|(?:and )", highlights) if p.strip()]
        if not parts:
            parts = [highlights]
        for part in parts:
            matches = _find_matches(part)
            if matches:
                lines.append(f'  "{part[:60]}" → likely: {matches[0]}')
                for m in matches[1:]:
                    lines.append(f"    also: {m}")

    if avoid:
        parts = [p.strip() for p in re.split(r"[,;]|(?:and )", avoid) if p.strip()]
        if not parts:
            parts = [avoid]
        for part in parts:
            matches = _find_matches(part)
            if matches:
                lines.append(f'  Avoid "{part[:60]}" → check: {matches[0]}')

    if lines:
        return "\nClip references for filmmaker constraints:\n" + "\n".join(lines)
    return ""


# ---------------------------------------------------------------------------
# Few-shot example for Phase 2A reasoning
# ---------------------------------------------------------------------------

_PHASE2A_FEWSHOT_EXAMPLE = """
<example>
Here is an example of a well-structured editorial plan:

1. CONSTRAINT CHECK:
- MUST INCLUDE "the group photo at the summit" → C0023 usable segment [2] at 142.5-155.0s
  shows the full group standing at the summit marker. Will place as climax (segment 8).
- MUST EXCLUDE "camera bag footage" → C0005 segment [0] 0-45s and C0012 segment [0] 0-12s
  are accidental bag recordings. Excluding both clips entirely.

2. STORY CONCEPT: A family conquering a challenging trail together — the payoff is
earning the view at the top.

3. OPENING HOOK: C0023 usable segment [2] at 148.0-153.0s — flash-forward to the summit
celebration. Immediately shows the payoff, then we cut to the beginning of the hike.

4. SEGMENT SEQUENCE:
- C0023 segment [2] — hook, flash-forward to summit (ambient_only)
- C0001 segment [0] — establishing, parking lot arrival (ambient_only)
- C0003 segment [1] — context, trail entrance and gear check (preserve_dialogue)
- C0008 segment [0] — action, steep section with rope assist (ambient_only)
- C0012 segment [1] — action, forest canopy walk (music_bed)
- C0018 segment [0] — context, rest stop conversation about the view (preserve_dialogue)
- C0020 segment [0] — b_roll, panoramic ridge view (music_bed)
- C0023 segment [2] — climax, summit group photo MUST-INCLUDE (preserve_dialogue)
- C0025 segment [0] — reflection, descent through forest (music_bed)
- C0030 segment [0] — outro, restaurant dinner together (preserve_dialogue)

5. DISCARDED: C0005 (bag footage — MUST-EXCLUDE), C0009 (duplicate of C0008, same rope
section from different angle), C0015 (blurry, very shaky throughout).

6. PACING: Slow opening (establishing → context), accelerate through the climb (action
segments), breathe at the summit (climax), gentle descent to resolution.

7. MUSIC: Ambient guitar for establishing/context, build energy for action segments,
emotional piano at summit, gentle fade for descent and outro.
</example>
"""


# ---------------------------------------------------------------------------
# Multi-call Phase 2 prompt builders
# ---------------------------------------------------------------------------


_PHASE2A_REASONING_PROMPT = """\
You are a professional video editor who has watched all the dailies.
Write an editorial plan for a {style} from {clip_count} clips ({total_duration} of raw footage).

The available clip IDs are: {clip_ids}

Your plan MUST address these points IN ORDER:

1. CONSTRAINT CHECK: For each filmmaker MUST-INCLUDE and MUST-EXCLUDE item, \
state which clip and usable segment satisfies it. If a constraint cannot be satisfied, \
explain why.

2. CREATIVE DIRECTION: If the filmmaker provided a CREATIVE DIRECTION section below, \
acknowledge their intent, audience, and narrative direction. Your story concept and all \
segment selections must serve these. If key beats are specified, map each beat to candidate clips.

3. STORY CONCEPT: What narrative does this footage support? What is the editorial angle?

4. OPENING HOOK: What are the strongest first 10 seconds? Which clip and segment?

5. SEGMENT SEQUENCE: List each segment in output order:
   - Clip ID (use FULL clip IDs from the list above — never abbreviate)
   - Which usable segment (by [index] number from the clip data below)
   - Purpose (hook, establishing, context, action, reaction, b_roll, climax, outro)
   - Audio strategy (preserve_dialogue, music_bed, ambient_only)

6. DISCARDED CLIPS: Which clips are you cutting and why?

7. PACING: Where is the edit fast vs slow? Where does it breathe?

8. MUSIC DIRECTION: What audio approach ties this together?

Think freely. Write in natural language. Be specific about clip and segment references.
"""


def build_phase2a_reasoning_prompt(
    clip_reviews: list[dict],
    style: str,
    total_duration_sec: float,
    cast: list[dict],
    condensed_clips: list[dict],
    transcripts: dict[str, str] | None = None,
    filming_timeline: str | None = None,
    user_context_text: str | None = None,
    user_context: dict | None = None,
    style_supplement: str | None = None,
) -> str:
    """Build the freeform reasoning prompt for Call 2A (no structured output).

    Args:
        user_context_text: Pre-formatted constraint/preference text.
        user_context: Raw user_context dict for constraint → clip resolution.
    """
    clip_ids = [c["clip_id"] for c in condensed_clips]
    prompt = _PHASE2A_REASONING_PROMPT.format(
        style=style,
        clip_count=len(condensed_clips),
        total_duration=format_duration(total_duration_sec),
        clip_ids=", ".join(clip_ids),
    )

    # Few-shot example (shows the model what a good plan looks like)
    prompt += _PHASE2A_FEWSHOT_EXAMPLE

    if user_context_text:
        prompt += "\n\n" + user_context_text

    # Constraint → clip resolution (fuzzy-match user mentions to specific clips)
    if user_context:
        resolved = resolve_constraints_to_clips(user_context, clip_reviews)
        if resolved:
            prompt += "\n" + resolved

    if style_supplement:
        prompt += "\n\n" + style_supplement

    if filming_timeline:
        prompt += f"\n\nFilming Timeline (chronological shooting order):\n{filming_timeline}\n"

    # Cast list
    prompt += "\n\nCast (deduplicated across all clips):\n" + _format_cast_list(cast)

    # Condensed clip data
    prompt += "\n\n---\nClip Data:\n\n" + _format_condensed_clips(condensed_clips) + "\n---"

    # Inline transcripts
    if transcripts:
        prompt += "\n\nTranscripts (trimmed to usable segments):"
        for cid, text in transcripts.items():
            if text.strip():
                prompt += f"\n\n### {cid}\n{text}"

    prompt += (
        "\n\nNow write your editorial plan. Think freely — address every point above, "
        "starting with the CONSTRAINT CHECK."
    )
    return prompt


_PHASE2A_STRUCTURING_PROMPT = """\
Convert the editorial plan below into a StoryPlan JSON. \
Your job is faithful translation — do not add, remove, or change editorial decisions from the plan.

If the plan references a clip ambiguously, resolve it to the closest matching full clip ID \
from this list: {clip_ids}

## Editorial Plan

{editorial_plan}
"""


def build_phase2a_structuring_prompt(
    editorial_plan_text: str,
    clip_ids: list[str],
) -> str:
    """Build the structuring prompt for Call 2A.5 (converts freeform plan → StoryPlan JSON)."""
    return _PHASE2A_STRUCTURING_PROMPT.format(
        editorial_plan=editorial_plan_text,
        clip_ids=", ".join(clip_ids),
    )


_PHASE2B_ASSEMBLY_PROMPT = """\
You are assembling a video edit from a pre-approved editorial plan.

For each planned segment below, select precise in_sec and out_sec timestamps \
WITHIN the usable segment range shown. The creative decisions are already made — \
your job is mechanical refinement: precise timestamp selection and detailed descriptions.

HARD CONSTRAINTS:
- in_sec and out_sec must fall within the "Usable range" shown for each segment
- in_sec < out_sec
- Timestamps are in seconds, relative to clip start (not global timeline)
- Use the transcript to find natural cut points (sentence boundaries, pauses, scene transitions)
- clip_id must be EXACT — copy it character-for-character from each segment header
"""


def build_phase2b_assembly_prompt(
    story_plan,
    clip_reviews: list[dict],
    transcripts: dict[str, str] | None = None,
    style: str = "vlog",
) -> str:
    """Build the precise assembly prompt for Call 2B (StoryPlan → EditorialStoryboard)."""
    reviews_by_id = {r.get("clip_id", ""): r for r in clip_reviews}
    prompt = _PHASE2B_ASSEMBLY_PROMPT

    # Story context from the plan
    prompt += f"\n\nVideo title: {story_plan.title}"
    prompt += f"\nStyle: {story_plan.style}"
    prompt += f"\nStory concept: {story_plan.story_concept}"
    if story_plan.pacing_notes:
        prompt += f"\nPacing: {story_plan.pacing_notes}"
    if story_plan.music_direction:
        prompt += f"\nMusic: {story_plan.music_direction}"

    # Cast reference
    if story_plan.cast:
        prompt += "\n\nCast:"
        for c in story_plan.cast:
            prompt += f"\n- {c.name} ({c.role}): {c.description}"

    # Story arc
    if story_plan.story_arc:
        prompt += "\n\nStory arc:"
        for arc in story_plan.story_arc:
            prompt += f"\n- {arc.title}: {arc.description}"

    # Per-segment assembly instructions with bounded windows
    prompt += "\n\n---\nPlanned Segments:\n"
    for i, ps in enumerate(story_plan.planned_segments):
        review = reviews_by_id.get(ps.clip_id, {})
        usable = review.get("usable_segments", [])
        seg_data = (
            usable[ps.usable_segment_index] if ps.usable_segment_index < len(usable) else None
        )

        prompt += f"\n## Segment {i}"
        prompt += f"\nClip: {ps.clip_id}"
        if seg_data:
            in_s = seg_data.get("in_sec", 0)
            out_s = seg_data.get("out_sec", 0)
            dur = seg_data.get("duration_sec", out_s - in_s)
            prompt += f"\nUsable range: {in_s:.1f}s – {out_s:.1f}s ({dur:.1f}s available)"
            prompt += f"\nSegment content: {seg_data.get('description', '')}"
        else:
            prompt += "\nUsable range: (could not resolve segment index)"
        prompt += f"\nPurpose: {ps.purpose}"
        prompt += f"\nPlan: {ps.narrative_role}"
        prompt += f"\nAudio: {ps.audio_strategy}"

        # Inline transcript for this segment's time range
        if transcripts and ps.clip_id in transcripts and seg_data:
            trimmed = trim_transcript_to_usable(
                transcripts[ps.clip_id],
                [seg_data],
            )
            if trimmed.strip():
                prompt += f"\nTranscript:\n{trimmed}"

        prompt += "\n→ Select in_sec and out_sec. Write the segment description and audio_note.\n"

    prompt += "---"

    # Discarded clips for context
    if story_plan.discarded:
        prompt += "\n\nDiscarded clips:"
        for d in story_plan.discarded:
            prompt += f"\n- {d.clip_id}: {d.reason}"

    prompt += (
        f"\n\nNow produce the EditorialStoryboard with precise timestamps for a compelling {style}."
        "\nUse editorial_reasoning to briefly confirm the plan is being followed, "
        "then fill in every segment with exact in_sec/out_sec within the bounded ranges."
        "\n\nMUSIC PLAN: For each story arc section, populate one MusicCue in music_plan with:"
        "\n- section: the arc section name (must match a story_arc title)"
        "\n- strategy: one of upbeat_background, emotional_underscore, ambient_texture, silence, natural_audio_only"
        "\n- notes: optional tempo/mood/genre suggestions (e.g. 'gentle acoustic, 100 BPM')"
    )
    return prompt


def classify_clip_priority(review: dict) -> str:
    """Classify a clip review as high/medium/low editorial priority.

    Used for tiered context compression in large projects (15+ clips).
    - high: clips with high-value key moments, speech, or multiple usable segments
    - medium: clips with moderate usable content
    - low: B-roll only, single short segment, or poor quality
    """
    high_moments = sum(
        1 for km in review.get("key_moments", []) if km.get("editorial_value") == "high"
    )
    usable = review.get("usable_segments", [])
    total_usable_sec = sum(s.get("duration_sec", 0) for s in usable)
    has_speech = review.get("audio", {}).get("has_speech", False)
    quality = review.get("quality", {}).get("overall", "fair")

    if high_moments >= 2 or (has_speech and total_usable_sec > 30):
        return "high"
    if quality == "poor" or total_usable_sec < 5:
        return "low"
    content = review.get("content_type", [])
    if isinstance(content, list) and all(
        c in ("b_roll", "establishing", "landscape") for c in content
    ):
        if total_usable_sec < 15:
            return "low"
    return "medium"


def _format_clip_review_full(r: dict, transcripts: dict[str, str] | None = None) -> str:
    """Format a single clip review at full detail (Tier A)."""
    cid = r.get("clip_id", "unknown")
    lines = [f"## {cid}"]
    lines.append(r.get("summary", ""))

    q = r.get("quality", {})
    if q:
        parts = [f"{k}={v}" for k, v in q.items()]
        lines.append(f"Quality: {', '.join(parts)}")

    ct = r.get("content_type", [])
    if ct:
        lines.append(f"Content: {', '.join(ct) if isinstance(ct, list) else ct}")

    for p in r.get("people", []):
        role = p.get("role", "")
        desc = p.get("description", "")
        speaking = " (speaking)" if p.get("speaking") else ""
        pct = p.get("screen_time_pct")
        pct_str = f" {pct:.0%}" if pct else ""
        lines.append(f"  Person: {p.get('label', '?')} — {role}{pct_str}{speaking}: {desc}")

    for km in r.get("key_moments", []):
        ts = km.get("timestamp_sec", 0)
        val = km.get("editorial_value", "")
        use = km.get("suggested_use", "")
        lines.append(f"  @{ts:.1f}s [{val}] {km.get('description', '')} (use: {use})")

    segs = r.get("usable_segments", [])
    if segs:
        lines.append("Usable segments:")
        for s in segs:
            lines.append(
                f"  {s.get('in_sec', 0):.1f}s–{s.get('out_sec', 0):.1f}s "
                f"({s.get('duration_sec', 0):.1f}s, {s.get('quality', '?')}): "
                f"{s.get('description', '')}"
            )

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

    notes = r.get("editorial_notes", "")
    if notes:
        lines.append(f"Editorial: {notes}")

    if transcripts and cid in transcripts:
        lines.append(f"Transcript:\n{transcripts[cid]}")

    return "\n".join(lines)


def _format_clip_review_summary(r: dict) -> str:
    """Format a single clip review at summary detail (Tier B)."""
    cid = r.get("clip_id", "unknown")
    lines = [f"## {cid}"]
    lines.append(r.get("summary", ""))

    # Best 2-3 usable segments only
    segs = r.get("usable_segments", [])
    best = sorted(segs, key=lambda s: s.get("duration_sec", 0), reverse=True)[:3]
    if best:
        lines.append("Best segments:")
        for s in best:
            lines.append(
                f"  {s.get('in_sec', 0):.1f}s–{s.get('out_sec', 0):.1f}s "
                f"({s.get('duration_sec', 0):.1f}s): {s.get('description', '')}"
            )

    a = r.get("audio", {})
    if a and a.get("speech_summary"):
        lines.append(f"Speech: {a['speech_summary']}")

    return "\n".join(lines)


def _format_clip_review_oneliner(r: dict) -> str:
    """Format a single clip review as a one-liner (Tier C)."""
    cid = r.get("clip_id", "unknown")
    total_usable = sum(s.get("duration_sec", 0) for s in r.get("usable_segments", []))
    n_segs = len(r.get("usable_segments", []))
    summary = r.get("summary", "")[:80]
    ct = r.get("content_type", [])
    ct_str = ", ".join(ct) if isinstance(ct, list) else str(ct)

    best = r.get("usable_segments", [])[:1]
    seg_str = ""
    if best:
        s = best[0]
        seg_str = f" Best: {s.get('in_sec', 0):.1f}-{s.get('out_sec', 0):.1f}s"

    return f"## {cid} — {ct_str}, {total_usable:.0f}s usable ({n_segs} segs).{seg_str} {summary}"


def _format_clip_reviews_text(
    clip_reviews: list[dict],
    transcripts: dict[str, str] | None = None,
    tiered: bool = False,
) -> str:
    """Flatten clip review dicts into compact plain-text for the Phase 2 prompt.

    Drops JSON overhead, duplicate timestamp formats (keeps seconds only),
    and discard_segments (Phase 2 only needs usable segments).
    Each clip's transcript (if available) is inlined after its review.

    If tiered=True and there are 15+ clips, applies tiered compression:
    - high priority: full detail (Tier A)
    - medium priority: summary with best segments (Tier B)
    - low priority: one-liner (Tier C)
    """
    use_tiers = tiered and len(clip_reviews) >= 15

    blocks = []
    for r in clip_reviews:
        if use_tiers:
            priority = classify_clip_priority(r)
            if priority == "high":
                blocks.append(_format_clip_review_full(r, transcripts))
            elif priority == "medium":
                blocks.append(_format_clip_review_summary(r))
            else:
                blocks.append(_format_clip_review_oneliner(r))
        else:
            blocks.append(_format_clip_review_full(r, transcripts))

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
{orientation_line}"""

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
    orientation: str | None = None,
    aspect_ratio: str | None = None,
    transcript_text: str | None = None,
    style_supplement: str | None = None,
    user_context: dict | None = None,
    include_json_template: bool = True,
) -> str:
    orientation_line = ""
    if orientation:
        orientation_line = f"Orientation: {orientation}"
        if aspect_ratio:
            orientation_line += f" ({aspect_ratio})"
    fmt = dict(
        clip_id=clip_id,
        filename=filename,
        duration=format_duration(duration_sec),
        resolution=resolution,
        orientation_line=orientation_line,
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
        from .briefing import format_brief_for_prompt

        prompt += (
            "\n\n"
            + format_brief_for_prompt(user_context, phase="phase1")
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
    reviews_text = _format_clip_reviews_text(clip_reviews, transcripts, tiered=True)
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
        "\n\nNow produce the EditorialStoryboard."
        "\n\nBEFORE writing segments, use editorial_reasoning to:"
        "\n1. State how you satisfy each filmmaker MUST-INCLUDE/MUST-EXCLUDE constraint"
        "\n2. Explain your story arc and opening hook choice"
        "\n3. Note any constraints you cannot satisfy and why"
        "\n\nThen produce the segments. The filmmaker's MUST-INCLUDE and MUST-EXCLUDE "
        "items are non-negotiable requirements, not suggestions."
        "\n\nMUSIC PLAN: For each story arc section, populate one MusicCue in music_plan with:"
        "\n- section: the arc section name (must match a story_arc title)"
        "\n- strategy: one of upbeat_background, emotional_underscore, ambient_texture, silence, natural_audio_only"
        "\n- notes: optional tempo/mood/genre suggestions (e.g. 'gentle acoustic, 100 BPM')"
    )
    return prompt


# ---------------------------------------------------------------------------
# Multi-call Phase 3 — Visual Monologue prompt builders
# ---------------------------------------------------------------------------


def build_monologue_call1_prompt(
    storyboard,
    transcripts: dict[str, str] | None = None,
    user_context_text: str | None = None,
) -> str:
    """Call 1: Segment analysis & arc planning — which segments get overlays?"""
    rows = []
    for seg in storyboard.segments:
        has_speech = "SPEECH" if seg.audio_note and "dialogue" in seg.audio_note.lower() else ""
        if not has_speech and seg.audio_note and "preserve" in seg.audio_note.lower():
            has_speech = "SPEECH"
        rows.append(
            f"  [{seg.index}] {seg.clip_id} {seg.in_sec:.1f}-{seg.out_sec:.1f}s "
            f"({seg.duration_sec:.1f}s) purpose={seg.purpose} audio={seg.audio_note or 'none'} "
            f"{'[HAS SPEECH]' if has_speech else '[NO SPEECH]'}: {seg.description}"
        )

    prompt = (
        "You are analyzing a video edit timeline to plan text overlay placement.\n\n"
        f"Title: {storyboard.title}\n"
        f"Style: {storyboard.style}\n"
        f"Story: {storyboard.story_concept}\n"
    )

    if user_context_text:
        prompt += f"\n{user_context_text}\n"

    prompt += "\nSegments:\n" + "\n".join(rows)

    if transcripts:
        prompt += "\n\nTranscripts (to verify speech presence):"
        for cid, text in transcripts.items():
            if text.strip():
                prompt += f"\n### {cid}\n{text}"

    prompt += (
        "\n\n---\n"
        "For each segment, determine if it is ELIGIBLE for text overlays:\n"
        "- ELIGIBLE: No speech in the segment (check transcript + audio_note)\n"
        "- NOT ELIGIBLE: Contains dialogue or speech (those get speech captions instead)\n\n"
        "For eligible segments, provide:\n"
        "- arc_phase: grounding_hook (first 15-20%), wandering_middle (20-80%), "
        "resolution (final 20%)\n"
        "- intent: what should the overlay accomplish at this narrative moment?\n"
        "- preceding/following context: 1-line summary of adjacent speech segments\n"
        "- max_overlay_count: 1-3 based on segment duration and arc phase\n\n"
        "Also recommend a persona (conversational_confidant, detached_observer, "
        "or stream_of_consciousness) with rationale.\n\n"
        "Output as OverlayPlan JSON."
    )
    return prompt


def build_monologue_call2_prompt(
    overlay_plan,
    storyboard,
    persona_hint: str | None = None,
) -> str:
    """Call 2: Creative text generation within bounded segment windows."""
    persona = persona_hint or overlay_plan.persona_recommendation

    prompt = (
        f"You are writing visual monologue text overlays as the **{persona}** persona.\n\n"
        "WRITING RULES:\n"
        "- ALL TEXT MUST BE LOWERCASE (the 'lowercase whisper' — soft, intimate tone)\n"
        "- Use '...' for pauses, passage of time, or deep sighs\n"
        "- Keep overlays concise: 5-8 words each\n"
        "- Break one thought across multiple overlays on consecutive segments\n"
        "- Two-Breath Rule: duration = word_count * 0.4 minimum, word_count * 0.6 recommended\n"
        "- Leave at least 3 seconds of no-text between consecutive overlays\n\n"
    )

    prompt += "ELIGIBLE SEGMENTS (write overlays ONLY for these):\n\n"
    for es in overlay_plan.eligible_segments:
        seg = next((s for s in storyboard.segments if s.index == es.segment_index), None)
        prompt += f"## Segment {es.segment_index}"
        if seg:
            prompt += f" — {seg.description}"
        prompt += (
            f"\nDuration: {es.segment_duration_sec:.1f}s"
            f"\nArc phase: {es.arc_phase}"
            f"\nIntent: {es.intent}"
            f"\nMax overlays: {es.max_overlay_count}"
        )
        if es.preceding_context:
            prompt += f"\nPreceding speech: {es.preceding_context}"
        if es.following_context:
            prompt += f"\nFollowing speech: {es.following_context}"
        if es.notes:
            prompt += f"\nNotes: {es.notes}"
        prompt += (
            f"\n→ Write 1-{es.max_overlay_count} overlays. appear_at must be 0.0 to "
            f"{es.segment_duration_sec:.1f}. appear_at + duration must not exceed "
            f"{es.segment_duration_sec:.1f}.\n\n"
        )

    prompt += (
        "MONOLOGUE ARC:\n"
        "- grounding_hook segments: Welcome audience, establish theme/location/mood. "
        "Most text-heavy section.\n"
        "- wandering_middle segments: Reflect on what's happening. "
        "Sparser — let visuals breathe.\n"
        "- resolution segments: Wind down. Warm sign-off.\n\n"
        "Output as OverlayDrafts JSON with all overlays in chronological order."
    )
    return prompt


def validate_monologue_overlays(
    overlays: list,
    eligible_segments: list,
    storyboard_segments: list,
) -> tuple[list, list[str]]:
    """Deterministic validation and auto-fix of monologue overlays.

    Returns (fixed_overlays, fix_log).
    """
    fix_log = []
    seg_durations = {es.segment_index: es.segment_duration_sec for es in eligible_segments}
    eligible_indices = {es.segment_index for es in eligible_segments}

    fixed = []
    for ov in overlays:
        # Skip overlays on non-eligible segments
        if ov.segment_index not in eligible_indices:
            fix_log.append(
                f"Removed overlay on non-eligible segment {ov.segment_index}: '{ov.text}'"
            )
            continue

        seg_dur = seg_durations.get(ov.segment_index, 999)

        # Ensure lowercase
        if ov.text != ov.text.lower():
            fix_log.append(f"Lowercased: '{ov.text}' → '{ov.text.lower()}'")
            ov.text = ov.text.lower()

        # Clamp appear_at
        if ov.appear_at < 0:
            fix_log.append(f"Seg {ov.segment_index}: clamped appear_at {ov.appear_at:.1f} → 0.0")
            ov.appear_at = 0.0

        # Enforce word count
        wc = len(ov.text.split())
        ov.word_count = wc

        # Enforce two-breath rule minimum
        min_dur = wc * 0.4
        if ov.duration_sec < min_dur:
            fix_log.append(
                f"Seg {ov.segment_index}: duration {ov.duration_sec:.1f}s "
                f"below minimum {min_dur:.1f}s for {wc} words → fixed"
            )
            ov.duration_sec = min_dur

        # Clamp to segment boundary
        if ov.appear_at + ov.duration_sec > seg_dur:
            old_dur = ov.duration_sec
            ov.duration_sec = max(0.1, seg_dur - ov.appear_at)
            fix_log.append(
                f"Seg {ov.segment_index}: clamped duration {old_dur:.1f}s → {ov.duration_sec:.1f}s "
                f"(segment boundary)"
            )

        # Skip if no time left
        if ov.duration_sec < 0.5:
            fix_log.append(f"Removed overlay too short after clamping: '{ov.text}'")
            continue

        fixed.append(ov)

    return fixed, fix_log


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
