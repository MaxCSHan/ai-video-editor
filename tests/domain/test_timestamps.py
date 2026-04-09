"""Unit tests for timestamp clamping — constraining segments to usable bounds."""

from types import SimpleNamespace

from ai_video_editor.domain.timestamps import clamp_segments_to_usable


def _seg(index=0, clip_id="C001", in_sec=0.0, out_sec=5.0):
    return SimpleNamespace(index=index, clip_id=clip_id, in_sec=in_sec, out_sec=out_sec)


def _storyboard(segments):
    return SimpleNamespace(segments=segments)


class TestClampSegmentsToUsable:
    def test_no_clamping_needed(self):
        sb = _storyboard([_seg(0, "C001", 2.0, 8.0)])
        reviews = {"C001": {"usable_segments": [{"in_sec": 0, "out_sec": 10}]}}
        fixes = clamp_segments_to_usable(sb, reviews)
        assert fixes == []
        assert sb.segments[0].in_sec == 2.0
        assert sb.segments[0].out_sec == 8.0

    def test_clamps_in_sec_up(self):
        sb = _storyboard([_seg(0, "C001", 1.0, 8.0)])
        reviews = {"C001": {"usable_segments": [{"in_sec": 3.0, "out_sec": 10.0}]}}
        fixes = clamp_segments_to_usable(sb, reviews)
        assert len(fixes) == 1
        assert "in_sec" in fixes[0]
        assert sb.segments[0].in_sec == 3.0

    def test_clamps_out_sec_down(self):
        sb = _storyboard([_seg(0, "C001", 2.0, 15.0)])
        reviews = {"C001": {"usable_segments": [{"in_sec": 0, "out_sec": 10.0}]}}
        fixes = clamp_segments_to_usable(sb, reviews)
        assert len(fixes) == 1
        assert "out_sec" in fixes[0]
        assert sb.segments[0].out_sec == 10.0

    def test_clamps_both_ends(self):
        sb = _storyboard([_seg(0, "C001", 1.0, 20.0)])
        reviews = {"C001": {"usable_segments": [{"in_sec": 5.0, "out_sec": 12.0}]}}
        fixes = clamp_segments_to_usable(sb, reviews)
        assert len(fixes) == 2
        assert sb.segments[0].in_sec == 5.0
        assert sb.segments[0].out_sec == 12.0

    def test_picks_best_overlap(self):
        """When multiple usable segments exist, picks the one with best overlap."""
        sb = _storyboard([_seg(0, "C001", 8.0, 14.0)])
        reviews = {
            "C001": {
                "usable_segments": [
                    {"in_sec": 0, "out_sec": 5},  # no overlap
                    {"in_sec": 10, "out_sec": 20},  # best overlap (4s)
                ]
            }
        }
        clamp_segments_to_usable(sb, reviews)
        assert sb.segments[0].in_sec == 10.0  # clamped to best match
        assert sb.segments[0].out_sec == 14.0  # within bounds, no change

    def test_unknown_clip_skipped(self):
        sb = _storyboard([_seg(0, "UNKNOWN", 0, 5)])
        reviews = {"C001": {"usable_segments": [{"in_sec": 3, "out_sec": 10}]}}
        fixes = clamp_segments_to_usable(sb, reviews)
        assert fixes == []

    def test_no_usable_segments_skipped(self):
        sb = _storyboard([_seg(0, "C001", 0, 5)])
        reviews = {"C001": {"usable_segments": []}}
        fixes = clamp_segments_to_usable(sb, reviews)
        assert fixes == []

    def test_multiple_segments(self):
        sb = _storyboard(
            [
                _seg(0, "C001", 0.0, 20.0),
                _seg(1, "C002", 0.0, 5.0),
            ]
        )
        reviews = {
            "C001": {"usable_segments": [{"in_sec": 2, "out_sec": 10}]},
            "C002": {"usable_segments": [{"in_sec": 0, "out_sec": 8}]},
        }
        clamp_segments_to_usable(sb, reviews)
        assert sb.segments[0].in_sec == 2.0
        assert sb.segments[0].out_sec == 10.0
        assert sb.segments[1].in_sec == 0.0  # already within bounds
        assert sb.segments[1].out_sec == 5.0  # already within bounds
