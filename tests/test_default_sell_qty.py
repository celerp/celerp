# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Default Sell Qty is ordinary top-level item state: every lot made from another lot carries
it, every write path validates it the same way (unset, Full, or a positive number the item's
unit allows), CSV import and export round-trip it, and the API and the field schema expose it
directly after Allow Splitting."""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

from celerp.events.engine import emit_event
from celerp.services.company_lock import locked_company
from celerp.services.field_schema import _BASE_FIELDS, get_effective_field_schema
from celerp.routers.companies import UnitRecord
from celerp.services.line_measures import proposed_sell_qty, validate_default_sell_qty
from celerp.services.list_behavior import FINALIZED
from celerp.services.units import build_unit_map
from mfg_runs import complete, issue, product, receive, reopen, run
from test_cost_restatement import _item, _merge, _state
from test_inventory_transform import _transform_payload
from test_receipt_accounting import _doc, _finalize, _parcels, _receive, _return
from test_supplier_credit_note import _trial_balance
from test_supplier_return_settles_bill import supplier_credits
from test_helpers import doc_version

LOCALES = Path(__file__).resolve().parent.parent / "ui" / "locales"


async def _lot(client, auth, sell_by: str = "carat", qty: float = 6, **fields) -> str:
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": f"DSQ-{uuid.uuid4().hex[:6]}", "name": "Parcel", "quantity": qty, "sell_by": sell_by,
        "status": "available", "allow_splitting": True, **fields})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _patch(client, auth, item_id: str, **fields):
    return await client.patch(f"/items/{item_id}", headers=auth["headers"], json={
        "fields_changed": {k: {"old": None, "new": v} for k, v in fields.items()}})


async def _sell_qty(session, auth, item_id: str):
    state = await _state(session, auth, item_id)
    assert "default_sell_qty" not in (state.get("attributes") or {}), state
    return state.get("default_sell_qty")


# Lots made from another lot

async def test_split_child_carries_the_value(client, session, auth):
    lot = await _lot(client, auth, default_sell_qty=2)
    r = await client.post(f"/items/{lot}/split", headers=auth["headers"], json={"children": [{"quantity": 1}]})
    assert r.status_code == 200, r.text
    [child] = r.json()["children"]
    assert await _sell_qty(session, auth, child["id"]) == 2


async def test_transform_child_carries_the_value(client, session, auth):
    lot = await _lot(client, auth, sell_by="gram", qty=10, default_sell_qty=2)
    r = await client.post(f"/items/{lot}/transform", headers=auth["headers"], json=_transform_payload(
        child_sku=f"DSQ-T-{uuid.uuid4().hex[:6]}"))
    assert r.status_code == 200, r.text
    assert await _sell_qty(session, auth, r.json()["child_id"]) == 2


async def test_merge_keeps_the_surviving_lot_value(client, session, auth):
    a = await _lot(client, auth, default_sell_qty=2)
    b = await _lot(client, auth, default_sell_qty="full")
    merged = await _merge(client, auth, [a, b])
    assert await _sell_qty(session, auth, merged) == 2


async def test_received_parcel_carries_the_catalog_item_value(client, session, auth):
    sku = f"DSQ-R-{uuid.uuid4().hex[:6]}"
    await _lot(client, auth, qty=1, sku=sku, default_sell_qty=2)
    po = await _doc(client, auth, "purchase_order", [{"sku": sku, "name": "Beads", "quantity": 4, "unit_price": 12.5}])
    r = await _receive(client, auth, po, {"po_line_index": 0, "sku": sku, "name": "Beads", "quantity_received": 4})
    assert r.status_code == 200, r.text
    [parcel] = await _parcels(session, auth, po)
    assert parcel.get("default_sell_qty") == 2, parcel


async def test_manufactured_lot_carries_the_product_value(client, session, auth):
    raw = await _item(client, auth, 1000.0, qty=100)
    fg = await product(client, auth, [(raw, 1)])
    assert (await _patch(client, auth, fg, default_sell_qty=2)).status_code == 200
    order = await run(client, auth, fg, 1)
    assert (await issue(client, auth, order, key="dsq-i")).status_code == 200
    r = await receive(client, auth, order, 1, key="dsq-r")
    assert r.status_code == 200, r.text
    assert await _sell_qty(session, auth, r.json()["lot_item_id"]) == 2


async def _carat_run(client, auth, output_qty: float, input_qty: float = 1,
                     stock: float = 10) -> tuple[str, str, str]:
    """(run, product, component): a run making ``output_qty`` ct from ``input_qty`` ct of a carat
    component holding ``stock`` ct."""
    made, used = await _lot(client, auth, qty=6), await _lot(client, auth, qty=stock)
    r = await client.post("/manufacturing", headers=auth["headers"], json={
        "description": "Cut", "output_item_id": made, "quantity": output_qty,
        "inputs": [{"item_id": used, "quantity": input_qty}]})
    assert r.status_code == 200, r.text
    return r.json()["id"], made, used


@pytest.mark.parametrize("output_qty, input_qty", [(0.27, 1), (1, 0.27)])
async def test_a_manufacturing_run_finer_than_its_units_is_refused(client, auth, output_qty, input_qty):
    made, used = await _lot(client, auth, qty=6), await _lot(client, auth, qty=10)
    assert (await _set_carat_decimals(client, auth, 1)).status_code == 200
    body = {"description": "Cut", "output_item_id": made, "inputs": [{"item_id": used, "quantity": input_qty}]}
    r = await client.post("/manufacturing", headers=auth["headers"], json={**body, "quantity": output_qty})
    assert r.status_code == 422 and "1 decimal places" in r.text, r.text
    r = await client.post("/manufacturing", headers=auth["headers"], json={
        **body, "quantity": round(output_qty, 1), "inputs": [{"item_id": used, "quantity": round(input_qty, 1)}]})
    assert r.status_code == 200, r.text


async def test_a_manufacturing_issue_or_receipt_finer_than_its_unit_is_refused(client, session, auth):
    order, _, used = await _carat_run(client, auth, 1)
    assert (await _set_carat_decimals(client, auth, 1)).status_code == 200
    r = await issue(client, auth, order, [(used, 0.25)])
    assert r.status_code == 422 and "1 decimal places" in r.text, r.text
    assert (await issue(client, auth, order, [(used, 0.5)])).status_code == 200
    assert (await issue(client, auth, order)).status_code == 200
    assert (await _state(session, auth, used))["quantity"] == 9

    r = await receive(client, auth, order, 0.25)
    assert r.status_code == 422 and "1 decimal places" in r.text, r.text
    r = await receive(client, auth, order, 0.5)
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, r.json()["lot_item_id"]))["quantity"] == 0.5


async def test_returning_run_components_finer_than_their_unit_is_refused_before_any_write(client, session, auth):
    order, _, used = await _carat_run(client, auth, 1)
    assert (await issue(client, auth, order)).status_code == 200
    assert (await _state(session, auth, used))["quantity"] == 9
    before = (await _state(session, auth, order))["inputs"]

    async def give_back(qty: float):
        return await client.post(f"/manufacturing/{order}/return", headers=auth["headers"],
                                 json={"items": [{"item_id": used, "quantity": qty}]})

    r = await give_back(0.275)
    assert r.status_code == 422 and "2 decimal places" in r.text, r.text
    assert (await _state(session, auth, used))["quantity"] == 9
    assert (await _state(session, auth, order))["inputs"] == before
    r = await give_back(0.25)
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, used))["quantity"] == 9.25


async def test_an_open_manufacturing_run_blocks_fewer_decimals_until_it_is_cancelled(client, session, auth):
    order, made, _ = await _carat_run(client, auth, 0.27)
    sku = await _sku(session, auth, made)
    r = await _set_carat_decimals(client, auth, 1)
    assert r.status_code == 422 and sku in r.text, r.text
    assert await _carat_decimals(client, auth) == 2

    assert (await client.post(f"/manufacturing/{order}/cancel", headers=auth["headers"], json={})).status_code == 200
    assert (await _set_carat_decimals(client, auth, 1)).status_code == 200


async def test_a_partly_issued_run_blocks_only_by_what_it_still_has_to_issue(client, session, auth):
    order, _, used = await _carat_run(client, auth, 1, input_qty=1.25, stock=10.25)
    assert (await issue(client, auth, order)).status_code == 200
    assert (await _set_carat_decimals(client, auth, 1)).status_code == 200
    assert (await _set_carat_decimals(client, auth, 2)).status_code == 200

    order, made, used = await _carat_run(client, auth, 1, input_qty=1.25, stock=10.5)
    assert (await issue(client, auth, order, [(used, 0.5)])).status_code == 200
    r = await _set_carat_decimals(client, auth, 1)
    assert r.status_code == 422 and await _sku(session, auth, made) in r.text, r.text


async def test_duplicate_keeps_the_value_top_level():
    from ui.routes.inventory import _duplicate_payload

    payload = _duplicate_payload({"sku": "A", "name": "A", "sell_by": "carat", "allow_splitting": True,
                                  "default_sell_qty": 2, "attributes": {}}, "NEW", can_set_prices=True)
    assert payload["default_sell_qty"] == 2 and payload["allow_splitting"] is True, payload
    assert "default_sell_qty" not in (payload.get("attributes") or {})


async def test_an_edit_keeps_the_value_top_level(client, session, auth):
    lot = await _lot(client, auth)
    r = await _patch(client, auth, lot, default_sell_qty=2)
    assert r.status_code == 200, r.text
    assert await _sell_qty(session, auth, lot) == 2


# Validation, the same on every write path

@pytest.mark.parametrize("value", [0, -1, "abc", "NaN", "inf", "-inf", True, False])
async def test_create_refuses_a_value_that_is_not_full_or_above_zero(client, auth, value):
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": f"DSQ-{uuid.uuid4().hex[:6]}", "name": "Parcel", "quantity": 1, "sell_by": "carat",
        "default_sell_qty": value})
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
async def test_a_json_nan_or_infinity_is_refused_not_a_server_error(client, auth, raw):
    lot = await _lot(client, auth)
    body = '{"fields_changed": {"default_sell_qty": {"old": null, "new": %s}}}' % raw
    r = await client.patch(f"/items/{lot}", headers={**auth["headers"], "Content-Type": "application/json"},
                           content=body)
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("value", [0, -1, "abc", "inf", True])
async def test_edit_refuses_a_value_that_is_not_full_or_above_zero(client, session, auth, value):
    lot = await _lot(client, auth, default_sell_qty=2)
    assert (await _patch(client, auth, lot, default_sell_qty=value)).status_code == 422
    assert await _sell_qty(session, auth, lot) == 2


async def test_a_fraction_on_a_piece_item_is_refused(client, auth):
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": f"DSQ-{uuid.uuid4().hex[:6]}", "name": "Screws", "quantity": 10, "sell_by": "piece",
        "default_sell_qty": 0.5})
    assert r.status_code == 422, r.text


async def test_full_in_any_case_is_stored_lower_case(client, session, auth):
    lot = await _lot(client, auth, default_sell_qty="FULL")
    assert await _sell_qty(session, auth, lot) == "full"
    assert (await _patch(client, auth, lot, default_sell_qty=" Full ")).status_code == 200
    assert await _sell_qty(session, auth, lot) == "full"


async def test_a_blank_value_clears_it(client, session, auth):
    lot = await _lot(client, auth)
    assert (await _patch(client, auth, lot, default_sell_qty=2)).status_code == 200
    assert await _sell_qty(session, auth, lot) == 2
    assert (await _patch(client, auth, lot, default_sell_qty="")).status_code == 200
    assert await _sell_qty(session, auth, lot) is None


async def test_a_unit_change_that_invalidates_the_stored_number_is_refused(client, session, auth):
    lot = await _lot(client, auth, default_sell_qty=0.25)
    r = await client.patch(f"/items/{lot}", headers=auth["headers"], json={
        "fields_changed": {"sell_by": {"old": "carat", "new": "piece"}}})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message_key"] == "item.default_sell_qty_unit_change", r.text
    assert "Change Default Sell Qty" in r.json()["detail"]["message"], r.text
    assert (await _state(session, auth, lot))["sell_by"] == "carat"
    assert await _sell_qty(session, auth, lot) == 0.25


async def test_an_item_with_no_known_unit_accepts_any_positive_number(client, session, auth):
    entity_id = f"item:{uuid.uuid4()}"
    await emit_event(session, company_id=auth["company_id"], entity_id=entity_id, entity_type="item",
                     event_type="item.created", data={"status": "available", "sku": "LOOSE", "name": "Loose",
                                                      "quantity": 1, "sell_by": "bundle", "allow_splitting": True},
                     actor_id=None, location_id=None, source="test", idempotency_key=str(uuid.uuid4()), metadata_={})
    await session.flush()
    assert (await _patch(client, auth, entity_id, default_sell_qty=0.123)).status_code == 200
    assert await _sell_qty(session, auth, entity_id) == 0.123


# A unit's decimal places lowered under quantities stored in it

async def _set_carat_decimals(client, auth, decimals: int):
    units = (await client.get("/companies/me/units", headers=auth["headers"])).json()
    units = [{**u, "decimals": decimals} if u["name"] == "carat" else u for u in units]
    return await client.put("/companies/me/units", headers=auth["headers"], json={"units": units})


async def _carat_decimals(client, auth) -> int:
    units = (await client.get("/companies/me/units", headers=auth["headers"])).json()
    return next(u["decimals"] for u in units if u["name"] == "carat")


async def _sku(session, auth, item_id: str) -> str:
    return (await _state(session, auth, item_id))["sku"]


async def _carat_doc(session, auth, status: str, qty: float, number: str, doc_type: str = "invoice",
                     entity_type: str = "doc", **state) -> None:
    kind = {"doc_type": doc_type} if entity_type == "doc" else {"list_type": doc_type}
    await emit_event(session, company_id=auth["company_id"], entity_id=f"{entity_type}:{uuid.uuid4()}",
                     entity_type=entity_type, event_type=f"{entity_type}.created",
                     data={**kind, "doc_number": number, "status": status, **state,
                           "line_items": [{"name": "Stone", "quantity": qty, "unit": "carat", "unit_price": 10}]},
                     actor_id=None, location_id=None, source="test", idempotency_key=str(uuid.uuid4()), metadata_={})
    await session.commit()


@pytest.mark.parametrize("fields", [{"qty": 0.27}, {"qty": 6, "default_sell_qty": 0.27}])
async def test_fewer_decimals_under_a_finer_item_quantity_is_refused_and_names_it(client, session, auth, fields):
    lot = await _lot(client, auth, **fields)
    await _lot(client, auth, qty=6, default_sell_qty=0.5)

    r = await _set_carat_decimals(client, auth, 1)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "units.precision_lowered_under_quantities", detail
    assert detail["params"] == {"unit": "carat", "decimals": 1,
                                "examples": await _sku(session, auth, lot), "current": 2}, detail
    assert detail["message"] == (f"carat cannot go to 1 decimal places: items or open documents still hold "
                                 f"quantities with more ({await _sku(session, auth, lot)}). Change those quantities "
                                 f"or finish those documents first, or keep 2 decimal places."), detail
    assert await _carat_decimals(client, auth) == 2
    assert await _sell_qty(session, auth, lot) == fields.get("default_sell_qty")


async def _template(client, session, auth, doc_type: str, qty: float, status: str = "active",
                    name: str = "Monthly stones") -> str:
    lot = await _lot(client, auth, qty=1)
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": doc_type, "contact_id": "customer:1", "frequency": "monthly", "start_date": "2026-01-01",
        "name": name, "line_items": [{"item_id": lot, "sku": await _sku(session, auth, lot), "name": "Stone",
                                      "quantity": qty, "unit": "carat", "unit_price": 10}]})
    assert r.status_code == 200, r.text
    sub = r.json()["id"]
    assert (await client.post(f"/subscriptions/{sub}/activate", headers=auth["headers"])).status_code == 200
    if status == "paused":
        assert (await client.post(f"/subscriptions/{sub}/pause", headers=auth["headers"])).status_code == 200
    return sub


# Each refusal's advice, followed exactly, ends in the lower it refused.
@pytest.mark.parametrize("doc_type", ["subscription_invoice", "subscription_po"])
@pytest.mark.parametrize("status", ["active", "paused"])
async def test_a_subscription_blocking_fewer_decimals_says_to_cancel_it_and_set_up_a_new_one(
        client, session, auth, doc_type, status):
    assert (await _set_carat_decimals(client, auth, 3)).status_code == 200
    sub = await _template(client, session, auth, doc_type, 0.275, status)

    r = await _set_carat_decimals(client, auth, 2)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "units.precision_lowered_under_templates", detail
    ref = (await _state(session, auth, sub))["ref_id"]
    assert detail["params"] == {"unit": "carat", "decimals": 2, "examples": ref, "current": 3}, detail
    assert detail["message"] == (f"carat cannot go to 2 decimal places: subscriptions still list quantities with "
                                 f"more ({ref}). Cancel them and set up new ones with quantities carat "
                                 "allows, or keep 3 decimal places."), detail
    assert await _carat_decimals(client, auth) == 3
    # An active or paused template's lines cannot be edited, so the refusal never says to change them.
    line = {**(await _state(session, auth, sub))["line_items"][0], "quantity": 0.27}
    r = await client.patch(f"/docs/{sub}", headers=auth["headers"], json={"expected_version": await doc_version(client, auth["headers"], sub), 
        "fields_changed": {"line_items": {"old": None, "new": [line]}}})
    assert r.status_code == 409, r.text

    assert (await client.post(f"/subscriptions/{sub}/cancel", headers=auth["headers"])).status_code == 200
    assert (await _set_carat_decimals(client, auth, 2)).status_code == 200
    new = await _template(client, session, auth, doc_type, 0.27, status, name="Monthly stones again")
    assert (await _state(session, auth, new))["status"] == status
    assert await _carat_decimals(client, auth) == 2


async def test_items_and_subscriptions_blocking_fewer_decimals_are_named_with_the_way_out_of_each(
        client, session, auth):
    assert (await _set_carat_decimals(client, auth, 3)).status_code == 200
    lot = await _lot(client, auth, qty=0.275)
    sku = await _sku(session, auth, lot)
    sub = await _template(client, session, auth, "subscription_invoice", 0.125)

    r = await _set_carat_decimals(client, auth, 2)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "units.precision_lowered_under_quantities_and_templates", detail
    ref = (await _state(session, auth, sub))["ref_id"]
    assert detail["params"] == {"unit": "carat", "decimals": 2, "examples": sku, "templates": ref,
                                "current": 3}, detail
    assert detail["message"] == (f"carat cannot go to 2 decimal places: items or open documents ({sku}) and "
                                 f"subscriptions ({ref}) still hold quantities with more. Change those "
                                 f"quantities or finish those documents, and cancel those subscriptions and set up "
                                 f"new ones with quantities carat allows. Or keep 3 decimal places."), detail

    assert (await client.post(f"/items/{lot}/adjust", headers=auth["headers"], json={"new_qty": 0.27})).status_code == 200
    r = await _set_carat_decimals(client, auth, 2)
    assert r.json()["detail"]["message_key"] == "units.precision_lowered_under_templates", r.text
    assert (await client.post(f"/subscriptions/{sub}/cancel", headers=auth["headers"])).status_code == 200
    assert (await _set_carat_decimals(client, auth, 2)).status_code == 200


async def test_a_paid_invoice_blocking_fewer_decimals_is_finished_by_shipping_it(client, session, auth):
    assert (await _set_carat_decimals(client, auth, 3)).status_code == 200
    lot = await _lot(client, auth, qty=0.275)
    sku = await _sku(session, auth, lot)
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "contact_id": "customer:1", "line_items": [
            {"item_id": lot, "sku": sku, "name": "Stone", "quantity": 0.275, "unit": "carat", "unit_price": 10}]})
    assert r.status_code == 200, r.text
    invoice = r.json()["id"]
    assert (await client.post(f"/docs/{invoice}/finalize", headers=auth["headers"])).status_code == 200
    total = (await _state(session, auth, invoice))["total"]
    r = await client.post(f"/docs/{invoice}/payment", headers=auth["headers"], json={
        "amount": total, "payment_date": "2026-10-01", "bank_account": "1111"})
    assert r.status_code == 200, r.text
    number = (await _state(session, auth, invoice))["doc_number"]

    r = await _set_carat_decimals(client, auth, 2)
    assert r.status_code == 422, r.text
    assert set(r.json()["detail"]["params"]["examples"].split(", ")) == {sku, number}, r.text
    assert "Change those quantities or finish those documents first" in r.json()["detail"]["message"], r.text
    # A paid invoice's lines cannot be changed: it does not go back to draft.
    assert (await client.post(f"/docs/{invoice}/revert-to-draft", headers=auth["headers"], json={})).status_code == 409

    r = await client.post(f"/docs/{invoice}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": [lot]})
    assert r.status_code == 200, r.text
    r = await _set_carat_decimals(client, auth, 2)
    assert r.json()["detail"]["params"]["examples"] == sku, r.text
    assert (await client.post(f"/items/{lot}/adjust", headers=auth["headers"], json={"new_qty": 0.27})).status_code == 200
    assert (await _set_carat_decimals(client, auth, 2)).status_code == 200


@pytest.mark.parametrize("doc_type", ["purchase_order", "bill"])
async def test_a_partly_received_document_blocking_fewer_decimals_is_finished_by_returning_and_issuing_again(
        client, session, auth, doc_type):
    """The supplier sent 0.1 of 0.275 ct and will send no more. Such a document cannot be closed,
    voided or edited while it holds received goods, so the refusal names the route that finishes
    it, and following it ends in the lower with only what arrived in stock."""
    assert (await _set_carat_decimals(client, auth, 3)).status_code == 200
    lot = await _lot(client, auth, qty=1)
    line = {"item_id": lot, "sku": await _sku(session, auth, lot), "name": "Stone", "unit": "carat",
            "sell_by": "carat", "unit_price": 10.0}
    doc = await _doc(client, auth, doc_type, [{**line, "quantity": 0.275}])
    await _finalize(client, auth, doc)
    r = await _receive(client, auth, doc, {"po_line_index": 0, "item_id": lot, "quantity_received": 0.1})
    assert r.status_code == 200, r.text
    state = await _state(session, auth, doc)
    number = state.get("doc_number") or state["ref_id"]

    r = await _set_carat_decimals(client, auth, 2)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "units.precision_lowered_under_partly_received", detail
    assert detail["params"] == {"unit": "carat", "decimals": 2, "examples": number, "current": 3}, detail
    assert ("While what arrived is still in stock, send it back with Return Goods, void the document, and issue it "
            "again for what arrived. Otherwise, receive the rest and send that back with Return Goods.") \
        in detail["message"], detail
    # None of the plain ways out finishes it.
    assert (await client.post(f"/docs/{doc}/void", headers=auth["headers"], json={})).status_code == 409
    assert (await client.post(f"/docs/{doc}/close", headers=auth["headers"], json={})).status_code == 422

    [arrived] = state["received_item_ids"]
    r = await client.post(f"/docs/{doc}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": arrived, "quantity_returned": 0.1}]})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{doc}/void", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    again = await _doc(client, auth, doc_type, [{**line, "quantity": 0.1}])
    await _finalize(client, auth, again)
    r = await _receive(client, auth, again, {"po_line_index": 0, "item_id": lot, "quantity_received": 0.1})
    assert r.status_code == 200, r.text
    assert (await _set_carat_decimals(client, auth, 2)).status_code == 200
    # Only what arrived is in stock: the issued-again document's 0.1, nothing for the 0.175 that never came.
    [kept] = (await _state(session, auth, again))["received_item_ids"]
    lots = {i: ((held := await _state(session, auth, i))["quantity"], held.get("status"))
            for i in (lot, arrived, kept)}
    assert lots == {lot: (1, "available"), arrived: (0.1, "disposed"), kept: (0.1, "available")}, lots


async def test_a_partly_received_document_beside_a_subscription_names_both_ways_out(client, session, auth):
    assert (await _set_carat_decimals(client, auth, 3)).status_code == 200
    lot = await _lot(client, auth, qty=1)
    po = await _doc(client, auth, "purchase_order", [{"item_id": lot, "sku": await _sku(session, auth, lot),
                                                      "name": "Stone", "quantity": 0.275, "unit": "carat",
                                                      "unit_price": 10.0}])
    await _finalize(client, auth, po)
    assert (await _receive(client, auth, po, {"po_line_index": 0, "item_id": lot,
                                              "quantity_received": 0.1})).status_code == 200
    sub = await _template(client, session, auth, "subscription_invoice", 0.125)
    state = await _state(session, auth, po)

    r = await _set_carat_decimals(client, auth, 2)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "units.precision_lowered_under_partly_received_and_templates", detail
    assert detail["params"] == {"unit": "carat", "decimals": 2,
                                "examples": state.get("doc_number") or state["ref_id"],
                                "templates": (await _state(session, auth, sub))["ref_id"], "current": 3}, detail
    assert "cancel those subscriptions" in detail["message"] and "Return Goods" in detail["message"], detail
    # A document that received in full, or nothing yet, keeps the plain advice.
    assert (await _receive(client, auth, po, {"po_line_index": 0, "item_id": lot,
                                              "quantity_received": 0.175})).status_code == 200
    unreceived = await _doc(client, auth, "purchase_order", [{"item_id": lot, "name": "Stone", "quantity": 0.275,
                                                              "unit": "carat", "unit_price": 10.0}])
    await _finalize(client, auth, unreceived)
    r = await _set_carat_decimals(client, auth, 2)
    assert r.json()["detail"]["message_key"] == "units.precision_lowered_under_quantities_and_templates", r.text


async def _short_shipped(client, session, auth, doc_type: str) -> tuple[str, dict, str]:
    """A purchase order or bill for 0.275 ct of which 0.1 ct arrived and no more will, while carat
    has 3 decimal places. -> (the catalog lot, the line, the document)"""
    assert (await _set_carat_decimals(client, auth, 3)).status_code == 200
    lot = await _lot(client, auth, qty=1)
    line = {"item_id": lot, "sku": await _sku(session, auth, lot), "name": "Stone", "unit": "carat",
            "sell_by": "carat", "unit_price": 10.0}
    doc = await _doc(client, auth, doc_type, [{**line, "quantity": 0.275}])
    await _finalize(client, auth, doc)
    r = await _receive(client, auth, doc, {"po_line_index": 0, "item_id": lot, "quantity_received": 0.1})
    assert r.status_code == 200, r.text
    r = await _set_carat_decimals(client, auth, 2)
    assert r.json()["detail"]["message_key"] == "units.precision_lowered_under_partly_received", r.text
    return lot, line, doc


async def _bought_once(client, auth, doc_type: str, line: dict, lot: str, qty: float) -> str:
    doc = await _doc(client, auth, doc_type, [{**line, "quantity": qty}])
    await _finalize(client, auth, doc)
    r = await _receive(client, auth, doc, {"po_line_index": 0, "item_id": lot, "quantity_received": qty})
    assert r.status_code == 200, r.text
    return doc


async def _return_void_issue_again(client, session, auth, doc_type: str, doc: str, line: dict, lot: str) -> None:
    """The way out while what arrived is in stock: Return Goods, void, issue again for what arrived."""
    [arrived] = (await _state(session, auth, doc))["received_item_ids"]
    assert (await _return(client, auth, doc, arrived, 0.1)).status_code == 200
    r = await client.post(f"/docs/{doc}/void", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    await _bought_once(client, auth, doc_type, line, lot, 0.1)


async def _pay(client, auth, doc: str, amount: float) -> None:
    r = await client.post(f"/docs/{doc}/payment", headers=auth["headers"], json={
        "amount": amount, "method": "cash", "payment_date": "2026-10-09", "bank_account": "1111"})
    assert r.status_code == 200, r.text


async def _sell(client, session, auth, item: str, qty: float) -> None:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "contact_id": "customer:1", "line_items": [
            {"item_id": item, "sku": await _sku(session, auth, item), "name": "Stone", "quantity": qty,
             "unit": "carat", "unit_price": 30.0}]})
    assert r.status_code == 200, r.text
    invoice = r.json()["id"]
    assert (await client.post(f"/docs/{invoice}/finalize", headers=auth["headers"])).status_code == 200
    r = await client.post(f"/docs/{invoice}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": [item]})
    assert r.status_code == 200, r.text


def _moved(before: dict[str, float], after: dict[str, float]) -> dict[str, float]:
    return {code: round(after.get(code, 0) - before.get(code, 0), 2) for code in {*before, *after}
            if round(after.get(code, 0) - before.get(code, 0), 2)}


@pytest.mark.parametrize("paid", [False, True])
@pytest.mark.parametrize("doc_type", ["purchase_order", "bill"])
async def test_returning_voiding_and_issuing_again_books_only_what_arrived(client, session, auth, doc_type, paid):
    before = await _trial_balance(client, auth)
    lot, line, doc = await _short_shipped(client, session, auth, doc_type)
    if paid:  # for what arrived, the usual state of a short-shipped bill
        await _pay(client, auth, doc, 1.0)
    await _return_void_issue_again(client, session, auth, doc_type, doc, line, lot)
    assert (await _set_carat_decimals(client, auth, 2)).status_code == 200
    route = _moved(before, await _trial_balance(client, auth))
    base = await _trial_balance(client, auth)
    once = await _bought_once(client, auth, doc_type, line, lot, 0.1)
    if paid:
        await _pay(client, auth, once, 1.0)
    assert route == _moved(base, await _trial_balance(client, auth)), route


@pytest.mark.parametrize("doc_type", ["purchase_order", "bill"])
async def test_once_what_arrived_is_sold_receiving_the_rest_and_returning_it_finishes_the_document(
        client, session, auth, doc_type):
    """Return Goods cannot send back goods already sold, so the refusal names the other way out:
    receive the rest and send that back. The books end as one purchase of what arrived, sold, and
    the lot sent back (written off as it is) never holds the unit."""
    before = await _trial_balance(client, auth)
    lot, line, doc = await _short_shipped(client, session, auth, doc_type)
    assert ("Otherwise, receive the rest and send that back with Return Goods."
            in (await _set_carat_decimals(client, auth, 2)).json()["detail"]["message"])
    [arrived] = (await _state(session, auth, doc))["received_item_ids"]
    await _sell(client, session, auth, arrived, 0.1)
    assert (await _return(client, auth, doc, arrived, 0.1)).status_code == 409  # sold, not on hand

    r = await _receive(client, auth, doc, {"po_line_index": 0, "item_id": lot, "quantity_received": 0.175})
    assert r.status_code == 200, r.text
    [rest] = [i for i in (await _state(session, auth, doc))["received_item_ids"] if i != arrived]
    assert (await _return(client, auth, doc, rest, 0.175)).status_code == 200
    assert (await _set_carat_decimals(client, auth, 2)).status_code == 200
    route = _moved(before, await _trial_balance(client, auth))
    assert (await _state(session, auth, rest))["status"] == "disposed"

    base = await _trial_balance(client, auth)
    once = await _bought_once(client, auth, doc_type, line, lot, 0.1)
    [bought] = (await _state(session, auth, once))["received_item_ids"]
    await _sell(client, session, auth, bought, 0.1)
    assert route == _moved(base, await _trial_balance(client, auth)), route


async def test_voiding_a_supplier_credit_note_holds_the_goods_it_brings_back_to_their_unit(client, session, auth):
    """Goods sent back are history while they are with the supplier, so they never block fewer
    decimal places; voiding the credit note their return raised brings them back, and is held to
    the unit as it is now, before anything is put back."""
    assert (await _set_carat_decimals(client, auth, 3)).status_code == 200
    lot = await _lot(client, auth, qty=1)
    bill = await _bought_once(client, auth, "bill", {"item_id": lot, "sku": await _sku(session, auth, lot),
                                                     "name": "Stone", "unit": "carat", "unit_price": 10.0}, lot, 0.275)
    await _pay(client, auth, bill, 2.75)
    [arrived] = (await _state(session, auth, bill))["received_item_ids"]
    assert (await _return(client, auth, bill, arrived, 0.125)).status_code == 200
    [returned] = (await _state(session, auth, bill))["returned_items"]
    back = returned.get("returned_lot_id") or arrived
    [note] = [n["id"] for n in await supplier_credits(session, auth, bill)]
    assert (await _set_carat_decimals(client, auth, 2)).status_code == 200

    r = await client.post(f"/docs/{note}/void", headers=auth["headers"], json={})
    _finer_refusal(r, label=await _sku(session, auth, back), qty="0.125")
    assert (await _state(session, auth, back))["status"] == "disposed"
    assert (await _state(session, auth, note))["status"] != "void"
    assert (await _set_carat_decimals(client, auth, 3)).status_code == 200
    r = await client.post(f"/docs/{note}/void", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, back))["status"] == "available"


async def test_split_pieces_are_named_once_in_the_refusal(client, session, auth):
    """A split leaves two items under one SKU; the refusal names it once and keeps its other
    example slots for other holders, in the order they were found."""
    assert (await _set_carat_decimals(client, auth, 3)).status_code == 200
    lot = await _lot(client, auth, qty=1)
    r = await client.post(f"/items/{lot}/split", headers=auth["headers"], json={"children": [{"quantity": 0.275}]})
    assert r.status_code == 200, r.text
    other = await _lot(client, auth, qty=0.125)
    r = await _set_carat_decimals(client, auth, 2)
    assert r.status_code == 422, r.text
    names = r.json()["detail"]["params"]["examples"].split(", ")
    assert sorted(names) == sorted([await _sku(session, auth, lot), await _sku(session, auth, other)]), r.text


async def _new_subscription(client, session, auth, doc_type: str, qty: float) -> str:
    """A template made as New subscription makes it: no name, then its customer and lines on the draft."""
    lot = await _lot(client, auth, qty=1)
    r = await client.post("/docs", headers=auth["headers"], json={"doc_type": doc_type, "frequency": "monthly"})
    assert r.status_code == 200, r.text
    sub = r.json()["id"]
    line = {"item_id": lot, "sku": await _sku(session, auth, lot), "name": "Stone", "quantity": qty,
            "unit": "carat", "unit_price": 10}
    for fields in ({"contact_id": "customer:1", "start_date": "2026-01-01"}, {"line_items": [line]}):
        r = await client.patch(f"/docs/{sub}", headers=auth["headers"], json={
            "fields_changed": {key: {"old": None, "new": value} for key, value in fields.items()},
            "expected_version": await doc_version(client, auth["headers"], sub)})
        assert r.status_code == 200, r.text
    assert (await client.post(f"/subscriptions/{sub}/activate", headers=auth["headers"])).status_code == 200
    assert not (await _state(session, auth, sub)).get("name")
    return sub


async def test_an_unnamed_subscription_is_named_as_the_subscriptions_list_names_it(client, session, auth):
    from fasthtml.common import to_xml

    from ui.routes.subscriptions import _sub_table

    assert (await _set_carat_decimals(client, auth, 3)).status_code == 200
    sub = await _new_subscription(client, session, auth, "subscription_invoice", 0.275)
    ref = (await _state(session, auth, sub))["ref_id"]
    assert ref in to_xml(_sub_table([{**await _state(session, auth, sub), "id": sub}], "sales"))

    r = await _set_carat_decimals(client, auth, 2)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["params"]["examples"] == ref, detail
    assert f"({ref})" in detail["message"], detail

    # Cancelled while carat goes to 2 places, then active again as stored: making its next
    # document is refused under the same name, and one that fits is made with that name in its note.
    fits = await _new_subscription(client, session, auth, "subscription_invoice", 0.27)
    assert (await client.post(f"/subscriptions/{sub}/cancel", headers=auth["headers"])).status_code == 200
    assert (await _set_carat_decimals(client, auth, 2)).status_code == 200
    from celerp.models.projections import Projection
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": sub})
    row.state = {**row.state, "status": "active"}
    await session.commit()
    r = await client.post(f"/subscriptions/{sub}/generate-due", headers=auth["headers"], json={})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "units.template_finer_than_unit", detail
    assert detail["params"]["template"] == ref, detail
    assert detail["message"].startswith(f"Subscription {ref} lists Stone at 0.275 carat"), detail
    r = await client.post(f"/subscriptions/{fits}/generate-due", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    fits_ref = (await _state(session, auth, fits))["ref_id"]
    assert f"Auto-created from subscription template {fits_ref}" in (
        await _state(session, auth, r.json()["doc_id"]))["notes"]


async def test_one_holder_reads_without_a_count(client, session, auth):
    await _lot(client, auth, qty=0.27)
    r = await _set_carat_decimals(client, auth, 1)
    assert r.status_code == 422, r.text
    assert "count" not in r.json()["detail"]["params"], r.text
    assert re.search(r"\d items", r.json()["detail"]["message"]) is None, r.text


async def test_fewer_decimals_that_every_quantity_fits_and_more_decimals_pass(client, session, auth):
    await _lot(client, auth, qty=6.5, default_sell_qty=0.5)
    await _lot(client, auth, sell_by="gram", qty=0.27)

    assert (await _set_carat_decimals(client, auth, 3)).status_code == 200
    assert (await _set_carat_decimals(client, auth, 1)).status_code == 200
    assert await _carat_decimals(client, auth) == 1


# A document blocks while its lines can still be received, shipped, sent back or converted, and a
# subscription template until it is cancelled, since it keeps making documents; one in any other
# status is history.
_STILL_MOVING = [("doc", "invoice", "draft"), ("doc", "invoice", "final"), ("doc", "invoice", "paid"),
                 ("doc", "purchase_order", "sent"), ("doc", "purchase_order", "partially_received"),
                 ("doc", "bill", "received"), ("doc", "memo", "sent"), ("doc", "consignment_in", "final"),
                 ("doc", "consignment_in", "partially_received"), ("doc", "consignment_in", "returned"),
                 ("doc", "quotation", "sent"), ("doc", "quotation", "accepted"), ("list", "transfer", "draft"),
                 ("list", "audit", "finalized"), ("doc", "subscription_invoice", "draft"),
                 ("doc", "subscription_invoice", "active"), ("doc", "subscription_po", "paused")]
# A converted memo or consignment billed what sat in its lots, which are items counted on their own.
_HISTORY = [("doc", "invoice", "void"), ("doc", "memo", "closed"), ("doc", "memo", "converted"),
            ("doc", "quotation", "converted"), ("doc", "quotation", "void"), ("doc", "consignment_in", "converted"),
            ("doc", "credit_note", "final"), ("list", "transfer", "closed"), ("list", "quotation", "void"),
            ("doc", "subscription_po", "cancelled")]


@pytest.mark.parametrize("entity_type, doc_type, status", _STILL_MOVING)
async def test_an_open_document_line_blocks_fewer_decimals(client, session, auth, entity_type, doc_type, status):
    await _carat_doc(session, auth, status, 0.27, "OPEN-1", doc_type, entity_type)
    r = await _set_carat_decimals(client, auth, 1)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == ("units.precision_lowered_under_templates" if doc_type.startswith("subscription")
                                     else "units.precision_lowered_under_quantities"), detail
    assert detail["params"]["examples"] == "OPEN-1", detail
    assert "quantities with more (OPEN-1)" in detail["message"], detail
    assert await _carat_decimals(client, auth) == 2


@pytest.mark.parametrize("entity_type, doc_type, status", _HISTORY)
async def test_a_history_document_line_never_blocks(client, session, auth, entity_type, doc_type, status):
    await _carat_doc(session, auth, status, 0.27, "DONE-1", doc_type, entity_type)
    assert (await _set_carat_decimals(client, auth, 1)).status_code == 200
    assert await _carat_decimals(client, auth) == 1


async def test_a_memo_blocks_what_it_has_still_to_send_and_its_lot_while_out_never_once_converted(
        client, session, auth):
    assert (await _set_carat_decimals(client, auth, 3)).status_code == 200
    lot = await _lot(client, auth, qty=0.275)
    sku = await _sku(session, auth, lot)
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "memo", "contact_id": "customer:1", "line_items": [
            {"item_id": lot, "sku": sku, "name": "Stone", "quantity": 0.275, "unit": "carat", "unit_price": 10}]})
    assert r.status_code == 200, r.text
    memo = r.json()["id"]
    assert (await client.post(f"/docs/{memo}/finalize", headers=auth["headers"])).status_code == 200
    memo_state = await _state(session, auth, memo)
    number = memo_state.get("doc_number") or memo_state["ref_id"]

    async def holders() -> str:
        r = await _set_carat_decimals(client, auth, 2)
        assert r.status_code == 422, r.text
        return r.json()["detail"]["params"]["examples"]

    # Not yet sent: the memo line and the lot both hold 0.275.
    assert set((await holders()).split(", ")) == {number, sku}
    r = await client.post(f"/docs/{memo}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": [lot]})
    assert r.status_code == 200, r.text
    # Out on memo: the lot holds it, the line has nothing left to send.
    assert await holders() == sku
    r = await client.post(f"/docs/{memo}/convert", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, memo))["status"] == "final"
    # The invoice draft carries its lines until it is issued over the lot it already holds.
    invoice = r.json()["target_doc_id"]
    assert (await client.post(f"/docs/{invoice}/finalize", headers=auth["headers"])).status_code == 200
    # Only the lot, which counts in every status but written off or returned, holds 0.275 now: the
    # memo it was invoiced from never does.
    assert await holders() == sku
    assert await _carat_decimals(client, auth) == 3


# A document, list or run that is history never blocks fewer decimal places, so when it moves again
# (unvoided, reopened, back to draft, undone, or copied into a new draft) what it can then move is
# held to its unit as it is that day, before anything is written.
def _finer_refusal(r, label: str = "Stone", qty: str = "0.275") -> None:
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "units.moving_finer_than_unit", detail
    assert detail["params"] == {"label": label, "qty": qty, "unit": "carat", "decimals": 2, "places": 3}, detail
    assert "set carat back to 3 decimal places" in detail["message"], detail


async def _rows(session, auth) -> dict[str, str]:
    """Document and list ids by number."""
    from sqlalchemy import select

    from celerp.models.projections import Projection
    rows = (await session.execute(select(Projection.entity_id, Projection.state).where(
        Projection.company_id == auth["company_id"], Projection.entity_type.in_(("doc", "list"))))).all()
    return {state.get("doc_number"): entity_id for entity_id, state in rows if state.get("doc_number")}


async def _doc_count(session, auth) -> int:
    from sqlalchemy import func, select

    from celerp.models.projections import Projection
    return (await session.execute(select(func.count()).select_from(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type.in_(("doc", "list"))))).scalar_one()


@pytest.mark.parametrize("doc_type", ["invoice", "quotation"])
@pytest.mark.parametrize("qty, refused", [(0.275, True), (0.27, False)])
async def test_an_unvoided_document_is_held_to_its_units_as_they_are_now(client, session, auth, doc_type, qty, refused):
    assert (await _set_carat_decimals(client, auth, 3)).status_code == 200
    lot = await _lot(client, auth, qty=1)
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": doc_type, "contact_id": "customer:1", "line_items": [
            {"item_id": lot, "sku": await _sku(session, auth, lot), "name": "Stone", "quantity": qty, "unit": "carat",
             "unit_price": 10}]})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    assert (await client.post(f"/docs/{doc}/finalize", headers=auth["headers"])).status_code == 200
    assert (await client.post(f"/docs/{doc}/void", headers=auth["headers"], json={})).status_code == 200
    # Void is history: it never blocks fewer decimal places.
    assert (await _set_carat_decimals(client, auth, 2)).status_code == 200

    r = await client.post(f"/docs/{doc}/unvoid", headers=auth["headers"], json={})
    if refused:
        _finer_refusal(r)
        assert (await _state(session, auth, doc))["status"] == "void"
        # The way out the message names: the unit back to 3 places, then it unvoids.
        assert (await _set_carat_decimals(client, auth, 3)).status_code == 200
        r = await client.post(f"/docs/{doc}/unvoid", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, doc))["status"] == "final"
    if doc_type == "invoice":
        r = await client.post(f"/docs/{doc}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": [lot]})
    else:
        r = await client.post(f"/docs/{doc}/convert", headers=auth["headers"])
    assert r.status_code == 200, r.text


# Records written before a precision change looked at them, so what they hold is finer than the unit
# allows today; each way they could move again checks them.
async def test_a_quotation_or_list_copied_into_a_new_draft_is_held_to_its_units(client, session, auth):
    for entity_type, status in (("doc", "final"), ("list", "void"), ("list", FINALIZED)):
        await _carat_doc(session, auth, status, 0.275, f"Q-{entity_type}-{status}", doc_type="quotation",
                         entity_type=entity_type)
    rows = await _rows(session, auth)
    before = await _doc_count(session, auth)
    _finer_refusal(await client.post(f"/docs/{rows['Q-doc-final']}/convert", headers=auth["headers"]))
    _finer_refusal(await client.post(f"/lists/{rows['Q-list-void']}/duplicate", headers=auth["headers"]))
    _finer_refusal(await client.post(f"/lists/{rows[f'Q-list-{FINALIZED}']}/convert", headers=auth["headers"],
                                     json={"target_type": "invoice"}))
    assert await _doc_count(session, auth) == before
    assert (await _state(session, auth, rows["Q-doc-final"]))["status"] == "final"

    # The same records holding what the unit allows copy as before.
    for entity_type, status in (("doc", "final"), ("list", "void"), ("list", FINALIZED)):
        await _carat_doc(session, auth, status, 0.27, f"R-{entity_type}-{status}", doc_type="quotation",
                         entity_type=entity_type)
    rows = await _rows(session, auth)
    assert (await client.post(f"/docs/{rows['R-doc-final']}/convert", headers=auth["headers"])).status_code == 200
    assert (await client.post(f"/lists/{rows['R-list-void']}/duplicate", headers=auth["headers"])).status_code == 200
    r = await client.post(f"/lists/{rows[f'R-list-{FINALIZED}']}/convert", headers=auth["headers"],
                          json={"target_type": "invoice"})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("qty, refused", [(0.275, True), (0.27, False)])
async def test_a_reopened_memo_or_a_document_back_to_draft_is_held_to_its_units(client, session, auth, qty, refused):
    await _carat_doc(session, auth, "closed", qty, "MEMO-1", doc_type="memo")
    await _carat_doc(session, auth, "final", qty, "INV-1")
    rows = await _rows(session, auth)
    for doc, path, back in ((rows["MEMO-1"], "reopen", "final"), (rows["INV-1"], "revert-to-draft", "draft")):
        r = await client.post(f"/docs/{doc}/{path}", headers=auth["headers"], json={})
        if refused:
            _finer_refusal(r)
        else:
            assert r.status_code == 200, r.text
        assert (await _state(session, auth, doc))["status"] == ({"reopen": "closed", "revert-to-draft": "final"}[path]
                                                                if refused else back)


@pytest.mark.parametrize("doc_type", ["subscription_invoice", "subscription_po"])
async def test_a_subscription_never_makes_a_document_finer_than_its_units(client, session, auth, doc_type):
    assert (await _set_carat_decimals(client, auth, 3)).status_code == 200
    lot = await _lot(client, auth, qty=1)
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": doc_type, "contact_id": "customer:1", "frequency": "monthly", "start_date": "2026-01-01",
        "name": "Monthly stones", "line_items": [
            {"item_id": lot, "sku": await _sku(session, auth, lot), "name": "Stone", "quantity": 0.275,
             "unit": "carat", "unit_price": 10}]})
    assert r.status_code == 200, r.text
    sub = r.json()["id"]
    assert (await client.post(f"/subscriptions/{sub}/activate", headers=auth["headers"])).status_code == 200
    # An active template keeps making documents, so it blocks fewer decimal places.
    r = await _set_carat_decimals(client, auth, 2)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message_key"] == "units.precision_lowered_under_templates", r.text
    assert await _carat_decimals(client, auth) == 3

    # A template saved before a precision change looked at it is refused when it makes the next one,
    # naming the subscription, and nothing is made. Stand-in for one the precision check never saw:
    # cancelled while carat goes to 2 places, then active again as it was stored.
    await _carat_doc(session, auth, "cancelled", 0.275, "SUB-OLD", doc_type, name="Old stones", frequency="monthly",
                     start_date="2026-01-01", anchor_date="2026-01-01")
    await _carat_doc(session, auth, "active", 0.27, "SUB-FITS", doc_type, name="Fitting stones",
                     frequency="monthly", start_date="2026-01-01", anchor_date="2026-01-01")
    rows = await _rows(session, auth)
    old, fits = rows["SUB-OLD"], rows["SUB-FITS"]
    assert (await client.post(f"/subscriptions/{sub}/cancel", headers=auth["headers"])).status_code == 200
    assert (await _set_carat_decimals(client, auth, 2)).status_code == 200
    from celerp.models.projections import Projection
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": old})
    row.state = {**row.state, "status": "active"}
    await session.commit()

    before = await _doc_count(session, auth)
    r = await client.post(f"/subscriptions/{old}/generate-due", headers=auth["headers"], json={})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "units.template_finer_than_unit", detail
    assert detail["params"] == {"template": "SUB-OLD", "label": "Stone", "qty": "0.275", "unit": "carat",
                                "decimals": 2, "places": 3}, detail
    assert detail["message"].startswith("Subscription SUB-OLD lists Stone at 0.275 carat"), detail
    assert "set carat back to 3 decimal places" in detail["message"], detail
    assert await _doc_count(session, auth) == before
    assert not (await _state(session, auth, old)).get("periods")

    # A template whose lines fit makes its document as before.
    r = await client.post(f"/subscriptions/{fits}/generate-due", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, r.json()["doc_id"]))["line_items"][0]["quantity"] == 0.27

    # The way out the message names: the unit back to 3 places, then the old template makes its document.
    assert (await _set_carat_decimals(client, auth, 3)).status_code == 200
    r = await client.post(f"/subscriptions/{old}/generate-due", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, r.json()["doc_id"]))["line_items"][0]["quantity"] == 0.275


@pytest.mark.parametrize("qty, refused", [(0.275, True), (0.27, False)])
async def test_a_shipment_copied_from_a_shipped_invoice_is_held_to_its_units(client, session, auth, qty, refused):
    # A paid invoice whose goods all went out is history, so it never blocked the precision change.
    await _carat_doc(session, auth, "paid", qty, "INV-SHIPPED", fulfillment_status="fulfilled",
                     contact_id="customer:1", contact_name="Customer", currency="USD")
    assert (await _set_carat_decimals(client, auth, 2)).status_code == 200
    doc = (await _rows(session, auth))["INV-SHIPPED"]
    before = await _doc_count(session, auth)
    r = await client.post("/docs/shipment", headers=auth["headers"], json={"doc_ids": [doc]})
    if refused:
        _finer_refusal(r)
        assert await _doc_count(session, auth) == before
    else:
        assert r.status_code == 200, r.text
        assert (await _state(session, auth, r.json()["id"]))["line_items"][0]["quantity"] == 0.27


async def test_undoing_a_stock_count_never_puts_back_a_quantity_finer_than_its_unit(client, session, auth):
    assert (await _set_carat_decimals(client, auth, 3)).status_code == 200
    r = await client.post("/companies/me/locations", headers=auth["headers"], json={"name": "Vault", "type": "warehouse"})
    assert r.status_code == 200, r.text
    lot = await _lot(client, auth, qty=0.275, location_id=r.json()["id"])
    r = await client.post("/lists/audit", headers=auth["headers"], json={"location_id": r.json()["id"]})
    assert r.status_code == 200, r.text
    audit = r.json()["id"]
    assert (await client.post(f"/lists/{audit}/finalize", headers=auth["headers"])).status_code == 200
    r = await client.patch(f"/lists/{audit}/line/{lot}", headers=auth["headers"], json={"counted_qty": 0.3})
    assert r.status_code == 200, r.text
    assert (await client.post(f"/lists/{audit}/adjust", headers=auth["headers"])).status_code == 200
    assert (await _state(session, auth, lot))["quantity"] == 0.3
    # The closed count is history and the lot holds 0.3, so carat can go to 2 places.
    assert (await _set_carat_decimals(client, auth, 2)).status_code == 200

    r = await client.post(f"/lists/{audit}/undo-adjust", headers=auth["headers"])
    # The reopened count's line is named as the list shows it.
    _finer_refusal(r, label="Parcel")
    assert (await _state(session, auth, lot))["quantity"] == 0.3
    assert (await _state(session, auth, audit))["status"] == "closed"
    assert (await _set_carat_decimals(client, auth, 3)).status_code == 200
    assert (await client.post(f"/lists/{audit}/undo-adjust", headers=auth["headers"])).status_code == 200
    assert (await _state(session, auth, lot))["quantity"] == 0.275


async def test_completing_a_run_after_part_of_its_output_came_in_receives_the_rest(client, session, auth):
    assert (await _set_carat_decimals(client, auth, 3)).status_code == 200
    order, _, _ = await _carat_run(client, auth, 0.275)
    assert (await issue(client, auth, order)).status_code == 200
    assert (await receive(client, auth, order, 0.1)).status_code == 200
    # The rest is 0.175, which carat allows, never a float remainder finer than it.
    r = await complete(client, auth, order)
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, order))["received_qty"] == 0.275


async def test_a_reopened_run_is_held_to_its_units_as_they_are_now(client, session, auth):
    assert (await _set_carat_decimals(client, auth, 3)).status_code == 200
    # A run whose components were all wasted completes without its 0.175 still to make.
    order, made, _ = await _carat_run(client, auth, 0.275)
    assert (await issue(client, auth, order)).status_code == 200
    assert (await receive(client, auth, order, 0.1)).status_code == 200
    r = await complete(client, auth, order, waste_quantity=1)
    assert r.status_code == 200, r.text
    # A completed run moves nothing more, so carat can go to 2 places under it.
    assert (await _set_carat_decimals(client, auth, 2)).status_code == 200

    r = await reopen(client, auth, order)
    _finer_refusal(r, label=await _sku(session, auth, made), qty="0.175")
    assert (await _state(session, auth, order))["status"] == "completed"
    assert (await _set_carat_decimals(client, auth, 3)).status_code == 200
    assert (await reopen(client, auth, order)).status_code == 200


async def test_an_issued_purchase_order_keeps_its_ordered_quantity_receivable(client, session, auth):
    item = await _lot(client, auth, qty=1)
    po = await _doc(client, auth, "purchase_order", [{"item_id": item, "sku": await _sku(session, auth, item),
                                                      "name": "Stone", "quantity": 0.27, "unit": "carat",
                                                      "sell_by": "carat", "unit_price": 10.0}])
    await _finalize(client, auth, po)
    assert (await _set_carat_decimals(client, auth, 1)).status_code == 422
    r = await _receive(client, auth, po, {"po_line_index": 0, "quantity_received": 0.27})
    assert r.status_code == 200, r.text


# Only what a line can still receive or ship counts: what came in already sits in an item, which
# is counted on its own, and what shipped is history.
async def _carat_items_in_tenths(client, auth) -> None:
    for row in (await client.get("/items", headers=auth["headers"], params={"limit": 500})).json().get("items", []):
        q = float(row.get("quantity") or 0)
        if row.get("sell_by") == "carat" and round(q, 1) != q:
            assert (await _patch(client, auth, row["id"], quantity=round(q + 0.05, 1))).status_code == 200


async def test_a_fully_received_purchase_order_never_blocks_fewer_decimals(client, session, auth):
    item = await _lot(client, auth, qty=1)
    po = await _doc(client, auth, "purchase_order", [{"item_id": item, "sku": await _sku(session, auth, item),
                                                      "name": "Stone", "quantity": 0.27, "unit": "carat",
                                                      "sell_by": "carat", "unit_price": 10.0}])
    await _finalize(client, auth, po)
    assert (await _receive(client, auth, po, {"po_line_index": 0, "quantity_received": 0.27})).status_code == 200
    assert (await _state(session, auth, po))["status"] == "received"
    # The received 0.27 ct sits in an item, which blocks until it is counted in tenths.
    assert (await _set_carat_decimals(client, auth, 1)).status_code == 422
    await _carat_items_in_tenths(client, auth)
    assert (await _set_carat_decimals(client, auth, 1)).status_code == 200, await _carat_decimals(client, auth)


async def test_a_partly_received_purchase_order_line_blocks_with_what_is_left(client, session, auth):
    assert (await _set_carat_decimals(client, auth, 3)).status_code == 200
    item = await _lot(client, auth, qty=1)
    po = await _doc(client, auth, "purchase_order", [{"item_id": item, "sku": await _sku(session, auth, item),
                                                      "name": "Stone", "quantity": 0.275, "unit": "carat",
                                                      "sell_by": "carat", "unit_price": 10.0}])
    await _finalize(client, auth, po)
    assert (await _receive(client, auth, po, {"po_line_index": 0, "quantity_received": 0.1})).status_code == 200
    po_state = await _state(session, auth, po)
    assert po_state["status"] == "partially_received", po_state["status"]
    await _carat_items_in_tenths(client, auth)
    # 0.175 is still to come: finer than 1 or 2 decimal places.
    for decimals in (1, 2):
        r = await _set_carat_decimals(client, auth, decimals)
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["params"]["examples"] == po_state["doc_number"], r.text
    assert await _carat_decimals(client, auth) == 3


async def _invoice_event(session, auth, doc_id: str, number: str, lines: list[dict], **state) -> None:
    await emit_event(session, company_id=auth["company_id"], entity_id=doc_id, entity_type="doc",
                     event_type="doc.created",
                     data={"doc_type": "invoice", "doc_number": number, "status": "paid", **state,
                           "line_items": [{"name": "Stone", "unit": "carat", "unit_price": 10, **line}
                                          for line in lines]},
                     actor_id=None, location_id=None, source="test", idempotency_key=str(uuid.uuid4()), metadata_={})
    await session.commit()


async def test_a_paid_fully_shipped_invoice_never_blocks_fewer_decimals(client, session, auth):
    await _invoice_event(session, auth, f"doc:{uuid.uuid4()}", "PAID-1", [{"quantity": 0.27}],
                         fulfillment_status="fulfilled", fulfilled_items=[{"item_id": None, "quantity": 0.27}])
    assert (await _set_carat_decimals(client, auth, 1)).status_code == 200
    assert await _carat_decimals(client, auth) == 1


@pytest.mark.parametrize("unshipped, blocks", [(0.25, True), (0.3, False)])
async def test_a_partly_shipped_invoice_blocks_only_with_its_unshipped_lines(client, session, auth, unshipped, blocks):
    doc_id = f"doc:{uuid.uuid4()}"
    # The shipped line's lot is sold to this invoice and holds a whole number of tenths.
    shipped = await _lot(client, auth, qty=0.3)
    await emit_event(session, company_id=auth["company_id"], entity_id=shipped, entity_type="item",
                     event_type="item.status.set",
                     data={"new_status": "sold", "source_doc_id": doc_id, "doc_number": "PART-1"},
                     actor_id=None, location_id=None, source="test", idempotency_key=str(uuid.uuid4()), metadata_={})
    await session.commit()
    assert (await _state(session, auth, shipped))["status"] == "sold"
    await _invoice_event(session, auth, doc_id, "PART-1", [{"item_id": shipped, "quantity": 0.27},
                                                           {"quantity": unshipped}], fulfillment_status="partial")

    r = await _set_carat_decimals(client, auth, 1)
    assert r.status_code == (422 if blocks else 200), r.text
    if blocks:
        assert r.json()["detail"]["params"]["examples"] == "PART-1", r.text
    assert await _carat_decimals(client, auth) == (2 if blocks else 1)


@pytest.mark.parametrize("by", ["item_id", "sku"])
async def test_a_line_with_no_unit_is_checked_in_its_items_unit(client, session, auth, by):
    carat = await _lot(client, auth, qty=6)
    gram = await _lot(client, auth, sell_by="gram", qty=6)
    ref = {"item_id": lambda i: {"item_id": i}, "sku": lambda i: {"sku": i}}[by]
    lines = [{**ref(item if by == "item_id" else await _sku(session, auth, item)),
              "name": "Stone", "quantity": 0.27, "unit_price": 10.0} for item in (carat, gram)]
    doc = await _doc(client, auth, "invoice", lines)
    stored = (await _state(session, auth, doc))["line_items"]
    assert not any(line.get("unit") or line.get("sell_by") for line in stored), stored

    r = await _set_carat_decimals(client, auth, 1)
    assert r.status_code == 422, r.text
    assert ", " not in r.json()["detail"]["params"]["examples"], r.text  # one holder: the document
    assert await _carat_decimals(client, auth) == 2
    # The gram item's line counts once, under gram.
    assert (await client.put("/companies/me/units", headers=auth["headers"], json={"units": [
        {**u, "decimals": 1} if u["name"] == "gram" else u for u in await _units(client, auth)]})).status_code == 422

async def _units(client, auth) -> list[dict]:
    return (await client.get("/companies/me/units", headers=auth["headers"])).json()


async def _put_units(client, auth, units: list[dict]):
    return await client.put("/companies/me/units", headers=auth["headers"], json={"units": units})


async def _store_units(session, auth, units: list[dict]) -> None:
    """Units stored as they are, as data saved before removing a unit in use was refused can hold
    them: an item sold by a unit that is no longer one of the company's."""
    company = await locked_company(session, auth["company_id"])
    company.settings = {**company.settings, "units": units}
    await session.commit()


