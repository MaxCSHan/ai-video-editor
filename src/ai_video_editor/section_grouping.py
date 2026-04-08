"""Section grouping and merge for Divide & Conquer Phase 2.

Groups clips into hierarchical sections (day → scene) using creation_time
metadata and time-gap splitting. Provides deterministic merge of per-section
storyboards into a single EditorialStoryboard.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from .models import (
    CastMember,
    DiscardedClip,
    EditorialStoryboard,
    HookStoryboard,
    MusicCue,
    Section,
    SectionGroup,
    SectionPlan,
    SectionStoryboard,
    StoryArcSection,
)


# ---------------------------------------------------------------------------
# Section grouping (deterministic)
# ---------------------------------------------------------------------------


def group_clips_into_sections(
    manifest: dict,
    clip_reviews: list[dict],
    gap_threshold_minutes: float = 30.0,
) -> list[SectionGroup]:
    """Group clips into hierarchical day → scene sections.

    Tier 1: group by date (from creation_time in manifest).
    Tier 2: within each date, split by time gaps exceeding the threshold.

    Clips without creation_time are placed in an "unknown" group at the end.
    """
    clips_data = manifest.get("clips", [])
    reviews_by_id = {r.get("clip_id", ""): r for r in clip_reviews}

    # Parse creation_time and group by date
    date_groups: dict[str, list[dict]] = defaultdict(list)
    no_date: list[dict] = []

    for clip in clips_data:
        ct = clip.get("creation_time")
        if ct:
            try:
                dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                clip["_parsed_dt"] = dt
                date_key = dt.strftime("%Y-%m-%d")
                date_groups[date_key].append(clip)
            except (ValueError, TypeError):
                no_date.append(clip)
        else:
            no_date.append(clip)

    # Sort dates chronologically
    sorted_dates = sorted(date_groups.keys())

    groups: list[SectionGroup] = []
    for day_idx, date_key in enumerate(sorted_dates, 1):
        day_clips = sorted(date_groups[date_key], key=lambda c: c.get("_parsed_dt", ""))
        sections = _split_by_gap(day_clips, gap_threshold_minutes, day_idx, reviews_by_id)

        # Format label
        try:
            dt = datetime.strptime(date_key, "%Y-%m-%d")
            label = f"Day {day_idx} — {dt.strftime('%b %d')}"
        except ValueError:
            label = f"Day {day_idx} — {date_key}"

        groups.append(
            SectionGroup(
                group_id=f"day{day_idx}",
                date=date_key,
                label=label,
                sections=sections,
            )
        )

    # Handle clips with no date
    if no_date:
        day_idx = len(groups) + 1
        sections = [
            Section(
                section_id=f"day{day_idx}_scene1",
                label="Unknown time",
                clip_ids=[c.get("clip_id", "") for c in no_date],
                activity="unknown",
            )
        ]
        groups.append(
            SectionGroup(
                group_id=f"day{day_idx}",
                date="unknown",
                label=f"Day {day_idx} — Unknown date",
                sections=sections,
            )
        )

    # Label sections from review content
    groups = label_sections_from_reviews(groups, clip_reviews)
    return groups


def _split_by_gap(
    day_clips: list[dict],
    gap_minutes: float,
    day_idx: int,
    reviews_by_id: dict[str, dict],
) -> list[Section]:
    """Split a day's clips into sections by time gaps."""
    if not day_clips:
        return []

    gap_seconds = gap_minutes * 60
    sections: list[Section] = []
    current_group: list[dict] = [day_clips[0]]

    for i in range(1, len(day_clips)):
        prev_dt = day_clips[i - 1].get("_parsed_dt")
        curr_dt = day_clips[i].get("_parsed_dt")

        if prev_dt and curr_dt:
            # Account for previous clip's duration
            prev_dur = day_clips[i - 1].get("duration_sec", 0)
            gap = (curr_dt - prev_dt).total_seconds() - prev_dur
            if gap > gap_seconds:
                sections.append(_make_section(current_group, day_idx, len(sections) + 1))
                current_group = []

        current_group.append(day_clips[i])

    if current_group:
        sections.append(_make_section(current_group, day_idx, len(sections) + 1))

    return sections


