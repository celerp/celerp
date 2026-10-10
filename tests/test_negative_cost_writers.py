# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Every writer refuses a negative goods cost with the same message.

A lot's cost is never below zero, whether it is created from the item page or
the API, imported from a file or a batch, carried over by a migration,
received on a bill, merged, transformed, rolled up from a recipe, or edited
afterwards. Each refusal names the lot and writes nothing.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from test_cost_restatement import _item, _state
from test_helpers import company_auth

MESSAGE = "a cost cannot be negative"

pytestmark = pytest.mark.asyncio


async def _auth(session) -> dict:
    return await company_auth(session, uuid.uuid4(), uuid.uuid4())


async def _item_events(session, company_id) -> int:
    session.expire_all()
    return await session.scalar(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == company_id, LedgerEntry.entity_type == "item"))


def _sku() -> str:
    return f"NC-{uuid.uuid4().hex[:6]}"


# -- API: POST /items ------------------------------------------------------

@pytest.mark.parametrize("field", ["cost_total", "cost_price"])
async def test_create_refuses_a_negative_cost(client, session, field):
    auth = await _auth(session)
    sku = _sku()
    before = await _item_events(session, auth["company_id"])
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": sku, "name": "Lot", "quantity": 2, "sell_by": "piece", field: -5})
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == f"{sku}: {MESSAGE}"
    assert await _item_events(session, auth["company_id"]) == before


async def test_create_refuses_a_negative_unit_cost_with_no_stock(client, session):
    auth = await _auth(session)
    sku = _sku()
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": sku, "name": "Lot", "quantity": 0, "sell_by": "piece", "cost_price": -5})
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == f"{sku}: {MESSAGE}"


# -- Edits: the unit cost of a lot with no stock ---------------------------

async def test_patch_refuses_a_negative_unit_cost_on_a_lot_with_no_stock(client, session):
    auth = await _auth(session)
    item = await _item(client, auth, None, qty=0)
    sku = (await _state(session, auth, item))["sku"]
    r = await client.patch(f"/items/{item}", headers=auth["headers"],
                           json={"fields_changed": {"cost_price": {"old": None, "new": -5}}})
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == f"{sku}: {MESSAGE}"
    assert (await _state(session, auth, item)).get("cost_price") is None


async def test_price_endpoint_refuses_a_negative_unit_cost_on_a_lot_with_no_stock(client, session):
    auth = await _auth(session)
    item = await _item(client, auth, None, qty=0)
    sku = (await _state(session, auth, item))["sku"]
    r = await client.post(f"/items/{item}/price", headers=auth["headers"],
                          json={"price_type": "cost_price", "new_price": -5})
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == f"{sku}: {MESSAGE}"
    assert (await _state(session, auth, item)).get("cost_price") is None


# -- Imports ---------------------------------------------------------------