async def test_a_unit_deleted_then_added_back_is_checked_against_what_it_holds(client, session, auth):
    lot = await _lot(client, auth, qty=0.27)
    units = await _units(client, auth)
    carat = next(u for u in units if u["name"] == "carat")
    without = [u for u in units if u["name"] != "carat"]
    await _store_units(session, auth, without)

    r = await _put_units(client, auth, [*without, {**carat, "decimals": 1}])
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["params"] == {"unit": "carat", "decimals": 1,
                                            "examples": await _sku(session, auth, lot), "current": 2}, r.text
    assert all(u["name"] != "carat" for u in await _units(client, auth))
    # Back at a precision that holds the stored quantity, and a new unit nothing uses, both pass.
    assert (await _put_units(client, auth, [*without, carat])).status_code == 200
    assert (await _put_units(client, auth, [*await _units(client, auth), {**carat, "name": "tola", "decimals": 0}])
            ).status_code == 200


async def _business_type(client, session, auth, ids):
    """The business-type change, which only the owner role may make, run as the company admin's actor."""
    from celerp.services.business_type import set_business_type

    try:
        return await set_business_type(session, auth["company_id"], ids["user_id"], "gemstones")
    except HTTPException as exc:
        return exc


_SEEDS = {
    "apply-category": lambda client, session, auth, ids: client.post(
        "/companies/me/apply-category", params={"name": "diamond"}, headers=auth["headers"]),
    "apply-preset": lambda client, session, auth, ids: client.post(
        "/companies/me/apply-preset", params={"vertical": "gemstones"}, headers=auth["headers"]),
    "business-type": _business_type,
}


