# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Fields only the app writes cannot be entered through a lot's attributes, however the lot
is made: received from an order, split from a parent, or merged from several lots."""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from celerp.models.ledger import LedgerEntry
from test_cost_restatement import _item, _merge
from test_helpers import in_language
from test_receipt_accounting import _doc

APP_OWNED = {"consignment_flag": "in", "status_doc_id": "doc:FAKE", "reserved_quantity": 2,
             "is_expired": True, "inventory_on_books": True}


async def _events(session, auth) -> int:
    return (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"]))).scalar_one()


def _refused(r) -> None:
    """Refused with a sentence naming the fields, in the reader's language."""
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "item.app_owned_fields", detail
    assert detail["message"] == (
        "These fields are set by the app and cannot be entered: "
        f"{', '.join(sorted(APP_OWNED))}. Remove them and try again."), detail
    german = in_language("de", detail)
    assert german != detail["message"] and ", ".join(sorted(APP_OWNED)) in german, german


@pytest.mark.asyncio
async def test_a_receipt_cannot_enter_app_owned_fields_as_attributes(client, session, auth):
    h = auth["headers"]
    r = await client.post("/companies/me/locations", headers=h, json={"name": "Receiving", "type": "warehouse"})
    assert r.status_code == 200, r.text
    location = r.json()["id"]
    goods = await _item(client, auth, None, qty=0, sku="RCV-ATTR")
    line = {"entity_id": goods, "sku": "RCV-ATTR", "name": "Goods", "quantity": 4, "unit_price": 25.0,
            "line_total": 100.0, "sell_by": "piece"}
    bill = await _doc(client, auth, "bill", [line], total=100.0)
    r = await client.post(f"/docs/{bill}/finalize", headers=h)
    assert r.status_code == 200, r.text
    received = {"item_id": goods, "sku": "RCV-ATTR", "name": "Goods", "quantity_received": 4, "cost_price": 25,
                "receive_as": "stock", "attributes": APP_OWNED}
    before = await _events(session, auth)
    _refused(await client.post(f"/docs/{bill}/receive", headers=h,
                               json={"location_id": location, "received_items": [received]}))
    assert await _events(session, auth) == before


@pytest.mark.asyncio
async def test_a_split_cannot_enter_app_owned_fields_as_child_attributes(client, session, auth):
    parent = await _item(client, auth, 300.0, qty=3)
    before = await _events(session, auth)
    _refused(await client.post(f"/items/{parent}/split", headers=auth["headers"],
                               json={"children": [{"quantity": 1, "attributes": APP_OWNED}]}))
    assert await _events(session, auth) == before


@pytest.mark.asyncio
async def test_a_merge_cannot_enter_app_owned_fields_as_resolved_attributes(client, session, auth):
    a, b = await _item(client, auth, 50.0), await _item(client, auth, 70.0)
    with pytest.raises(AssertionError, match="set by the app"):
        await _merge(client, auth, [a, b], resolved_attributes=APP_OWNED)


def _goods_line(goods: str, **extra) -> dict:
    return {"entity_id": goods, "sku": "BILL-ATTR", "name": "Goods", "quantity": 4, "unit_price": 25.0,
            "line_total": 100.0, "sell_by": "piece", **extra}


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["create", "edit"])
async def test_a_document_line_cannot_enter_app_owned_fields_as_attributes(client, session, auth, via):
    """A bill line's attributes become the received lot's, so app-owned keys there are refused
    when the line is written, never accepted and then dropped."""
    goods = await _item(client, auth, None, qty=0, sku="BILL-ATTR")
    line = _goods_line(goods, attributes=APP_OWNED)
    before = await _events(session, auth)
    if via == "create":
        r = await client.post("/docs", headers=auth["headers"], json={
            "doc_type": "bill", "contact_id": "supplier:1", "line_items": [line], "total": 100.0})
    else:
        bill = await _doc(client, auth, "bill", [_goods_line(goods)], total=100.0)
        before = await _events(session, auth)
        r = await client.patch(f"/docs/{bill}", headers=auth["headers"],
                               json={"fields_changed": {"line_items": {"new": [line]}}})
    _refused(r)
    assert await _events(session, auth) == before


@pytest.mark.asyncio
async def test_a_bill_lines_own_attributes_reach_the_received_lot(client, session, auth):
    h = auth["headers"]
    r = await client.post("/companies/me/locations", headers=h, json={"name": "Receiving", "type": "warehouse"})
    assert r.status_code == 200, r.text
    goods = await _item(client, auth, None, qty=0, sku="BILL-ATTR")
    bill = await _doc(client, auth, "bill", [_goods_line(goods, attributes={"colour": "Red"})], total=100.0)
    assert (await client.get(f"/docs/{bill}", headers=h)).json()["line_items"][0]["attributes"] == {"colour": "Red"}
    assert (await client.post(f"/docs/{bill}/finalize", headers=h)).status_code == 200
    r = await client.post(f"/docs/{bill}/receive", headers=h, json={"location_id": r.json()["id"], "received_items": [
        {"item_id": goods, "sku": "BILL-ATTR", "name": "Goods", "quantity_received": 4, "cost_price": 25,
         "receive_as": "stock"}]})
    assert r.status_code == 200, r.text
    lots = [i for i in (await client.get("/items", headers=h, params={"q": "BILL-ATTR"})).json()["items"]
            if i["id"] != goods]
    assert lots and all(i.get("colour") == "Red" or (i.get("attributes") or {}).get("colour") == "Red" for i in lots), lots