async def test_batch_import_refuses_a_created_row_with_a_negative_cost(client, session):
    auth = await _auth(session)
    sku = _sku()
    entity_id = f"item:{uuid.uuid4()}"
    r = await client.post("/items/import/batch", headers=auth["headers"], json={"records": [{
        "entity_id": entity_id, "event_type": "item.created", "source": "import",
        "data": {"sku": sku, "name": "Lot", "sell_by": "piece", "quantity": 1, "cost_total": -5},
        "idempotency_key": f"neg-{uuid.uuid4().hex}"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 0, body
    assert [e["message"] for e in body["errors"]] == [f"Row (SKU={sku}): {sku}: {MESSAGE}"], body
    assert await _state(session, auth, entity_id) == {}


async def test_file_import_preview_and_commit_refuse_a_negative_cost(client, session):
    auth = await _auth(session)
    sku = _sku()
    r = await client.post("/companies/me/locations", headers=auth["headers"], json={"name": "WH", "type": "warehouse"})
    assert r.status_code == 200, r.text
    rows = [{"sku": sku, "name": "Lot", "sell_by": "piece", "quantity": "1", "cost_price": "-5",
             "location_name": "WH"}]
    r = await client.post("/items/import/rows/preview", headers=auth["headers"],
                          json={"rows": rows, "upsert": False, "idempotency_key": "neg-1"})
    assert r.status_code == 200, r.text
    errors = r.json()["errors"]
    assert [(e["row"], e["field"], e["code"], e["message"]) for e in errors] == [
        (1, "cost_price", "negative_value", f"{sku}: {MESSAGE}")], errors
    before = await _item_events(session, auth["company_id"])
    r = await client.post("/items/import/rows", headers=auth["headers"],
                          json={"rows": rows, "upsert": False, "idempotency_key": "neg-1"})
    assert r.status_code == 422, r.text
    assert await _item_events(session, auth["company_id"]) == before


# -- Migration -------------------------------------------------------------

async def test_migration_item_with_a_negative_cost_is_refused(client, session):
    from celerp.importers.schema import CIFItem
    from celerp.importers.sinks import sink_for
    from test_migration_sinks import _no_attachments, _sink_context

    _headers, context = await _sink_context(client, session, _no_attachments)
    item = CIFItem(source_system="manager_io", source_type="InventoryItem", source_external_id="neg-1",
                   sku="NEG-1", name="Widget", status="available", cost_per_unit=Decimal("-5"))
    before = await _item_events(session, context.company_id)
    result = await sink_for("items").import_batch(context, [item])
    assert result.created == 0, result
    assert any(f"NEG-1: {MESSAGE}" in str(e) for e in result.errors), result.errors
    assert await _item_events(session, context.company_id) == before


async def test_migration_opening_position_with_a_negative_value_is_refused(client, session):
    from celerp.importers.schema import CIFInventoryAdjustment, CIFItem
    from celerp.importers.sinks import sink_for
    from test_migration_sinks import _no_attachments, _persist_mappings, _sink_context

    _headers, context = await _sink_context(client, session, _no_attachments)
    item = CIFItem(source_system="manager_io", source_type="InventoryItem", source_external_id="neg-2",
                   sku="NEG-2", name="Widget", status="available")
    created = await sink_for("items").import_batch(context, [item])
    assert created.created == 1, created
    await _persist_mappings(session, context.run_id, created)
    opening = CIFInventoryAdjustment(
        source_system="manager_io", source_type="InventoryItem", source_external_id="neg-2-open",
        kind="opening", adjustment_date="2025-01-01", item_external_id="neg-2",
        quantity=Decimal("3"), value=Decimal("-30"))
    result = await sink_for("inventory_adjustments").import_batch(context, [opening])
    assert result.created == 0, result
    assert any(f"NEG-2: {MESSAGE}" in str(e) for e in result.errors), result.errors
    state = (await session.get(Projection, (context.company_id, created.mappings[0].target_entity_id))).state
    assert state.get("quantity") == 0 and state.get("cost_total") is None


async def test_historical_delivery_with_a_negative_cost_is_refused(client, session):
    from fastapi import HTTPException

    from celerp_docs.routes import record_historical_delivery

    auth = await _auth(session)
    item = await _item(client, auth, 40.0, qty=10, sku="HIST-NEG")
    r = await client.post("/docs", headers=auth["headers"], json={"doc_type": "invoice", "line_items": [
        {"item_id": item, "sku": "HIST-NEG", "name": "Lot", "quantity": 5, "unit_price": 10,
         "line_total": 50}], "total": 50})
    assert r.status_code == 200, r.text
    invoice = r.json()["id"]
    assert (await client.post(f"/docs/{invoice}/finalize", headers=auth["headers"])).status_code == 200
    lot = f"item:{uuid.uuid4()}"
    with pytest.raises(HTTPException) as exc:
        await record_historical_delivery(
            session, auth["company_id"], invoice, actor_id=auth["user_id"], source="migration",
            idempotency_key=f"m:{invoice}:delivered",
            lines=[{"line": 0, "item_id": item, "quantity": 5, "cost": -20, "lot_id": lot, "date": "2025-02-03"}])
    assert (exc.value.status_code, exc.value.detail) == (422, f"HIST-NEG: {MESSAGE}")


# -- Connectors ------------------------------------------------------------

async def test_connector_product_with_a_negative_cost_is_refused(client, session, monkeypatch):
    import contextlib
    from types import SimpleNamespace

    from fastapi import HTTPException

    import celerp.connectors.upsert as upsert
    from celerp_inventory.services import CostRestatementConflict

    @contextlib.asynccontextmanager
    async def _test_session():
        yield session
    monkeypatch.setattr("celerp.db.SessionLocal", _test_session)

    auth = await _auth(session)
    company_id = str(auth["company_id"])

    def _product(key: str, cost: float):
        return SimpleNamespace(sku=key.upper(), name="Widget", idempotency_key=f"qb:item:{key}",
                               sale_price=None, quantity=None, cost_price=cost, description=None)

    before = await _item_events(session, auth["company_id"])
    with pytest.raises(HTTPException) as exc:
        await upsert.upsert_item(company_id, _product("conn-neg", -5))
    assert (exc.value.status_code, exc.value.detail) == (422, f"CONN-NEG: {MESSAGE}")
    assert await _item_events(session, auth["company_id"]) == before

    assert await upsert.upsert_item(company_id, _product("conn-upd", 5)) == "created"
    with pytest.raises(CostRestatementConflict) as exc:
        await upsert.upsert_item(company_id, _product("conn-upd", -5))
    assert str(exc.value) == f"CONN-UPD: {MESSAGE}"


# -- Receiving, merging, transforming, recipes -----------------------------

async def test_receiving_goods_at_a_negative_cost_is_refused(client, session):
    auth = await _auth(session)
    loc = (await client.post("/companies/me/locations", headers=auth["headers"],
                             json={"name": "WH", "type": "warehouse"})).json()["id"]
    item = await _item(client, auth, None, qty=0, sku="RECV-NEG")
    r = await client.post("/docs", headers=auth["headers"], json={"doc_type": "bill", "line_items": [
        {"item_id": item, "sku": "RECV-NEG", "name": "Lot", "quantity": 10, "unit_price": -4,
         "line_total": -40}], "total": -40})
    assert r.status_code == 200, r.text
    bill = r.json()["id"]
    assert (await client.post(f"/docs/{bill}/finalize", headers=auth["headers"])).status_code == 200
    before = await _item_events(session, auth["company_id"])
    r = await client.post(f"/docs/{bill}/receive", headers=auth["headers"], json={
        "location_id": loc, "received_items": [{"po_line_index": 0, "quantity_received": 10}]})
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == f"RECV-NEG: {MESSAGE}"
    assert await _item_events(session, auth["company_id"]) == before


async def test_merge_refuses_a_negative_resulting_cost(client, session):
    auth = await _auth(session)
    a, b = await _item(client, auth, 10.0, sku="MRG-NEG"), await _item(client, auth, 20.0, sku="MRG-NEG")
    r = await client.post("/items/merge", headers=auth["headers"], json={
        "source_entity_ids": [a, b], "target_sku_from": a, "resulting_cost_total": -5})
    # A merge keeps the sum of its parts' costs, so any other resulting cost is refused.
    assert r.status_code == 422, r.text
    assert r.json()["detail"].startswith("A merge keeps the cost of the items it combines."), r.text
    assert (await _state(session, auth, a))["status"] != "merged"


async def test_transform_refuses_a_negative_child_cost(client, session):
    auth = await _auth(session)
    parent = await _item(client, auth, 100.0, qty=10)
    r = await client.post(f"/items/{parent}/transform", headers=auth["headers"], json={
        "child_sku": "TRF-NEG", "child_category": "Processed", "child_sell_by": "piece",
        "child_quantity": 8.0, "child_cost_total": -5})
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == f"TRF-NEG: {MESSAGE}"
    assert (await _state(session, auth, parent))["status"] == "available"


async def test_recipe_that_rolls_up_to_a_negative_cost_is_refused(client, session):
    auth = await _auth(session)
    part = await _item(client, auth, 10.0, qty=1)
    product = await _item(client, auth, None, qty=0, sku="RCP-NEG")
    r = await client.put(f"/manufacturing/items/{product}/recipe", headers=auth["headers"], json={
        "output_qty": 1, "components": [{"item_id": part, "quantity": 1}], "labor": [],
        "overhead": [{"description": "Credit", "amount": -100}]})
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == f"RCP-NEG: {MESSAGE}"
    assert (await _state(session, auth, product)).get("cost_price") is None


# -- The event boundary ----------------------------------------------------

@pytest.mark.parametrize("event_type,data", [
    ("item.created", {"sku": "EVT-NEG", "name": "Lot", "quantity": 1, "cost_total": -1}),
    ("item.pricing.set", {"price_type": "cost_price", "new_price": -1}),
    ("item.updated", {"fields_changed": {"cost_total": {"old": None, "new": -1}}}),
    ("item.quantity.adjusted", {"new_qty": 1, "cost_base": -1}),
    ("item.cost_adjusted", {"cost_total": -1}),
])
async def test_the_event_boundary_refuses_a_negative_cost_from_any_writer(client, session, event_type, data):
    from fastapi import HTTPException

    from celerp.events.engine import emit_event

    auth = await _auth(session)
    item = await _item(client, auth, 10.0, sku="EVT-NEG")
    target = f"item:{uuid.uuid4()}" if event_type == "item.created" else item
    with pytest.raises(HTTPException) as exc:
        await emit_event(session, company_id=auth["company_id"], entity_id=target, entity_type="item",
                         event_type=event_type, data=data, actor_id=auth["user_id"], location_id=None,
                         source="api", idempotency_key=f"evt-{uuid.uuid4().hex}", metadata_={})
    assert exc.value.status_code == 422
    assert exc.value.detail == f"EVT-NEG: {MESSAGE}"