@pytest.mark.parametrize("seed", list(_SEEDS))
async def test_a_category_seeding_a_deleted_unit_back_is_checked_against_what_it_holds(client, session, auth, ids, seed):
    finer = [{**u, "decimals": 3} if u["name"] == "gram" else u for u in await _units(client, auth)]
    assert (await _put_units(client, auth, finer)).status_code == 200
    lot = await _lot(client, auth, sell_by="gram", qty=0.275)
    company = await locked_company(session, auth["company_id"])
    company.settings = {**company.settings, "units": [u for u in finer if u["name"] != "gram"], **{
        # gram gone while the lot holds it, and the category new to the company
        key: {k: v for k, v in (company.settings.get(key) or {}).items() if k != "diamond"}
        for key in ("category_schemas", "category_display_names")}}
    await session.commit()

    r = await _SEEDS[seed](client, session, auth, ids)
    status, detail = (r.status_code, r.detail) if isinstance(r, HTTPException) else (r.status_code, r.json()["detail"])
    assert status == 422, detail
    assert (detail["params"]["examples"], detail["params"]["current"]) == (await _sku(session, auth, lot), 3), detail
    assert all(u["name"] != "gram" for u in await _units(client, auth))
    assert (await _state(session, auth, lot))["quantity"] == 0.275


