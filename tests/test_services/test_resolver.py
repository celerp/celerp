# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Canonical code -> item resolver (2026-06-17 sku/batch plan).

Barcode (unique) wins; an exact SKU may resolve to N physical lots and must be
flagged ambiguous rather than silently first-picked.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from celerp.events.engine import emit_event
from celerp.inventory_codes import BARCODE_UNIQUE_INDEX
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


async def _deactivate(session, cid, eid, into):
    """Mark an item as a merged source (status -> "merged"), keeping its barcode - the real
    lifecycle a merge applies to source lots (item.source_deactivated)."""
    await emit_event(
        session, company_id=cid, entity_id=eid, entity_type="item",
        event_type="item.source_deactivated", data={"merged_into": into},
        actor_id=None, location_id=None, source="test", idempotency_key=str(uuid.uuid4()), metadata_={},
    )


async def _allow_legacy_duplicate_barcodes(session):
    """Reproduce a database whose barcode uniqueness was not index-enforced when a merged source
    and a live lot came to share a barcode. The current schema's partial unique index (built by
    create_all for the test DB, and covering every status including `merged`) forbids seeding that
    pair directly, so it is dropped for the duration of this test's rolled-back transaction. This is
    the exact legacy/import shape the resolver fix guards against - unreachable on a current database
    through create or merge, both of which mint or refuse a barcode. Dropped first so the whole
    table-level lock is taken once, up front, not mid-seed."""
    await session.execute(text(f"DROP INDEX IF EXISTS {BARCODE_UNIQUE_INDEX}"))


@pytest.mark.asyncio
async def test_resolve_barcode_excludes_merged_source(session):
    """A `merged` historical source keeps its barcode but is no longer a current lot, so it must
    not make a live item's barcode look duplicated. The pair is unreachable through the create/merge
    paths (barcode uniqueness; a merge mints a fresh barcode), so it is seeded by direct event
    injection - the legacy/import shape the fix targets."""
    await _allow_legacy_duplicate_barcodes(session)
    cid = await _seed(session, "MergeExclCo")
    await _emit(session, cid, "item:live", {"sku": "LIVE", "name": "Live", "quantity": 5, "barcode": "700001"})
    await _emit(session, cid, "item:src", {"sku": "SRC", "name": "Src", "quantity": 0, "barcode": "700001"})
    await _deactivate(session, cid, "item:src", "item:live")
    await ProjectionEngine.rebuild(session)

    res = await resolve_item_by_code(session, cid, "700001")
    assert res.kind == "barcode"
    assert not res.duplicate_barcode           # the merged source no longer inflates the count
    assert res.one is not None and res.one.entity_id == "item:live"


@pytest.mark.asyncio
async def test_resolve_batch_barcode_excludes_merged_source(session):
    """The batch resolver uses the same lifecycle-aware rule as the single-code path (acceptance
    criterion 5): live+merged sharing a barcode resolves to the live lot, not a duplicate."""
    from celerp_inventory.routes import resolve_items_by_codes

    await _allow_legacy_duplicate_barcodes(session)
    cid = await _seed(session, "MergeExclBatchCo")
    await _emit(session, cid, "item:live", {"sku": "LIVE", "name": "Live", "quantity": 5, "barcode": "700002"})
    await _emit(session, cid, "item:src", {"sku": "SRC", "name": "Src", "quantity": 0, "barcode": "700002"})
    await _deactivate(session, cid, "item:src", "item:live")
    await ProjectionEngine.rebuild(session)

    out = await resolve_items_by_codes(session, cid, ["700002"])
    res = out["700002"]
    assert res.kind == "barcode"
    assert not res.duplicate_barcode
    assert res.one is not None and res.one.entity_id == "item:live"


@pytest.mark.asyncio
async def test_resolve_merged_only_not_operational(session):
    """A barcode owned only by a merged source does not resolve as a current operational item: the
    merged row is excluded from the barcode candidate set, so there is no barcode match."""
    cid = await _seed(session, "MergedOnlyCo")
    await _emit(session, cid, "item:src", {"sku": "SRC", "name": "Src", "quantity": 0, "barcode": "700003"})
    await _deactivate(session, cid, "item:src", "item:gone")
    await ProjectionEngine.rebuild(session)

    res = await resolve_item_by_code(session, cid, "700003")
    assert res.kind == "none"
    assert res.one is None


@pytest.mark.asyncio
async def test_resolve_two_live_barcodes_still_duplicate(session):
    """Genuine ambiguity between two LIVE lots sharing a barcode still errors (acceptance criteria
    3/6). This path is unchanged by the merged-only exclusion (both rows survive it), so it guards
    against over-filtering; it is green both before and after the fix."""
    await _allow_legacy_duplicate_barcodes(session)
    cid = await _seed(session, "TwoLiveCo")
    await _emit(session, cid, "item:a", {"sku": "A", "name": "A", "quantity": 1, "barcode": "700004"})
    await _emit(session, cid, "item:b", {"sku": "B", "name": "B", "quantity": 1, "barcode": "700004"})
    await ProjectionEngine.rebuild(session)

    res = await resolve_item_by_code(session, cid, "700004")
    assert res.kind == "barcode"
    assert res.duplicate_barcode is True
    assert res.one is None                     # never silently picks one live lot


