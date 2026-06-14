"""Stage-timing instrumentation (tracing.record_stage_timing / load / time_stage)."""

import pytest

from ai_video_editor.tracing import load_stage_timings, record_stage_timing, time_stage


def test_record_and_load(tmp_path):
    record_stage_timing(tmp_path, "preprocess", 12.34, {"clips": 3})
    record_stage_timing(tmp_path, "rough_cut", 5.0)
    rows = load_stage_timings(tmp_path)
    assert [r["stage"] for r in rows] == ["preprocess", "rough_cut"]
    assert rows[0]["seconds"] == 12.34
    assert rows[0]["meta"]["clips"] == 3
    assert "at" in rows[0]


def test_load_missing_returns_empty(tmp_path):
    assert load_stage_timings(tmp_path) == []


def test_time_stage_records_on_success(tmp_path):
    with time_stage(tmp_path, "demo"):
        pass
    rows = load_stage_timings(tmp_path)
    assert len(rows) == 1
    assert rows[0]["stage"] == "demo"
    assert rows[0]["meta"]["ok"] is True
    assert rows[0]["seconds"] >= 0


def test_time_stage_records_on_error(tmp_path):
    with pytest.raises(ValueError):
        with time_stage(tmp_path, "boom"):
            raise ValueError("x")
    rows = load_stage_timings(tmp_path)
    assert len(rows) == 1
    assert rows[0]["meta"]["ok"] is False
