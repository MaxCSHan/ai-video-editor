"""Run the sidecar: ``python -m ai_video_editor.server`` (or via ``vx serve``).

Honors VX_HOST / VX_PORT env vars; defaults to 127.0.0.1:8765.
"""

from __future__ import annotations

import os


def main():
    import uvicorn

    host = os.environ.get("VX_HOST", "127.0.0.1")
    port = int(os.environ.get("VX_PORT", "8765"))
    uvicorn.run("ai_video_editor.server.app:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
