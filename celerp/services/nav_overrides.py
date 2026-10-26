# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""The company's choices about its side menu: which entries and groups are hidden, and in what order.

Stored in ``Company.settings["nav_overrides"]`` and read by the UI when it builds the menu. Entries
are named by their stable key, or by their href when they declare no key, and groups by their
declared name in lower case with underscores (``Sales Documents`` -> ``sales_documents``), never by
the translated label, so a saved choice survives a language switch and a label rename. An override
only hides and reorders what the viewer's permissions already show; it never adds an entry.
"""
from __future__ import annotations

from fastapi import HTTPException

from celerp.accounting_roles import refusal

KEY = "nav_overrides"
FIELDS = ("hidden_entries", "hidden_groups", "entry_order", "group_order")
MAX_IDS = 200
MAX_LENGTH = 200


def is_settings_entry(entry_id: str) -> bool:
    """Whether an entry id names Settings (its key or a page under /settings). Settings is how an
    owner undoes a hidden menu, so it is never hidden."""
    return entry_id.split("?", 1)[0].strip("/").split("/", 1)[0] == "settings"


def normalize(raw: dict | None) -> dict[str, list[str]]:
    """The stored form of ``raw``: each field a de-duplicated list of ids, empty fields left out.

    Refuses a field that is too long, an id that is blank or too long, and an attempt to hide
    Settings."""
    out: dict[str, list[str]] = {}
    for field in FIELDS:
        ids: list[str] = []
        for value in (raw or {}).get(field) or []:
            entry_id = str(value).strip()
            if not entry_id or len(entry_id) > MAX_LENGTH or any(ord(ch) < 32 for ch in entry_id):
                raise HTTPException(status_code=422, detail=refusal(
                    "company.nav_override_bad_id", "A menu entry name is blank, too long or has control characters."))
            if entry_id not in ids:
                ids.append(entry_id)
        if len(ids) > MAX_IDS:
            raise HTTPException(status_code=422, detail=refusal(
                "company.nav_override_too_many", f"Too many menu entries in one list (at most {MAX_IDS}).", max=MAX_IDS))
        if field == "hidden_entries" and any(is_settings_entry(i) for i in ids):
            raise HTTPException(status_code=422, detail=refusal(
                "company.nav_override_settings", "Settings cannot be hidden from the menu."))
        if ids:
            out[field] = ids
    return out
