"""Tool implementations for the Editorial Director agent.

Tools organized into three categories:
- INSPECT: screenshot_segment, get_transcript_excerpt, get_clip_review,
           run_eval_check, get_unused_footage, get_full_transcript
- EDIT: edit_timeline (unified: add, remove, move, update)
- CONTROL: finalize_review

All tools receive a DirectorToolContext and return a dict result.
Edit actions include regression protection — computable eval scores are
checked before and after, and the edit is reverted if scores drop.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import ReviewBudget
from .models import EditorialStoryboard, Segment, SegmentChange

log = logging.getLogger(__name__)


@dataclass
class DirectorToolContext:
    """Mutable context shared across all tool calls within a review session."""

    storyboard: EditorialStoryboard
    clip_reviews: list[dict]
    clips_dir: Path
    user_context: dict | None = None
    transcripts_by_clip: dict[str, list[dict]] = field(default_factory=dict)
    budget: ReviewBudget = field(default_factory=ReviewBudget)
    finalized: bool = False
    final_passed: bool = False
    final_summary: str = ""
    # Change tracking for review summary
    segment_changes: list[SegmentChange] = field(default_factory=list)
    # Cache for segment grid images (segment_index → JPEG bytes)
    _grid_cache: dict[int, bytes] = field(default_factory=dict)
    # Cached proposal from propose_edits, awaiting user approval
    pending_proposal: list[dict] | None = None
    pending_proposal_plan: str = ""

    @property
    def reviews_by_id(self) -> dict[str, dict]:
        return {r.get("clip_id", ""): r for r in self.clip_reviews}


# ---------------------------------------------------------------------------
# INSPECT tools
# ---------------------------------------------------------------------------


def screenshot_segment(ctx: DirectorToolContext, segment_index: int) -> dict:
    """Extract a 2×2 thumbnail grid from a segment. Returns image bytes or error."""
    if segment_index in ctx._grid_cache:
        return {"type": "image", "data": ctx._grid_cache[segment_index]}

    from .render import generate_segment_grid

    data = generate_segment_grid(segment_index, ctx.storyboard, ctx.clips_dir)
    if data is None:
        return {
            "type": "text",
            "data": f"Could not extract thumbnails for segment {segment_index}.",
        }
    ctx._grid_cache[segment_index] = data
    return {"type": "image", "data": data}


def get_transcript_excerpt(
    ctx: DirectorToolContext, clip_id: str, start_sec: float, end_sec: float
) -> dict:
    """Return transcript lines within the time range, with speaker labels."""
    entries = ctx.transcripts_by_clip.get(clip_id, [])
    if not entries:
        return {"type": "text", "data": f"No transcript available for {clip_id}."}

    lines = []
    for entry in entries:
        e_start = entry.get("start", 0)
        e_end = entry.get("end", 0)
        if e_end < start_sec or e_start > end_sec:
            continue
        speaker = entry.get("speaker", "")
        text = entry.get("text", "").strip()
        seg_type = entry.get("type", "speech")
        prefix = f"[{speaker}] " if speaker else ""
        if seg_type != "speech":
            prefix = f"[{seg_type}] "
        lines.append(f"{e_start:.1f}-{e_end:.1f}s: {prefix}{text}")

    if not lines:
        return {
            "type": "text",
            "data": f"No transcript entries in {clip_id} "
            f"between {start_sec:.1f}s and {end_sec:.1f}s.",
        }
    return {"type": "text", "data": "\n".join(lines)}


def get_clip_review(ctx: DirectorToolContext, clip_id: str) -> dict:
    """Return Phase 1 review data for a clip in compact format."""
    review = ctx.reviews_by_id.get(clip_id)
    if not review:
        return {"type": "text", "data": f"No review found for clip {clip_id}."}

    parts = [f"Clip: {clip_id}"]
    parts.append(f"Summary: {review.get('summary', 'N/A')}")

    quality = review.get("quality", {})
    if quality:
        parts.append(
            f"Quality: {quality.get('overall', '?')} "
            f"(stability={quality.get('stability', '?')}, "
            f"lighting={quality.get('lighting', '?')})"
        )

    people = review.get("people", [])
    if people:
        ppl = [f"{p.get('label', '?')} ({p.get('role', '?')})" for p in people]
        parts.append(f"People: {', '.join(ppl)}")

    usable = review.get("usable_segments", [])
    if usable:
        segs = [
            f"  [{u.get('in_sec', 0):.1f}-{u.get('out_sec', 0):.1f}s] {u.get('description', '')}"
            for u in usable
        ]
        parts.append("Usable segments:\n" + "\n".join(segs))

    audio = review.get("audio", {})
    if audio:
        parts.append(
            f"Audio: speech={audio.get('has_speech', False)}, "
            f"ambient={audio.get('ambient_description', 'N/A')}"
        )

    notes = review.get("editorial_notes", "")
    if notes:
        parts.append(f"Notes: {notes}")

    return {"type": "text", "data": "\n".join(parts)}


def run_eval_check(ctx: DirectorToolContext, dimension: str) -> dict:
    """Run a specific computable eval dimension and return detailed results."""
    from .eval import (
        score_constraint_satisfaction,
        score_coverage,
        score_speech_cut_safety,
        score_structural_completeness,
        score_timestamp_precision,
    )

    sb = ctx.storyboard

    if dimension == "constraint_satisfaction":
        if not ctx.user_context:
            return {
                "type": "text",
                "data": "No constraints to check (no user context available).",
            }
        results = score_constraint_satisfaction(sb, ctx.user_context)
        if not results:
            return {"type": "text", "data": "No constraints found in user context."}
        lines = []
        for r in results:
            status = "PASS" if r.satisfied else "FAIL"
            lines.append(f"[{status}] {r.constraint_type}: {r.text}")
            lines.append(f"  {r.evidence}")
        return {"type": "text", "data": "\n".join(lines)}

    elif dimension == "timestamp_precision":
        total, valid, clamped, invalid = score_timestamp_precision(sb, ctx.clip_reviews)
        lines = [
            f"Timestamp precision: {valid}/{total} valid ({valid / total:.0%})"
            if total
            else "No segments to check.",
        ]
        if clamped:
            lines.append(f"  {clamped} segments would need clamping")
        if invalid:
            lines.append(f"  {invalid} segments reference unknown clip IDs")
        reviews_by_id = ctx.reviews_by_id
        for seg in sb.segments:
            review = reviews_by_id.get(seg.clip_id)
            if not review:
                lines.append(f"  Segment {seg.index}: clip {seg.clip_id} not found in reviews")
                continue
            if seg.in_sec >= seg.out_sec:
                lines.append(
                    f"  Segment {seg.index}: in_sec ({seg.in_sec}) >= out_sec ({seg.out_sec})"
                )
        return {"type": "text", "data": "\n".join(lines)}

    elif dimension == "structural_completeness":
        score = score_structural_completeness(sb)
        checks = {
            "editorial_reasoning (>50 chars)": bool(
                sb.editorial_reasoning and len(sb.editorial_reasoning) > 50
            ),
            "story_arc present": bool(sb.story_arc),
            "cast present": bool(sb.cast),
            "discarded present": bool(sb.discarded),
            "no duplicate indices": len([s.index for s in sb.segments])
            == len(set(s.index for s in sb.segments)),
        }
        lines = [f"Structural completeness: {score:.0%}"]
        for check, passed in checks.items():
            lines.append(f"  {'PASS' if passed else 'FAIL'}: {check}")
        return {"type": "text", "data": "\n".join(lines)}

    elif dimension == "speech_cut_safety":
        rate, unsafe = score_speech_cut_safety(sb, ctx.transcripts_by_clip)
        lines = [
            f"Speech cut safety: {rate:.0%} "
            f"({len(sb.segments) - len(unsafe)}/{len(sb.segments)} safe)"
        ]
        for u in unsafe:
            lines.append(
                f"  UNSAFE segment {u['segment_index']}: "
                f"cut at {u['cut_time']:.1f}s, speech ends at {u['speech_end']:.1f}s"
            )
            lines.append(f'    Text: "{u["speech_text"]}"')
        return {"type": "text", "data": "\n".join(lines)}

    elif dimension == "coverage":
        score = score_coverage(sb, ctx.clip_reviews)
        used = {s.clip_id for s in sb.segments}
        discarded = {d.clip_id for d in sb.discarded} if sb.discarded else set()
        all_ids = {r.get("clip_id", "") for r in ctx.clip_reviews}
        unaccounted = all_ids - used - discarded - {""}
        lines = [
            f"Coverage: {score:.0%} ({len(used)} used, "
            f"{len(discarded)} discarded, {len(all_ids)} total)"
        ]
        if unaccounted:
            lines.append(f"  Unaccounted clips: {', '.join(sorted(unaccounted))}")
        return {"type": "text", "data": "\n".join(lines)}

    else:
        return {
            "type": "text",
            "data": f"Unknown dimension: {dimension}. "
            f"Available: constraint_satisfaction, timestamp_precision, "
            f"structural_completeness, speech_cut_safety, coverage",
        }


def get_unused_footage(ctx: DirectorToolContext, clip_id: str | None = None) -> dict:
    """Browse unused usable segments and key moments not in the storyboard."""
    sb = ctx.storyboard
    # Build set of used time ranges per clip
    used_ranges: dict[str, list[tuple[float, float]]] = {}
    for seg in sb.segments:
        used_ranges.setdefault(seg.clip_id, []).append((seg.in_sec, seg.out_sec))

    def _is_used(cid: str, in_s: float, out_s: float) -> bool:
        """Check if a time range is covered by any storyboard segment (>50% overlap)."""
        for u_in, u_out in used_ranges.get(cid, []):
            overlap = min(out_s, u_out) - max(in_s, u_in)
            duration = out_s - in_s
            if duration > 0 and overlap / duration > 0.5:
                return True
        return False

    clips_to_show = (
        [r for r in ctx.clip_reviews if r.get("clip_id") == clip_id]
        if clip_id
        else ctx.clip_reviews
    )
    if clip_id and not clips_to_show:
        return {"type": "text", "data": f"No review found for clip {clip_id}."}

    lines = []
    for review in clips_to_show:
        cid = review.get("clip_id", "")
        usable = review.get("usable_segments", [])
        key_moments = review.get("key_moments", [])

        unused_segs = [
            u for u in usable if not _is_used(cid, u.get("in_sec", 0), u.get("out_sec", 0))
        ]
        unused_moments = [
            m
            for m in key_moments
            if not _is_used(cid, m.get("timestamp_sec", 0), m.get("timestamp_sec", 0) + 1)
        ]

        if not unused_segs and not unused_moments:
            if clip_id:  # Only show "nothing unused" for specific clip queries
                lines.append(f"{cid}: all footage used in storyboard.")
            continue

        summary = review.get("summary", "")[:60]
        lines.append(f"\n{cid}: {summary}")

        if unused_segs:
            lines.append("  Unused usable segments:")
            for u in unused_segs:
                qual = u.get("quality", "?")
                desc = u.get("description", "")[:60]
                lines.append(
                    f"    [{u.get('in_sec', 0):.1f}-{u.get('out_sec', 0):.1f}s] ({qual}) {desc}"
                )

        if unused_moments:
            high_val = [m for m in unused_moments if m.get("editorial_value") == "high"]
            if high_val:
                lines.append("  Unused HIGH-VALUE moments:")
                for m in high_val:
                    lines.append(
                        f"    {m.get('timestamp_sec', 0):.1f}s: {m.get('description', '')[:60]} "
                        f"[{m.get('suggested_use', '')}]"
                    )

    if not lines:
        return {"type": "text", "data": "All available footage is used in the storyboard."}
    return {"type": "text", "data": "\n".join(lines)}


def get_full_transcript(ctx: DirectorToolContext, clip_id: str) -> dict:
    """Return full transcript for a clip, grouped by speaker turns with pause markers."""
    entries = ctx.transcripts_by_clip.get(clip_id, [])
    if not entries:
        return {"type": "text", "data": f"No transcript available for {clip_id}."}

    lines = []
    prev_end = 0.0
    prev_speaker = None

    for entry in entries:
        e_start = entry.get("start", 0)
        e_end = entry.get("end", 0)
        text = entry.get("text", "").strip()
        speaker = entry.get("speaker", "")
        seg_type = entry.get("type", "speech")

        if not text:
            continue

        # Mark pauses > 1.5s
        gap = e_start - prev_end
        if prev_end > 0 and gap > 1.5:
            lines.append(f"  [pause {gap:.1f}s]")

        # Mark speaker transitions with blank line
        if speaker != prev_speaker and prev_speaker is not None:
            lines.append("")

        prefix = f"[{speaker}]" if speaker else ""
        if seg_type != "speech":
            prefix = f"[{seg_type}]"

        lines.append(f"{e_start:.1f}-{e_end:.1f}s: {prefix} {text}")

        prev_end = e_end
        prev_speaker = speaker

    if len(lines) > 100:
        lines = lines[:100]
        lines.append(f"... (truncated, {len(entries)} total entries)")

    return {"type": "text", "data": "\n".join(lines)}


# ---------------------------------------------------------------------------
# EDIT tool — unified timeline editing with regression protection
# ---------------------------------------------------------------------------


def _compute_eval_scores(ctx: DirectorToolContext) -> dict[str, float]:
    """Compute all computable eval scores (free, no LLM)."""
    from .eval import (
        score_constraint_satisfaction,
        score_coverage,
        score_speech_cut_safety,
        score_structural_completeness,
        score_timestamp_precision,
    )

    sb = ctx.storyboard
    total, valid, _, _ = score_timestamp_precision(sb, ctx.clip_reviews)
    ts_score = valid / total if total else 1.0
    struct_score = score_structural_completeness(sb)
    cov_score = score_coverage(sb, ctx.clip_reviews)
    speech_rate, _ = score_speech_cut_safety(sb, ctx.transcripts_by_clip)

    constraint_score = 1.0
    if ctx.user_context:
        results = score_constraint_satisfaction(sb, ctx.user_context)
        if results:
            constraint_score = sum(1 for r in results if r.satisfied) / len(results)

    return {
        "constraint_satisfaction": constraint_score,
        "timestamp_precision": ts_score,
        "structural_completeness": struct_score,
        "coverage": cov_score,
        "speech_cut_safety": speech_rate,
    }


_REGRESSION_WEIGHTS = {
    "constraint_satisfaction": 0.30,
    "timestamp_precision": 0.20,
    "structural_completeness": 0.10,
    "coverage": 0.10,
    "speech_cut_safety": 0.25,
}

_MAX_DIMENSION_DROP = 0.10


def _check_regression(before: dict[str, float], after: dict[str, float]) -> str | None:
    """Return a description of regressions, or None if acceptable."""
    for key in before:
        drop = before[key] - after.get(key, 0)
        if drop > _MAX_DIMENSION_DROP:
            return (
                f"Scores regressed: {key}: {before[key]:.2f} → {after.get(key, 0):.2f} "
                f"(drop of {drop:.2f} exceeds {_MAX_DIMENSION_DROP} limit)"
            )

    before_weighted = sum(before.get(k, 0) * w for k, w in _REGRESSION_WEIGHTS.items())
    after_weighted = sum(after.get(k, 0) * w for k, w in _REGRESSION_WEIGHTS.items())

    if after_weighted >= before_weighted - 0.005:
        return None

    regressions = []
    for key in before:
        if after.get(key, 0) < before[key] - 0.01:
            regressions.append(f"{key}: {before[key]:.2f} → {after.get(key, 0):.2f}")
    return "Scores regressed: " + "; ".join(regressions)


def _preserve_text_overlay(seg_description: str, new_description: str) -> str:
    """Auto-restore text overlay content if the model truncated or removed it."""
    overlay_match = re.search(r"(Text overlay: '.+?'\.?)$", seg_description)
    if not overlay_match:
        return new_description

    original_overlay = overlay_match.group(1)
    new_overlay_match = re.search(r"Text overlay: '.*$", new_description)
    if new_overlay_match:
        new_overlay = new_overlay_match.group(0)
        if len(new_overlay) < len(original_overlay) or not new_overlay.endswith("'"):
            return new_description[: new_overlay_match.start()].rstrip() + " " + original_overlay
    else:
        return new_description.rstrip() + " " + original_overlay
    return new_description


def _renumber_segments(sb: EditorialStoryboard) -> None:
    """Renumber all segment indices 0..N-1."""
    for i, seg in enumerate(sb.segments):
        seg.index = i


# ── Action handlers (called by edit_timeline) ────────────────────────────


def _action_update(
    ctx: DirectorToolContext,
    segment_index: int,
    updated_fields: dict,
    *,
    skip_regression: bool = False,
) -> dict:
    """Update fields on an existing segment."""
    sb = ctx.storyboard
    if segment_index < 0 or segment_index >= len(sb.segments):
        return {"type": "text", "data": f"Invalid segment index: {segment_index}", "ok": False}

    seg = sb.segments[segment_index]

    valid_fields = {
        "in_sec",
        "out_sec",
        "purpose",
        "description",
        "transition",
        "audio_note",
        "text_overlay",
    }

    # Block clip_id changes via UPDATE — this is the "manual move" anti-pattern.
    # To relocate a clip, the agent must use action="move" or remove+add.
    if "clip_id" in updated_fields:
        return {
            "type": "text",
            "data": (
                "Rejected: cannot change clip_id via update. "
                "To relocate a clip, use action='move' (or remove + add)."
            ),
            "ok": False,
        }

    invalid = set(updated_fields.keys()) - valid_fields
    if invalid:
        return {
            "type": "text",
            "data": f"Invalid fields: {invalid}. Valid: {sorted(valid_fields)}",
            "ok": False,
        }

    # Auto-preserve text overlay
    if "description" in updated_fields:
        updated_fields["description"] = _preserve_text_overlay(
            seg.description, updated_fields["description"]
        )

    if not skip_regression:
        scores_before = _compute_eval_scores(ctx)
    original = seg.model_copy()

    for field_name, value in updated_fields.items():
        setattr(seg, field_name, value)

    if seg.in_sec >= seg.out_sec:
        sb.segments[segment_index] = original
        return {
            "type": "text",
            "data": f"Rejected: in_sec ({seg.in_sec}) >= out_sec ({seg.out_sec})",
            "ok": False,
        }

    if not skip_regression:
        scores_after = _compute_eval_scores(ctx)
        regression = _check_regression(scores_before, scores_after)
        if regression:
            sb.segments[segment_index] = original
            return {
                "type": "text",
                "data": f"Reverted: {regression}. Try a different approach.",
                "ok": False,
            }

    before_vals = {k: getattr(original, k) for k in updated_fields}
    after_vals = {k: getattr(seg, k) for k in updated_fields}
    ctx.segment_changes.append(
        SegmentChange(
            change_type="update",
            segment_index=segment_index,
            fields_changed=list(updated_fields.keys()),
            before=before_vals,
            after=after_vals,
        )
    )
    ctx._grid_cache.pop(segment_index, None)

    changes = ", ".join(f"{k}={v}" for k, v in updated_fields.items())
    return {"type": "text", "data": f"Segment {segment_index} updated: {changes}", "ok": True}


def _action_remove(
    ctx: DirectorToolContext, segment_index: int, *, skip_regression: bool = False
) -> dict:
    """Remove a segment and renumber."""
    sb = ctx.storyboard
    if segment_index < 0 or segment_index >= len(sb.segments):
        return {"type": "text", "data": f"Invalid segment index: {segment_index}", "ok": False}

    if not skip_regression:
        scores_before = _compute_eval_scores(ctx)
    removed = sb.segments.pop(segment_index)
    _renumber_segments(sb)

    for arc in sb.story_arc:
        arc.segment_indices = [
            i if i < segment_index else i - 1 for i in arc.segment_indices if i != segment_index
        ]

    if not skip_regression:
        scores_after = _compute_eval_scores(ctx)
        regression = _check_regression(scores_before, scores_after)
        if regression:
            sb.segments.insert(segment_index, removed)
            _renumber_segments(sb)
            return {"type": "text", "data": f"Remove reverted: {regression}.", "ok": False}

    ctx.segment_changes.append(
        SegmentChange(
            change_type="remove",
            segment_index=segment_index,
            before={
                "clip_id": removed.clip_id,
                "in_sec": removed.in_sec,
                "out_sec": removed.out_sec,
                "purpose": removed.purpose,
            },
        )
    )
    ctx._grid_cache.clear()

    result = (
        f"Segment {segment_index} removed ({removed.clip_id} "
        f"{removed.in_sec:.1f}-{removed.out_sec:.1f}s). "
        f"{len(sb.segments)} segments remaining."
    )
    empty_arcs = [a.title for a in sb.story_arc if not a.segment_indices]
    if empty_arcs:
        result += f"\nWarning: empty arc sections: {', '.join(empty_arcs)}"
    return {"type": "text", "data": result, "ok": True}


def _action_move(
    ctx: DirectorToolContext, segment_index: int, to_position: int, *, skip_regression: bool = False
) -> dict:
    """Move a segment to a new position."""
    sb = ctx.storyboard
    n = len(sb.segments)
    if segment_index < 0 or segment_index >= n:
        return {"type": "text", "data": f"Invalid segment_index: {segment_index}", "ok": False}
    if to_position < 0 or to_position >= n:
        return {"type": "text", "data": f"Invalid to_position: {to_position}", "ok": False}
    if segment_index == to_position:
        return {"type": "text", "data": "No move needed — same position.", "ok": True}

    if not skip_regression:
        scores_before = _compute_eval_scores(ctx)
    old_segments = list(sb.segments)

    seg = sb.segments.pop(segment_index)
    sb.segments.insert(to_position, seg)
    _renumber_segments(sb)

    if not skip_regression:
        scores_after = _compute_eval_scores(ctx)
        regression = _check_regression(scores_before, scores_after)
        if regression:
            sb.segments = old_segments
            _renumber_segments(sb)
            return {"type": "text", "data": f"Move reverted: {regression}.", "ok": False}

    ctx.segment_changes.append(
        SegmentChange(
            change_type="move",
            segment_index=segment_index,
            before={"position": segment_index},
            after={"position": to_position},
        )
    )
    ctx._grid_cache.clear()
    return {
        "type": "text",
        "data": f"Segment moved from position {segment_index} to {to_position}.",
        "ok": True,
    }


def _action_add(
    ctx: DirectorToolContext,
    clip_id: str,
    in_sec: float,
    out_sec: float,
    position: int,
    purpose: str,
    description: str,
    transition: str = "cut",
    audio_note: str = "",
    *,
    skip_regression: bool = False,
) -> dict:
    """Add a new segment from unused footage."""
    sb = ctx.storyboard
    n = len(sb.segments)

    if position < 0 or position > n:
        return {"type": "text", "data": f"Invalid position: {position} (0..{n})", "ok": False}
    if clip_id not in ctx.reviews_by_id:
        return {"type": "text", "data": f"Clip {clip_id} not found in reviews.", "ok": False}
    if in_sec >= out_sec:
        return {
            "type": "text",
            "data": f"Invalid range: in_sec ({in_sec}) >= out_sec ({out_sec})",
            "ok": False,
        }

    # Validate time range falls within a usable segment or covers a key moment
    review = ctx.reviews_by_id[clip_id]
    usable = review.get("usable_segments", [])
    in_usable = False
    for u in usable:
        if in_sec >= u.get("in_sec", 0) - 0.5 and out_sec <= u.get("out_sec", 0) + 0.5:
            in_usable = True
            break
    if not in_usable:
        # Fall back: accept if range covers a key moment (director sees these as available)
        for m in review.get("key_moments", []):
            ts = m.get("timestamp_sec", 0)
            if in_sec <= ts + 1.0 and out_sec >= ts - 1.0:
                in_usable = True
                break
    if not in_usable:
        return {
            "type": "text",
            "data": f"Time range {in_sec:.1f}-{out_sec:.1f}s is not within any usable segment "
            f"or key moment for clip {clip_id}.",
            "ok": False,
        }

    if not skip_regression:
        scores_before = _compute_eval_scores(ctx)

    new_seg = Segment(
        index=position,
        clip_id=clip_id,
        in_sec=in_sec,
        out_sec=out_sec,
        purpose=purpose,
        description=description,
        transition=transition,
        audio_note=audio_note,
    )
    sb.segments.insert(position, new_seg)
    _renumber_segments(sb)

    # Update story_arc indices
    for arc in sb.story_arc:
        arc.segment_indices = [i + 1 if i >= position else i for i in arc.segment_indices]

    if not skip_regression:
        scores_after = _compute_eval_scores(ctx)
        regression = _check_regression(scores_before, scores_after)
        if regression:
            sb.segments.pop(position)
            _renumber_segments(sb)
            return {"type": "text", "data": f"Add reverted: {regression}.", "ok": False}

    ctx.segment_changes.append(
        SegmentChange(
            change_type="add",
            segment_index=position,
            after={
                "clip_id": clip_id,
                "in_sec": in_sec,
                "out_sec": out_sec,
                "purpose": purpose,
                "description": description[:60],
            },
        )
    )
    ctx._grid_cache.clear()
    return {
        "type": "text",
        "data": f"Segment added at position {position}: {clip_id} "
        f"{in_sec:.1f}-{out_sec:.1f}s ({purpose}).",
        "ok": True,
    }


# ── Unified edit_timeline dispatcher ─────────────────────────────────────


def edit_timeline(ctx: DirectorToolContext, action: str, **kwargs) -> dict:
    """Unified timeline editing tool.

    Actions:
        update: Change fields on an existing segment.
            Params: segment_index (int), updated_fields (dict)
        remove: Delete a segment.
            Params: segment_index (int)
        move: Move a segment to a new position.
            Params: segment_index (int), to_position (int)
        add: Insert a new segment from unused footage.
            Params: clip_id, in_sec, out_sec, position, purpose, description,
                    transition (optional), audio_note (optional)
    """
    handlers = {
        "update": _action_update,
        "remove": _action_remove,
        "move": _action_move,
        "add": _action_add,
    }
    handler = handlers.get(action)
    if not handler:
        return {
            "type": "text",
            "ok": False,
            "data": f"Unknown action: {action}. Available: {', '.join(handlers.keys())}",
        }
    return handler(ctx, **kwargs)


# ---------------------------------------------------------------------------
# CONTROL tools
# ---------------------------------------------------------------------------


def finalize_review(ctx: DirectorToolContext, passed: bool, summary: str) -> dict:
    """Signal that the review is complete."""
    ctx.finalized = True
    ctx.final_passed = passed
    ctx.final_summary = summary
    status = "PASSED" if passed else "FAILED"
    return {
        "type": "text",
        "data": f"Review finalized: {status}. {summary}",
    }


def propose_edits(ctx: DirectorToolContext, plan: str, edits: list[dict]) -> dict:
    """Propose planned edits for user review without executing them.

    Used in conversational mode: the director describes what it wants to do,
    the user confirms, then the director calls edit_timeline to execute.
    """
    sb = ctx.storyboard
    lines = [f"Proposed plan: {plan}", ""]

    for i, edit in enumerate(edits):
        action = edit.get("action", "?")
        if action == "update":
            idx = edit.get("segment_index", "?")
            fields = edit.get("updated_fields", {})
            if idx != "?" and 0 <= idx < len(sb.segments):
                seg = sb.segments[idx]
                lines.append(f"  {i + 1}. UPDATE segment {idx} ({seg.clip_id}):")
                for k, v in fields.items():
                    old_val = getattr(seg, k, "?")
                    if isinstance(old_val, float):
                        old_val = f"{old_val:.1f}"
                    lines.append(f"       {k}: {old_val} -> {v}")
            else:
                lines.append(f"  {i + 1}. UPDATE segment {idx}: {fields}")
        elif action == "add":
            clip = edit.get("clip_id", "?")
            in_s = edit.get("in_sec", 0)
            out_s = edit.get("out_sec", 0)
            pos = edit.get("position", "?")
            purpose = edit.get("purpose", "?")
            desc = edit.get("description", "")[:60]
            lines.append(
                f"  {i + 1}. ADD at position {pos}: {clip} {in_s:.1f}-{out_s:.1f}s ({purpose})"
            )
            if desc:
                lines.append(f"       {desc}")
        elif action == "remove":
            idx = edit.get("segment_index", "?")
            if idx != "?" and 0 <= idx < len(sb.segments):
                seg = sb.segments[idx]
                lines.append(
                    f"  {i + 1}. REMOVE segment {idx}: {seg.clip_id} "
                    f"{seg.in_sec:.1f}-{seg.out_sec:.1f}s"
                )
            else:
                lines.append(f"  {i + 1}. REMOVE segment {idx}")
        elif action == "move":
            idx = edit.get("segment_index", "?")
            to = edit.get("to_position", "?")
            lines.append(f"  {i + 1}. MOVE segment {idx} -> position {to}")
        else:
            lines.append(f"  {i + 1}. {action}: {edit}")

    lines.append("")
    lines.append(f"Total: {len(edits)} edit(s). Awaiting filmmaker confirmation.")

    # Cache the structured edits for auto-execution on approval
    ctx.pending_proposal = edits
    ctx.pending_proposal_plan = plan

    return {"type": "text", "data": "\n".join(lines)}


# ---------------------------------------------------------------------------
# Batch proposal execution
# ---------------------------------------------------------------------------


def _find_segment_by_anchor(
    sb: EditorialStoryboard, clip_id: str, in_sec: float, out_sec: float
) -> int | None:
    """Find current index of a segment by its clip_id and time range."""
    for seg in sb.segments:
        if (
            seg.clip_id == clip_id
            and abs(seg.in_sec - in_sec) < 0.1
            and abs(seg.out_sec - out_sec) < 0.1
        ):
            return seg.index
    return None


def execute_proposal_batch(ctx: DirectorToolContext) -> list[dict]:
    """Execute all edits from a cached proposal with batch regression protection.

    Resolves index-based references to clip anchors before execution so that
    sequential moves/removes don't corrupt indices. Computes eval scores once
    before and once after the entire batch (not per-edit).

    Returns a list of per-edit result dicts.
    """
    edits = ctx.pending_proposal or []
    ctx.pending_proposal = None
    ctx.pending_proposal_plan = ""

    if not edits:
        return [{"type": "text", "data": "No edits in proposal."}]

    sb = ctx.storyboard

    # Snapshot for rollback
    original_segments = [seg.model_copy() for seg in sb.segments]
    original_story_arc = [arc.model_copy() for arc in sb.story_arc]

    # Resolve each edit's segment_index to a clip anchor (clip_id, in_sec, out_sec)
    # so we can re-find the segment after indices shift
    resolved = []
    for edit in edits:
        edit = dict(edit)  # shallow copy
        action = edit.get("action", "")
        idx = edit.get("segment_index")

        if action in ("update", "remove", "move") and idx is not None:
            idx = int(idx)
            if 0 <= idx < len(sb.segments):
                anchor_seg = sb.segments[idx]
                edit["_anchor"] = (anchor_seg.clip_id, anchor_seg.in_sec, anchor_seg.out_sec)
            else:
                edit["_anchor"] = None
        else:
            edit["_anchor"] = None

        resolved.append(edit)

    # Compute scores once before the batch
    scores_before = _compute_eval_scores(ctx)

    # Partition by action type and apply in safe order:
    # 1. updates (don't shift indices)
    # 2. removes (descending original index to keep lower indices stable)
    # 3. adds (ascending position)
    # 4. moves
    updates = [e for e in resolved if e.get("action") == "update"]
    removes = [e for e in resolved if e.get("action") == "remove"]
    adds = [e for e in resolved if e.get("action") == "add"]
    moves = [e for e in resolved if e.get("action") == "move"]
    # Any unrecognized actions
    others = [e for e in resolved if e.get("action") not in ("update", "remove", "add", "move")]

    results = []

    # Apply updates
    for edit in updates:
        anchor = edit.get("_anchor")
        if not anchor:
            results.append(
                {"type": "text", "data": "Skipped update: invalid segment reference", "ok": False}
            )
            continue
        current_idx = _find_segment_by_anchor(sb, *anchor)
        if current_idx is None:
            results.append(
                {
                    "type": "text",
                    "data": f"Skipped update: segment {anchor[0]} not found",
                    "ok": False,
                }
            )
            continue
        updated_fields = edit.get("updated_fields", {})
        if not updated_fields:
            results.append(
                {"type": "text", "data": "Skipped update: no fields specified", "ok": False}
            )
            continue
        result = _action_update(ctx, current_idx, updated_fields, skip_regression=True)
        results.append(result)

    # Apply removes in descending order of original index to keep lower indices stable
    removes_sorted = sorted(removes, key=lambda e: e.get("segment_index", 0), reverse=True)
    # Track original indices of successful removes for position drift correction
    removed_original_indices: list[int] = []
    for edit in removes_sorted:
        anchor = edit.get("_anchor")
        if not anchor:
            results.append(
                {"type": "text", "data": "Skipped remove: invalid segment reference", "ok": False}
            )
            continue
        current_idx = _find_segment_by_anchor(sb, *anchor)
        if current_idx is None:
            results.append(
                {"type": "text", "data": "Skipped remove: segment not found", "ok": False}
            )
            continue
        result = _action_remove(ctx, current_idx, skip_regression=True)
        results.append(result)
        if result.get("ok"):
            removed_original_indices.append(int(edit.get("segment_index", 0)))

    # Apply adds in ascending position order
    # Adjust positions for prior removes (segments shifted down) and prior adds (shifted up)
    adds_sorted = sorted(adds, key=lambda e: e.get("position", 0))
    adds_offset = 0  # net offset from successful adds
    for edit in adds_sorted:
        original_pos = int(edit.get("position", 0))
        # Subtract removes that were at positions below this add's target
        removes_below = sum(1 for idx in removed_original_indices if idx < original_pos)
        pos = original_pos - removes_below + adds_offset
        clip_id = edit.get("clip_id", "")
        in_sec = float(edit.get("in_sec", 0))
        out_sec = float(edit.get("out_sec", 0))
        purpose = edit.get("purpose", "b_roll")
        description = edit.get("description", "")
        transition = edit.get("transition", "cut")
        audio_note = edit.get("audio_note", "")
        # Clamp position to current segment count
        pos = max(0, min(pos, len(sb.segments)))
        result = _action_add(
            ctx,
            clip_id=clip_id,
            in_sec=in_sec,
            out_sec=out_sec,
            position=pos,
            purpose=purpose,
            description=description,
            transition=transition,
            audio_note=audio_note,
            skip_regression=True,
        )
        results.append(result)
        if result.get("ok"):
            adds_offset += 1

    # Apply moves — re-resolve both source and target after prior edits
    for edit in moves:
        anchor = edit.get("_anchor")
        if not anchor:
            results.append(
                {"type": "text", "data": "Skipped move: invalid segment reference", "ok": False}
            )
            continue
        current_idx = _find_segment_by_anchor(sb, *anchor)
        if current_idx is None:
            results.append({"type": "text", "data": "Skipped move: segment not found", "ok": False})
            continue
        to_pos = int(edit.get("to_position", 0))
        # Clamp to_position to valid range
        to_pos = max(0, min(to_pos, len(sb.segments) - 1))
        result = _action_move(ctx, current_idx, to_pos, skip_regression=True)
        results.append(result)

    # Handle unrecognized actions
    for edit in others:
        results.append(
            {"type": "text", "data": f"Skipped: unknown action '{edit.get('action')}'", "ok": False}
        )

    # Log score delta (informational only for user-approved batches).
    # Regression protection is for the autonomous auto-review loop, not for
    # user-confirmed proposals — the user IS the quality gate.
    scores_after = _compute_eval_scores(ctx)
    deltas = []
    for key in scores_before:
        diff = scores_after.get(key, 0) - scores_before[key]
        if abs(diff) > 0.01:
            direction = "+" if diff > 0 else ""
            deltas.append(f"{key}: {direction}{diff:.2f}")
    if deltas:
        results.append({"type": "text", "data": f"Score changes: {', '.join(deltas)}", "ok": True})

    # Only revert on hard constraint failure (constraints must stay 100% satisfied)
    constraint_before = scores_before.get("constraint_satisfaction", 1.0)
    constraint_after = scores_after.get("constraint_satisfaction", 1.0)
    if constraint_after < constraint_before and constraint_before >= 1.0:
        sb.segments = original_segments
        sb.story_arc = original_story_arc
        _renumber_segments(sb)
        ctx._grid_cache.clear()
        return [
            {
                "type": "text",
                "data": f"Batch reverted: constraint satisfaction dropped "
                f"({constraint_before:.0%} → {constraint_after:.0%}). "
                f"User constraints must remain satisfied.",
                "ok": False,
            }
        ]

    # Clear grid cache since segments changed
    ctx._grid_cache.clear()

    # Record aggregate changes
    successful = [r for r in results if "skipped" not in r.get("data", "").lower()]
    log.info("Batch executed: %d/%d edits applied", len(successful), len(edits))

    return results


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOL_HANDLERS = {
    # INSPECT
    "screenshot_segment": screenshot_segment,
    "get_transcript_excerpt": get_transcript_excerpt,
    "get_clip_review": get_clip_review,
    "run_eval_check": run_eval_check,
    "get_unused_footage": get_unused_footage,
    "get_full_transcript": get_full_transcript,
    # EDIT
    "edit_timeline": edit_timeline,
    # CONTROL
    "finalize_review": finalize_review,
    "propose_edits": propose_edits,
}

FIX_TOOLS = {"edit_timeline"}
