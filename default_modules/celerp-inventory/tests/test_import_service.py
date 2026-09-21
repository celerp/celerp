# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Service-layer tests for the CIF item importer.

These cover the transform and commit logic that used to live in the browser
confirm handler and now lives in celerp_inventory.services: location resolution
and creation, category default sell-by, unit-rate derivation, upsert accounting,
dry-run safety, and the single committer shared by /import/rows and /import/batch.
"""

from __future__ import annotations

import sys
import types
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from celerp.models.company import Company, Location, User
from celerp.models.projections import Projection

import celerp_inventory.services as svc
from celerp_inventory.services import (
    BatchImportRequest,
    ImportRecord,
    build_import_records,
    commit_import_batch,
    import_items,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _seed(session, *, locations, currency=None) -> tuple[uuid.UUID, uuid.UUID, list[Location]]:
    """Create a company, an actor user, and its locations.

    ``locations`` is a list of dicts: {"name": str, "is_default": bool (optional)}.
    """
    company_id = uuid.uuid4()
    user_id = uuid.uuid4()
    settings: dict = {}
    if currency:
        settings["currency"] = currency
    session.add(Company(id=company_id, name="ImportCo", slug=f"importco-{company_id.hex[:8]}", settings=settings))
    session.add(User(
        id=user_id, email=f"actor-{user_id.hex[:8]}@example.com", name="Actor",
        auth_hash="x", is_active=True,
    ))
    await session.flush()  # parents before location FK rows for Postgres
    loc_rows: list[Location] = []
    for spec in locations:
        loc = Location(
            id=uuid.uuid4(), company_id=company_id, name=spec["name"],
            type="warehouse", is_default=spec.get("is_default", False),
        )
        session.add(loc)
        loc_rows.append(loc)
    await session.commit()
    return company_id, user_id, loc_rows


async def _item_projections(session, company_id) -> list[Projection]:
    return list((await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "item",
        )
    )).scalars().all())


# ---------------------------------------------------------------------------
# Location resolution (build_import_records)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_location_default(session):
    """A company with exactly one location resolves rows that carry no location_name."""
    company_id, _, locs = await _seed(session, locations=[{"name": "Main"}])
    build = await build_import_records(
        session, company_id,
        [{"name": "Widget", "sell_by": "piece", "pieces": "5"}],
        upsert=False, dry_run=True,
    )
    assert build.errors == []
    assert len(build.records) == 1
    assert build.records[0]["data"]["location_id"] == str(locs[0].id)


@pytest.mark.asyncio
async def test_multi_location_without_name_errors(session):
    """Two locations and no default: a row without location_name is a per-row error, not an abort."""
    company_id, _, _ = await _seed(session, locations=[{"name": "North"}, {"name": "South"}])
    build = await build_import_records(
        session, company_id,
        [{"name": "Widget", "sell_by": "piece", "pieces": "1"}],
        upsert=False, dry_run=True,
    )
    assert build.records == []
    assert len(build.errors) == 1
    assert build.errors[0]["field"] == "location_name"


@pytest.mark.asyncio
async def test_unknown_location_created(session):
    """A row naming a location that does not exist creates it (dry_run=False) and resolves there."""
    company_id, _, _ = await _seed(session, locations=[{"name": "Main"}])
    build = await build_import_records(
        session, company_id,
        [{"name": "Widget", "sell_by": "piece", "pieces": "1", "location_name": "Annex"}],
        upsert=False, dry_run=False, create_missing_locations=True,
    )
    assert build.errors == []
    annex = (await session.execute(
        select(Location).where(Location.company_id == company_id, Location.name == "Annex")
    )).scalars().first()
    assert annex is not None, "the unknown location must be created"
    assert build.records[0]["data"]["location_id"] == str(annex.id)


# ---------------------------------------------------------------------------
# Category default sell-by (build_import_records, vertical library)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_category_default_sell_by(session, monkeypatch):
    """A row without sell_by inherits the category's default_sell_by from the vertical library."""
    verticals = types.ModuleType("celerp_verticals")
    routes = types.ModuleType("celerp_verticals.routes")
    routes._all_categories = lambda: {"gems": {"name": "Gems", "default_sell_by": "carat"}}
    verticals.routes = routes  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "celerp_verticals", verticals)
    monkeypatch.setitem(sys.modules, "celerp_verticals.routes", routes)

    company_id, _, _ = await _seed(session, locations=[{"name": "Main"}])
    build = await build_import_records(
        session, company_id,
        [{"name": "Ruby", "category": "Gems", "weight": "1.5"}],
        upsert=False, dry_run=True,
    )
    assert build.errors == []
    assert build.records[0]["data"]["sell_by"] == "carat"


