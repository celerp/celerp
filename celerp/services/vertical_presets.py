# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Business type (vertical) presets and category library.

The data ships inside the first-party ``celerp-verticals`` package. It is located
through the module loader's own resolution so dev, wheel and desktop layouts all
read the same copy the loader would run, whether or not the module is enabled.

Everything here is pure: callers pass a settings dict in and get a new one back.
"""
from __future__ import annotations

import json
import logging
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

from celerp.modules.loader import module_search_path, resolve_runtime_module_path
from celerp.services.units import DEFAULT_UNITS

log = logging.getLogger(__name__)

_PACKAGE = "celerp-verticals"
_UNIT_FIELDS = ("default_sell_by", "default_purchase_unit", "default_weight_unit")


@lru_cache(maxsize=None)
def _resolved_data_dir(search_path: str) -> Path | None:
    pkg = resolve_runtime_module_path(_PACKAGE, search_path)
    return pkg / "celerp_verticals" if pkg is not None else None


def _data_dir() -> Path | None:
    return _resolved_data_dir(module_search_path())


@lru_cache(maxsize=None)
def _parse_folder(folder: Path, kind: str) -> tuple[dict, ...]:
    """The library ships with the installed package and does not change while the
    process runs, so each folder is parsed once."""
    if not folder.is_dir():
        return ()
    out: list[dict] = []
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            log.warning("skipping unreadable %s file %s: %s", kind, path.name, exc)
            continue
        if not isinstance(data, dict) or not data.get("name"):
            log.warning("skipping %s file %s: no name", kind, path.name)
            continue
        out.append(data)
    return tuple(out)


def _read_all(kind: str) -> list[dict]:
    """A private copy per call, so no caller can change the cached library."""
    root = _data_dir()
    return deepcopy(list(_parse_folder(root / kind, kind))) if root is not None else []


def list_presets(include_hidden: bool = False) -> list[dict]:
    """Presets sorted by name. Hidden presets stage a vertical the product cannot yet
    honestly support, so they are left out unless explicitly requested."""
    return [p for p in _read_all("presets") if include_hidden or not p.get("hidden")]


def load_preset(name: str, allow_hidden: bool = False) -> dict | None:
    """The named preset, or None when it does not exist (or is hidden and not allowed)."""
    return next((p for p in list_presets(include_hidden=allow_hidden) if p["name"] == name), None)


def list_categories() -> list[dict]:
    return _read_all("categories")


def load_category(name: str) -> dict | None:
    return next((c for c in list_categories() if c["name"] == name), None)


# Item field filled by each library category default.
_ITEM_DEFAULT_FIELDS = {
    "sell_by": "default_sell_by",
    "purchase_unit": "default_purchase_unit",
    "weight_unit": "default_weight_unit",
    "inventory_type": "default_inventory_type",
}


def category_item_defaults(category: str | None) -> dict[str, str]:
    """The item field values a library category supplies when the item leaves them out.

    Only fields the category declares are returned; an empty, unknown, or company-only
    category supplies nothing. Item creation and every import apply this one mapping."""
    cat = load_category(category) if category else None
    if cat is None:
        return {}
    return {field: cat[key] for field, key in _ITEM_DEFAULT_FIELDS.items() if cat.get(key)}


def installed_preset_modules(preset: dict) -> list[str]:
    """The preset's modules that are installed. A name with no installed copy is
    logged and skipped so it never lands in company settings or the config file."""
    out: list[str] = []
    search_path = module_search_path()
    for name in preset.get("modules") or []:
        if resolve_runtime_module_path(name, search_path) is None:
            log.warning("preset %s names module %r, which is not installed", preset.get("name"), name)
            continue
        out.append(name)
    return out


def ensure_unit_seeded(settings: dict, unit_name: str) -> None:
    """Add unit_name to the company units from DEFAULT_UNITS if it is missing.

    Mutates settings in place. Unknown unit names are ignored. Seeds the canonical
    unit entry so the unit_type (weight/pieces classification) always comes along."""
    seed_by_name = {u["name"]: u for u in DEFAULT_UNITS}
    if unit_name not in seed_by_name:
        return
    current_units: list[dict] = list(settings.get("units") or DEFAULT_UNITS)
    if not any(u["name"] == unit_name for u in current_units):
        current_units.append(seed_by_name[unit_name])
        settings["units"] = current_units


def seed_category_units(settings: dict, category: dict) -> None:
    for field in _UNIT_FIELDS:
        if category.get(field):
            ensure_unit_seeded(settings, category[field])


def merge_missing_preset_categories(settings: dict, preset: dict) -> tuple[dict, list[str]]:
    """Add the preset's category schemas the company does not have yet.

    An existing schema (seeded earlier or customised by the user) is never
    overwritten. Returns the new settings and the preset's category names that
    resolved in the library (whether added now or already present)."""
    out = dict(settings)
    schemas = dict(out.get("category_schemas") or {})
    names = dict(out.get("category_display_names") or {})
    resolved: list[str] = []
    for cat_name in preset.get("categories") or []:
        cat = load_category(cat_name)
        if cat is None:
            log.warning("preset %s names unknown category %r", preset.get("name"), cat_name)
            continue
        schemas.setdefault(cat["name"], deepcopy(cat.get("fields") or []))
        names.setdefault(cat["name"], cat.get("display_name", cat["name"]))
        seed_category_units(out, cat)
        resolved.append(cat["name"])
    out["category_schemas"] = schemas
    out["category_display_names"] = names
    return out, resolved


def reconcile_preset_settings(settings: dict, previous_preset: dict | None, target_preset: dict) -> dict:
    """Move preset-owned company settings from the previous preset to the target.

    A value is still system-owned when it is absent or exactly equals what the
    previous preset set. Those take the target's value, or are removed when the
    target does not set that key. Anything the user changed is kept."""
    previous = (previous_preset or {}).get("company_settings") or {}
    target = target_preset.get("company_settings") or {}
    out = dict(settings)
    for key in {*previous, *target}:
        system_owned = key not in out or (key in previous and out[key] == previous[key])
        if not system_owned:
            continue
        if key in target:
            out[key] = deepcopy(target[key])
        else:
            out.pop(key, None)
    return out
