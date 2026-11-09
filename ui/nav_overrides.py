# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Apply a company's side-menu choices to the entries a viewer is already allowed to see.

The choices (``celerp.services.nav_overrides``) name entries and groups by stable ids, never by
their translated labels. Applying them can only remove entries from the list it is given or
reorder them: it is run after the permission filter, so it can never show an entry the viewer lacks
permission for. Settings is never hidden, and anything the choices do not mention (a module
installed later) keeps its declared place.
"""
from __future__ import annotations

from celerp.services.nav_overrides import KEY, is_settings_entry


def entry_id(item: dict) -> str:
    """The stable id of a menu entry: its key, or its href when it declares none."""
    return str(item.get("key") or item.get("href") or "")


def group_id(name: str) -> str:
    """The stable id of a menu group, from its declared name (the sidebar's own ``data-group``)."""
    return name.lower().replace(" ", "_")


def protected(item: dict) -> bool:
    """Settings entries, which stay in the menu whatever the choices say."""
    return is_settings_entry(entry_id(item)) or is_settings_entry(str(item.get("href") or ""))


def reorder(sequence: list, ids: list[str], order: list[str]) -> list:
    """``sequence`` with the members named in ``order`` placed in that order, in the places those
    members held; everything else stays exactly where it was."""
    rank = {value: n for n, value in enumerate(order)}
    slots = [n for n, value in enumerate(ids) if value in rank]
    ordered = sorted((n for n in slots), key=lambda n: rank[ids[n]])
    out = list(sequence)
    for slot, source in zip(slots, ordered):
        out[slot] = sequence[source]
    return out


def apply_nav_overrides(items: list[dict], settings: dict | None) -> list[dict]:
    """``items`` (already permission-filtered, in default order) with the company's choices applied."""
    chosen = (settings or {}).get(KEY) or {}
    if not chosen:
        return items
    hidden_entries = set(chosen.get("hidden_entries") or [])
    hidden_groups = set(chosen.get("hidden_groups") or [])

    top = [i for i in items if not i.get("group")]
    groups: dict[str, list[dict]] = {}
    for item in items:
        if item.get("group"):
            groups.setdefault(group_id(item["group"]), []).append(item)

    entry_order = chosen.get("entry_order") or []
    top = reorder(top, [entry_id(i) for i in top], entry_order)
    for gid in groups:
        groups[gid] = reorder(groups[gid], [entry_id(i) for i in groups[gid]], entry_order)

    gids = list(groups)
    gids = reorder(gids, gids, chosen.get("group_order") or [])

    def shown(item: dict, in_hidden_group: bool = False) -> bool:
        return protected(item) or (entry_id(item) not in hidden_entries and not in_hidden_group)

    # Settings stays shown even in a hidden group: hiding it would hide the way back.
    visible_top = [i for i in top if shown(i)]
    visible_groups = [
        item for gid in gids for item in groups[gid] if shown(item, gid in hidden_groups)
    ]
    return visible_top + visible_groups