# ---------------------------------------------------------------------------
# Unit-rate derivation (build_import_records, price-total back-calculation)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unit_rate_derivation(session):
    """A *_price_total column back-calculates the unit price = total / quantity."""
    company_id, _, _ = await _seed(session, locations=[{"name": "Main"}], currency="USD")
    build = await build_import_records(
        session, company_id,
        [{"name": "Widget", "sell_by": "piece", "pieces": "4", "retail_price_total": "100"}],
        upsert=False, dry_run=True,
    )
    assert build.errors == []
    assert build.records[0]["data"]["retail_price"] == 25.0


# ---------------------------------------------------------------------------
# Dry run creates nothing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dry_run_creates_nothing(session):
    """dry_run=True lists locations it would create but writes none, and leaves the row unresolved."""
    company_id, _, _ = await _seed(session, locations=[{"name": "Main"}])
    build = await build_import_records(
        session, company_id,
        [{"name": "Widget", "sell_by": "piece", "pieces": "1", "location_name": "Ghost"}],
        upsert=False, dry_run=True,
    )
    assert build.locations_to_create == ["Ghost"]
    assert build.records == []
    assert len(build.errors) == 1
    remaining = (await session.execute(
        select(Location).where(Location.company_id == company_id)
    )).scalars().all()
    assert {loc.name for loc in remaining} == {"Main"}, "dry run must not create the referenced location"


# ---------------------------------------------------------------------------
# Upsert accounting (import_items -> commit_import_batch)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert(client, session):
    """Re-importing the same content: first creates, upsert updates once, then skips."""
    company_id, user_id, _ = await _seed(session, locations=[{"name": "Main"}])
    rows = [{"name": "Upsert Item", "sku": f"UP-{uuid.uuid4().hex[:6]}", "sell_by": "piece", "pieces": "3"}]

    r1 = await import_items(session, company_id, user_id, "admin", {}, rows, upsert=False, filename=None, idempotency_key=None)
    assert (r1.created, r1.updated, r1.skipped) == (1, 0, 0)

    r2 = await import_items(session, company_id, user_id, "admin", {}, rows, upsert=True, filename=None, idempotency_key=None)
    assert (r2.created, r2.updated, r2.skipped) == (0, 1, 0)

    items = await _item_projections(session, company_id)
    assert len(items) == 1
    entity_id = items[0].entity_id

    r3 = await import_items(session, company_id, user_id, "admin", {}, rows, upsert=True, filename=None, idempotency_key=None)
    assert (r3.created, r3.updated, r3.skipped) == (0, 0, 1)

    changed = [{**rows[0], "name": "Updated name", "pieces": "4"}]
    r4 = await import_items(session, company_id, user_id, "admin", {}, changed, upsert=True, filename=None, idempotency_key=None)
    assert (r4.created, r4.updated, r4.skipped) == (0, 1, 0)
    items = await _item_projections(session, company_id)
    assert len(items) == 1
    assert items[0].entity_id == entity_id
    assert items[0].state["name"] == "Updated name"
    assert float(items[0].state["quantity"]) == 4.0


@pytest.mark.asyncio
async def test_import_keeps_distinct_lots_with_same_sku(session):
    company_id, user_id, _ = await _seed(session, locations=[{"name": "Main"}])
    rows = [
        {"name": "Lot A", "sku": "LOT-SKU", "sell_by": "piece", "pieces": "1"},
        {"name": "Lot B", "sku": "LOT-SKU", "sell_by": "piece", "pieces": "2"},
    ]
    result = await import_items(
        session, company_id, user_id, "admin", {}, rows, upsert=False,
        filename=None, idempotency_key="same-sku-batch",
    )
    assert result.created == 2
    items = await _item_projections(session, company_id)
    assert len(items) == 2
    assert {item.state["name"] for item in items} == {"Lot A", "Lot B"}


