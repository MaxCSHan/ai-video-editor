"""Evaluation scoring for editorial storyboard quality.

Provides automated scoring functions that can compare storyboard outputs
across pipeline variants (single-call vs split, with/without prompt hardening)
without requiring API calls. Scores are computed from the storyboard JSON
against clip reviews and user context.

Usage:
    from ai_video_editor.eval import score_storyboard
    report = score_storyboard(storyboard, clip_reviews, user_context)
    print(report.summary())
"""

import re
from dataclasses import dataclass, field


@dataclass
class ConstraintResult:
    """Result of checking a single filmmaker constraint."""

    constraint_type: str  # "must_include" or "must_exclude"
    text: str  # the constraint text
    satisfied: bool
    evidence: str  # which segment(s) matched, or why it wasn't found
    matching_segments: list[int] = field(default_factory=list)


@dataclass
class EvalReport:
    """Full evaluation report for a storyboard."""

    # Constraint satisfaction
    constraints: list[ConstraintResult] = field(default_factory=list)

    # Timestamp precision
    total_segments: int = 0
    valid_timestamps: int = 0  # in_sec < out_sec within usable bounds
    clamped_timestamps: int = 0  # would need clamping
    invalid_clips: int = 0  # clip_id not found in reviews

    # Structural quality
    has_editorial_reasoning: bool = False
    reasoning_mentions_constraints: bool = False
    has_story_arc: bool = False
    has_cast: bool = False
    has_discarded: bool = False
    duplicate_segment_indices: int = 0

    # Coverage
    total_clips_available: int = 0
    clips_used: int = 0
    clips_discarded_explicitly: int = 0
    estimated_duration_sec: float = 0.0

    # Speech cut safety
    speech_cut_safety_rate: float = 1.0
    unsafe_cuts: list[dict] = field(default_factory=list)

    # Aggregated scores (0.0-1.0)
    structural_completeness_score: float = 0.0
    coverage_score: float = 0.0

    def constraint_satisfaction_rate(self) -> float:
        """Fraction of constraints satisfied (0.0-1.0)."""
        if not self.constraints:
            return 1.0
        return sum(1 for c in self.constraints if c.satisfied) / len(self.constraints)

    def timestamp_precision_rate(self) -> float:
        """Fraction of segments with valid timestamps (0.0-1.0)."""
        if self.total_segments == 0:
            return 1.0
        return self.valid_timestamps / self.total_segments

    def summary(self) -> str:
        """Human-readable summary of the evaluation."""
        lines = ["Storyboard Evaluation Report", "=" * 40]

        # Constraints
        sat = sum(1 for c in self.constraints if c.satisfied)
        total = len(self.constraints)
        if total > 0:
            lines.append(
                f"\nConstraints: {sat}/{total} satisfied ({self.constraint_satisfaction_rate():.0%})"
            )
            for c in self.constraints:
                status = "PASS" if c.satisfied else "FAIL"
                lines.append(f"  [{status}] {c.constraint_type}: {c.text[:60]}")
                lines.append(f"         {c.evidence}")
        else:
            lines.append("\nConstraints: none specified")

        # Timestamps
        lines.append(
            f"\nTimestamps: {self.valid_timestamps}/{self.total_segments} valid "
            f"({self.timestamp_precision_rate():.0%})"
        )
        if self.clamped_timestamps:
            lines.append(f"  {self.clamped_timestamps} would need clamping")
        if self.invalid_clips:
            lines.append(f"  {self.invalid_clips} reference unknown clip IDs")

        # Structure
        lines.append("\nStructure:")
        lines.append(
            f"  editorial_reasoning: {'yes' if self.has_editorial_reasoning else 'MISSING'}"
        )
        lines.append(
            f"  mentions constraints: {'yes' if self.reasoning_mentions_constraints else 'no'}"
        )
        lines.append(f"  story_arc: {'yes' if self.has_story_arc else 'no'}")
        lines.append(f"  cast: {'yes' if self.has_cast else 'no'}")
        lines.append(f"  discarded: {'yes' if self.has_discarded else 'no'}")
        if self.duplicate_segment_indices:
            lines.append(f"  WARNING: {self.duplicate_segment_indices} duplicate indices")

        # Coverage
        lines.append(
            f"\nCoverage: {self.clips_used}/{self.total_clips_available} clips used, "
            f"{self.clips_discarded_explicitly} explicitly discarded"
        )
        lines.append(f"  Estimated duration: {self.estimated_duration_sec:.0f}s")

        return "\n".join(lines)


