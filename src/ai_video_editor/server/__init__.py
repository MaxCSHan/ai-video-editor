"""VX FastAPI sidecar — the HTTP/WebSocket bridge for the native macOS app.

This package wraps the existing pipeline (editorial_agent, rough_cut, render,
versioning, tracing) behind a small REST + WebSocket API. It adds NO new
pipeline logic: every mutating endpoint dispatches to the same functions the
CLI uses, and every read endpoint serves the same JSON artifacts under
``library/``.

Run it with ``vx serve`` (see ``ai_video_editor.cli``) or
``uvicorn ai_video_editor.server.app:app``.
"""

from .app import create_app

__all__ = ["create_app"]
