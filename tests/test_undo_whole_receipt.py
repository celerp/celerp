# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Undoing a bill's receipt takes back the whole receipt, and only the receipt.

A receipt exists when the bill has received anything, expense and asset lines included.
Undoing it zeroes what every line has received, and clears from a line only the parcel
the receipt created, never an item the line named before. The bill's own entry stays
booked. Goods already returned to the supplier keep the receipt in place, and undoing a
receipt that is already undone says so instead of failing.
"""
from __future__ import annotations

import uuid

import pytest
from fasthtml.common import to_xml

import ui.api_client as api_client
from celerp.models.projections import Projection
from test_cost_restatement import _state
from test_receipt_accounting import _books, _doc, _finalize, _receive, _return
from test_receive_goods_form import _Request, _Routes

pytestmark = pytest.mark.asyncio


def _sku() -> str:
    return f"UW-{uuid.uuid4().hex[:6]}"


async def _undo(client, auth, doc_id: str):
    return await client.delete(f"/docs/{doc_id}/receive", headers=auth["headers"])


async def test_expense_only_receipt_undo(client, session, auth):
    bill = await _doc(client, auth, "bill", [
        {"name": "Courier fee", "quantity": 1, "unit_price": 25.0, "receive_as": "expense"},
        {"name": "Software", "quantity": 1, "unit_price": 40.0, "receive_as": "expense"},
    ])
    await _finalize(client, auth, bill)
    booked = await _books(session, auth, "2110")
    assert booked == {"2110": -65.0}
    r = await _receive(client, auth, bill,
                       {"po_line_index": 0, "quantity_received": 1, "receive_as": "expense"},
                       {"po_line_index": 1, "quantity_received": 1, "receive_as": "expense"})
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, bill))["status"] == "received"

    r = await _undo(client, auth, bill)
    assert r.status_code == 200, r.text
    assert r.json() == {"undone": True, "item_ids": []}
    doc = await _state(session, auth, bill)
    assert doc["received_items"] == []
    assert [li.get("quantity_received") for li in doc["line_items"]] == [0, 0]
    # Back to the status the receipt found it in.
    assert doc["status"] == "awaiting_payment"
    # The bill's own entry is untouched: undoing a receipt never unbooks the bill.
    assert await _books(session, auth, "2110") == booked


async def test_undo_strips_only_receipt_parcels(client, session, auth):
    sku = _sku()
    named = (await client.post("/items", headers=auth["headers"], json={
        "status": "available", "sku": f"{sku}-N", "name": "Named", "quantity": 0, "sell_by": "piece",
    })).json()["id"]
    bill = await _doc(client, auth, "bill", [
        {"sku": f"{sku}-N", "name": "Named", "quantity": 2, "unit_price": 5.0},
        {"sku": sku, "name": "Fresh", "quantity": 3, "unit_price": 4.0},
    ])
    await _finalize(client, auth, bill)
    # A line that already named an item before any receipt, as a converted line does.
    session.expire_all()
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": bill})
    lines = [dict(li) for li in row.state["line_items"]]
    lines[0]["entity_id"] = named
    row.state = {**row.state, "line_items": lines}
    await session.commit()

    r = await _receive(client, auth, bill, {"po_line_index": 1, "sku": sku, "name": "Fresh", "quantity_received": 3})
    assert r.status_code == 200, r.text
    [parcel] = (await _state(session, auth, bill))["received_item_ids"]
    assert [li.get("entity_id") for li in (await _state(session, auth, bill))["line_items"]] == [named, parcel]

    r = await _undo(client, auth, bill)
    assert r.status_code == 200, r.text
    doc = await _state(session, auth, bill)
    assert [li.get("entity_id") for li in doc["line_items"]] == [named, None]
    assert [li.get("quantity_received") for li in doc["line_items"]] == [0, 0]
    assert (await _state(session, auth, parcel))["status"] == "archived"


async def test_undo_refused_after_supplier_return(client, session, auth):
    sku = _sku()
    bill = await _doc(client, auth, "bill", [{"sku": sku, "name": "Goods", "quantity": 4, "unit_price": 5.0}])
    await _finalize(client, auth, bill)
    r = await _receive(client, auth, bill, {"po_line_index": 0, "sku": sku, "name": "Goods", "quantity_received": 4})
    assert r.status_code == 200, r.text
    [parcel] = (await _state(session, auth, bill))["received_item_ids"]
    r = await _return(client, auth, bill, parcel, 1)
    assert r.status_code == 200, r.text
    before = await _state(session, auth, bill)
    books = await _books(session, auth, "1130-P", "2110")

    r = await _undo(client, auth, bill)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "docs.undo_receipt_after_return"
    assert await _state(session, auth, bill) == before
    assert (await _state(session, auth, parcel))["status"] != "archived"
    assert await _books(session, auth, "1130-P", "2110") == books


async def test_undo_retry_is_idempotent(client, session, auth):
    sku = _sku()
    bill = await _doc(client, auth, "bill", [{"sku": sku, "name": "Goods", "quantity": 2, "unit_price": 5.0}])
    await _finalize(client, auth, bill)
    # Nothing was ever received: there is nothing to undo.
    r = await _undo(client, auth, bill)
    assert r.status_code == 409, r.text

    r = await _receive(client, auth, bill, {"po_line_index": 0, "sku": sku, "name": "Goods", "quantity_received": 2})
    assert r.status_code == 200, r.text
    first = await _undo(client, auth, bill)
    assert first.status_code == 200, first.text
    after = await _state(session, auth, bill)

    again = await _undo(client, auth, bill)
    assert again.status_code == 200, again.text
    assert again.json() == {"undone": True, "item_ids": [], "already_undone": True}
    assert await _state(session, auth, bill) == after


def _undo_form(html: str) -> str | None:
    import re
    m = re.search(r'<form[^>]*id="undo-receipt-form"[^>]*>', html)
    return m.group(0) if m else None


async def test_undo_receipt_button(client, session, auth):
    from ui.routes.documents import _doc_detail

    sku = _sku()
    bill = await _doc(client, auth, "bill", [{"sku": sku, "name": "Goods", "quantity": 2, "unit_price": 5.0}])
    await _finalize(client, auth, bill)
    doc = (await client.get(f"/docs/{bill}", headers=auth["headers"])).json()
    assert _undo_form(to_xml(_doc_detail(doc))) is None, "nothing received, nothing to undo"

    r = await _receive(client, auth, bill, {"po_line_index": 0, "sku": sku, "name": "Goods", "quantity_received": 2})
    assert r.status_code == 200, r.text
    doc = (await client.get(f"/docs/{bill}", headers=auth["headers"])).json()
    form = _undo_form(to_xml(_doc_detail(doc)))
    assert form is not None
    assert f'hx-delete="/docs/{bill}/receive"' in form
    assert "hx-confirm=" in form
    # Hidden from a role the API would refuse.
    assert _undo_form(to_xml(_doc_detail(doc, role="viewer", settings={}))) is None


async def test_undo_receipt_route(client, session, auth, monkeypatch):
    from ui.routes import documents

    sku = _sku()
    bill = await _doc(client, auth, "bill", [{"sku": sku, "name": "Goods", "quantity": 2, "unit_price": 5.0}])
    await _finalize(client, auth, bill)
    r = await _receive(client, auth, bill, {"po_line_index": 0, "sku": sku, "name": "Goods", "quantity_received": 2})
    assert r.status_code == 200, r.text

    async def undo_receive_goods(_tok, entity_id):
        return api_client._raise(await client.delete(f"/docs/{entity_id}/receive", headers=auth["headers"])).json()

    monkeypatch.setattr(documents, "_token", lambda request: "tok")
    monkeypatch.setattr(api_client, "undo_receive_goods", undo_receive_goods)
    routes = _Routes()
    documents.setup_routes(routes)
    handler = routes.routes[("delete", "/docs/{entity_id}/receive")]

    resp = await handler(_Request([]), bill)
    assert resp.status_code == 204
    assert resp.headers["HX-Redirect"] == f"/docs/{bill}"
    assert (await _state(session, auth, bill))["received_items"] == []

    # A refusal comes back as a message, never a raw payload.
    parcel_bill = await _doc(client, auth, "bill", [{"sku": f"{sku}-R", "name": "R", "quantity": 2, "unit_price": 5.0}])
    await _finalize(client, auth, parcel_bill)
    r = await _receive(client, auth, parcel_bill, {"po_line_index": 0, "sku": f"{sku}-R", "name": "R", "quantity_received": 2})
    [parcel] = (await _state(session, auth, parcel_bill))["received_item_ids"]
    assert (await _return(client, auth, parcel_bill, parcel, 1)).status_code == 200
    resp = await handler(_Request([]), parcel_bill)
    assert "returned to the supplier" in resp.headers["HX-Trigger"]
    assert "message_key" not in resp.headers["HX-Trigger"]


@pytest.mark.parametrize("key", ["docs.undo_receipt_after_return", "documents.undo_receipt",
                                 "documents.undo_receipt_confirm"])
def test_undo_receipt_copy_in_every_locale(key):
    from ui import i18n
    for code in i18n.available_langs():
        assert i18n.t(key, code) != key, (key, code)