def _make_section(clips: list[dict], day_idx: int, scene_idx: int) -> Section:
    """Create a Section from a list of clip dicts."""
    clip_ids = [c.get("clip_id", "") for c in clips]

    # Build time range from first/last creation_time
    time_range = ""
    first_dt = clips[0].get("_parsed_dt")
    last_dt = clips[-1].get("_parsed_dt")
    if first_dt and last_dt:
        time_range = f"{first_dt.strftime('%H:%M')}-{last_dt.strftime('%H:%M')}"

    return Section(
        section_id=f"day{day_idx}_scene{scene_idx}",
        label=f"Scene {scene_idx}",
        clip_ids=clip_ids,
        time_range=time_range,
    )


def label_sections_from_reviews(
    groups: list[SectionGroup],
    clip_reviews: list[dict],
) -> list[SectionGroup]:
    """Enrich section labels using Phase 1 review content (content_type, key_moments)."""
    reviews_by_id = {r.get("clip_id", ""): r for r in clip_reviews}

    for group in groups:
        for section in group.sections:
            # Collect content types and key moment descriptions from this section's clips
            content_types: list[str] = []
            descriptions: list[str] = []

            for clip_id in section.clip_ids:
                review = reviews_by_id.get(clip_id, {})
                ct = review.get("content_type", [])
                if isinstance(ct, list):
                    content_types.extend(ct)
                for km in review.get("key_moments", []):
                    desc = km.get("description", "")
                    if desc and km.get("editorial_value") in ("high", "medium"):
                        descriptions.append(desc)

            # Pick the best label from available data
            label = _infer_label(content_types, descriptions, section.time_range)
            if label:
                section.activity = label
                section.label = label

    return groups


def _infer_label(
    content_types: list[str],
    descriptions: list[str],
    time_range: str,
) -> str:
    """Infer a human-readable section label from review content."""
    # Use the most common non-generic content type
    generic = {"b_roll", "establishing", "transition", "unknown"}
    specific = [ct for ct in content_types if ct not in generic]

    if descriptions:
        # Use the shortest key moment description as the label
        best = min(descriptions, key=len)
        if len(best) <= 50:
            return best

    if specific:
        # Use the most common specific content type
        from collections import Counter

        most_common = Counter(specific).most_common(1)[0][0]
        return most_common.replace("_", " ").title()

    return ""


# ---------------------------------------------------------------------------
# Section summary for prompts
# ---------------------------------------------------------------------------


