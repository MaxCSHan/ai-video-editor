"""Cache-key behavior for rough-cut segment extraction.

Regression guard for the bug where the segment cache filename keyed on the
segment INDEX (+ clip_id) but not on the in/out points — so a trim that kept
the same index served a stale cached segment, and a pure reorder re-encoded
identical pixels. The cache name must be content-addressed.
"""

from types import SimpleNamespace

from ai_video_editor.config import OutputFormat
from ai_video_editor.rough_cut import _segment_cache_name


def _seg(clip_id="C001", in_sec=1.0, out_sec=5.0, transition="cut"):
    return SimpleNamespace(clip_id=clip_id, in_sec=in_sec, out_sec=out_sec, transition=transition)


def _name(seg, *, overlays=None, captions=None, color="sdr", fmt=None, proxy=False):
    return _segment_cache_name(
        seg,
        seg_overlays=overlays,
        caption_segments=captions,
        color_target=color,
        output_format=fmt,
        proxy_mode=proxy,
    )


def test_reorder_reuses_cache():
    """Two segments with identical content map to the SAME cache file,
    regardless of their position/index in the edit."""
    a = _seg()
    b = _seg()  # same content, would be a different index after a reorder
    assert _name(a) == _name(b)


def test_trim_invalidates_cache():
    """Changing in/out (a trim) must produce a DIFFERENT cache file."""
    base = _seg(in_sec=1.0, out_sec=5.0)
    trimmed_in = _seg(in_sec=2.0, out_sec=5.0)
    trimmed_out = _seg(in_sec=1.0, out_sec=4.0)
    assert _name(base) != _name(trimmed_in)
    assert _name(base) != _name(trimmed_out)
    assert _name(trimmed_in) != _name(trimmed_out)


def test_transition_change_invalidates():
    assert _name(_seg(transition="cut")) != _name(_seg(transition="dissolve"))


def test_proxy_and_source_never_collide():
    seg = _seg()
    assert _name(seg, proxy=True) != _name(seg, proxy=False)


def test_overlays_and_captions_invalidate():
    seg = _seg()
    assert _name(seg) != _name(seg, overlays=["hello world"])
    assert _name(seg) != _name(seg, captions=[{"start": 0, "end": 1}])


def test_output_format_invalidates():
    seg = _seg()
    fhd = OutputFormat(width=1920, height=1080)
    uhd = OutputFormat(width=3840, height=2160)
    assert _name(seg, fmt=fhd) != _name(seg, fmt=uhd)


def test_name_shape():
    name = _name(_seg())
    assert name.startswith("seg_") and name.endswith(".mp4")