# A unit removed, or its type changed, while items or open records still use it

def _without_carat(units: list[dict]) -> list[dict]:
    return [u for u in units if u["name"] != "carat"]


def _carat_as_pieces(units: list[dict]) -> list[dict]:
    return [{**u, "unit_type": "pieces"} if u["name"] == "carat" else u for u in units]


_CARAT_CHANGES = {"removed": (_without_carat, "units.unit_removed_in_use"),
                  "retyped": (_carat_as_pieces, "units.unit_type_changed_in_use")}


async def _scanned(client, auth, sku: str) -> float:
    """What one scan of ``sku`` adds to a new invoice."""
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "contact_id": "customer:1", "line_items": []})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{r.json()['id']}/scan", headers=auth["headers"], json={"barcode": sku})
    assert r.status_code == 200 and r.json()["scanned"] == 1, r.text
    return r.json()["results"][0]["quantity"]


@pytest.mark.parametrize("change", list(_CARAT_CHANGES))
async def test_a_unit_an_item_is_sold_by_cannot_be_removed_or_retyped_so_its_scan_adds_the_same(
        client, session, auth, change):
    """A splittable 3.50 ct lot with no Default Sell Qty: a scan adds all 3.5. Removing carat, or
    making it a piece unit, would make the same scan add 1."""
    sku = await _sku(session, auth, await _lot(client, auth, qty=3.5))
    assert await _scanned(client, auth, sku) == 3.5
    edit, key = _CARAT_CHANGES[change]
    units = await _units(client, auth)

    r = await _put_units(client, auth, edit(units))
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == key, detail
    assert detail["params"]["unit"] == "carat" and sku in detail["params"]["examples"].split(", "), detail
    assert detail["message"].startswith(f"carat cannot {'be removed' if change == 'removed' else 'change its type'}: "
                                        f"items, open documents or subscriptions still use it ("), detail
    assert await _units(client, auth) == units
    assert await _scanned(client, auth, sku) == 3.5


