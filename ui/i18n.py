# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

import json
import os
from contextvars import ContextVar
from pathlib import Path
from functools import lru_cache

_LOCALES_DIR = Path(__file__).parent / "locales"
_DEBUG = os.getenv("CELERP_DEBUG_I18N") == "1"

# Context variable holds the active language for the current request
_current_lang: ContextVar[str] = ContextVar("celerp_lang", default="en")

# RTL languages
RTL_LANGS = frozenset({"ar", "he", "fa", "ur"})


def _load(lang: str) -> dict:
    path = _LOCALES_DIR / f"{lang}.json"
    if not path.exists():
        path = _LOCALES_DIR / "en.json"
    return json.loads(path.read_text())


_cached_load = lru_cache(maxsize=32)(_load) if not _DEBUG else _load


def t(key: str, lang: str | None = None, **kwargs) -> str:
    """Translate *key*. Uses context language when *lang* is not passed.

    Falls back: locale -> English -> key itself.
    Supports ``{param}`` interpolation via **kwargs.
    """
    if lang is None:
        lang = _current_lang.get()
    locale = _cached_load(lang)
    en = _cached_load("en")
    text = locale.get(key, en.get(key, key))
    return text.format(**kwargs) if kwargs else text


def get_lang(request) -> str:
    """Extract language from cookie, falling back to Accept-Language header, then 'en'."""
    if request is None:
        return "en"
    lang = request.cookies.get("celerp_lang", "")
    if lang:
        return lang
    accept = request.headers.get("accept-language", "")
    for part in accept.split(","):
        code = part.split(";")[0].strip().split("-")[0].lower()
        if code and (_LOCALES_DIR / f"{code}.json").exists():
            return code
    return "en"


def set_lang(lang: str) -> None:
    """Set the context language for the current request."""
    _current_lang.set(lang)


def current_lang() -> str:
    """Return the current context language."""
    return _current_lang.get()


def is_rtl(lang: str | None = None) -> bool:
    """Check if the given (or current) language is RTL."""
    return (lang or _current_lang.get()) in RTL_LANGS
