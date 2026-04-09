"""Unit tests for clip ID resolution — fuzzy matching abbreviated IDs."""

from types import SimpleNamespace

from ai_video_editor.domain.clip_resolution import resolve_clip_id_refs


def _seg(clip_id):
    return SimpleNamespace(clip_id=clip_id, index=0, in_sec=0, out_sec=5)


def _discarded(clip_id):
    return SimpleNamespace(clip_id=clip_id, reason="test")


def _cast(appears_in):
    return SimpleNamespace(appears_in=list(appears_in))


def _storyboard(segments=None, discarded=None, cast=None):
    return SimpleNamespace(
        segments=segments or [],
        discarded=discarded or [],
        cast=cast or [],
    )


class TestResolveClipIdRefs:
    def test_exact_match_unchanged(self):
        sb = _storyboard([_seg("20260330_C0073")])
        resolve_clip_id_refs(sb, {"20260330_C0073"})
        assert sb.segments[0].clip_id == "20260330_C0073"

    def test_suffix_match(self):
        sb = _storyboard([_seg("C0073")])
        resolve_clip_id_refs(sb, {"20260330114125_C0073"})
        assert sb.segments[0].clip_id == "20260330114125_C0073"

    def test_case_insensitive_match(self):
        sb = _storyboard([_seg("c0073")])
        resolve_clip_id_refs(sb, {"20260330114125_C0073"})
        assert sb.segments[0].clip_id == "20260330114125_C0073"

    def test_no_match_returns_as_is(self):
        sb = _storyboard([_seg("UNKNOWN")])
        resolve_clip_id_refs(sb, {"20260330114125_C0073"})
        assert sb.segments[0].clip_id == "UNKNOWN"

    def test_resolves_discarded(self):
        sb = _storyboard(discarded=[_discarded("C0059")])
        resolve_clip_id_refs(sb, {"20260330_C0059"})
        assert sb.discarded[0].clip_id == "20260330_C0059"

    def test_resolves_cast_appears_in(self):
        sb = _storyboard(cast=[_cast(["C0073", "C0059"])])
        known = {"20260330_C0073", "20260330_C0059"}
        resolve_clip_id_refs(sb, known)
        assert sb.cast[0].appears_in == ["20260330_C0073", "20260330_C0059"]

    def test_multiple_segments(self):
        sb = _storyboard([_seg("C0073"), _seg("C0059"), _seg("20260330_C0042")])
        known = {"20260330_C0073", "20260330_C0059", "20260330_C0042"}
        resolve_clip_id_refs(sb, known)
        assert sb.segments[0].clip_id == "20260330_C0073"
        assert sb.segments[1].clip_id == "20260330_C0059"
        assert sb.segments[2].clip_id == "20260330_C0042"

    def test_empty_storyboard(self):
        sb = _storyboard()
        resolve_clip_id_refs(sb, {"20260330_C0073"})
        # No error