async def test_a_unit_items_are_weighed_in_cannot_be_removed(client, session, auth):
    """Splitting reads the weight unit's decimal places."""
    lot = await _lot(client, auth, sell_by="piece", qty=1, weight_unit="carat")
    r = await _put_units(client, auth, _without_carat(await _units(client, auth)))
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["params"] == {"unit": "carat", "examples": await _sku(session, auth, lot)}, r.text


@pytest.mark.parametrize("change", list(_CARAT_CHANGES))
async def test_the_way_out_named_gives_the_items_another_unit_then_the_change_goes_through(
        client, session, auth, change):
    """Selling by gram takes the quantity from the lot's weight in grams."""
    lot = await _lot(client, auth, qty=3.5, weight=0.7, weight_unit="gram")
    edit, _key = _CARAT_CHANGES[change]
    assert (await _put_units(client, auth, edit(await _units(client, auth)))).status_code == 422
    assert (await _patch(client, auth, lot, sell_by="gram")).status_code == 200
    assert (await _put_units(client, auth, edit(await _units(client, auth)))).status_code == 200
    assert await _scanned(client, auth, await _sku(session, auth, lot)) == 0.7


@pytest.mark.parametrize("entity_type, doc_type, status", _STILL_MOVING)
async def test_an_open_document_line_blocks_removing_its_unit(client, session, auth, entity_type, doc_type, status):
    await _carat_doc(session, auth, status, 0.25, "OPEN-1", doc_type, entity_type)
    r = await _put_units(client, auth, _without_carat(await _units(client, auth)))
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["params"] == {"unit": "carat", "examples": "OPEN-1"}, r.text


