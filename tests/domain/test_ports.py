"""Tests for domain Protocol definitions."""

from ai_video_editor.domain.ports import Phase1Reviewer


class _MockReviewer:
    """Mock that satisfies the Phase1Reviewer Protocol."""

    def run(
        self,
        editorial_paths,
        manifest,
        force=False,
        tracer=None,
        style_supplement=None,
        only_clip_ids=None,
        user_context=None,
    ):
        return [{"clip_id": "C001", "summary": "test"}], []


def test_mock_satisfies_protocol():
    """A class with the right method signature satisfies Phase1Reviewer."""
    reviewer: Phase1Reviewer = _MockReviewer()
    reviews, failed = reviewer.run(None, {"clips": []})
    assert len(reviews) == 1
    assert failed == []
