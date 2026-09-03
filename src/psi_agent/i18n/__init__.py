"""Lightweight i18n for user-visible psi-agent messages.

Keeps translated strings in flat JSON dictionaries next to this module
(``zh-CN.json`` / ``en-US.json``) and exposes a tiny ``t()`` helper.  No
framework dependency: callers pick a language from ``HAITUN_LANG``, the
Gateway ``--language`` flag, user prefs, or the installer default, and ask for
a key.
"""

from __future__ import annotations

import json
import os
from functools import cache
from pathlib import Path
from typing import Any

DEFAULT_LANGUAGE = "zh-CN"
SUPPORTED_LANGUAGES = ("zh-CN", "zh-TW", "en-US")

_PACKAGE_DIR = Path(__file__).parent
_ALIASES = {
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh-hans": "zh-CN",
    "zh-hant": "zh-TW",
    "zh-tw": "zh-TW",
    "zh-hk": "zh-TW",
    "zh-mo": "zh-TW",
    "en": "en-US",
    "en-us": "en-US",
    "en-gb": "en-US",
}


def normalize_language(raw: str | None) -> str:
    """Map a loose language tag (``zh``, ``en_US``, …) to a supported code."""
    if not raw:
        return DEFAULT_LANGUAGE
    code = raw.strip().lower().replace("_", "-")
    return _ALIASES.get(code, DEFAULT_LANGUAGE)


def language_from_env() -> str:
    """Resolve the process-level language from ``HAITUN_LANG``."""
    return normalize_language(os.environ.get("HAITUN_LANG"))


@cache
def _load_messages(language: str) -> dict[str, str]:
    path = _PACKAGE_DIR / f"{normalize_language(language)}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        raw = {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def messages(language: str = DEFAULT_LANGUAGE) -> dict[str, str]:
    """Return the message dictionary for *language* (empty dict on failure)."""
    return _load_messages(language)


def t(key: str, language: str = DEFAULT_LANGUAGE, **kwargs: Any) -> str:
    """Translate *key*; fall back to zh-CN, then to the key itself.

    Positional ``{name}`` placeholders are filled from ``kwargs``.
    """
    text = _load_messages(language).get(key)
    if text is None and normalize_language(language) != DEFAULT_LANGUAGE:
        text = _load_messages(DEFAULT_LANGUAGE).get(key)
    if text is None:
        return key
    if kwargs:
        for name, value in kwargs.items():
            text = text.replace("{" + name + "}", str(value))
    return text