@pytest.mark.parametrize("entity_type, doc_type, status", _HISTORY)
async def test_a_history_document_line_never_blocks_removing_its_unit(client, session, auth, entity_type, doc_type,
                                                                      status):
    await _carat_doc(session, auth, status, 0.25, "DONE-1", doc_type, entity_type)
    assert (await _put_units(client, auth, _without_carat(await _units(client, auth)))).status_code == 200


async def test_a_written_off_lot_never_blocks_a_units_change(client, session, auth):
    assert (await _set_carat_decimals(client, auth, 3)).status_code == 200
    lot = await _lot(client, auth, qty=0.275)
    await emit_event(session, company_id=auth["company_id"], entity_id=lot, entity_type="item",
                     event_type="item.written_off", data={"account": "5900", "qty": 0.275, "unit_cost": 0.0, "cost_total": 0.0,
                           "source_list_id": "list:w", "doc_number": "WO-1"},
                     actor_id=None, location_id=None, source="test", idempotency_key=str(uuid.uuid4()), metadata_={})
    await session.commit()
    assert (await _set_carat_decimals(client, auth, 2)).status_code == 200
    assert (await _put_units(client, auth, _without_carat(await _units(client, auth)))).status_code == 200


@pytest.mark.parametrize("change, key", [("removed", "units.unit_removed_under_partly_received"),
                                         ("retyped", "units.unit_type_changed_under_partly_received")])
