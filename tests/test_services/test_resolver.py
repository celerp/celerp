# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Canonical code -> item resolver (2026-06-17 sku/batch plan).

Barcode (unique) wins; an exact SKU may resolve to N physical lots and must be
flagged ambiguous rather than silently first-picked.
"""
from __future__ import annotations

import uuid

import pytest

from celerp.events.engine import emit_event
from celerp.models.company import Company
from celerp.projections.engine import ProjectionEngine
from celerp_inventory.routes import resolve_item_by_code


async def _emit(session, cid, eid, data):
    await emit_event(
        session, company_id=cid, entity_id=eid, entity_type="item",
        event_type="item.created", data=data, actor_id=None, location_id=None,
        source="test", idempotency_key=str(uuid.uuid4()), metadata_={},
    )


async def _seed(session, name):
    cid = uuid.uuid4()
    session.add(Company(id=cid, name=name, slug=f"{name.lower()}-{cid.hex[:8]}", settings={}))
    await session.flush()
    return cid


@pytest.mark.asyncio
async def test_resolver_barcode_wins_and_sku_ambiguous(session):
    cid = await _seed(session, "ResolveCo")
    await _emit(session, cid, "item:a", {"sku": "SH", "name": "A", "quantity": 1, "barcode": "900001", "batch_no": "A1"})
    await _emit(session, cid, "item:b", {"sku": "SH", "name": "B", "quantity": 1, "barcode": "900002", "batch_no": "B1"})
    await _emit(session, cid, "item:c", {"sku": "UNIQ", "name": "C", "quantity": 1, "barcode": "900003"})
    await ProjectionEngine.rebuild(session)

    # Barcode -> exactly one lot, unambiguous.
    res = await resolve_item_by_code(session, cid, "900002")
    assert res.kind == "barcode" and not res.ambiguous
    assert res.one.entity_id == "item:b"

    # A unique sku -> one match.
    res = await resolve_item_by_code(session, cid, "UNIQ")
    assert res.kind == "sku" and not res.ambiguous
    assert res.one.entity_id == "item:c"

    # A shared sku -> ambiguous, both lots present, no silent pick.
    res = await resolve_item_by_code(session, cid, "SH")
    assert res.kind == "sku" and res.ambiguous
    assert res.one is None
    assert {r.entity_id for r in res.matches} == {"item:a", "item:b"}

    # No match.
    res = await resolve_item_by_code(session, cid, "NOPE")
    assert res.kind == "none" and res.one is None


def test_duplicate_barcode_result_is_flagged_and_never_first_picked():
    """A barcode matching more than one item - only possible for legacy data predating the
    per-company barcode unique index - is flagged, never silently resolved to one lot. The one
    operator-facing message is sourced in a single place for every scan surface."""
    from celerp_inventory.routes import ResolveResult, duplicate_barcode_detail

    dup = ResolveResult("barcode", ["m1", "m2"])
    assert dup.duplicate_barcode is True
    assert dup.one is None            # never silently picks a lot
    assert dup.ambiguous is False     # ambiguity is the SKU-only concept, distinct from this
    solo = ResolveResult("barcode", ["m1"])
    assert solo.duplicate_barcode is False and solo.one == "m1"
    assert duplicate_barcode_detail("900001") == "Duplicate barcode '900001' exists on multiple inventory items"