def _parse_constraint_phrases(text: str) -> list[str]:
    """Split a highlights/avoid string into individual constraint phrases."""
    # Split on commas, semicolons, or "and" at phrase boundaries
    parts = re.split(r"[,;]|(?:\band\b)", text)
    return [p.strip() for p in parts if p.strip() and len(p.strip()) > 5]


def _fuzzy_match(query: str, text: str) -> bool:
    """Check if query keywords appear in text (case-insensitive word overlap)."""
    stop = {"the", "a", "an", "at", "in", "on", "to", "of", "and", "is", "my", "we", "i", "where"}
    query_words = set(query.lower().split()) - stop
    text_words = set(text.lower().split()) - stop
    if not query_words:
        return False
    overlap = len(query_words & text_words)
    return overlap >= min(2, len(query_words))


def score_constraint_satisfaction(
    storyboard,
    user_context: dict,
) -> list[ConstraintResult]:
    """Check whether MUST-INCLUDE and MUST-EXCLUDE constraints are satisfied."""
    results = []

    # Build searchable text from all segments
    segment_texts = []
    for seg in storyboard.segments:
        combined = f"{seg.clip_id} {seg.description} {seg.purpose}"
        segment_texts.append((seg.index, combined))

    # Check MUST-INCLUDE (highlights)
    highlights = user_context.get("highlights", "")
    if highlights:
        phrases = _parse_constraint_phrases(highlights)
        for phrase in phrases:
            matching = [idx for idx, text in segment_texts if _fuzzy_match(phrase, text)]
            results.append(
                ConstraintResult(
                    constraint_type="must_include",
                    text=phrase,
                    satisfied=len(matching) > 0,
                    evidence=(
                        f"Found in segment(s) {matching}"
                        if matching
                        else "Not found in any segment description"
                    ),
                    matching_segments=matching,
                )
            )

    # Check MUST-EXCLUDE (avoid)
    avoid = user_context.get("avoid", "")
    if avoid:
        phrases = _parse_constraint_phrases(avoid)
        for phrase in phrases:
            matching = [idx for idx, text in segment_texts if _fuzzy_match(phrase, text)]
            results.append(
                ConstraintResult(
                    constraint_type="must_exclude",
                    text=phrase,
                    satisfied=len(matching) == 0,
                    evidence=(
                        f"Still present in segment(s) {matching}"
                        if matching
                        else "Correctly excluded"
                    ),
                    matching_segments=matching,
                )
            )

    return results


def score_timestamp_precision(
    storyboard,
    clip_reviews: list[dict],
) -> tuple[int, int, int, int]:
    """Check timestamp validity against clip review usable segments.

    Returns (total_segments, valid, clamped, invalid_clips).
    """
    reviews_by_id = {r.get("clip_id", ""): r for r in clip_reviews}
    total = len(storyboard.segments)
    valid = 0
    clamped = 0
    invalid_clips = 0

    for seg in storyboard.segments:
        review = reviews_by_id.get(seg.clip_id)
        if not review:
            invalid_clips += 1
            continue

        if seg.in_sec >= seg.out_sec:
            continue

        # Check if timestamps fall within any usable segment
        usable = review.get("usable_segments", [])
        in_bounds = False
        needs_clamp = False
        for us in usable:
            us_in = us.get("in_sec", 0)
            us_out = us.get("out_sec", 0)
            # Check overlap
            if seg.in_sec >= us_in - 1.0 and seg.out_sec <= us_out + 1.0:
                in_bounds = True
                break
            if seg.in_sec < us_in or seg.out_sec > us_out:
                # Partial overlap — would need clamping
                overlap = min(seg.out_sec, us_out) - max(seg.in_sec, us_in)
                if overlap > 0:
                    needs_clamp = True

        if in_bounds:
            valid += 1
        elif needs_clamp:
            clamped += 1

    return total, valid, clamped, invalid_clips