@pytest.mark.asyncio
async def test_resolve_sku_excludes_merged_source_when_code_equals_barcode(session):
    """A `merged` source whose sku equals its barcode (a numeric sku is legal) must not re-enter
    resolution through the sku fallback: excluded from the barcode candidate set, it is also
    excluded from the sku candidate set, so the code resolves to nothing. The merged source is the
    sole owner of the barcode, so no legacy duplicate-barcode state is needed."""
    cid = await _seed(session, "MergedSkuEqBarcodeCo")
    await _emit(session, cid, "item:src", {"sku": "700700", "name": "Src", "quantity": 0, "barcode": "700700"})
    await _deactivate(session, cid, "item:src", "item:gone")
    await ProjectionEngine.rebuild(session)

    res = await resolve_item_by_code(session, cid, "700700")
    assert res.kind == "none"
    assert res.one is None


@pytest.mark.asyncio
async def test_resolve_sku_merged_and_live_share_sku_selects_live(session):
    """A `merged` source whose barcode equals a sku shared with a LIVE lot must not create a false
    sku ambiguity: the merged row is excluded from sku matching, so the scan resolves to the single
    live lot. Live and merged own different barcodes, so no legacy duplicate-barcode state is
    needed."""
    cid = await _seed(session, "MergedLiveShareSkuCo")
    await _emit(session, cid, "item:live", {"sku": "700701", "name": "Live", "quantity": 5, "barcode": "700801"})
    await _emit(session, cid, "item:src", {"sku": "700701", "name": "Src", "quantity": 0, "barcode": "700701"})
    await _deactivate(session, cid, "item:src", "item:live")
    await ProjectionEngine.rebuild(session)

    res = await resolve_item_by_code(session, cid, "700701")
    assert res.kind == "sku"
    assert not res.ambiguous
    assert res.one is not None and res.one.entity_id == "item:live"


@pytest.mark.asyncio
async def test_resolve_batch_sku_excludes_merged_source_when_code_equals_barcode(session):
    """Batch form of the sku==barcode merged-exclusion: the merged source resolves to nothing."""
    from celerp_inventory.routes import resolve_items_by_codes

    cid = await _seed(session, "BatchMergedSkuEqBarcodeCo")
    await _emit(session, cid, "item:src", {"sku": "700702", "name": "Src", "quantity": 0, "barcode": "700702"})
    await _deactivate(session, cid, "item:src", "item:gone")
    await ProjectionEngine.rebuild(session)

    out = await resolve_items_by_codes(session, cid, ["700702"])
    res = out["700702"]
    assert res.kind == "none"
    assert res.one is None


@pytest.mark.asyncio
async def test_resolve_batch_sku_merged_and_live_share_sku_selects_live(session):
    """Batch form of the merged+live shared-sku case: resolves to the single live lot, no false
    ambiguity."""
    from celerp_inventory.routes import resolve_items_by_codes

    cid = await _seed(session, "BatchMergedLiveShareSkuCo")
    await _emit(session, cid, "item:live", {"sku": "700703", "name": "Live", "quantity": 5, "barcode": "700803"})
    await _emit(session, cid, "item:src", {"sku": "700703", "name": "Src", "quantity": 0, "barcode": "700703"})
    await _deactivate(session, cid, "item:src", "item:live")
    await ProjectionEngine.rebuild(session)

    out = await resolve_items_by_codes(session, cid, ["700703"])
    res = out["700703"]
    assert res.kind == "sku"
    assert not res.ambiguous
    assert res.one is not None and res.one.entity_id == "item:live"


@pytest.mark.asyncio
async def test_resolve_sku_two_live_plus_merged_excludes_only_merged(session):
    """The merged exclusion removes ONLY merged rows: two LIVE lots plus a merged source all sharing
    a sku still resolve to a genuine ambiguity over exactly the two live lots (guarding against
    over-filtering the live candidates). Each lot owns a distinct barcode, so no legacy
    duplicate-barcode state is needed."""
    cid = await _seed(session, "TwoLivePlusMergedCo")
    await _emit(session, cid, "item:a", {"sku": "SHARED", "name": "A", "quantity": 1, "barcode": "700901"})
    await _emit(session, cid, "item:b", {"sku": "SHARED", "name": "B", "quantity": 1, "barcode": "700902"})
    await _emit(session, cid, "item:src", {"sku": "SHARED", "name": "Src", "quantity": 0, "barcode": "700903"})
    await _deactivate(session, cid, "item:src", "item:a")
    await ProjectionEngine.rebuild(session)

    res = await resolve_item_by_code(session, cid, "SHARED")
    assert res.kind == "sku"
    assert res.ambiguous
    assert {r.entity_id for r in res.matches} == {"item:a", "item:b"}
