# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

import json
import logging
import os
from contextvars import ContextVar
from pathlib import Path
from functools import lru_cache

_log = logging.getLogger(__name__)

_LOCALES_DIR = Path(__file__).parent / "locales"
_DEBUG = os.getenv("CELERP_DEBUG_I18N") == "1"

# Language codes with a central catalog file on disk, frozen once at import
# (no per-request glob). Module-contributed languages are held in _registry.
_DISK_LANGS = frozenset(p.stem for p in _LOCALES_DIR.glob("*.json"))

# Module-contributed catalogs, keyed by language code:
#   {lang: {"catalog": {key: value}, "rtl": bool}}
# Modules push into this via register_catalog(); central files still win for an
# existing language, so a module can only ADD keys or a new language.
_registry: dict[str, dict] = {}

# Context variable holds the active language for the current request
_current_lang: ContextVar[str] = ContextVar("celerp_lang", default="en")

# RTL languages (central set; a module may additionally declare its own RTL)
RTL_LANGS = frozenset({"ar", "he", "fa", "ur"})


def _load(lang: str) -> dict:
    """Merged catalog for *lang*: module contributions overlaid by the central
    file, so the central copy wins for an existing language. A language with no
    central file (a wholly module-owned language) returns the module catalog."""
    path = _LOCALES_DIR / f"{lang}.json"
    central = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    mod = _registry.get(lang, {}).get("catalog", {})
    return {**mod, **central}


_cached_load = lru_cache(maxsize=32)(_load) if not _DEBUG else _load


def _invalidate_cache() -> None:
    """Drop the memoized catalogs. In CELERP_DEBUG_I18N mode _cached_load is the
    raw function with no cache_clear, so the call is guarded."""
    getattr(_cached_load, "cache_clear", lambda: None)()


def register_catalog(lang: str, mapping, *, rtl: bool = False) -> None:
    """Register a module's UI catalog for *lang*.

    Validates that *mapping* is a dict of string values: a non-dict mapping is
    logged and ignored; individual non-str values are dropped and logged, so a
    bad value degrades honestly instead of failing later in t().format(). On a
    module-vs-module clash for the same language and key, the FIRST registration
    wins. The load cache is invalidated so the new catalog is visible at once.
    """
    if not isinstance(lang, str) or not lang.strip():
        _log.warning(
            "register_catalog: ignoring catalog with invalid language code %r", lang,
        )
        return
    # Language tags are case-insensitive (BCP-47); normalize to lower case so a
    # module registering "pt-BR" and a browser sending "pt-BR" resolve to the
    # same key, and the code matches the lower-case central files on disk.
    lang = lang.strip().lower()
    if not isinstance(mapping, dict):
        _log.warning(
            "register_catalog: ignoring non-dict catalog for %r (%s)",
            lang, type(mapping).__name__,
        )
        return
    clean: dict[str, str] = {}
    for key, value in mapping.items():
        if isinstance(value, str):
            clean[key] = value
        else:
            _log.warning(
                "register_catalog: dropping non-str value for key %r in %r (%s)",
                key, lang, type(value).__name__,
            )
    is_new = lang not in _registry
    entry = _registry.setdefault(lang, {"catalog": {}, "rtl": rtl is True})
    # First-registered wins for both keys and direction: existing keys overlay the
    # incoming ones, and direction is fixed by the first registration (a language's
    # reading direction is intrinsic, not something a later module may OR on).
    entry["catalog"] = {**clean, **entry["catalog"]}
    if not is_new and (rtl is True) != entry["rtl"]:
        _log.warning(
            "register_catalog: language %r already registered rtl=%s; ignoring "
            "conflicting rtl=%s (first registration wins)",
            lang, entry["rtl"], rtl is True,
        )
    _invalidate_cache()


def clear_registry() -> None:
    """Clear all module-contributed catalogs and drop the load cache. Called by
    the module loader at the start of every load_all() pass so the registry is
    rebuilt from scratch (no stale/orphaned catalogs across re-scans), and by
    tests for isolation."""
    _registry.clear()
    _invalidate_cache()


def available_langs() -> list[str]:
    """Sorted language codes available in the UI: central files plus every
    module-contributed language. The single source for language discovery."""
    return sorted(_DISK_LANGS | _registry.keys())


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


def field_label(f: dict) -> str:
    """Display label for an item-schema field, resolved through t() at render time.

    Built-in fields carry a ``label_key`` (mirroring ``tooltip_key``); a request
    in another language renders the translated label. Stored custom fields and
    dynamic price columns have no ``label_key`` and render their raw ``label`` -
    a user-defined label is data, never translated. Falls back to the field key
    so a malformed field never renders blank."""
    key = f.get("label_key")
    if key:
        return t(key)
    return f.get("label", f.get("key", ""))


def tier_label(tier: str) -> str:
    """Display name for a paid-tier key. Keys are wire/Stripe identifiers and
    never change; this is the one client-side map from key to product name."""
    return {"cloud": "Connect", "ai": "Connect + AI", "team": "Team"}.get(tier, tier.title())


def get_lang(request) -> str:
    """Extract language from cookie, falling back to Accept-Language header, then 'en'."""
    if request is None:
        return "en"
    lang = request.cookies.get("celerp_lang", "").strip().lower()
    if lang and (lang in _DISK_LANGS or lang in _registry):
        return lang
    accept = request.headers.get("accept-language", "")
    for part in accept.split(","):
        # Try the full regional tag first (pt-br), then its base language (pt),
        # so a module contributing a regional catalog is reachable while plain
        # base-language negotiation still works.
        tag = part.split(";")[0].strip().lower()
        for code in (tag, tag.split("-")[0]):
            if code and (code in _DISK_LANGS or code in _registry):
                return code
    return "en"


def set_lang(lang: str) -> None:
    """Set the context language for the current request."""
    _current_lang.set(lang)


def current_lang() -> str:
    """Return the current context language."""
    return _current_lang.get()


def is_rtl(lang: str | None = None) -> bool:
    """Check if the given (or current) language is RTL, consulting the central
    RTL set and any module-declared RTL flag."""
    code = lang or _current_lang.get()
    if code in RTL_LANGS:
        return True
    if code in _DISK_LANGS:
        # A central language's direction is owned centrally: a module contributing
        # extra keys for it (central wins for text) cannot flip its direction either.
        return False
    entry = _registry.get(code)
    return bool(entry and entry.get("rtl"))
