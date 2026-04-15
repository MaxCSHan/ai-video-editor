"""Lightweight i18n for VX — locale-aware string lookup with {variable} interpolation.

Usage:
    from .i18n import t, get_locale, set_locale

    # Simple lookup
    print(t("app.title"))  # "VX — AI Video Editor"

    # With interpolation
    print(t("setup.python_found", version="3.11.8"))  # "Python 3.11.8"

    # Missing key falls back to the key itself
    print(t("nonexistent.key"))  # "nonexistent.key"

Locale resolution order:
    1. Explicit set_locale() call
    2. VX_LANG environment variable (e.g., "zh-TW")
    3. .vx.json "locale" field
    4. System LC_ALL / LANG
    5. Fallback: "en"
"""

import json
import os
from pathlib import Path

_LOCALE_DIR = Path(__file__).parent / "locales"
_strings: dict[str, str] = {}
_current_locale: str = ""
_fallback_strings: dict[str, str] = {}  # Always English


def _detect_locale() -> str:
    """Detect user locale from environment and config."""
    # 1. VX_LANG env var (explicit override)
    vx_lang = os.environ.get("VX_LANG", "").strip()
    if vx_lang:
        return _normalize_locale(vx_lang)

    # 2. .vx.json config
    vx_config = Path(".vx.json")
    if vx_config.exists():
        try:
            data = json.loads(vx_config.read_text())
            if loc := data.get("locale", "").strip():
                return _normalize_locale(loc)
        except (json.JSONDecodeError, OSError):
            pass

    # 3. System locale
    for var in ("LC_ALL", "LANG", "LC_MESSAGES"):
        val = os.environ.get(var, "").strip()
        if val and val != "C" and val != "POSIX":
            return _normalize_locale(val)

    return "en"


def _normalize_locale(raw: str) -> str:
    """Normalize locale string: 'zh_TW.UTF-8' → 'zh-TW', 'en_US' → 'en'."""
    # Strip encoding suffix
    code = raw.split(".")[0].split("@")[0].strip()
    # Convert underscore to hyphen
    code = code.replace("_", "-")

    # Try exact match first
    if (_LOCALE_DIR / f"{code}.json").exists():
        return code

    # Try base language (e.g., "zh-TW" → "zh")
    base = code.split("-")[0]
    if (_LOCALE_DIR / f"{base}.json").exists():
        return base

    # For English variants, just use "en"
    if base == "en":
        return "en"

    return "en"


def _load_locale(locale: str) -> dict[str, str]:
    """Load a locale JSON file. Returns empty dict on failure."""
    path = _LOCALE_DIR / f"{locale}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _ensure_loaded():
    """Lazy-load strings on first access."""
    global _strings, _current_locale, _fallback_strings
    if _current_locale:
        return

    # Always load English as fallback
    _fallback_strings = _load_locale("en")

    _current_locale = _detect_locale()
    if _current_locale == "en":
        _strings = _fallback_strings
    else:
        _strings = _load_locale(_current_locale)


def t(key: str, **kwargs) -> str:
    """Look up a translated string by key, with optional {variable} interpolation.

    Falls back to English, then to the raw key if not found.
    """
    _ensure_loaded()
    template = _strings.get(key) or _fallback_strings.get(key) or key
    if kwargs:
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError):
            return template
    return template


def get_locale() -> str:
    """Return the current active locale code (e.g., 'en', 'zh-TW')."""
    _ensure_loaded()
    return _current_locale


def set_locale(locale: str):
    """Switch locale at runtime. Reloads strings immediately."""
    global _strings, _current_locale
    _current_locale = _normalize_locale(locale)
    if _current_locale == "en":
        _strings = _fallback_strings
    else:
        _strings = _load_locale(_current_locale)


def get_available_locales() -> list[dict[str, str]]:
    """Return list of available locales with their display names.

    Returns: [{"code": "en", "name": "English"}, {"code": "zh-TW", "name": "繁體中文"}, ...]
    """
    _ensure_loaded()
    locales = []
    if not _LOCALE_DIR.exists():
        return [{"code": "en", "name": "English"}]
    for f in sorted(_LOCALE_DIR.glob("*.json")):
        code = f.stem
        strings = _load_locale(code)
        name = strings.get("_locale_name", code)
        locales.append({"code": code, "name": name})
    return locales


def locale_language_name() -> str:
    """Return the full language name for the current locale (for AI prompt injection).

    E.g., 'en' → 'English', 'zh-TW' → 'Traditional Chinese', 'ja' → 'Japanese'.
    """
    _ensure_loaded()
    return _strings.get("_locale_language", _fallback_strings.get("_locale_language", "English"))