@pytest.mark.asyncio
async def test_import_no_sku_rows_receive_distinct_internal_codes(session):
    company_id, user_id, _ = await _seed(session, locations=[{"name": "Main"}])
    rows = [
        {"name": "Unnamed A", "sell_by": "piece", "pieces": "1"},
        {"name": "Unnamed B", "sell_by": "piece", "pieces": "1"},
    ]
    result = await import_items(
        session, company_id, user_id, "admin", {}, rows, upsert=False,
        filename=None, idempotency_key="no-sku-batch",
    )
    assert result.created == 2
    items = await _item_projections(session, company_id)
    skus = [item.state.get("sku") for item in items]
    assert len(set(skus)) == 2
    assert all(skus)


@pytest.mark.asyncio
async def test_upsert_repeated_sku_requires_barcode_to_choose_lot(session):
    company_id, user_id, _ = await _seed(session, locations=[{"name": "Main"}])
    seed_rows = [
        {"name": "Lot A", "sku": "AMB", "barcode": "700001", "sell_by": "piece", "pieces": "1"},
        {"name": "Lot B", "sku": "AMB", "barcode": "700002", "sell_by": "piece", "pieces": "1"},
    ]
    seeded = await import_items(
        session, company_id, user_id, "admin", {}, seed_rows, upsert=False,
        filename=None, idempotency_key="amb-seed",
    )
    assert seeded.created == 2

    build = await build_import_records(
        session, company_id,
        [{"name": "Which lot?", "sku": "AMB", "sell_by": "piece", "pieces": "2"}],
        upsert=True, dry_run=True,
    )
    assert build.records == []
    assert "matches multiple lots" in build.errors[0]["message"]


@pytest.mark.asyncio
async def test_exact_create_replay_uses_content_identity(session):
    company_id, user_id, _ = await _seed(session, locations=[{"name": "Main"}])
    rows = [{"name": "Replay Item", "sku": "REPLAY-1", "sell_by": "piece", "pieces": "1"}]

    first = await import_items(
        session, company_id, user_id, "admin", {}, rows, upsert=False,
        filename="replay.csv", idempotency_key=None,
    )
    second = await import_items(
        session, company_id, user_id, "admin", {}, rows, upsert=False,
        filename="replay.csv", idempotency_key=None,
    )

    assert (first.created, first.skipped) == (1, 0)
    assert (second.created, second.skipped) == (0, 1)
    assert len(await _item_projections(session, company_id)) == 1


@pytest.mark.asyncio
async def test_upsert_omitted_fields_preserve_existing_state(session):
    company_id, user_id, locs = await _seed(session, locations=[{"name": "Main"}])
    original = [{
        "name": "Original", "sku": "KEEP-1", "sell_by": "piece", "pieces": "2",
        "description": "keep this description",
    }]
    created = await import_items(
        session, company_id, user_id, "admin", {}, original, upsert=False,
        filename=None, idempotency_key="keep-seed",
    )
    assert created.created == 1

    changed = [{"name": "Renamed", "sku": "KEEP-1", "sell_by": "piece"}]
    updated = await import_items(
        session, company_id, user_id, "admin", {}, changed, upsert=True,
        filename=None, idempotency_key=None,
    )
    assert updated.updated == 1

    item = (await _item_projections(session, company_id))[0]
    assert item.state["name"] == "Renamed"
    assert item.state["description"] == "keep this description"
    assert str(item.location_id) == str(locs[0].id)
    assert float(item.state["quantity"]) == 2.0


@pytest.mark.asyncio
async def test_raw_upsert_binds_to_original_created_entity(session):
    company_id, user_id, _ = await _seed(session, locations=[{"name": "Main"}])
    user = SimpleNamespace(id=user_id)
    seed_rows = [{"name": "Raw One", "sku": "RAW-UP", "sell_by": "piece", "pieces": "1"}]

    first_build = await build_import_records(
        session, company_id, seed_rows, upsert=False, dry_run=False,
    )
    original_id = first_build.records[0]["entity_id"]
    first = await commit_import_batch(
        session, company_id, user, "admin", {},
        BatchImportRequest(records=[ImportRecord(**first_build.records[0])]),
    )
    assert first.created == 1

    changed_rows = [{"name": "Raw Two", "sku": "RAW-UP", "sell_by": "piece", "pieces": "2"}]
    replay_build = await build_import_records(
        session, company_id, changed_rows, upsert=False, dry_run=False,
    )
    assert replay_build.records[0]["entity_id"] != original_id
    second = await commit_import_batch(
        session, company_id, user, "admin", {},
        BatchImportRequest(records=[ImportRecord(**replay_build.records[0])], upsert=True),
    )
    assert second.updated == 1

    items = await _item_projections(session, company_id)
    assert len(items) == 1
    assert items[0].entity_id == original_id
    assert items[0].state["name"] == "Raw Two"
    assert float(items[0].state["quantity"]) == 2.0


