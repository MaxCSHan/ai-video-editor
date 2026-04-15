"""Tests for infra/atomic_write.py — atomic file write utility."""

from unittest.mock import patch

import pytest

from ai_video_editor.infra.atomic_write import atomic_write_text


class TestAtomicWriteText:
    """atomic_write_text writes content atomically via temp + rename."""

    def test_writes_content(self, tmp_path):
        path = tmp_path / "test.json"
        atomic_write_text(path, '{"key": "value"}')
        assert path.read_text() == '{"key": "value"}'

    def test_overwrites_existing(self, tmp_path):
        path = tmp_path / "test.json"
        path.write_text("old content")
        atomic_write_text(path, "new content")
        assert path.read_text() == "new content"

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "sub" / "dir" / "test.json"
        atomic_write_text(path, "content")
        assert path.read_text() == "content"

    def test_no_temp_file_on_success(self, tmp_path):
        path = tmp_path / "test.json"
        atomic_write_text(path, "content")
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []

    def test_cleans_up_temp_on_write_error(self, tmp_path):
        path = tmp_path / "test.json"
        path.write_text("original")

        with patch("os.fdopen", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                atomic_write_text(path, "new content")

        # Original file untouched
        assert path.read_text() == "original"
        # No temp files left behind
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []

    def test_encoding_parameter(self, tmp_path):
        path = tmp_path / "test.json"
        atomic_write_text(path, "日本語テスト", encoding="utf-8")
        assert path.read_text(encoding="utf-8") == "日本語テスト"

    def test_empty_content(self, tmp_path):
        path = tmp_path / "test.json"
        atomic_write_text(path, "")
        assert path.read_text() == ""
