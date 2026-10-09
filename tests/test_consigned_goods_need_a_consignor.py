# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Consigned goods are received only once the consignment names the consignor they
belong to, since that is who is owed when they sell. A consignment with no consignor
refuses the receipt and asks for one, in the API and on the Receive Goods form, and
receives nothing."""
from __future__ import annotations

import json
import uuid

import pytest

import ui.api_client as api_client
from test_consignment_in_sale import _state
from test_consignor_payable_per_consignor import _consignor
from test_receive_goods_form import _Request, _Routes

pytestmark = pytest.mark.asyncio

KEY = "consignment.receive.no_consignor"


async def _unnamed_consignment(client, auth) -> tuple[str, dict]:
    """A finalized consignment with no consignor, and the receipt body for its goods."""
    sku = f"CN-{uuid.uuid4().hex[:6]}"
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": sku, "name": "Lot", "quantity": 0, "sell_by": "piece", "status": "available"})
    assert r.status_code == 200, r.text
    template = r.json()["id"]
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "consignment_in",
        "line_items": [{"item_id": template, "sku": sku, "name": "Lot", "quantity": 2,
                        "unit_price": 5.0, "line_total": 10.0}],
        "total": 10.0})
    assert r.status_code == 200, r.text
    con = r.json()["id"]
    r = await client.post(f"/docs/{con}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text
    body = {"location_id": "", "received_items": [{
        "item_id": template, "sku": sku, "name": "Lot", "quantity_received": 2, "po_line_index": 0,
        "receive_as": "stock", "cost_price": 4.0}]}
    return con, body


async def test_consigned_goods_are_not_received_until_the_consignor_is_chosen(client, session, auth):
    con, body = await _unnamed_consignment(client, auth)
    r = await client.post(f"/docs/{con}/receive", headers=auth["headers"], json=body)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == KEY
    assert "choose the consignor" in detail["message"]
    session.expire_all()
    assert not (await _state(session, auth, con)).get("received_item_ids")

    a = await _consignor(client, auth, "Consignor A")
    r = await client.patch(f"/docs/{con}", headers=auth["headers"],
                           json={"fields_changed": {"contact_id": {"new": a}}})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{con}/receive", headers=auth["headers"], json=body)
    assert r.status_code == 200, r.text
    assert len((await _state(session, auth, con))["received_item_ids"]) == 1


async def test_the_receive_goods_form_says_to_choose_the_consignor(client, session, auth, monkeypatch):
    from ui.routes import documents

    con, body = await _unnamed_consignment(client, auth)
    item = body["received_items"][0]
    form = [("location_id", ""), ("item_id_0", item["item_id"]), ("sku_0", item["sku"]),
            ("name_0", item["name"]), ("qty_0", "2"), ("po_line_index_0", "0"), ("receive_as_0", "stock")]

    async def receive_po(_tok, entity_id, data):
        r = await client.post(f"/docs/{entity_id}/receive", headers=auth["headers"], json=data)
        return api_client._raise(r).json()

    monkeypatch.setattr(documents, "_token", lambda request: "tok")
    monkeypatch.setattr(api_client, "receive_po", receive_po)
    routes = _Routes()
    documents.setup_routes(routes)
    resp = await routes.routes[("post", "/docs/{entity_id}/receive")](_Request(form), con)
    toast = json.loads(resp.headers["HX-Trigger"])["celerpToast"]
    assert toast["type"] == "error"
    assert "choose the consignor" in toast["message"]
    session.expire_all()
    assert not (await _state(session, auth, con)).get("received_item_ids")
