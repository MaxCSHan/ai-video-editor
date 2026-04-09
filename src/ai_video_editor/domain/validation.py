"""Storyboard and clip review validation — pure domain logic.

These functions validate structural correctness of LLM-generated artifacts.
No I/O, no LLM calls, no provider dependencies. Operates on Pydantic models
and dicts from Phase 1/Phase 2 outputs.
"""

from __future__ import annotations


def validate_clip_review(review: dict, clip_info: dict) -> tuple[list[str], bool]:
    """Validate a Phase 1 clip review for structural correctness.

    Returns (warnings, is_critical). is_critical means the review should be retried.

    Critical conditions:
    - No usable or discard segments on a clip > 5 seconds
    - More than 50% of usable segments have bad timestamps (in_sec >= out_sec)
    """
    warnings = []
    dur = clip_info.get("duration_sec", 0)
    clip_id = clip_info.get("clip_id", "")

    # Check clip_id match
    review_cid = review.get("clip_id", "")
    if review_cid and clip_id and not clip_id.endswith(review_cid):
        warnings.append(f"clip_id mismatch: expected '{clip_id}', got '{review_cid}'")

    # Check usable segments
    for seg in review.get("usable_segments", []):
        in_s = seg.get("in_sec", 0)
        out_s = seg.get("out_sec", 0)
        if in_s >= out_s:
            warnings.append(f"Segment in_sec ({in_s}) >= out_sec ({out_s})")
        if dur > 0 and out_s > dur + 1.0:
            warnings.append(f"Segment out_sec ({out_s:.1f}) exceeds clip duration ({dur:.1f})")

    # Check for empty review on non-trivial clips
    has_segments = bool(review.get("usable_segments") or review.get("discard_segments"))
    if not has_segments and dur > 5.0:
        warnings.append("No usable or discard segments for a clip > 5s")

    # Critical if: no segments on a real clip, or majority of segments have bad timestamps
    bad_count = sum(
        1
        for seg in review.get("usable_segments", [])
        if seg.get("in_sec", 0) >= seg.get("out_sec", 0)
    )
    total_segs = len(review.get("usable_segments", []))
    is_critical = (not has_segments and dur > 5.0) or (
        total_segs > 0 and bad_count > total_segs / 2
    )

    return warnings, is_critical


def validate_storyboard(storyboard, clip_reviews: list[dict]) -> tuple[list[str], bool]:
    """Validate a Phase 2 storyboard for structural correctness.

    Returns (warnings, is_critical).

    Checks:
    - All clip_id values exist in known review IDs
    - in_sec < out_sec for all segments
    - out_sec within clip duration (+1s tolerance)
    - No duplicate segment indices
    - Non-empty segment list

    Critical: no segments, or >30% unknown clip IDs.
    """
    warnings = []
    known_ids = {r.get("clip_id", "") for r in clip_reviews}
    dur_map = {}
    for r in clip_reviews:
        cid = r.get("clip_id", "")
        dur_map[cid] = r.get("duration_sec", 0)

    unknown_count = 0
    for seg in storyboard.segments:
        if seg.clip_id not in known_ids:
            warnings.append(f"Seg {seg.index}: unknown clip_id '{seg.clip_id}'")
            unknown_count += 1
        if seg.in_sec >= seg.out_sec:
            warnings.append(f"Seg {seg.index}: in_sec ({seg.in_sec}) >= out_sec ({seg.out_sec})")
        max_dur = dur_map.get(seg.clip_id, 0)
        if max_dur > 0 and seg.out_sec > max_dur + 1.0:
            warnings.append(
                f"Seg {seg.index}: out_sec ({seg.out_sec:.1f}) > clip duration ({max_dur:.1f})"
            )

    if not storyboard.segments:
        warnings.append("Storyboard has no segments")

    # Check for duplicate indices
    indices = [s.index for s in storyboard.segments]
    if len(indices) != len(set(indices)):
        warnings.append("Duplicate segment indices detected")

    total = len(storyboard.segments)
    is_critical = total == 0 or (total > 0 and unknown_count > total * 0.3)

    return warnings, is_critical
