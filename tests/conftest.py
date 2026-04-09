"""Shared test fixtures for the VX test suite."""

from ai_video_editor.domain.exceptions import (
    FileUploadError,
    LLMProviderError,
    VXError,
)

# Re-export domain exceptions for convenient use in tests
__all__ = ["FileUploadError", "LLMProviderError", "VXError"]
