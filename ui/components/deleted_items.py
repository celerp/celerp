# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The "[Deleted]" mark a record shows beside a deleted item's SKU.

A deleted item stays on every record that used it; each record names it as
"<SKU> [Deleted]", with the label taken from the item itself. Item lists leave
deleted items out, so a surface reads the items it names on their own.
"""
from __future__ import annotations

import ui.api_client as api
from ui.api_client import APIError
from ui.i18n import t


def is_deleted(item: dict | None) -> bool:
    return (item or {}).get("status") == "deleted"


def deleted_label(sku: str) -> str:
    """*sku* followed by the "[Deleted]" mark."""
    return f"{sku} {t('item.deleted_mark')}"


async def deleted_items(token: str, ids) -> dict[str, dict]:
    """The deleted items among *ids*, keyed by id. When they cannot be read the records
    show their items unmarked, as stored."""
    wanted = [i for i in dict.fromkeys(ids) if i]
    if not wanted:
        return {}
    try:
        found = await api.get_items_metadata(token, wanted)
    except APIError:
        return {}
    return {i: it for i, it in found.items() if is_deleted(it)}