@pytest.mark.asyncio
async def test_authorized_preview_can_plan_missing_location_without_writing(session):
    company_id, _, _ = await _seed(session, locations=[{"name": "Main"}])
    build = await build_import_records(
        session, company_id,
        [{"name": "Widget", "sell_by": "piece", "pieces": "1", "location_name": "Annex"}],
        upsert=False, dry_run=True, create_missing_locations=True,
    )
    assert build.errors == []
    assert build.locations_to_create == ["Annex"]
    assert len(build.records) == 1
    assert build.records[0]["data"]["location_id"] is None
    names = (await session.execute(
        select(Location.name).where(Location.company_id == company_id)
    )).scalars().all()
    assert names == ["Main"]


# ---------------------------------------------------------------------------
# Shared committer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_import_rows_and_import_batch_share_committer(client, session, monkeypatch):
    """/import/rows (import_items) and /import/batch (commit_import_batch) converge on one committer."""
    # /import/rows path: import_items must delegate to commit_import_batch.
    company_a, user_a, _ = await _seed(session, locations=[{"name": "Main"}])
    rows = [{"name": "Shared", "sku": "SHARE-1", "sell_by": "piece", "pieces": "2"}]

    calls: list[int] = []
    real_committer = svc.commit_import_batch

    async def _spy(*args, **kwargs):
        calls.append(1)
        return await real_committer(*args, **kwargs)

    monkeypatch.setattr(svc, "commit_import_batch", _spy)
    ra = await import_items(session, company_a, user_a, "admin", {}, rows, upsert=False, filename=None, idempotency_key=None)
    monkeypatch.undo()
    assert calls, "import_items must route through commit_import_batch"
    assert ra.created == 1
    items_a = await _item_projections(session, company_a)
    assert [p.state.get("sku") for p in items_a] == ["SHARE-1"]

    # /import/batch path: the same committer lands an identical record hand-built as a raw batch.
    company_b, user_b, _ = await _seed(session, locations=[{"name": "Main"}])
    build = await build_import_records(session, company_b, rows, upsert=False, dry_run=False)
    body = BatchImportRequest(records=[ImportRecord(**r) for r in build.records], upsert=False)
    rb = await commit_import_batch(session, company_b, SimpleNamespace(id=user_b), "admin", {}, body)
    assert rb.created == 1
    items_b = await _item_projections(session, company_b)
    assert [p.state.get("sku") for p in items_b] == ["SHARE-1"]


# ---------------------------------------------------------------------------
# Quantity derivation and unit canonicalization (build_import_records)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_quantity_derivation(session):
    """Quantity is taken from an explicit column, else the pieces/weight column for the unit type."""
    company_id, _, _ = await _seed(session, locations=[{"name": "Main"}])
    build = await build_import_records(
        session, company_id,
        [
            {"name": "A", "sell_by": "piece", "pieces": "7"},                  # pieces unit -> pieces
            {"name": "B", "sell_by": "piece", "pieces": "5", "quantity": "99"},  # explicit qty wins
            {"name": "C", "sell_by": "carat", "weight": "2.5"},                # weight unit -> weight
            {"name": "D", "sell_by": "piece"},                                 # no source -> 0
        ],
        upsert=False, dry_run=True,
    )
    assert build.errors == []
    assert [rec["data"]["quantity"] for rec in build.records] == [7.0, 99.0, 2.5, 0.0]
    # The pieces column rides along in data when present, and is absent otherwise.
    assert build.records[0]["data"]["pieces"] == 7.0
    assert build.records[2]["data"].get("pieces") is None


@pytest.mark.asyncio
async def test_weight_unit_canonicalized(session):
    """weight_unit is canonicalized case-insensitively; an absent column leaves it None."""
    company_id, _, _ = await _seed(session, locations=[{"name": "Main"}])
    build = await build_import_records(
        session, company_id,
        [
            {"name": "A", "sell_by": "carat", "weight": "100", "weight_unit": "Gram"},
            {"name": "B", "sell_by": "carat", "weight": "100"},
        ],
        upsert=False, dry_run=True,
    )
    assert build.errors == []
    assert build.records[0]["data"]["weight_unit"] == "gram"
    assert build.records[1]["data"].get("weight_unit") is None
