# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Events no release emits anymore that a ledger can still hold.

Each replays exactly as the release that emitted it applied it, so a rebuild reproduces the
projections that release wrote. Only events listed here are replayable without a catalog
schema; any other event outside the catalog belongs to a module this build does not have.
"""
from __future__ import annotations


def _bom_created(state: dict, data: dict) -> dict:
    return {"components": [], **state, "entity_type": "bom", **data}


def _bom_updated(state: dict, data: dict) -> dict:
    return {**state, **data}


def _bom_deleted(state: dict, data: dict) -> dict:
    return {**state, "deleted": True}


# The standalone BOM, replaced by recipes on the inventory item.
RETIRED = {
    "bom.created": _bom_created,
    "bom.updated": _bom_updated,
    "bom.deleted": _bom_deleted,
}
