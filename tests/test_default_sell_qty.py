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

from celerp.events.engine import emit_event
from celerp.services.company_lock import locked_company
from celerp.services.field_schema import _BASE_FIELDS, get_effective_field_schema
from celerp.services.line_measures import validate_default_sell_qty
from mfg_runs import issue, product, receive, run
from test_cost_restatement import _item, _merge, _state
from test_inventory_transform import _transform_payload
from test_receipt_accounting import _doc, _parcels, _receive

pytestmark = pytest.mark.asyncio

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
