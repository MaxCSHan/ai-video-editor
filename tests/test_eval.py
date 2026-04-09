"""Unit tests for eval.py — deterministic storyboard scoring."""

from types import SimpleNamespace

from ai_video_editor.eval import (
    EvalReport,
    _fuzzy_match,
    _parse_constraint_phrases,
    compare_reports,
    score_constraint_satisfaction,
    score_coverage,
    score_speech_cut_safety,
    score_storyboard,
    score_structural_completeness,
    score_timestamp_precision,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seg(index=0, clip_id="C001", in_sec=0.0, out_sec=5.0, description="test", purpose="test"):
    return SimpleNamespace(
        index=index,
        clip_id=clip_id,
        in_sec=in_sec,
        out_sec=out_sec,
        description=description,
        purpose=purpose,
    )


def _storyboard(
    segments=None,
    editorial_reasoning="",
    story_arc=None,
    cast=None,
    discarded=None,
    estimated_duration_sec=0,
):
    return SimpleNamespace(
        segments=segments or [],
        editorial_reasoning=editorial_reasoning,
        story_arc=story_arc or [],
        cast=cast or [],
        discarded=discarded or [],
        estimated_duration_sec=estimated_duration_sec,
    )


def _review(clip_id="C001", usable=None, duration_sec=30.0):
    return {
        "clip_id": clip_id,
        "duration_sec": duration_sec,
        "usable_segments": usable or [{"in_sec": 0, "out_sec": 30}],
    }


# ---------------------------------------------------------------------------
# _parse_constraint_phrases
# ---------------------------------------------------------------------------


class TestParseConstraintPhrases:
    def test_comma_separated(self):
        assert _parse_constraint_phrases("hiking trail, waterfall scene") == [
            "hiking trail",
            "waterfall scene",
        ]

    def test_semicolon_separated(self):
        assert _parse_constraint_phrases("sunset; group photo") == ["sunset", "group photo"]

    def test_and_separated(self):
        result = _parse_constraint_phrases("hiking trail and waterfall scene")
        assert "hiking trail" in result
        assert "waterfall scene" in result

    def test_short_phrases_filtered(self):
        assert _parse_constraint_phrases("hi, hello, this is a longer phrase") == [
            "this is a longer phrase"
        ]

    def test_empty_string(self):
        assert _parse_constraint_phrases("") == []


# ---------------------------------------------------------------------------
# _fuzzy_match
# ---------------------------------------------------------------------------


class TestFuzzyMatch:
    def test_exact_words_match(self):
        assert _fuzzy_match("hiking trail", "Beautiful hiking trail in the mountains")

    def test_no_match(self):
        assert not _fuzzy_match("swimming pool", "hiking trail in mountains")

    def test_single_keyword_needs_two_word_overlap(self):
        # With only 1 query word (after stop word removal), min(2, 1) = 1, so 1 overlap suffices
        assert _fuzzy_match("waterfall", "Big waterfall splash")

    def test_stop_words_ignored(self):
        assert not _fuzzy_match("the a an", "the a an in on to")  # all stop words

    def test_case_insensitive(self):
        assert _fuzzy_match("HIKING TRAIL", "hiking trail in forest")


# ---------------------------------------------------------------------------
# score_constraint_satisfaction
# ---------------------------------------------------------------------------


class TestScoreConstraintSatisfaction:
    def test_must_include_satisfied(self):
        sb = _storyboard([_seg(0, "C001", description="hiking trail in mountains")])
        ctx = {"highlights": "hiking trail"}
        results = score_constraint_satisfaction(sb, ctx)
        assert len(results) == 1
        assert results[0].satisfied
        assert results[0].constraint_type == "must_include"

    def test_must_include_not_found(self):
        sb = _storyboard([_seg(0, "C001", description="beach party")])
        ctx = {"highlights": "hiking trail"}
        results = score_constraint_satisfaction(sb, ctx)
        assert not results[0].satisfied

    def test_must_exclude_satisfied(self):
        sb = _storyboard([_seg(0, "C001", description="hiking trail")])
        ctx = {"avoid": "swimming pool"}
        results = score_constraint_satisfaction(sb, ctx)
        assert results[0].satisfied  # swimming pool not found = good

    def test_must_exclude_violated(self):
        sb = _storyboard([_seg(0, "C001", description="swimming pool party")])
        ctx = {"avoid": "swimming pool"}
        results = score_constraint_satisfaction(sb, ctx)
        assert not results[0].satisfied  # swimming pool found = violation

    def test_no_constraints(self):
        sb = _storyboard([_seg()])
        results = score_constraint_satisfaction(sb, {})
        assert results == []

    def test_multiple_constraints(self):
        sb = _storyboard(
            [
                _seg(0, "C001", description="hiking trail view"),
                _seg(1, "C002", description="sunset over lake"),
            ]
        )
        ctx = {"highlights": "hiking trail, sunset view"}
        results = score_constraint_satisfaction(sb, ctx)
        assert len(results) == 2


# ---------------------------------------------------------------------------
# score_timestamp_precision
# ---------------------------------------------------------------------------


class TestScoreTimestampPrecision:
    def test_all_valid(self):
        sb = _storyboard([_seg(0, "C001", 2.0, 8.0)])
        reviews = [_review("C001", [{"in_sec": 0, "out_sec": 10}])]
        total, valid, clamped, invalid = score_timestamp_precision(sb, reviews)
        assert total == 1
        assert valid == 1

    def test_needs_clamping(self):
        sb = _storyboard([_seg(0, "C001", 0.0, 15.0)])
        reviews = [_review("C001", [{"in_sec": 2, "out_sec": 10}])]
        total, valid, clamped, invalid = score_timestamp_precision(sb, reviews)
        assert clamped == 1

    def test_unknown_clip(self):
        sb = _storyboard([_seg(0, "UNKNOWN", 0.0, 5.0)])
        reviews = [_review("C001")]
        total, valid, clamped, invalid = score_timestamp_precision(sb, reviews)
        assert invalid == 1

    def test_inverted_timestamps_not_valid(self):
        sb = _storyboard([_seg(0, "C001", 10.0, 5.0)])
        reviews = [_review("C001")]
        total, valid, clamped, invalid = score_timestamp_precision(sb, reviews)
        assert valid == 0

    def test_empty_storyboard(self):
        sb = _storyboard([])
        total, valid, clamped, invalid = score_timestamp_precision(sb, [])
        assert total == 0


# ---------------------------------------------------------------------------
# score_structural_completeness
# ---------------------------------------------------------------------------


class TestScoreStructuralCompleteness:
    def test_fully_complete(self):
        sb = _storyboard(
            segments=[_seg(0), _seg(1)],
            editorial_reasoning="A" * 60,
            story_arc=[{"title": "intro"}],
            cast=[{"name": "Max"}],
            discarded=[{"clip_id": "C999"}],
        )
        assert score_structural_completeness(sb) == 1.0

    def test_missing_everything(self):
        sb = _storyboard(segments=[_seg(0)])
        score = score_structural_completeness(sb)
        assert score < 0.5  # missing reasoning, arc, cast, discarded

    def test_duplicate_indices_penalized(self):
        sb = _storyboard(
            segments=[_seg(0), _seg(0)],  # duplicate index
            editorial_reasoning="A" * 60,
            story_arc=[{"title": "intro"}],
            cast=[{"name": "Max"}],
            discarded=[{"clip_id": "C999"}],
        )
        assert score_structural_completeness(sb) == 0.8  # 4/5 checks pass


# ---------------------------------------------------------------------------
# score_coverage
# ---------------------------------------------------------------------------


class TestScoreCoverage:
    def test_full_coverage(self):
        sb = _storyboard(
            segments=[_seg(0, "C001"), _seg(1, "C002")],
            discarded=[SimpleNamespace(clip_id="C003")],
        )
        reviews = [_review("C001"), _review("C002"), _review("C003")]
        assert score_coverage(sb, reviews) == 1.0

    def test_partial_coverage(self):
        sb = _storyboard(segments=[_seg(0, "C001")])
        reviews = [_review("C001"), _review("C002"), _review("C003")]
        score = score_coverage(sb, reviews)
        assert 0.3 < score < 0.4  # 1/3

    def test_no_reviews(self):
        sb = _storyboard()
        assert score_coverage(sb, []) == 1.0


# ---------------------------------------------------------------------------
# score_speech_cut_safety
# ---------------------------------------------------------------------------


class TestScoreSpeechCutSafety:
    def test_no_speech_at_cut(self):
        sb = _storyboard([_seg(0, "C001", 0, 5)])
        transcripts = {"C001": [{"start": 6, "end": 10, "text": "hello world.", "type": "speech"}]}
        rate, unsafe = score_speech_cut_safety(sb, transcripts)
        assert rate == 1.0
        assert unsafe == []

    def test_cut_mid_sentence(self):
        sb = _storyboard([_seg(0, "C001", 0, 5)])
        transcripts = {"C001": [{"start": 3, "end": 8, "text": "I was walking", "type": "speech"}]}
        rate, unsafe = score_speech_cut_safety(sb, transcripts)
        assert rate == 0.0
        assert len(unsafe) == 1

    def test_cut_at_sentence_end(self):
        sb = _storyboard([_seg(0, "C001", 0, 5)])
        transcripts = {
            "C001": [{"start": 3, "end": 5.3, "text": "I was walking.", "type": "speech"}]
        }
        rate, unsafe = score_speech_cut_safety(sb, transcripts)
        assert rate == 1.0

    def test_no_transcript(self):
        sb = _storyboard([_seg(0, "C001", 0, 5)])
        rate, unsafe = score_speech_cut_safety(sb, {})
        assert rate == 1.0

    def test_empty_storyboard(self):
        rate, unsafe = score_speech_cut_safety(_storyboard(), {})
        assert rate == 1.0


# ---------------------------------------------------------------------------
# score_storyboard (integration)
# ---------------------------------------------------------------------------


class TestScoreStoryboard:
    def test_full_report(self):
        sb = _storyboard(
            segments=[_seg(0, "C001", 2, 8, "hiking trail view", "intro")],
            editorial_reasoning="A" * 60 + " must include constraint",
            story_arc=[{"title": "intro"}],
            cast=[{"name": "Max"}],
            discarded=[SimpleNamespace(clip_id="C002")],
            estimated_duration_sec=6,
        )
        reviews = [_review("C001"), _review("C002")]
        ctx = {"highlights": "hiking trail"}
        report = score_storyboard(sb, reviews, ctx)

        assert isinstance(report, EvalReport)
        assert report.total_segments == 1
        assert report.has_editorial_reasoning
        assert report.constraint_satisfaction_rate() == 1.0
        assert report.coverage_score == 1.0

    def test_report_summary_is_string(self):
        report = score_storyboard(_storyboard([_seg()]), [_review()])
        assert isinstance(report.summary(), str)


# ---------------------------------------------------------------------------
# EvalReport methods
# ---------------------------------------------------------------------------


class TestEvalReport:
    def test_constraint_rate_no_constraints(self):
        assert EvalReport().constraint_satisfaction_rate() == 1.0

    def test_timestamp_rate_no_segments(self):
        assert EvalReport().timestamp_precision_rate() == 1.0

    def test_constraint_rate_calculation(self):
        r = EvalReport(
            constraints=[
                SimpleNamespace(satisfied=True),
                SimpleNamespace(satisfied=False),
            ]
        )
        assert r.constraint_satisfaction_rate() == 0.5


# ---------------------------------------------------------------------------
# compare_reports
# ---------------------------------------------------------------------------


class TestCompareReports:
    def test_produces_table(self):
        a = score_storyboard(_storyboard([_seg()]), [_review()])
        b = score_storyboard(_storyboard([_seg(), _seg(1, "C001", 6, 10)]), [_review()])
        result = compare_reports(a, b)
        assert "Dimension" in result
        assert "Segments" in result
        assert "Delta" in result
