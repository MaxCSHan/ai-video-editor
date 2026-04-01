"""Gemini File API URI cache — shared across pipeline stages.

Allows proxy video URIs to be uploaded once and reused by briefing,
transcription, Phase 1, and Phase 2 without redundant uploads.
"""

import json
import time

from .config import EditorialProjectPaths

FILE_API_CACHE_MAX_AGE_SEC = 90 * 60  # 90 minutes (Gemini keeps files for 2 hours)


def load_file_api_cache(editorial_paths: EditorialProjectPaths) -> dict:
    """Load cached Gemini File API URIs."""
    cache_path = editorial_paths.root / "file_api_cache.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text())
    return {}


def save_file_api_cache(editorial_paths: EditorialProjectPaths, cache: dict):
    """Save Gemini File API URI cache."""
    cache_path = editorial_paths.root / "file_api_cache.json"
    cache_path.write_text(json.dumps(cache, indent=2))


def cache_file_uri(editorial_paths: EditorialProjectPaths, clip_id: str, uri: str):
    """Cache a single file URI after upload."""
    cache = load_file_api_cache(editorial_paths)
    cache[clip_id] = {"uri": uri, "cached_at": time.time()}
    save_file_api_cache(editorial_paths, cache)


def get_cached_uri(cache: dict, clip_id: str) -> str | None:
    """Get a cached URI if still fresh (< 90 min old)."""
    entry = cache.get(clip_id)
    if not entry:
        return None
    age = time.time() - entry.get("cached_at", 0)
    if age > FILE_API_CACHE_MAX_AGE_SEC:
        return None
    return entry.get("uri")