def score_structural_completeness(storyboard) -> float:
    """Score 0.0-1.0 for structural completeness of a storyboard."""
    checks = [
        bool(
            getattr(storyboard, "editorial_reasoning", "")
            and len(getattr(storyboard, "editorial_reasoning", "")) > 50
        ),
        bool(getattr(storyboard, "story_arc", [])),
        bool(getattr(storyboard, "cast", [])),
        bool(getattr(storyboard, "discarded", [])),
        len([s.index for s in storyboard.segments])
        == len(set(s.index for s in storyboard.segments)),  # no duplicate indices
    ]
    return sum(checks) / len(checks) if checks else 1.0


def score_coverage(storyboard, clip_reviews: list[dict]) -> float:
    """Score 0.0-1.0 for clip coverage (used + explicitly discarded / total)."""
    total = len(clip_reviews)
    if total == 0:
        return 1.0
    used = len({s.clip_id for s in storyboard.segments})
    discarded = len(getattr(storyboard, "discarded", []))
    return min(1.0, (used + discarded) / total)


def score_speech_cut_safety(
    storyboard,
    transcripts_by_clip: dict[str, list[dict]],
) -> tuple[float, list[dict]]:
    """Check that segment out-points don't fall mid-sentence.

    Args:
        storyboard: EditorialStoryboard with segments
        transcripts_by_clip: {clip_id: [{start, end, text, type, ...}, ...]}

    Returns:
        (safety_rate 0.0-1.0, list of unsafe cuts with details)
    """
    if not storyboard.segments:
        return 1.0, []

    safe_count = 0
    unsafe_cuts = []

    for seg in storyboard.segments:
        transcript_entries = transcripts_by_clip.get(seg.clip_id, [])
        if not transcript_entries:
            safe_count += 1  # no transcript = can't check
            continue

        # Find speech entries near the out-point
        cut_time = seg.out_sec
        tolerance = 0.5  # seconds

        speech_at_cut = None
        for entry in transcript_entries:
            if entry.get("type", "speech") != "speech":
                continue
            e_start = entry.get("start", 0)
            e_end = entry.get("end", 0)
            # Speech is active at cut point
            if e_start <= cut_time <= e_end + tolerance:
                speech_at_cut = entry
                break

        if speech_at_cut is None:
            safe_count += 1  # no speech at cut point
            continue

        # Check if cut is near a sentence boundary
        text = speech_at_cut.get("text", "").strip()
        e_end = speech_at_cut.get("end", 0)

        # Safe if: cut is at or after the end of the speech entry,
        # or the text ends with sentence-ending punctuation
        if cut_time >= e_end - tolerance:
            safe_count += 1
        elif text and text[-1] in ".!?":
            safe_count += 1
        else:
            unsafe_cuts.append(
                {
                    "segment_index": seg.index,
                    "clip_id": seg.clip_id,
                    "cut_time": cut_time,
                    "speech_text": text[:80],
                    "speech_end": e_end,
                }
            )

    rate = safe_count / len(storyboard.segments)
    return rate, unsafe_cuts


