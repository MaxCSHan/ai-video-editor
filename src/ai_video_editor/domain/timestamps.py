"""Timestamp clamping — constrain storyboard segments to usable bounds.

After Phase 2 LLM calls, segment timestamps may fall outside the usable
ranges identified in Phase 1 clip reviews. This module clamps them to
the best-overlapping usable segment bounds.

Pure domain logic — no I/O, no LLM calls.
"""

from __future__ import annotations


def clamp_segments_to_usable(
    storyboard,
    reviews_by_id: dict[str, dict],
) -> list[str]:
    """Clamp storyboard segment timestamps to usable segment bounds.

    For each segment, finds the Phase 1 usable segment with the maximum
    time overlap and clamps in_sec/out_sec to its bounds.

    Mutates storyboard segments in-place.

    Args:
        storyboard: EditorialStoryboard with segments to clamp.
        reviews_by_id: {clip_id: review_dict} mapping from Phase 1.

    Returns:
        List of human-readable fix descriptions (e.g.,
        "Seg 3: clamped in_sec 1.2 → 2.0").
    """
    fix_log: list[str] = []

    for seg in storyboard.segments:
        review = reviews_by_id.get(seg.clip_id)
        if not review:
            continue
        usable = review.get("usable_segments", [])

        # Find usable segment with maximum overlap
        best = None
        best_overlap = -1.0
        for us in usable:
            us_in = us.get("in_sec", 0)
            us_out = us.get("out_sec", 0)
            overlap = min(seg.out_sec, us_out) - max(seg.in_sec, us_in)
            if overlap > best_overlap:
                best_overlap = overlap
                best = us

        if best:
            bound_in = best.get("in_sec", 0)
            bound_out = best.get("out_sec", 0)
            if seg.in_sec < bound_in:
                fix_log.append(f"Seg {seg.index}: clamped in_sec {seg.in_sec:.1f} → {bound_in:.1f}")
                seg.in_sec = bound_in
            if seg.out_sec > bound_out:
                fix_log.append(
                    f"Seg {seg.index}: clamped out_sec {seg.out_sec:.1f} → {bound_out:.1f}"
                )
                seg.out_sec = bound_out

    return fix_log