async def test_a_partly_received_document_blocking_a_unit_change_names_both_ways_out(
        client, session, auth, change, key):
    await _short_shipped(client, session, auth, "purchase_order")
    r = await _put_units(client, auth, _CARAT_CHANGES[change][0](await _units(client, auth)))
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == key, detail
    assert "While what arrived is still in stock" in detail["message"], detail
    assert "Otherwise, receive the rest and send that back with Return Goods." in detail["message"], detail


async def test_a_line_in_its_items_unit_removed_meanwhile_is_refused_when_it_moves_again(client, session, auth):
    """A unit removed before it was checked: the draft its item is on cannot come back from void."""
    lot = await _lot(client, auth, qty=1)
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "contact_id": "customer:1", "line_items": [
            {"item_id": lot, "name": "Stone", "quantity": 0.5, "unit_price": 10}]})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    assert (await client.post(f"/docs/{doc}/void", headers=auth["headers"], json={})).status_code == 200
    units = await _units(client, auth)
    await _store_units(session, auth, _without_carat(units))

    r = await client.post(f"/docs/{doc}/unvoid", headers=auth["headers"], json={})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "units.moving_unit_removed", detail
    assert detail["params"] == {"label": "Stone", "unit": "carat"}, detail
    await _store_units(session, auth, units)
    assert (await client.post(f"/docs/{doc}/unvoid", headers=auth["headers"], json={})).status_code == 200


# Every field of a company unit, changed on a unit in use: refused, or what its stored quantities
# mean (what a scan adds, whether they are accepted) is unchanged. A new field fails
# test_every_unit_field_is_covered until it is added here.
_FIELD_CHANGES = {
    "name": (lambda u: {**u, "name": "carats"}, 422),
    "label": (lambda u: {**u, "label": "Carats"}, 200),
    "decimals up": (lambda u: {**u, "decimals": 3}, 200),
    "decimals down": (lambda u: {**u, "decimals": 1}, 422),
    "unit_type pieces": (lambda u: {**u, "unit_type": "pieces"}, 422),
    "unit_type quantity": (lambda u: {**u, "unit_type": "quantity"}, 422),
}


def test_every_unit_field_is_covered():
    covered = {name.split()[0] for name in _FIELD_CHANGES}
    assert set(UnitRecord.model_fields) == covered, "classify the new unit field in _FIELD_CHANGES"


def _meaning(states: list[dict], unit_map: dict) -> list:
    def accepted(qty, sell_by):
        try:
            return validate_default_sell_qty(qty, sell_by, unit_map)
        except HTTPException as exc:
            return exc.detail["message_key"]
    return [(proposed_sell_qty(state, unit_map), accepted(state["quantity"], state["sell_by"]),
             accepted(0.25, state["sell_by"])) for state in states]


@pytest.mark.parametrize("change", list(_FIELD_CHANGES))
async def test_a_change_to_a_unit_in_use_is_refused_or_keeps_what_its_quantities_mean(client, session, auth, change):
    lots = [await _lot(client, auth, qty=3.55), await _lot(client, auth, qty=6, default_sell_qty=0.25)]
    await _carat_doc(session, auth, "draft", 0.25, "OPEN-G")
    units = await _units(client, auth)
    states = [await _state(session, auth, lot) for lot in lots]
    before = _meaning(states, build_unit_map(units))
    edit, expected = _FIELD_CHANGES[change]

    r = await _put_units(client, auth, [edit(u) if u["name"] == "carat" else u for u in units])
    assert r.status_code == expected, r.text
    after = await _units(client, auth)
    if r.status_code == 422:
        assert after == units
    else:
        assert _meaning(states, build_unit_map(after)) == before


# CSV import and export

@pytest.fixture
async def shelf(client, auth) -> str:
    r = await client.post("/companies/me/locations", headers=auth["headers"], json={"name": "Shelf", "type": "warehouse"})
    assert r.status_code == 200, r.text
    return "Shelf"


