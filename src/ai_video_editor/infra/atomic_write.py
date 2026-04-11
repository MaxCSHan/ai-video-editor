"""Atomic file write utility — write-to-temp + os.replace.

Prevents corrupt artifacts from partial writes (e.g., crash or power loss
mid-write). The two-phase commit protocol (versioning.py) tracks *metadata*
atomicity; this module handles *content* atomicity.

Usage:
    from ..infra.atomic_write import atomic_write_text
    atomic_write_text(path, storyboard.model_dump_json(indent=2))
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write content atomically via write-to-temp + rename.

    Creates a temp file in the same directory as ``path``, writes content,
    then uses ``os.replace`` (atomic on POSIX) to swap it in. If anything
    fails, the temp file is cleaned up and the original file is untouched.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
