# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

import json
import logging
import os
from contextvars import ContextVar
from pathlib import Path
from functools import lru_cache

from celerp.services.pricing import PRICE_LISTS_FALLBACK, price_key

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
    category_label_everywhere.cache_clear()
    _field_label_keys.cache_clear()


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


# The system price lists every company starts with (they cannot be deleted) are UI
# labels and translate; any other price list name is user data. One source: the
# names come from PRICE_LISTS_FALLBACK, each keyed chip.<name>.
_SYSTEM_PRICE_LIST_KEYS: dict[str, str] = {pl["name"]: f"chip.{pl['name'].lower()}" for pl in PRICE_LISTS_FALLBACK}
# Item-schema price columns of the system lists, keyed by field key: the label the
# column carries when nobody renamed it, and the price list name it shows.
_SYSTEM_PRICE_COLUMNS: dict[str, tuple[str, str]] = {
    **{price_key(n): (n, n) for n in _SYSTEM_PRICE_LIST_KEYS},
    **{f"{price_key(n)}_total": (f"{n} (Total)", n) for n in _SYSTEM_PRICE_LIST_KEYS},
}


def price_list_label(name: str, lang: str | None = None) -> str:
    """Display name for a price list: a system list (Retail, Wholesale, Cost) in the
    user's language, any other list exactly as the user named it."""
    key = _SYSTEM_PRICE_LIST_KEYS.get(name)
    return t(key, lang) if key else name


def category_label(key: str, stored: str | None = None, lang: str | None = None) -> str:
    """Display name for an item category: a library category in the user's language
    while it keeps its library name, a renamed or company-made one exactly as named.
    The English catalog mirrors the library's display names (tests hold them in
    lockstep). Falls back to the key so a category never renders blank."""
    tkey = f"category.{key}"
    default = _cached_load("en").get(tkey)
    if default is not None and (not stored or stored == default):
        return t(tkey, lang)
    return stored or key


@lru_cache(maxsize=4096)
def category_label_everywhere(key: str, stored: str | None = None) -> frozenset[str]:
    """``category_label`` in every UI language: each name the category is shown as,
    so a file written in any language can name it (a renamed category has one)."""
    return frozenset(category_label(key, stored, lang) for lang in available_langs())


def category_labels(names: dict) -> dict:
    """``category_label`` for every library category and every category in a company's
    stored {key: name} map, so a library category with no stored name still reads in
    the user's language rather than as its key."""
    names = names or {}
    library = (k.removeprefix("category.") for k in _cached_load("en") if k.startswith("category."))
    return {k: category_label(k, names.get(k)) for k in {*library, *names}}


def unit_label(name: str) -> str:
    """Display name for a unit of measure: a system unit (piece, gram, ...) in the user's
    language, a unit the company added exactly as named. Display only: the stored value
    stays the unit name."""
    tkey = f"unit.{name}"
    return t(tkey) if tkey in _cached_load("en") else name


@lru_cache(maxsize=1)
def _field_label_keys() -> dict[str, str]:
    """English text -> translation key of every item field label in the catalog: the
    built-in fields (``field.label.*``) and the category library's fields (``attr.*``),
    a built-in winning where both read the same."""
    en = _cached_load("en")
    keys = {v: k for k, v in en.items() if k.startswith("attr.")}
    keys.update({v: k for k, v in en.items() if k.startswith("field.label.")})
    return keys


def field_label_key(label: str) -> str | None:
    """The translation key of an item field label still reading as a library label, so
    a library category's field shows in the user's language; ``None`` for a label the
    user typed, which shows as typed."""
    return _field_label_keys().get(label)


def field_label(f: dict) -> str:
    """Display label for an item-schema field, resolved through t() at render time.

    Built-in fields carry a ``label_key`` (mirroring ``tooltip_key``); a request
    in another language renders the translated label. Stored custom fields and
    dynamic price columns have no ``label_key`` and render their raw ``label`` -
    a user-defined label is data, never translated - except the columns of the
    system price lists, which translate while they keep their default label. Falls back to the field key
    so a malformed field never renders blank."""
    key = f.get("label_key")
    if key:
        return t(key)
    label = f.get("label", f.get("key", ""))
    system = _SYSTEM_PRICE_COLUMNS.get(f.get("key", ""))
    if system and label == system[0]:
        name = price_list_label(system[1])
        return t("inventory.import_col_total", name=name) if label != system[1] else name
    return label


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