def summarize_section_for_prompt(
    group: SectionGroup,
    section: Section,
    clip_reviews: list[dict],
) -> str:
    """Condensed section summary for the storyline prompt."""
    reviews_by_id = {r.get("clip_id", ""): r for r in clip_reviews}

    lines = [f"### {section.label} ({section.section_id})"]
    lines.append(f"Day: {group.label} | Time: {section.time_range or 'unknown'}")
    lines.append(f"Clips: {len(section.clip_ids)}")

    total_usable = 0.0
    highlights: list[str] = []
    has_speech = False

    for clip_id in section.clip_ids:
        review = reviews_by_id.get(clip_id, {})
        usable = review.get("usable_segments", [])
        total_usable += sum(s.get("duration_sec", 0) for s in usable)
        if review.get("audio", {}).get("has_speech"):
            has_speech = True
        for km in review.get("key_moments", []):
            if km.get("editorial_value") == "high":
                highlights.append(km.get("description", ""))

    lines.append(f"Usable footage: {total_usable:.0f}s | Speech: {'yes' if has_speech else 'no'}")
    if highlights:
        lines.append("Highlights:")
        for h in highlights[:5]:
            lines.append(f"  - {h}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Display formatting
# ---------------------------------------------------------------------------


def format_sections_for_display(
    groups: list[SectionGroup],
    clip_reviews: list[dict],
) -> str:
    """Human-readable section tree for TUI/CLI."""
    reviews_by_id = {r.get("clip_id", ""): r for r in clip_reviews}
    lines: list[str] = []

    total_clips = sum(len(s.clip_ids) for g in groups for s in g.sections)
    total_sections = sum(len(g.sections) for g in groups)
    lines.append(f"  {total_clips} clips, {len(groups)} days, {total_sections} sections\n")

    for group in groups:
        group_clips = sum(len(s.clip_ids) for s in group.sections)
        lines.append(f"  {group.label} ({group_clips} clips)")

        for si, section in enumerate(group.sections):
            is_last_section = si == len(group.sections) - 1
            branch = "└── " if is_last_section else "├── "
            time_str = f", {section.time_range}" if section.time_range else ""
            lines.append(f"  {branch}{section.label} ({len(section.clip_ids)} clips{time_str})")

            child_prefix = "      " if is_last_section else "  │   "
            for ci, clip_id in enumerate(section.clip_ids):
                is_last_clip = ci == len(section.clip_ids) - 1
                clip_branch = "└── " if is_last_clip else "├── "
                review = reviews_by_id.get(clip_id, {})
                dur = review.get("duration_sec", 0)
                summary = review.get("summary", "")
                # Truncate summary
                if len(summary) > 40:
                    summary = summary[:37] + "..."
                lines.append(
                    f"  {child_prefix}{clip_branch}{clip_id} — {dur:.0f}s"
                    + (f" — {summary}" if summary else "")
                )

        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Merge section storyboards into final EditorialStoryboard
# ---------------------------------------------------------------------------


def merge_section_storyboards(
    hook: HookStoryboard | None,
    section_storyboards: list[SectionStoryboard],
    section_plan: SectionPlan,
    section_groups: list[SectionGroup],
) -> EditorialStoryboard:
    """Deterministic merge: hook + ordered sections → single EditorialStoryboard.

    Re-indexes segments sequentially, builds story_arc from section narratives,
    merges cast/discarded/music_plan.
    """
    all_segments = []
    all_discarded: list[DiscardedClip] = []
    all_cast: dict[str, CastMember] = {}
    all_music: list[MusicCue] = []
    story_arc: list[StoryArcSection] = []
    editorial_parts: list[str] = []

    # Narrative lookup
    narrative_by_id = {sn.section_id: sn for sn in section_plan.section_narratives}

    # Hook segments
    if hook and hook.segments:
        hook_indices = []
        for seg in hook.segments:
            seg.index = len(all_segments)
            hook_indices.append(seg.index)
            all_segments.append(seg)

        story_arc.append(
            StoryArcSection(
                title="Opening Hook",
                description=hook.hook_concept,
                segment_indices=hook_indices,
            )
        )
        if hook.editorial_reasoning:
            editorial_parts.append(f"[Hook] {hook.editorial_reasoning}")

    # Section storyboards in order
    for ssb in section_storyboards:
        section_indices = []
        for seg in ssb.segments:
            seg.index = len(all_segments)
            section_indices.append(seg.index)
            all_segments.append(seg)

        # Build arc section from narrative
        narrative = narrative_by_id.get(ssb.section_id)
        arc_title = narrative.narrative_role if narrative else ssb.section_id
        story_arc.append(
            StoryArcSection(
                title=arc_title,
                description=ssb.narrative_summary,
                segment_indices=section_indices,
            )
        )

        # Merge discarded
        all_discarded.extend(ssb.discarded)

        # Merge cast (deduplicate by name)
        for member in ssb.cast:
            key = member.name.strip().lower()
            if key in all_cast:
                # Merge appears_in lists
                existing = all_cast[key]
                merged_appears = list(dict.fromkeys(existing.appears_in + member.appears_in))
                all_cast[key] = CastMember(
                    name=existing.name,
                    description=existing.description or member.description,
                    role=existing.role or member.role,
                    appears_in=merged_appears,
                )
            else:
                all_cast[key] = member

        # Merge music
        if ssb.music_cue:
            all_music.append(ssb.music_cue)

        if ssb.editorial_reasoning:
            editorial_parts.append(f"[{ssb.section_id}] {ssb.editorial_reasoning}")

    # Compute total duration
    total_duration = sum(s.duration_sec for s in all_segments)

    return EditorialStoryboard(
        editorial_reasoning="\n\n".join(editorial_parts),
        title=section_plan.title,
        estimated_duration_sec=total_duration,
        style=section_plan.style,
        story_concept=section_plan.story_concept,
        cast=list(all_cast.values()),
        story_arc=story_arc,
        segments=all_segments,
        discarded=all_discarded,
        music_plan=all_music,
        pacing_notes=[section_plan.pacing_notes] if section_plan.pacing_notes else [],
        technical_notes=[],
    )
