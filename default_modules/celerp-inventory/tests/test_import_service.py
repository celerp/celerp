# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Service-layer tests for the CIF item importer.

These cover the transform and commit logic that used to live in the browser
confirm handler and now lives in celerp_inventory.services: location resolution
and creation, category default sell-by, unit-rate derivation, upsert accounting,
building without writing, and the single writer shared by /import/rows and /import/batch.
"""

from __future__ import annotations

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
        upsert=False,
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
        upsert=False,
    )
    assert build.records == []
    assert len(build.errors) == 1
    assert build.errors[0]["field"] == "location_name"


@pytest.mark.asyncio
async def test_unknown_location_created(session):
    """A clean import naming a location that does not exist creates it and puts the item there."""
    company_id, user_id, _ = await _seed(session, locations=[{"name": "Main"}])
    result = await import_items(
        session, company_id, user_id, "admin", {},
        [{"name": "Widget", "sell_by": "piece", "pieces": "1", "location_name": "Annex"}],
        upsert=False, filename=None, idempotency_key=None,
    )
    assert (result.created, result.errors) == (1, [])
    annex = (await session.execute(
        select(Location).where(Location.company_id == company_id, Location.name == "Annex")
    )).scalars().first()
    assert annex is not None, "the unknown location must be created"
    [item] = await _item_projections(session, company_id)
    assert item.location_id == annex.id


# ---------------------------------------------------------------------------
# Category default sell-by (build_import_records, vertical library)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_category_default_sell_by(session):
    """A row without sell_by inherits the category's default_sell_by from the shipped
    vertical library (diamond sells by gram)."""
    company_id, _, _ = await _seed(session, locations=[{"name": "Main"}])
    build = await build_import_records(
        session, company_id,
        [{"name": "Stone", "category": "diamond", "weight": "1.5", "weight_unit": "gram"}],
        upsert=False,
    )
    assert build.errors == []
    assert build.records[0]["data"]["sell_by"] == "gram"


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
        upsert=False,
    )
    assert build.errors == []
    assert build.records[0]["data"]["retail_price"] == 25.0


# ---------------------------------------------------------------------------
# Building records creates nothing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_creates_no_location(session):
    """Building records lists locations an import would create but writes none, and leaves the row unresolved."""
    company_id, _, _ = await _seed(session, locations=[{"name": "Main"}])
    build = await build_import_records(
        session, company_id,
        [{"name": "Widget", "sell_by": "piece", "pieces": "1", "location_name": "Ghost"}],
        upsert=False,
    )
    assert build.locations_to_create == ["Ghost"]
    assert build.records == []
    assert len(build.errors) == 1
    remaining = (await session.execute(
        select(Location).where(Location.company_id == company_id)
    )).scalars().all()
    assert {loc.name for loc in remaining} == {"Main"}, "building records must not create the referenced location"


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
async def test_resubmitting_rows_without_a_sku_changes_nothing(session):
    company_id, user_id, _ = await _seed(session, locations=[{"name": "Main"}])
    rows = [{"name": "No Code", "sell_by": "piece", "pieces": "3"}]
    first = await import_items(session, company_id, user_id, "admin", {}, rows, upsert=True, filename=None, idempotency_key=None)
    again = await import_items(session, company_id, user_id, "admin", {}, rows, upsert=True, filename=None, idempotency_key=None)
    assert (first.created, again.created, again.errors) == (1, 0, [])
    assert len(await _item_projections(session, company_id)) == 1


@pytest.mark.asyncio
async def test_import_keeps_distinct_lots_with_same_sku(session):
    company_id, user_id, _ = await _seed(session, locations=[{"name": "Main"}])
    rows = [
        {"name": "Lot A", "sku": "LOT-SKU", "sell_by": "piece", "pieces": "1"},
        {"name": "Lot B", "sku": "LOT-SKU", "sell_by": "piece", "pieces": "2"},
    ]
    result = await import_items(
        session, company_id, user_id, "admin", {}, rows, upsert=False,
        filename=None, idempotency_key="same-sku-batch", decisions={"separate_lots": ["LOT-SKU"]},
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
        filename=None, idempotency_key="amb-seed", decisions={"separate_lots": ["AMB"]},
    )
    assert seeded.created == 2

    build = await build_import_records(
        session, company_id,
        [{"name": "Which lot?", "sku": "AMB", "sell_by": "piece", "pieces": "2"}],
        upsert=True,
    )
    assert build.records == []
    assert "matches multiple lots" in build.errors[0]["message"]


@pytest.mark.asyncio
async def test_import_records_duplicate_barcodes_and_edit_resolves_them(session):
    """An import whose file carries the same barcode on two rows records both lots as
    given; the resolver reports the duplicate instead of picking one, and moving one lot
    to a fresh barcode through a normal edit resolves the lookup."""
    from celerp.events.engine import emit_event
    from celerp_inventory.routes import resolve_item_by_code

    company_id, user_id, _ = await _seed(session, locations=[{"name": "Main"}])
    rows = [
        {"name": "Lot A", "sku": "DUP-A", "barcode": "7508", "sell_by": "piece", "pieces": "1"},
        {"name": "Lot B", "sku": "DUP-B", "barcode": "7508", "sell_by": "piece", "pieces": "1"},
    ]
    result = await import_items(
        session, company_id, user_id, "admin", {}, rows, upsert=False,
        filename=None, idempotency_key="dup-barcode-batch",
    )
    assert result.created == 2
    res = await resolve_item_by_code(session, company_id, "7508")
    assert res.duplicate_physical and len(res.matches) == 2

    lot_a = next(i for i in await _item_projections(session, company_id) if i.state["sku"] == "DUP-A")
    await emit_event(
        session, company_id=company_id, entity_id=lot_a.entity_id, entity_type="item",
        event_type="item.updated", data={"fields_changed": {"barcode": {"new": "7510"}}},
        actor_id=user_id, location_id=None, source="test",
        idempotency_key=str(uuid.uuid4()), metadata_={},
    )
    await session.commit()
    assert (await resolve_item_by_code(session, company_id, "7508")).one.state["sku"] == "DUP-B"
    assert (await resolve_item_by_code(session, company_id, "7510")).one.entity_id == lot_a.entity_id


@pytest.mark.asyncio
async def test_upsert_on_shared_barcode_never_picks_arbitrarily(session):
    """An upsert row whose barcode several items share patches only the holder its SKU
    identifies; without a distinguishing SKU the row fails instead of patching an
    arbitrary item."""
    company_id, user_id, _ = await _seed(session, locations=[{"name": "Main"}])
    seed = [
        {"name": "Lot A", "sku": "SHR-A", "barcode": "7508", "sell_by": "piece", "pieces": "1"},
        {"name": "Lot B", "sku": "SHR-B", "barcode": "7508", "sell_by": "piece", "pieces": "1"},
    ]
    assert (await import_items(
        session, company_id, user_id, "admin", {}, seed, upsert=False,
        filename=None, idempotency_key="shared-barcode-seed",
    )).created == 2

    ambiguous = await build_import_records(
        session, company_id,
        [{"name": "Renamed", "barcode": "7508", "sell_by": "piece"},
         {"name": "Renamed", "sku": "OTHER", "barcode": "7508", "sell_by": "piece"}],
        upsert=True,
    )
    assert ambiguous.records == []
    assert [(e["row"], e["field"]) for e in ambiguous.errors] == [(1, "barcode"), (2, "barcode")]

    chosen = await import_items(
        session, company_id, user_id, "admin", {},
        [{"name": "Lot B renamed", "sku": "SHR-B", "barcode": "7508", "sell_by": "piece"}],
        upsert=True, filename=None, idempotency_key=None,
    )
    assert chosen.updated == 1
    names = {i.state["sku"]: i.state["name"] for i in await _item_projections(session, company_id)}
    assert names == {"SHR-A": "Lot A", "SHR-B": "Lot B renamed"}


@pytest.mark.asyncio
async def test_import_chunk_takes_code_namespace_before_first_item_write(session, monkeypatch):
    company_id, user_id, _ = await _seed(session, locations=[{"name": "Main"}])
    seed = [
        {"name": "One", "sku": "ORDER-1", "sell_by": "piece", "pieces": "1"},
        {"name": "Two", "sku": "ORDER-2", "sell_by": "piece", "pieces": "1"},
    ]
    assert (await import_items(
        session, company_id, user_id, "admin", {}, seed, upsert=False,
        filename=None, idempotency_key="order-seed",
    )).created == 2

    calls: list[str] = []
    real_lock, real_emit = svc.lock_item_code_namespace, svc.emit_event

    async def _lock(s, cid):
        calls.append("lock")
        return await real_lock(s, cid)

    async def _emit(s, **kwargs):
        calls.append(f"emit:{kwargs['data'].get('name') or kwargs['data'].get('fields_changed', {}).get('name')}")
        return await real_emit(s, **kwargs)

    monkeypatch.setattr(svc, "lock_item_code_namespace", _lock)
    monkeypatch.setattr(svc, "emit_event", _emit)
    patches = [
        {"name": "One renamed", "sku": "ORDER-1", "sell_by": "piece"},
        {"name": "Two renamed", "sku": "ORDER-2", "sell_by": "piece"},
    ]
    result = await import_items(
        session, company_id, user_id, "admin", {}, patches, upsert=True,
        filename=None, idempotency_key=None,
    )
    assert result.updated == 2
    assert calls == ["lock", "emit:One renamed", "emit:Two renamed"], calls


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
async def test_raw_upsert_updates_only_the_item_its_key_created(session):
    company_id, user_id, _ = await _seed(session, locations=[{"name": "Main"}])
    user = SimpleNamespace(id=user_id)
    seed_rows = [{"name": "Raw One", "sku": "RAW-UP", "sell_by": "piece", "pieces": "1"}]

    first_build = await build_import_records(
        session, company_id, seed_rows, upsert=False,
    )
    original = first_build.records[0]
    first = await commit_import_batch(
        session, company_id, user, "admin", {},
        BatchImportRequest(records=[ImportRecord(**original)]),
    )
    assert first.created == 1

    changed_rows = [{"name": "Raw Two", "sku": "RAW-UP", "sell_by": "piece", "pieces": "2"}]
    replay_build = await build_import_records(
        session, company_id, changed_rows, upsert=False,
    )
    changed = replay_build.records[0]
    elsewhere = await commit_import_batch(
        session, company_id, user, "admin", {},
        BatchImportRequest(records=[ImportRecord(**changed)], upsert=True),
    )
    assert (elsewhere.updated, len(elsewhere.errors)) == (0, 1)

    same = await commit_import_batch(
        session, company_id, user, "admin", {},
        BatchImportRequest(records=[ImportRecord(**{**changed, "entity_id": original["entity_id"]})], upsert=True),
    )
    assert same.updated == 1

    items = await _item_projections(session, company_id)
    assert len(items) == 1
    assert items[0].entity_id == original["entity_id"]
    assert items[0].state["name"] == "Raw Two"
    assert float(items[0].state["quantity"]) == 2.0


@pytest.mark.asyncio
async def test_authorized_preview_can_plan_missing_location_without_writing(session):
    company_id, _, _ = await _seed(session, locations=[{"name": "Main"}])
    build = await build_import_records(
        session, company_id,
        [{"name": "Widget", "sell_by": "piece", "pieces": "1", "location_name": "Annex"}],
        upsert=False, create_missing_locations=True,
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
# Shared writer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_import_rows_and_import_batch_share_writer(client, session, monkeypatch):
    """/import/rows (import_items) and /import/batch (commit_import_batch) converge on one writer."""
    # /import/rows path: import_items must delegate to write_import_batch.
    company_a, user_a, _ = await _seed(session, locations=[{"name": "Main"}])
    rows = [{"name": "Shared", "sku": "SHARE-1", "sell_by": "piece", "pieces": "2"}]

    calls: list[int] = []
    real_writer = svc.write_import_batch

    async def _spy(*args, **kwargs):
        calls.append(1)
        return await real_writer(*args, **kwargs)

    monkeypatch.setattr(svc, "write_import_batch", _spy)
    ra = await import_items(session, company_a, user_a, "admin", {}, rows, upsert=False, filename=None, idempotency_key=None)
    monkeypatch.undo()
    assert calls, "import_items must route through write_import_batch"
    assert ra.created == 1
    items_a = await _item_projections(session, company_a)
    assert [p.state.get("sku") for p in items_a] == ["SHARE-1"]

    # /import/batch path: the same writer lands an identical record hand-built as a raw batch.
    company_b, user_b, _ = await _seed(session, locations=[{"name": "Main"}])
    build = await build_import_records(session, company_b, rows, upsert=False)
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
    """Quantity is taken from an explicit column, else the pieces column, or the weight when it is in the selling unit."""
    company_id, _, _ = await _seed(session, locations=[{"name": "Main"}])
    build = await build_import_records(
        session, company_id,
        [
            {"name": "A", "sell_by": "piece", "pieces": "7"},                  # pieces unit -> pieces
            {"name": "B", "sell_by": "piece", "pieces": "5", "quantity": "99"},  # explicit qty wins
            {"name": "C", "sell_by": "carat", "weight": "2.5", "weight_unit": "carat"},  # same weight unit -> weight
            {"name": "D", "sell_by": "piece"},                                 # no source -> 0
        ],
        upsert=False,
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
            {"name": "A", "sell_by": "carat", "quantity": "1", "weight": "100", "weight_unit": "Gram"},
            {"name": "B", "sell_by": "carat", "quantity": "1", "weight": "100"},
        ],
        upsert=False,
    )
    assert build.errors == []
    assert build.records[0]["data"]["weight_unit"] == "gram"
    assert build.records[1]["data"].get("weight_unit") is None


# ---------------------------------------------------------------------------
# The import plan: money, units, quantity, totals, shared SKUs, decisions
# ---------------------------------------------------------------------------

def _codes(plan) -> list[tuple[int, str]]:
    return [(e["row"], e["code"]) for e in plan.errors]


@pytest.mark.parametrize("cell, expected", [
    ("65", (65.0, None)),
    ("฿65.00", (65.0, None)),
    ("฿ 85", (85.0, None)),
    ("1,250 THB", (1250.0, None)),
    ("12 USD", (None, "price_currency_mismatch")),
    ("€12", (None, "price_currency_mismatch")),
    ("$12", (None, "price_currency_ambiguous")),
    ("1,25", (None, "invalid_value")),
    ("฿12 THB", (None, "invalid_value")),
    ("12 XYZ", (None, "invalid_value")),
    ("", (None, None)),
])
def test_parse_import_money_reads_only_the_company_currency(cell, expected):
    assert svc.parse_import_money(cell, "THB") == expected


def test_parse_import_money_shared_symbol_needs_the_header_currency():
    assert svc.parse_import_money("$12", "USD", stated_currency="USD") == (12.0, None)
    assert svc.parse_import_money("$12", "USD", stated_currency="CAD") == (None, "price_currency_ambiguous")


def test_import_unit_lookup_matches_names_in_any_case_and_aliases_only_configured_units():
    lookup = svc.import_unit_lookup([{"name": "piece"}, {"name": "Box"}])
    assert lookup["piece"] == "piece" and lookup["box"] == "Box"
    assert lookup["ชิ้น"] == "piece"
    assert "litre" not in lookup  # liter is not configured, so its alias means nothing


@pytest.mark.asyncio
async def test_plan_reads_currency_formatted_prices_in_the_company_currency(session):
    company_id, _user_id, _ = await _seed(session, locations=[{"name": "Main"}], currency="THB")
    rows = [
        {"name": "A", "sku": "M-1", "sell_by": "piece", "quantity": "1", "cost_price": "฿1,250.00"},
        {"name": "B", "sku": "M-2", "sell_by": "piece", "quantity": "1", "cost_price": "$5"},
    ]
    plan = await svc.build_import_plan(session, company_id, "admin", {}, rows, upsert=False)
    assert plan.rows[0]["cost_price"] == "1250.0"
    assert _codes(plan) == [(2, "price_currency_ambiguous")]


@pytest.mark.asyncio
async def test_unknown_unit_blocks_with_settings_guidance_and_creates_no_unit(session):
    company_id, user_id, _ = await _seed(session, locations=[{"name": "Main"}])
    rows = [
        {"name": "Rope", "sku": "U-1", "sell_by": "fathom", "quantity": "1"},
        {"name": "Ring", "sku": "U-2", "sell_by": "PIECE", "quantity": "1"},
    ]
    plan = await svc.build_import_plan(session, company_id, "admin", {}, rows, upsert=False)
    assert _codes(plan) == [(1, "sell_by_invalid")]
    assert "Settings > Units" in plan.errors[0]["message"]
    with pytest.raises(svc.ImportRejected):
        await import_items(session, company_id, user_id, "admin", {}, rows, upsert=False,
                           filename=None, idempotency_key=None)
    await session.rollback()
    company = await session.get(Company, company_id)
    assert "units" not in (company.settings or {})
    assert await _item_projections(session, company_id) == []


@pytest.mark.asyncio
async def test_blank_mapped_quantity_blocks_but_an_unmapped_quantity_starts_at_zero(session):
    company_id, _user_id, _ = await _seed(session, locations=[{"name": "Main"}])
    blank = await svc.build_import_plan(
        session, company_id, "admin", {}, [{"name": "A", "sku": "Q-1", "sell_by": "piece", "quantity": ""}], upsert=False,
    )
    assert _codes(blank) == [(1, "quantity_blank")]
    absent = await svc.build_import_plan(
        session, company_id, "admin", {}, [{"name": "A", "sku": "Q-1", "sell_by": "piece"}], upsert=False,
    )
    assert absent.errors == []


_TOTAL_ROWS = [
    {"name": "A", "sku": "T-1", "sell_by": "piece", "quantity": "2"},
    {"name": "B", "sku": "T-2", "sell_by": "piece", "quantity": "3"},
    {"name": "Total:", "sku": "", "sell_by": "piece", "quantity": "5"},
]


@pytest.mark.asyncio
async def test_total_row_blocks_until_excluded_or_imported_on_purpose(session):
    company_id, user_id, _ = await _seed(session, locations=[{"name": "Main"}])
    plan = await svc.build_import_plan(session, company_id, "admin", {}, _TOTAL_ROWS, upsert=False)
    assert plan.summary_rows == [3] and _codes(plan) == [(3, "summary_row")]

    excluded = await svc.build_import_plan(session, company_id, "admin", {}, _TOTAL_ROWS, upsert=False,
                                           decisions={"exclude": [3]})
    assert excluded.errors == [] and excluded.counts == {"create": 2, "update": 0, "excluded": 1, "blocked": 0}

    result = await import_items(session, company_id, user_id, "admin", {}, _TOTAL_ROWS, upsert=False,
                                filename=None, idempotency_key=None, decisions={"import_summary": [3]})
    assert result.created == 3


@pytest.mark.asyncio
async def test_an_item_named_total_with_a_sku_is_an_item(session):
    company_id, _user_id, _ = await _seed(session, locations=[{"name": "Main"}])
    rows = [*_TOTAL_ROWS[:2], {**_TOTAL_ROWS[2], "sku": "T-3"}]
    plan = await svc.build_import_plan(session, company_id, "admin", {}, rows, upsert=False)
    assert plan.summary_rows == [] and plan.errors == []


_SECTIONED_ROWS = [
    {"name": "A", "sku": "", "sell_by": "piece", "quantity": "2"},
    {"name": "Subtotal", "sku": "", "sell_by": "piece", "quantity": "2"},
    {"name": "B", "sku": "", "sell_by": "piece", "quantity": "3"},
    {"name": "Total", "sku": "", "sell_by": "piece", "quantity": "5"},
]


@pytest.mark.asyncio
async def test_a_subtotal_and_its_grand_total_each_block(session):
    company_id, _user_id, _ = await _seed(session, locations=[{"name": "Main"}])
    plan = await svc.build_import_plan(session, company_id, "admin", {}, _SECTIONED_ROWS, upsert=False)
    assert plan.summary_rows == [2, 4]
    assert _codes(plan) == [(2, "summary_row"), (4, "summary_row")]


@pytest.mark.asyncio
async def test_every_subtotal_of_several_sections_blocks(session):
    company_id, _user_id, _ = await _seed(session, locations=[{"name": "Main"}])
    rows = [
        {"name": "A", "sku": "", "sell_by": "piece", "quantity": "1"},
        {"name": "Sub total:", "sku": "", "sell_by": "piece", "quantity": "1"},
        {"name": "B", "sku": "", "sell_by": "piece", "quantity": "4"},
        {"name": "C", "sku": "", "sell_by": "piece", "quantity": "6"},
        {"name": "SUBTOTAL", "sku": "", "sell_by": "piece", "quantity": "10"},
        {"name": "D", "sku": "", "sell_by": "piece", "quantity": "7"},
        {"name": "Sub-total", "sku": "", "sell_by": "piece", "quantity": "7"},
        {"name": "Grand Total", "sku": "", "sell_by": "piece", "quantity": "18"},
        {"name": "รวมทั้งสิ้น", "sku": "", "sell_by": "piece", "quantity": "99"},
    ]
    plan = await svc.build_import_plan(session, company_id, "admin", {}, rows, upsert=False)
    assert plan.summary_rows == [2, 5, 7, 8, 9]


@pytest.mark.asyncio
async def test_a_total_label_blocks_whatever_its_numbers_say(session):
    company_id, _user_id, _ = await _seed(session, locations=[{"name": "Main"}])
    rows = [
        {"name": "A", "sku": "", "sell_by": "piece", "quantity": "2"},
        {"name": "Totals", "sku": "", "sell_by": "piece"},
        {"name": "Sum", "sku": " ", "sell_by": "piece", "quantity": "41"},
    ]
    plan = await svc.build_import_plan(session, company_id, "admin", {}, rows, upsert=False)
    assert plan.summary_rows == [2, 3]


@pytest.mark.asyncio
async def test_excluding_rows_around_a_total_leaves_the_total_blocked(session):
    company_id, _user_id, _ = await _seed(session, locations=[{"name": "Main"}])
    around = await svc.build_import_plan(session, company_id, "admin", {}, _SECTIONED_ROWS, upsert=False,
                                         decisions={"exclude": [1, 3]})
    assert around.summary_rows == [2, 4] and _codes(around) == [(2, "summary_row"), (4, "summary_row")]
    one_out = await svc.build_import_plan(session, company_id, "admin", {}, _SECTIONED_ROWS, upsert=False,
                                          decisions={"exclude": [2]})
    assert one_out.summary_rows == [4] and _codes(one_out) == [(4, "summary_row")]
    both_out = await svc.build_import_plan(session, company_id, "admin", {}, _SECTIONED_ROWS, upsert=False,
                                           decisions={"exclude": [2, 4]})
    assert both_out.errors == [] and both_out.counts["create"] == 2


@pytest.mark.asyncio
async def test_a_total_imports_as_an_item_only_when_chosen_row_by_row(session):
    company_id, user_id, _ = await _seed(session, locations=[{"name": "Main"}])
    with pytest.raises(svc.ImportRejected):
        await import_items(session, company_id, user_id, "admin", {}, _SECTIONED_ROWS, upsert=False,
                           filename=None, idempotency_key=None, decisions={"import_summary": [4]})
    await session.rollback()
    assert await _item_projections(session, company_id) == []
    result = await import_items(session, company_id, user_id, "admin", {}, _SECTIONED_ROWS, upsert=False,
                                filename=None, idempotency_key=None, decisions={"import_summary": [2, 4]})
    assert result.created == 4


@pytest.mark.asyncio
async def test_shared_sku_needs_a_lots_decision_and_disagreeing_rows_block(session):
    company_id, user_id, _ = await _seed(session, locations=[{"name": "Main"}])
    lots = [
        {"name": "Lot A", "sku": "D-1", "sell_by": "piece", "quantity": "1", "cost_price": "10"},
        {"name": "Lot B", "sku": "D-1", "sell_by": "piece", "quantity": "2", "cost_price": "10"},
    ]
    plan = await svc.build_import_plan(session, company_id, "admin", {}, lots, upsert=False)
    assert _codes(plan) == [(1, "duplicate_sku_lots"), (2, "duplicate_sku_lots")]
    assert plan.duplicate_groups == [{"sku": "D-1", "rows": [1, 2], "quantity": 3.0, "conflict": False}]

    conflict = [lots[0], {**lots[1], "cost_price": "12"}]
    plan = await svc.build_import_plan(session, company_id, "admin", {}, conflict, upsert=False,
                                       decisions={"separate_lots": ["D-1"]})
    assert _codes(plan) == [(1, "duplicate_sku_conflict"), (2, "duplicate_sku_conflict")]

    result = await import_items(session, company_id, user_id, "admin", {}, lots, upsert=False,
                                filename=None, idempotency_key=None, decisions={"separate_lots": ["D-1"]})
    assert result.created == 2


@pytest.mark.asyncio
async def test_two_rows_updating_one_item_block(session):
    company_id, user_id, _ = await _seed(session, locations=[{"name": "Main"}])
    await import_items(session, company_id, user_id, "admin", {},
                       [{"name": "One", "sku": "UT-1", "sell_by": "piece", "quantity": "1"}],
                       upsert=False, filename=None, idempotency_key=None)
    rows = [{"name": "One", "sku": "UT-1", "sell_by": "piece", "quantity": "2"},
            {"name": "One", "sku": "UT-1", "sell_by": "piece", "quantity": "3"}]
    plan = await svc.build_import_plan(session, company_id, "admin", {}, rows, upsert=True,
                                       decisions={"separate_lots": ["UT-1"]})
    assert _codes(plan) == [(1, "duplicate_upsert_target"), (2, "duplicate_upsert_target")]


@pytest.mark.asyncio
async def test_decision_naming_no_row_blocks(session):
    company_id, _user_id, _ = await _seed(session, locations=[{"name": "Main"}])
    plan = await svc.build_import_plan(session, company_id, "admin", {}, _TOTAL_ROWS[:1], upsert=False,
                                       decisions={"exclude": [4], "import_summary": ["x"]})
    assert sorted(e["code"] for e in plan.errors) == ["decision_row_unknown", "decision_row_unknown"]
    assert all(e["row"] == 0 for e in plan.errors)


def test_decisions_are_part_of_the_import_identity():
    rows = _TOTAL_ROWS
    base = svc.import_operation_key("k", rows, False)
    assert svc.import_operation_key("k", rows, False, {"exclude": [3]}) != base
    assert (svc.import_operation_key("k", rows, False, {"exclude": ["3", 3], "separate_lots": [" A "]})
            == svc.import_operation_key("k", rows, False, {"exclude": [3], "separate_lots": ["A"]}))


@pytest.mark.asyncio
async def test_excluding_a_row_changes_what_the_plan_writes(session):
    company_id, _user_id, _ = await _seed(session, locations=[{"name": "Main"}])
    a = await svc.build_import_plan(session, company_id, "admin", {}, _TOTAL_ROWS, upsert=False,
                                    decisions={"exclude": [3]})
    b = await svc.build_import_plan(session, company_id, "admin", {}, _TOTAL_ROWS, upsert=False,
                                    decisions={"exclude": [2, 3]})
    assert a.semantic_fingerprint != b.semantic_fingerprint


@pytest.mark.asyncio
async def test_import_reports_undo_only_for_a_pure_create_and_counts_a_repeat(session):
    company_id, user_id, _ = await _seed(session, locations=[{"name": "Main"}])
    rows = [{"name": "R", "sku": "RV-1", "sell_by": "piece", "quantity": "1"}]
    first = await import_items(session, company_id, user_id, "admin", {}, rows, upsert=False,
                               filename=None, idempotency_key="op-1")
    assert (first.created, first.reversible, first.already_imported) == (1, True, 0)

    again = await import_items(session, company_id, user_id, "admin", {}, rows, upsert=False,
                               filename=None, idempotency_key="op-1")
    assert (again.created, again.reversible, again.already_imported) == (0, False, 1)

    update = await import_items(session, company_id, user_id, "admin", {}, [{**rows[0], "quantity": "4"}],
                                upsert=True, filename=None, idempotency_key=None)
    assert (update.updated, update.reversible) == (1, False)

    new_place = await import_items(
        session, company_id, user_id, "admin", {},
        [{"name": "S", "sku": "RV-2", "sell_by": "piece", "quantity": "1", "location_name": "Annex"}],
        upsert=False, filename=None, idempotency_key=None,
    )
    assert (new_place.created, new_place.reversible) == (1, False)