def score_storyboard(
    storyboard,
    clip_reviews: list[dict],
    user_context: dict | None = None,
    transcripts_by_clip: dict[str, list[dict]] | None = None,
) -> EvalReport:
    """Comprehensive evaluation of a storyboard against reviews and constraints.

    Returns an EvalReport with all scoring dimensions.
    """
    report = EvalReport()

    # Constraint satisfaction
    if user_context:
        report.constraints = score_constraint_satisfaction(storyboard, user_context)

    # Timestamp precision
    total, valid, clamped, invalid = score_timestamp_precision(storyboard, clip_reviews)
    report.total_segments = total
    report.valid_timestamps = valid
    report.clamped_timestamps = clamped
    report.invalid_clips = invalid

    # Structural quality
    reasoning = getattr(storyboard, "editorial_reasoning", "")
    report.has_editorial_reasoning = bool(reasoning and len(reasoning) > 50)
    report.reasoning_mentions_constraints = bool(
        reasoning and ("constraint" in reasoning.lower() or "must" in reasoning.lower())
    )
    report.has_story_arc = bool(getattr(storyboard, "story_arc", []))
    report.has_cast = bool(getattr(storyboard, "cast", []))
    report.has_discarded = bool(getattr(storyboard, "discarded", []))

    indices = [s.index for s in storyboard.segments]
    report.duplicate_segment_indices = len(indices) - len(set(indices))

    # Coverage
    report.total_clips_available = len(clip_reviews)
    report.clips_used = len({s.clip_id for s in storyboard.segments})
    report.clips_discarded_explicitly = len(getattr(storyboard, "discarded", []))
    report.estimated_duration_sec = getattr(storyboard, "estimated_duration_sec", 0)

    # Aggregated scores
    report.structural_completeness_score = score_structural_completeness(storyboard)
    report.coverage_score = score_coverage(storyboard, clip_reviews)

    # Speech cut safety (requires transcripts)
    if transcripts_by_clip:
        rate, unsafe = score_speech_cut_safety(storyboard, transcripts_by_clip)
        report.speech_cut_safety_rate = rate
        report.unsafe_cuts = unsafe

    return report


def compare_reports(
    report_a: EvalReport,
    report_b: EvalReport,
    label_a: str = "A",
    label_b: str = "B",
) -> str:
    """Produce a dimension-by-dimension comparison of two eval reports."""
    dims = [
        (
            "Segments",
            report_a.total_segments,
            report_b.total_segments,
            False,
        ),
        (
            "Timestamp precision",
            report_a.timestamp_precision_rate(),
            report_b.timestamp_precision_rate(),
            True,
        ),
        (
            "Structural completeness",
            report_a.structural_completeness_score,
            report_b.structural_completeness_score,
            True,
        ),
        ("Coverage", report_a.coverage_score, report_b.coverage_score, True),
        (
            "Speech cut safety",
            report_a.speech_cut_safety_rate,
            report_b.speech_cut_safety_rate,
            True,
        ),
        (
            "Constraint satisfaction",
            report_a.constraint_satisfaction_rate(),
            report_b.constraint_satisfaction_rate(),
            True,
        ),
    ]

    lines = [f"  {'Dimension':<28} {label_a:>8} {label_b:>8} {'Delta':>8}"]
    lines.append(f"  {'─' * 28} {'─' * 8} {'─' * 8} {'─' * 8}")

    for name, val_a, val_b, is_rate in dims:
        delta = val_b - val_a
        if is_rate:
            a_str = f"{val_a:.0%}" if isinstance(val_a, float) else str(val_a)
            b_str = f"{val_b:.0%}" if isinstance(val_b, float) else str(val_b)
            d_str = f"{delta:+.0%}" if isinstance(delta, float) else str(delta)
        else:
            a_str = str(val_a)
            b_str = str(val_b)
            d_str = f"{delta:+d}" if isinstance(delta, int) else f"{delta:+.1f}"
        lines.append(f"  {name:<28} {a_str:>8} {b_str:>8} {d_str:>8}")

    return "\n".join(lines)
