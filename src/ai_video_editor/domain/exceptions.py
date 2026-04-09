"""VX domain exception hierarchy.

All exceptions that cross module boundaries are defined here.
Adapters translate provider-specific errors into these types.
Entry points (CLI, TUI) catch VXError for user-facing messages.

Hierarchy:
    VXError
    ├── StoryboardValidationError
    ├── ClipResolutionError
    ├── ConstraintViolationError
    ├── LLMProviderError
    │   ├── LLMResponseParseError
    │   └── FileUploadError
    ├── LLMCostLimitExceeded
    ├── MediaProcessingError
    └── RenderTimeoutError
"""


class VXError(Exception):
    """Base for all VX domain errors."""


class StoryboardValidationError(VXError):
    """Storyboard violates structural constraints."""


class ClipResolutionError(VXError):
    """Clip ID could not be resolved to a known clip."""


class ConstraintViolationError(VXError):
    """User constraints (must-include, must-exclude) not satisfied."""


class LLMProviderError(VXError):
    """LLM API call failed after retries."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        phase: str,
        cause: Exception | None = None,
    ):
        self.provider = provider
        self.phase = phase
        super().__init__(f"[{provider}/{phase}] {message}")
        if cause:
            self.__cause__ = cause


class LLMResponseParseError(LLMProviderError):
    """LLM returned unparseable or invalid structured output."""


class FileUploadError(LLMProviderError):
    """File upload to LLM provider failed or timed out."""


class LLMCostLimitExceeded(VXError):
    """Cumulative LLM cost exceeded the configured limit."""


class MediaProcessingError(VXError):
    """FFmpeg or media operation failed."""

    def __init__(self, *, command: str, stderr: str, returncode: int):
        self.command = command
        self.stderr = stderr
        self.returncode = returncode
        super().__init__(f"ffmpeg failed (rc={returncode}): {stderr[:200]}")


class RenderTimeoutError(VXError):
    """Rough cut or preview render exceeded timeout."""
