# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Resolver identifier expansion: rfid_epc and gtin lookup with a fixed precedence.

The resolver must recognise physical identifiers (barcode, rfid_epc) and the
trade identifier (gtin) alongside sku, resolving a scanned code by the order
barcode, rfid_epc, gtin, sku, none, folding rfid_epc case-insensitively, and
failing closed on a duplicated physical identifier.
"""
from __future__ import annotations

import uuid

import pytest

from sqlalchemy import text

from celerp.events.engine import emit_event
from celerp.models.company import Company
from celerp.projections.engine import ProjectionEngine
from celerp_inventory.routes import (
    resolve_item_by_code,
    resolve_items_by_codes,
)

# The EPC partial unique index name (mirrors celerp.inventory_codes.RFID_EPC_UNIQUE_INDEX).
# Named by literal rather than import so this proof collects at the pre-change tree, where
# the constant does not yet exist, and each case reds on the absent resolver behaviour.
_RFID_EPC_UNIQUE_INDEX = "uq_projection_company_item_rfid_epc"


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
async def test_resolve_precedence_barcode_epc_gtin_sku(session):
    """Physical (barcode/rfid_epc) beats product (gtin/sku), and a code held as one
    item's barcode AND another item's rfid_epc fails closed: barcode and rfid_epc are
    ONE physical namespace, so a value spanning two distinct items resolves to neither.

    RED at head b764c0da: the resolver iterates the barcode field first and returns the
    first field with any match, so "55501" (item:x barcode, item:y rfid_epc) resolves to
    kind "barcode"/item:x instead of failing closed. The legitimate precedence with
    non-colliding values still holds: physical(epc) beats product(gtin) and gtin beats
    sku. GTIN values are 8 digits (a valid GTIN length)."""
    cid = await _seed(session, "PrecedenceCo")
    await _emit(session, cid, "item:x", {"sku": "X", "name": "X", "quantity": 1, "barcode": "55501"})
    await _emit(session, cid, "item:y", {"sku": "Y", "name": "Y", "quantity": 1, "barcode": "55502", "rfid_epc": "55501"})
    await _emit(session, cid, "item:p", {"sku": "P", "name": "P", "quantity": 1, "barcode": "66600", "rfid_epc": "66601066"})
    await _emit(session, cid, "item:q", {"sku": "Q", "name": "Q", "quantity": 1, "barcode": "66609", "gtin": "66601066"})
    await _emit(session, cid, "item:r", {"sku": "R", "name": "R", "quantity": 1, "barcode": "77700", "gtin": "77701077"})
    await _emit(session, cid, "item:s", {"sku": "77701077", "name": "S", "quantity": 1, "barcode": "77709"})
    await ProjectionEngine.rebuild(session)

    # A code that is item:x's barcode AND item:y's rfid_epc spans two physical items:
    # fail closed, never a silent barcode-first pick.
    res = await resolve_item_by_code(session, cid, "55501")
    assert res.duplicate_physical is True
    assert res.one is None

    # barcode-only resolution: 55502 is item:y's barcode and no one's epc.
    res = await resolve_item_by_code(session, cid, "55502")
    assert res.kind == "barcode"
    assert res.one is not None and res.one.entity_id == "item:y"

    # physical (epc) beats product (gtin)
    res = await resolve_item_by_code(session, cid, "66601066")
    assert res.kind == "rfid_epc"
    assert res.one is not None and res.one.entity_id == "item:p"

    # gtin beats sku
    res = await resolve_item_by_code(session, cid, "77701077")
    assert res.kind == "gtin"
    assert res.one is not None and res.one.entity_id == "item:r"


@pytest.mark.asyncio
async def test_resolve_cross_field_barcode_epc_fails_closed(session):
    """A code held as item A's barcode and item B's rfid_epc resolves to TWO distinct
    physical items, so both the single and the batch resolver fail closed rather than
    silently return the barcode holder. Seeded via direct events (the interactive create
    guard refuses this cross-field shape), mirroring an import or legacy row.

    RED at head b764c0da: both resolvers iterate the barcode field first and return
    item:A, so duplicate_physical is False and one is item:A."""
    cid = await _seed(session, "CrossFieldCo")
    await _emit(session, cid, "item:A", {"sku": "CA", "name": "CA", "quantity": 1, "barcode": "70707070"})
    await _emit(session, cid, "item:B", {"sku": "CB", "name": "CB", "quantity": 1, "rfid_epc": "70707070"})
    await ProjectionEngine.rebuild(session)

    single = await resolve_item_by_code(session, cid, "70707070")
    assert single.duplicate_physical is True
    assert single.one is None
    assert {r.entity_id for r in single.matches} == {"item:A", "item:B"}

    batch = await resolve_items_by_codes(session, cid, ["70707070"])
    res = batch["70707070"]
    assert res.duplicate_physical is True
    assert res.one is None
    assert {r.entity_id for r in res.matches} == {"item:A", "item:B"}


@pytest.mark.asyncio
async def test_resolve_epc_uppercase_lookup(session):
    """A stored rfid_epc "ABC123" must resolve for a lowercase-typed "abc123"
    (case-insensitive EPC lookup). RED at merge base: no rfid_epc branch, so the
    code resolves to kind "none"."""
    cid = await _seed(session, "EpcCaseCo")
    await _emit(session, cid, "item:e", {"sku": "E", "name": "E", "quantity": 1, "barcode": "44401", "rfid_epc": "ABC123"})
    await ProjectionEngine.rebuild(session)

    res = await resolve_item_by_code(session, cid, "abc123")
    assert res.kind == "rfid_epc"
    assert res.one is not None and res.one.entity_id == "item:e"


@pytest.mark.asyncio
async def test_resolve_gtin_multi_lot_ambiguous(session):
    """A gtin shared by two lots reports both matches, never a silent first-pick.
    RED at merge base: no gtin branch, so "88801088" resolves to kind "none" with
    no matches. GTIN is 8 digits (a valid GTIN length)."""
    cid = await _seed(session, "GtinMultiCo")
    await _emit(session, cid, "item:g1", {"sku": "G", "name": "G1", "quantity": 1, "barcode": "88811", "gtin": "88801088"})
    await _emit(session, cid, "item:g2", {"sku": "G", "name": "G2", "quantity": 1, "barcode": "88812", "gtin": "88801088"})
    await ProjectionEngine.rebuild(session)

    res = await resolve_item_by_code(session, cid, "88801088")
    assert res.kind == "gtin"
    assert len(res.matches) == 2
    assert {r.entity_id for r in res.matches} == {"item:g1", "item:g2"}


@pytest.mark.asyncio
async def test_resolve_batch_matches_single_for_new_kinds(session):
    """The batch resolver returns the same kind and match-set as the single
    resolver for an rfid_epc code and a gtin code. RED at merge base: both paths
    return kind "none" for these codes, so asserting they equal "rfid_epc"/"gtin"
    fails. GTIN is 8 digits (a valid GTIN length)."""
    cid = await _seed(session, "BatchNewKindsCo")
    await _emit(session, cid, "item:be", {"sku": "BE", "name": "BE", "quantity": 1, "barcode": "99911", "rfid_epc": "99901"})
    await _emit(session, cid, "item:bg", {"sku": "BG", "name": "BG", "quantity": 1, "barcode": "99912", "gtin": "99902099"})
    await ProjectionEngine.rebuild(session)

    out = await resolve_items_by_codes(session, cid, ["99901", "99902099"])
    single_epc = await resolve_item_by_code(session, cid, "99901")
    single_gtin = await resolve_item_by_code(session, cid, "99902099")

    assert out["99901"].kind == single_epc.kind == "rfid_epc"
    assert {r.entity_id for r in out["99901"].matches} == {r.entity_id for r in single_epc.matches}
    assert out["99902099"].kind == single_gtin.kind == "gtin"
    assert {r.entity_id for r in out["99902099"].matches} == {r.entity_id for r in single_gtin.matches}


@pytest.mark.asyncio
async def test_resolve_epc_lowercase_batch_lookup(session):
    """Batch form of the case-insensitive EPC fold: a stored "DEAD01" resolves for
    the batch code "dead01". RED at merge base: the batch path builds no rfid_epc
    map and does no case folding, so the code resolves to kind "none"."""
    cid = await _seed(session, "EpcBatchCaseCo")
    await _emit(session, cid, "item:d", {"sku": "D", "name": "D", "quantity": 1, "barcode": "33301", "rfid_epc": "DEAD01"})
    await ProjectionEngine.rebuild(session)

    out = await resolve_items_by_codes(session, cid, ["dead01"])
    res = out["dead01"]
    assert res.kind == "rfid_epc"
    assert res.one is not None and res.one.entity_id == "item:d"


@pytest.mark.asyncio
async def test_resolve_legacy_barcode_like_gtin_still_barcode(session):
    """An existing barcode that looks like an 8-digit GTIN keeps resolving as a
    barcode (barcode wins over the new gtin branch), while a code that is only a
    gtin resolves as gtin. RED at merge base: the gtin-only code "34567890" has no
    gtin branch to match, so it resolves to kind "none" and part (b) fails."""
    cid = await _seed(session, "LegacyGtinLikeCo")
    await _emit(session, cid, "item:l", {"sku": "L", "name": "L", "quantity": 1, "barcode": "12345670"})
    await _emit(session, cid, "item:m", {"sku": "M", "name": "M", "quantity": 1, "barcode": "12345679", "gtin": "34567890"})
    await _emit(session, cid, "item:mg", {"sku": "MG", "name": "MG", "quantity": 1, "barcode": "12345678", "gtin": "12345670"})
    await ProjectionEngine.rebuild(session)

    # (a) barcode wins over a gtin that equals it
    res = await resolve_item_by_code(session, cid, "12345670")
    assert res.kind == "barcode"
    assert res.one is not None and res.one.entity_id == "item:l"

    # (b) a code that is only a gtin resolves as gtin (proves the branch exists)
    res = await resolve_item_by_code(session, cid, "34567890")
    assert res.kind == "gtin"
    assert res.one is not None and res.one.entity_id == "item:m"


@pytest.mark.asyncio
async def test_resolve_epc_duplicate_fails_closed(session):
    """Two different items sharing one rfid_epc fail closed: kind "rfid_epc", no
    silent pick, and the physical-duplicate flag set. The current schema's EPC
    partial unique index forbids seeding that pair directly, so it is dropped for
    the duration of this test's rolled-back transaction - the legacy/import shape
    the resolver guard targets (unreachable through create, which refuses a
    duplicate EPC). RED at merge base: no rfid_epc branch (kind "none") and no
    duplicate_physical property, so the assertions and attribute access fail
    inside the test body."""
    await session.execute(text(f"DROP INDEX IF EXISTS {_RFID_EPC_UNIQUE_INDEX}"))
    cid = await _seed(session, "EpcDupCo")
    await _emit(session, cid, "item:d1", {"sku": "D1", "name": "D1", "quantity": 1, "barcode": "22201", "rfid_epc": "DUPEPC"})
    await _emit(session, cid, "item:d2", {"sku": "D2", "name": "D2", "quantity": 1, "barcode": "22202", "rfid_epc": "DUPEPC"})
    await ProjectionEngine.rebuild(session)

    res = await resolve_item_by_code(session, cid, "DUPEPC")
    assert res.kind == "rfid_epc"
    assert res.one is None
    assert res.duplicate_physical is True
