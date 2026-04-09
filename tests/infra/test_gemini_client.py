"""Unit tests for GeminiClient — the Golden Sample module.

All google.genai SDK interactions are mocked. No API keys or network
access required.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_video_editor.domain.exceptions import FileUploadError, LLMProviderError
from ai_video_editor.infra.gemini_client import GeminiClient


# ---------------------------------------------------------------------------
# Helpers: fake Gemini File API objects
# ---------------------------------------------------------------------------


def _make_file(state: str = "ACTIVE", uri: str = "files/abc123", name: str = "abc123"):
    """Create a mock google.genai File object with the given state."""
    f = MagicMock()
    f.state.name = state
    f.uri = uri
    f.name = name
    return f


def _make_mock_genai_client():
    """Create a mock google.genai.Client with default behaviors."""
    client = MagicMock()
    # files.upload returns a PROCESSING file that becomes ACTIVE on .get()
    client.files.upload.return_value = _make_file("ACTIVE")
    client.files.get.return_value = _make_file("ACTIVE")
    # models.generate_content returns a response with .text
    response = MagicMock()
    response.text = "Generated content"
    client.models.generate_content.return_value = response
    return client


# ---------------------------------------------------------------------------
# TestFromEnv
# ---------------------------------------------------------------------------


class TestFromEnv:
    def test_creates_client_when_key_set(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key-123")
        with patch("google.genai.Client") as mock_cls:
            mock_cls.return_value = MagicMock()
            client = GeminiClient.from_env()
            mock_cls.assert_called_once_with(api_key="test-key-123")
            assert client.raw is mock_cls.return_value

    def test_raises_when_key_missing(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with pytest.raises(LLMProviderError, match="GEMINI_API_KEY is not set"):
            GeminiClient.from_env()


# ---------------------------------------------------------------------------
# TestUploadAndWait
# ---------------------------------------------------------------------------


class TestUploadAndWait:
    def test_returns_file_when_immediately_active(self, tmp_path):
        mock_sdk = _make_mock_genai_client()
        mock_sdk.files.upload.return_value = _make_file("ACTIVE")
        client = GeminiClient(mock_sdk)

        test_file = tmp_path / "clip.mp4"
        test_file.write_bytes(b"\x00" * 1024)

        result = client.upload_and_wait(test_file, label="test-clip")
        assert result.state.name == "ACTIVE"
        mock_sdk.files.upload.assert_called_once_with(file=str(test_file))
        # Should not poll at all
        mock_sdk.files.get.assert_not_called()

    def test_polls_until_active(self, tmp_path):
        mock_sdk = _make_mock_genai_client()
        processing = _make_file("PROCESSING")
        active = _make_file("ACTIVE")
        mock_sdk.files.upload.return_value = processing
        mock_sdk.files.get.side_effect = [
            _make_file("PROCESSING"),
            _make_file("PROCESSING"),
            active,
        ]
        client = GeminiClient(mock_sdk)

        test_file = tmp_path / "clip.mp4"
        test_file.write_bytes(b"\x00" * 1024)

        result = client.upload_and_wait(
            test_file, poll_interval_sec=0, label="test-clip"
        )
        assert result.state.name == "ACTIVE"
        assert mock_sdk.files.get.call_count == 3

    def test_raises_on_failed_state(self, tmp_path):
        mock_sdk = _make_mock_genai_client()
        mock_sdk.files.upload.return_value = _make_file("PROCESSING")
        mock_sdk.files.get.return_value = _make_file("FAILED")
        client = GeminiClient(mock_sdk)

        test_file = tmp_path / "clip.mp4"
        test_file.write_bytes(b"\x00" * 1024)

        with pytest.raises(FileUploadError, match="processing failed"):
            client.upload_and_wait(test_file, poll_interval_sec=0, label="bad-clip")

    def test_raises_on_timeout(self, tmp_path):
        mock_sdk = _make_mock_genai_client()
        mock_sdk.files.upload.return_value = _make_file("PROCESSING")
        # Always return PROCESSING — will never finish
        mock_sdk.files.get.return_value = _make_file("PROCESSING")
        client = GeminiClient(mock_sdk)

        test_file = tmp_path / "clip.mp4"
        test_file.write_bytes(b"\x00" * 1024)

        with pytest.raises(FileUploadError, match="timed out"):
            client.upload_and_wait(
                test_file, timeout_sec=0, poll_interval_sec=0, label="slow-clip"
            )

    def test_raises_on_upload_sdk_error(self, tmp_path):
        mock_sdk = _make_mock_genai_client()
        mock_sdk.files.upload.side_effect = ConnectionError("network down")
        client = GeminiClient(mock_sdk)

        test_file = tmp_path / "clip.mp4"
        test_file.write_bytes(b"\x00" * 1024)

        with pytest.raises(FileUploadError, match="Upload failed"):
            client.upload_and_wait(test_file, label="no-network")


# ---------------------------------------------------------------------------
# TestGenerate
# ---------------------------------------------------------------------------


class TestGenerate:
    def test_passes_through_to_sdk(self):
        mock_sdk = _make_mock_genai_client()
        client = GeminiClient(mock_sdk)
        contents = [{"role": "user", "parts": ["Hello"]}]
        config = {"temperature": 0.5}

        client.generate(model="gemini-3-flash-preview", contents=contents, config=config)

        mock_sdk.models.generate_content.assert_called_once_with(
            model="gemini-3-flash-preview",
            contents=contents,
            config=config,
        )

    def test_returns_response_object(self):
        mock_sdk = _make_mock_genai_client()
        client = GeminiClient(mock_sdk)

        response = client.generate(
            model="gemini-3-flash-preview", contents=[], config=None
        )
        assert response.text == "Generated content"


# ---------------------------------------------------------------------------
# TestRawProperty
# ---------------------------------------------------------------------------


class TestRawProperty:
    def test_exposes_underlying_client(self):
        mock_sdk = _make_mock_genai_client()
        client = GeminiClient(mock_sdk)
        assert client.raw is mock_sdk
