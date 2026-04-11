"""Shared Gemini client infrastructure — single source of truth for client
creation, file upload, and processing wait.

Eliminates 4× duplicated _wait_for_gemini_file and 7× duplicated client
creation scattered across editorial_agent, briefing, transcribe, and
gemini_analyze.

This is the Golden Sample module. All conventions in the Architecture
Manifesto are demonstrated here. Use this as the template for new modules.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from ..domain.exceptions import FileUploadError, LLMProviderError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (single definition — was duplicated 4× across the codebase)
# ---------------------------------------------------------------------------

GEMINI_UPLOAD_TIMEOUT_SEC = 300  # 5 minutes
GEMINI_UPLOAD_POLL_SEC = 3


class GeminiClient:
    """Thin wrapper around google.genai.Client providing:

    - Centralized client creation with API key validation
    - File upload with processing wait, timeout, and FAILED state handling
    - Domain exception translation (google SDK errors → VX exceptions)

    Usage::

        client = GeminiClient.from_env()
        video_file = client.upload_and_wait(proxy_path, label="C0073")
        response = client.generate(model="gemini-3-flash-preview", contents=[...])
    """

    def __init__(self, client):
        """Wrap an existing google.genai.Client instance.

        Prefer ``from_env()`` for normal usage. Direct construction is
        useful for testing (pass a mock) or when sharing a client across
        multiple operations.
        """
        self._client = client

    @classmethod
    def from_env(cls) -> GeminiClient:
        """Create client from GEMINI_API_KEY environment variable.

        Raises LLMProviderError if the key is not set.
        """
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise LLMProviderError(
                "GEMINI_API_KEY is not set. Add it to your .env file (see .env.example).",
                provider="gemini",
                phase="init",
            )
        from google import genai

        return cls(genai.Client(api_key=api_key))

    @property
    def raw(self):
        """Access the underlying google.genai.Client for advanced operations.

        Prefer the typed methods on this class. Use .raw only for operations
        not yet wrapped (e.g., editorial_director tool-calling configs).
        """
        return self._client

    def upload_and_wait(
        self,
        file_path: Path,
        *,
        timeout_sec: int = GEMINI_UPLOAD_TIMEOUT_SEC,
        poll_interval_sec: int = GEMINI_UPLOAD_POLL_SEC,
        label: str = "",
    ):
        """Upload a file to Gemini File API and wait for processing.

        This is the single correct implementation, replacing 4 variants
        with inconsistent behavior. It:

        - Polls with configurable interval (was 2s/3s/5s depending on file)
        - Checks for FAILED state (was missing in 2 of 4 implementations)
        - Raises FileUploadError on timeout or failure
        - Logs progress with structured context

        Returns the processed google.genai File object.
        """
        display_name = label or file_path.name
        size_mb = file_path.stat().st_size / 1024 / 1024
        log.info(
            "gemini.upload.start",
            extra={"file": display_name, "size_mb": round(size_mb, 1)},
        )

        try:
            video_file = self._client.files.upload(file=str(file_path))
        except Exception as e:  # Intentional: translates all SDK errors to domain exception
            raise FileUploadError(
                f"Upload failed for {display_name}: {e}",
                provider="gemini",
                phase="upload",
                cause=e,
            )

        start = time.monotonic()
        while video_file.state.name == "PROCESSING":
            elapsed = time.monotonic() - start
            if elapsed > timeout_sec:
                raise FileUploadError(
                    f"File processing timed out after {timeout_sec}s for {display_name}",
                    provider="gemini",
                    phase="upload",
                )
            log.debug(
                "gemini.upload.polling",
                extra={"file": display_name, "elapsed_sec": round(elapsed, 1)},
            )
            time.sleep(poll_interval_sec)
            video_file = self._client.files.get(name=video_file.name)

        if video_file.state.name == "FAILED":
            raise FileUploadError(
                f"Gemini file processing failed for {display_name}",
                provider="gemini",
                phase="upload",
            )

        elapsed = time.monotonic() - start
        log.info(
            "gemini.upload.complete",
            extra={"file": display_name, "elapsed_sec": round(elapsed, 1)},
        )
        return video_file

    def generate(self, *, model: str, contents, config=None):
        """Generate content via the Gemini API.

        Thin wrapper around google.genai.Client.models.generate_content.
        Returns the full response object. No retry logic — the existing
        traced_gemini_generate in tracing.py handles retries.
        """
        return self._client.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )
