"""Unit tests for domain validation — storyboard and clip review checks."""

from types import SimpleNamespace

from ai_video_editor.domain.validation import validate_clip_review, validate_storyboard


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seg(index=0, clip_id="C001", in_sec=0.0, out_sec=5.0):
    return SimpleNamespace(index=index, clip_id=clip_id, in_sec=in_sec, out_sec=out_sec)


def _storyboard(segments=None):
    return SimpleNamespace(segments=segments or [])


def _review(clip_id="C001", duration_sec=30.0):
    return {"clip_id": clip_id, "duration_sec": duration_sec}


# ---------------------------------------------------------------------------
# validate_clip_review
# ---------------------------------------------------------------------------


class TestValidateClipReview:
    def test_valid_review_no_warnings(self):
        review = {
            "clip_id": "C001",
            "usable_segments": [{"in_sec": 0, "out_sec": 10}],
        }
        warnings, critical = validate_clip_review(review, {"clip_id": "C001", "duration_sec": 30})
        assert warnings == []
        assert not critical

    def test_bad_timestamp_warning(self):
        review = {
            "clip_id": "C001",
            "usable_segments": [{"in_sec": 10, "out_sec": 5}],
        }
        warnings, critical = validate_clip_review(review, {"clip_id": "C001", "duration_sec": 30})
        assert any("in_sec" in w and "out_sec" in w for w in warnings)

    def test_exceeds_duration_warning(self):
        review = {
            "clip_id": "C001",
            "usable_segments": [{"in_sec": 0, "out_sec": 50}],
        }
        warnings, _ = validate_clip_review(review, {"clip_id": "C001", "duration_sec": 30})
        assert any("exceeds clip duration" in w for w in warnings)

    def test_no_segments_critical_for_long_clip(self):
        review = {"clip_id": "C001", "usable_segments": [], "discard_segments": []}
        _, critical = validate_clip_review(review, {"clip_id": "C001", "duration_sec": 10})
        assert critical

    def test_no_segments_not_critical_for_short_clip(self):
        review = {"clip_id": "C001", "usable_segments": [], "discard_segments": []}
        _, critical = validate_clip_review(review, {"clip_id": "C001", "duration_sec": 3})
        assert not critical

    def test_majority_bad_timestamps_critical(self):
        review = {
            "clip_id": "C001",
            "usable_segments": [
                {"in_sec": 10, "out_sec": 5},
                {"in_sec": 20, "out_sec": 15},
                {"in_sec": 0, "out_sec": 3},
            ],
        }
        _, critical = validate_clip_review(review, {"clip_id": "C001", "duration_sec": 30})
        assert critical  # 2/3 have bad timestamps


# ---------------------------------------------------------------------------
# validate_storyboard
# ---------------------------------------------------------------------------


class TestValidateStoryboard:
    def test_valid_storyboard_no_warnings(self):
        sb = _storyboard([_seg(0, "C001", 0, 5), _seg(1, "C002", 2, 8)])
        reviews = [_review("C001", 30), _review("C002", 30)]
        warnings, critical = validate_storyboard(sb, reviews)
        assert warnings == []
        assert not critical

    def test_unknown_clip_id(self):
        sb = _storyboard([_seg(0, "UNKNOWN", 0, 5)])
        reviews = [_review("C001", 30)]
        warnings, _ = validate_storyboard(sb, reviews)
        assert any("unknown clip_id" in w for w in warnings)

    def test_inverted_timestamps(self):
        sb = _storyboard([_seg(0, "C001", 10, 5)])
        reviews = [_review("C001", 30)]
        warnings, _ = validate_storyboard(sb, reviews)
        assert any("in_sec" in w and "out_sec" in w for w in warnings)

    def test_exceeds_duration(self):
        sb = _storyboard([_seg(0, "C001", 0, 50)])
        reviews = [_review("C001", 30)]
        warnings, _ = validate_storyboard(sb, reviews)
        assert any("out_sec" in w and "clip duration" in w for w in warnings)

    def test_empty_storyboard_critical(self):
        sb = _storyboard([])
        warnings, critical = validate_storyboard(sb, [_review("C001")])
        assert critical
        assert any("no segments" in w.lower() for w in warnings)

    def test_duplicate_indices(self):
        sb = _storyboard([_seg(0, "C001", 0, 5), _seg(0, "C001", 6, 10)])
        reviews = [_review("C001", 30)]
        warnings, _ = validate_storyboard(sb, reviews)
        assert any("Duplicate" in w for w in warnings)

    def test_many_unknown_ids_critical(self):
        sb = _storyboard(
            [
                _seg(0, "BAD1", 0, 5),
                _seg(1, "BAD2", 0, 5),
                _seg(2, "C001", 0, 5),
            ]
        )
        reviews = [_review("C001", 30)]
        _, critical = validate_storyboard(sb, reviews)
        assert critical  # 2/3 unknown > 30%
