# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Projection handler for the per-item outbound sync flag (ERP -> Shopify).

shop.sync.enabled / shop.sync.disabled events toggle is_sync_to_shopify on an item's
projection, opting that item into (or out of) outbound product push. Default is off:
items are never pushed back to the store unless explicitly enabled, so an inbound import
can't accidentally mass-push the catalog to the store.
"""
from __future__ import annotations

from copy import deepcopy


def apply_shop_sync_event(state: dict, event_type: str, data: dict) -> dict:
    current = deepcopy(state)
    if event_type == "shop.sync.enabled":
        current["is_sync_to_shopify"] = True
    elif event_type == "shop.sync.disabled":
        current["is_sync_to_shopify"] = False
    else:
        raise ValueError(f"Unsupported shop-sync event: {event_type}")
    return current
