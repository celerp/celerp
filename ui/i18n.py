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


def t_or(key: str, fallback: str, **kwargs) -> str:
    """Translate *key* when a catalog has it, else return *fallback* (text the server
    already wrote in English). A translation missing one of *kwargs* falls back too."""
    lang = _current_lang.get()
    text = _cached_load(lang).get(key) or _cached_load("en").get(key)
    if text is None:
        return fallback
    try:
        return text.format(**kwargs) if kwargs else text
    except (KeyError, IndexError, ValueError):
        return fallback


def role_label(role: str, fallback: str) -> str:
    """Display label of a posting role (``posting.role.<role>``)."""
    return t_or(f"posting.role.{role}", fallback)


# Why a run needs reconciling, as the server records it, in the user's language.
_RECONCILE_REASONS = {
    "books disagree": "manufacturing.reconcile_reason_books_disagree",
    "received before tracking": "manufacturing.reconcile_reason_received",
    "component without an inventory account": "manufacturing.reconcile_reason_no_account",
    "books from elsewhere": "manufacturing.reconcile_reason_elsewhere",
}


def reconcile_reason(reason: str) -> str:
    """Why a production run waits for reconciling, in the user's language."""
    key = _RECONCILE_REASONS.get(reason)
    return t(key) if key else reason


def refusal_text(detail) -> str:
    """An API refusal in the user's language. A structured refusal carries ``message``
    (English), ``message_key`` and ``params``; its ``message_key`` is translated with
    those params. Anything else is shown as the server wrote it, the ``detail`` of a body
    carrying a machine code included."""
    if not isinstance(detail, dict):
        return str(detail or "")
    message = str(detail.get("message") or detail.get("detail") or "")
    key = detail.get("message_key")
    if not key:
        return message
    params = {name: _refusal_param(name, value) for name, value in (detail.get("params") or {}).items()}
    return t_or(str(key), message, **params)


def _refusal_param(name: str, value):
    """A refusal param as the user reads it: a nested refusal, or a list of them, in
    the user's language; a ``role`` as its label and ``roles`` as their labels;
    ``type``/``types`` as account types; ``status`` as an item status; ``reason`` as why
    a production run waits for reconciling."""
    from ui.components.table import display_enum

    if isinstance(value, dict) and "message" in value:
        return refusal_text(value)
    if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
        return " ".join(refusal_text(v) for v in value)
    if name == "role":
        return role_label(str(value), str(value).replace("_", " "))
    if name == "roles" and isinstance(value, list):
        return ", ".join(role_label(str(r), str(r).replace("_", " ")) for r in value)
    if name == "status":
        return display_enum(value, "item_status")
    if name == "reason":
        return reconcile_reason(str(value))
    if name == "type":
        return display_enum(value, "account_type")
    if name == "types" and isinstance(value, list):
        return t("posting.type_or").join(display_enum(v, "account_type") for v in value)
    return value


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