async def _import(client, auth, rows: list[dict], upsert: bool = False):
    return await client.post("/items/import/rows", headers=auth["headers"], json={
        "rows": rows, "upsert": upsert, "idempotency_key": f"dsq-{uuid.uuid4().hex[:8]}"})


async def _by_sku(session, auth, sku: str) -> dict:
    from sqlalchemy import select
    from celerp.models.projections import Projection

    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "item"))).scalars().all()
    [state] = [p.state for p in rows if p.state.get("sku") == sku]
    return state


async def test_csv_import_stores_valid_values_and_a_blank_cell_clears(client, session, auth, shelf):
    skus = [f"DSQ-C{i}-{uuid.uuid4().hex[:4]}" for i in range(2)]
    r = await _import(client, auth, [
        {"sku": skus[0], "name": "A", "sell_by": "carat", "quantity": "6", "location_name": shelf, "default_sell_qty": "FULL"},
        {"sku": skus[1], "name": "B", "sell_by": "carat", "quantity": "6", "location_name": shelf, "default_sell_qty": "2.5"},
    ])
    assert r.status_code == 200, r.text
    assert (await _by_sku(session, auth, skus[0]))["default_sell_qty"] == "full"
    assert (await _by_sku(session, auth, skus[1]))["default_sell_qty"] == 2.5

    r = await _import(client, auth, [{"sku": skus[1], "name": "B", "default_sell_qty": ""}], upsert=True)
    assert r.status_code == 200, r.text
    assert (await _by_sku(session, auth, skus[1])).get("default_sell_qty") is None


@pytest.mark.parametrize("value", ["0", "-1", "abc", "inf", "0.5"])
async def test_csv_import_refuses_an_invalid_value(client, auth, shelf, value):
    row = {"sku": f"DSQ-X-{uuid.uuid4().hex[:6]}", "name": "X", "sell_by": "piece", "quantity": "6", "location_name": shelf}
    assert (await _import(client, auth, [{**row, "default_sell_qty": "2"}])).status_code == 200
    r = await _import(client, auth, [{**row, "sku": row["sku"] + "B", "default_sell_qty": value}])
    assert r.status_code == 422, r.text
    assert "default_sell_qty" in r.text, r.text


async def test_raw_batch_import_refuses_an_invalid_value(client, session, auth):
    r = await client.post("/items/import/batch", headers=auth["headers"], json={"records": [
        {"entity_id": "item:dsq-bad", "event_type": "item.created", "source": "csv", "idempotency_key": "dsq-bad",
         "data": {"sku": "DSQ-BAD", "name": "Bad", "quantity": 1, "sell_by": "carat", "default_sell_qty": "abc"}},
        {"entity_id": "item:dsq-ok", "event_type": "item.created", "source": "csv", "idempotency_key": "dsq-ok",
         "data": {"sku": "DSQ-OK", "name": "Ok", "quantity": 1, "sell_by": "carat", "default_sell_qty": "Full"}},
    ]})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1 and len(r.json()["errors"]) == 1, r.json()
    assert (await _state(session, auth, "item:dsq-ok"))["default_sell_qty"] == "full"


def _batch_record(entity_id: str, event_type: str, **data) -> dict:
    return {"entity_id": entity_id, "event_type": event_type, "source": "csv",
            "idempotency_key": f"dsq-{uuid.uuid4().hex}", "data": data}


@pytest.mark.parametrize("event_type", ["item.created", "item.patched"])
async def test_raw_batch_import_refuses_a_quantity_finer_than_its_unit(client, session, auth, event_type):
    fine = f"item:{uuid.uuid4()}" if event_type == "item.created" else await _lot(client, auth, qty=6)
    assert (await _set_carat_decimals(client, auth, 1)).status_code == 200
    ok = f"item:{uuid.uuid4()}"
    r = await client.post("/items/import/batch", headers=auth["headers"], json={"records": [
        _batch_record(fine, event_type, sku="DSQ-FINE", name="Fine", quantity=0.27, sell_by="carat"),
        _batch_record(ok, "item.created", sku="DSQ-TENTH", name="Tenth", quantity=0.3, sell_by="carat"),
    ]})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1 and len(r.json()["errors"]) == 1, r.json()
    assert "DSQ-FINE" in r.text and "1 decimal places" in r.text, r.text
    assert (await _state(session, auth, ok))["quantity"] == 0.3
    stored = (await _state(session, auth, fine)) if event_type == "item.patched" else None
    assert stored is None or stored["quantity"] == 6, stored


async def test_raw_batch_import_refuses_a_unit_change_under_a_finer_stored_quantity(client, session, auth):
    item = await _lot(client, auth, qty=0.27)
    r = await client.post("/items/import/batch", headers=auth["headers"], json={"records": [
        _batch_record(item, "item.patched", sell_by="piece")]})
    assert r.status_code == 200, r.text
    assert len(r.json()["errors"]) == 1 and "decimal places" in r.text, r.json()
    assert (await _state(session, auth, item))["sell_by"] == "carat"



async def test_an_import_update_that_changes_the_unit_under_a_stored_fraction_is_refused(client, session, auth, shelf):
    sku = f"DSQ-U-{uuid.uuid4().hex[:5]}"
    r = await _import(client, auth, [{"sku": sku, "name": "U", "sell_by": "carat", "quantity": "6",
                                      "location_name": shelf, "default_sell_qty": "2.5"}])
    assert r.status_code == 200, r.text
    r = await _import(client, auth, [{"sku": sku, "name": "U", "sell_by": "piece", "quantity": "6"}], upsert=True)
    assert "Change Default Sell Qty" in r.text, r.text
    state = await _by_sku(session, auth, sku)
    assert state["sell_by"] == "carat" and state["default_sell_qty"] == 2.5, state

async def test_import_template_names_the_column(client, auth):
    from celerp_inventory.services import ITEM_IMPORT_TAIL_COLS

    assert "default_sell_qty" in ITEM_IMPORT_TAIL_COLS


async def test_csv_export_with_the_column_chosen_round_trips(client, session, auth, shelf):
    sku = f"DSQ-E-{uuid.uuid4().hex[:6]}"
    await _lot(client, auth, sku=sku, default_sell_qty=2.5)
    r = await client.get("/items/export/csv", headers=auth["headers"], params={"cols": "sku,default_sell_qty", "q": sku})
    assert r.status_code == 200, r.text
    header, row = r.text.strip().splitlines()[:2]
    assert "default_sell_qty" in header.lower().replace(" ", "_") or "Default Sell Qty" in header, header
    assert row.split(",")[-1].strip('"') == "2.5", r.text

    copy = f"{sku}-COPY"
    r = await _import(client, auth, [{"sku": copy, "name": "Parcel", "sell_by": "carat", "quantity": "6",
                                      "location_name": shelf, "default_sell_qty": row.split(",")[-1].strip('"')}])
    assert r.status_code == 200, r.text
    assert (await _by_sku(session, auth, copy))["default_sell_qty"] == 2.5

    r = await client.get("/items/export/csv", headers=auth["headers"], params={"q": sku})
    assert r.status_code == 200, r.text
    assert "default_sell_qty" not in r.text.splitlines()[0].lower().replace(" ", "_")


# API and schema

async def test_the_item_api_returns_the_value(client, auth):
    lot = await _lot(client, auth)
    assert (await _patch(client, auth, lot, default_sell_qty="FULL")).status_code == 200
    r = await client.get(f"/items/{lot}", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["default_sell_qty"] == "full"


async def test_openapi_describes_the_field():
    from celerp.main import app

    app.openapi_schema = None
    spec = app.openapi()
    prop = spec["components"]["schemas"]["ItemCreate"]["properties"]["default_sell_qty"]
    assert prop.get("description"), prop
    kinds = json.dumps(prop)
    assert '"full"' in kinds and "number" in kinds, prop


async def test_the_field_sits_after_allow_splitting_in_a_customised_schema(session, auth):
    company = await locked_company(session, auth["company_id"])
    stored = [f for f in _BASE_FIELDS if f["key"] != "default_sell_qty"]
    company.settings = {**(company.settings or {}), "item_schema": stored}
    await session.flush()
    keys = [f["key"] for f in await get_effective_field_schema(session, auth["company_id"])]
    assert keys[keys.index("allow_splitting") + 1] == "default_sell_qty", keys


async def test_the_default_schema_hides_it_from_the_table(session, auth):
    [field] = [f for f in await get_effective_field_schema(session, auth["company_id"]) if f["key"] == "default_sell_qty"]
    assert field["show_in_table"] is False and field["label_key"] == "field.label.default_sell_qty"
    assert field["tooltip_key"] == "field.tooltip.default_sell_qty"


# What the scan bar says

async def test_the_scan_bar_names_the_placed_quantity_and_unit():
    from celerp.services.units import DEFAULT_UNITS, build_unit_map
    from ui.routes.documents import _scan_added_label

    units = build_unit_map(DEFAULT_UNITS)
    placed = _scan_added_label({"sku": "SKU-1", "quantity": 0.6, "unit": "carat", "capped": False}, units)
    capped = _scan_added_label({"sku": "SKU-1", "quantity": 0.6, "unit": "carat", "capped": True}, units)
    assert "SKU-1" in placed and "0.60" in placed and "carat" in placed, placed
    assert capped != placed and capped.startswith(placed.rstrip(".")), capped
    piece = _scan_added_label({"sku": "S", "quantity": 2.0, "unit": "piece", "capped": False}, units)
    assert "2" in piece and "2.0" not in piece, piece


@pytest.mark.parametrize("path", sorted(LOCALES.glob("*.json")), ids=lambda p: p.stem)
def test_the_tooltip_example_number_is_accepted(path):
    """Numbers are typed with a decimal point in every language, so the tooltip's example must be."""
    tip = json.loads(path.read_text())["field.tooltip.default_sell_qty"]
    examples = re.findall(r"\d+[.,]\d+", tip)
    assert examples, f"{path.stem}: no example number in the tooltip"
    for example in examples:
        assert validate_default_sell_qty(example, "carat", {"carat": {"unit_type": "weight", "decimals": 2}}) == 0.5


@pytest.mark.parametrize("stored, shown", [(1234567, "1234567"), (1234.567, "1234.567"), (2.50, "2.5")])
def test_a_number_on_an_unlisted_unit_shows_every_digit(stored, shown):
    from ui.routes.inventory import _sell_qty_text

    assert _sell_qty_text({"sell_by": "bundle", "default_sell_qty": stored}, {}) == shown
