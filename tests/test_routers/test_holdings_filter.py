# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Endpoint tests for the consignment-in side of the contact-scoped holdings filter.

GET /items?consigned_from=<supplier_contact_id> lists items still held on
consignment from that supplier, valued at cost (celerp.services.holdings).

The memo side (GET /items?on_memo_to=...) is covered in tests/test_fulfillment.py,
where the memo create/fulfil helpers already live. These live in their own file
(rather than test_doc_workflows.py) so they collect under every supported Python.
"""

from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@holdings.test"
    r = await client.post("/auth/register",
                          json={"company_name": "Holdings Co", "email": addr, "name": "Admin", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_location(client, token: str) -> str:
    r = await client.post("/companies/me/locations", headers=_h(token),
                          json={"name": "Warehouse", "type": "warehouse", "is_default": True})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _consign_in_received(client, token, location_id, supplier, sku, qty=1, cost_price=250.0):
    """Create + finalize + receive a consignment_in doc; return (doc_id, created_item_id)."""
    total = cost_price * qty
    r = await client.post("/docs", headers=_h(token), json={
        "doc_type": "consignment_in", "contact_id": supplier,
        "line_items": [{"sku": sku, "name": sku, "quantity": qty, "unit_price": cost_price, "line_total": total}],
        "subtotal": total, "tax": 0, "total": total,
    })
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    await client.post("/docs/{}/finalize".format(doc_id), headers=_h(token))
    rec = await client.post("/docs/{}/receive".format(doc_id), headers=_h(token), json={
        "location_id": location_id,
        "received_items": [{"po_line_index": 0, "sku": sku, "name": sku,
                            "quantity_received": qty, "cost_price": cost_price, "receive_as": "stock"}],
    })
    assert rec.status_code == 200, rec.text
    doc = (await client.get("/docs/{}".format(doc_id), headers=_h(token))).json()
    return doc_id, doc["line_items"][0]["entity_id"]


@pytest.mark.asyncio
async def test_items_consigned_from_lists_held_at_cost(client, session):
    token = await _register(client)
    location_id = await _create_location(client, token)
    supplier = "contact:supHold"
    _doc, item_id = await _consign_in_received(client, token, location_id, supplier,
                                               "CIN-HOLD-1", cost_price=250.0)

    resp = (await client.get("/items", headers=_h(token), params={"consigned_from": supplier})).json()
    assert {i["id"] for i in resp["items"]} == {item_id}
    assert resp["items"][0]["holding_value"] == 250.0
    assert resp["value_total"] == 250.0


@pytest.mark.asyncio
async def test_items_consigned_from_excludes_returned(client, session):
    token = await _register(client)
    location_id = await _create_location(client, token)
    supplier = "contact:supReturn"
    doc_id, item_id = await _consign_in_received(client, token, location_id, supplier,
                                                 "CIN-RET-1", qty=1, cost_price=250.0)
    # Return the full quantity to the supplier -> consignment_flag clears -> out of scope.
    rr = await client.post("/docs/{}/return-items".format(doc_id), headers=_h(token),
                           json={"items": [{"item_id": item_id, "quantity_returned": 1}]})
    assert rr.status_code == 200, rr.text

    resp = (await client.get("/items", headers=_h(token), params={"consigned_from": supplier})).json()
    assert resp["items"] == []
    assert resp["value_total"] == 0.0


@pytest.mark.asyncio
async def test_items_consigned_from_excludes_owned_and_other_supplier(client, session):
    token = await _register(client)
    location_id = await _create_location(client, token)
    sup_a, sup_b = "contact:supA", "contact:supB"
    _da, item_a = await _consign_in_received(client, token, location_id, sup_a, "CIN-A-1", cost_price=100.0)
    await _consign_in_received(client, token, location_id, sup_b, "CIN-B-1", cost_price=999.0)
    # Owned (non-consignment) inventory must never appear under a consignment scope.
    owned = await client.post("/items", headers=_h(token),
                              json={"sku": "OWNED-1", "name": "Owned", "quantity": 1,
                                    "sell_by": "piece", "cost_total": 50.0})
    assert owned.status_code == 200, owned.text

    resp = (await client.get("/items", headers=_h(token), params={"consigned_from": sup_a})).json()
    assert {i["id"] for i in resp["items"]} == {item_a}
    assert resp["value_total"] == 100.0


@pytest.mark.asyncio
async def test_items_consigned_from_partial_return_keeps_item_in_scope(client, session):
    """A partial return leaves the balance on consignment: the item stays in scope with
    consignment_flag still "in", only the returned quantity leaves the shelf, and the
    value steps down with it.

    A partial return reduces quantity in place (unlike partial fulfillment, it does not
    split off a child), so the goods cost has to be rescaled with it or the remaining
    balance would keep carrying the whole lot's cost.
    """
    token = await _register(client)
    location_id = await _create_location(client, token)
    supplier = "contact:supPartial"
    doc_id, item_id = await _consign_in_received(client, token, location_id, supplier,
                                                 "CIN-PART-1", qty=2, cost_price=125.0)
    before = (await client.get("/items", headers=_h(token), params={"consigned_from": supplier})).json()
    assert before["value_total"] == 250.0

    rr = await client.post("/docs/{}/return-items".format(doc_id), headers=_h(token),
                           json={"items": [{"item_id": item_id, "quantity_returned": 1}]})
    assert rr.status_code == 200, rr.text

    after = (await client.get("/items", headers=_h(token), params={"consigned_from": supplier})).json()
    assert {i["id"] for i in after["items"]} == {item_id}, "partial return must keep the balance in scope"
    (item,) = after["items"]
    assert item["quantity"] == 1.0
    assert item["consignment_flag"] == "in", "flag must persist while a balance remains"
    # Card and list still agree, whatever the basis.
    assert after["value_total"] == item["holding_value"]
    # 1 of 2 units returned, so half the lot cost went back with them.
    assert after["value_total"] == 125.0
    # The quantity left on the lot is the record of how much was kept, and per-unit cost
    # is unchanged by the return: a lot is homogeneous, so scaling the lot cost with the
    # quantity is what a split would have produced anyway.
    assert item["cost_price"] == 125.0, "per-unit cost must survive a partial return"


@pytest.mark.asyncio
async def test_return_items_rejects_goods_not_on_hand(client, session):
    """Goods out with a customer cannot be handed back to a supplier: they are not on our
    shelf, and shrinking them here would write off stock still owed back to us."""
    token = await _register(client)
    location_id = await _create_location(client, token)
    supplier = "contact:supNotOnHand"
    doc_id, item_id = await _consign_in_received(client, token, location_id, supplier,
                                                 "CIN-OUT-1", qty=2, cost_price=100.0)
    # Send the consigned goods out on memo to a customer.
    memo = await client.post("/docs", headers=_h(token), json={
        "doc_type": "memo", "contact_id": "contact:cust1",
        "line_items": [{"sku": "CIN-OUT-1", "name": "CIN-OUT-1", "quantity": 2,
                        "unit_price": 200.0, "line_total": 400.0, "entity_id": item_id}],
        "subtotal": 400, "tax": 0, "total": 400,
    })
    assert memo.status_code == 200, memo.text
    memo_id = memo.json()["id"]
    await client.post("/docs/{}/finalize".format(memo_id), headers=_h(token))
    ff = await client.post("/docs/{}/fulfill-lines".format(memo_id), headers=_h(token),
                           json={"line_entity_ids": [item_id]})
    assert ff.status_code == 200, ff.text

    rr = await client.post("/docs/{}/return-items".format(doc_id), headers=_h(token),
                           json={"items": [{"item_id": item_id, "quantity_returned": 1}]})
    assert rr.status_code == 409, rr.text
    assert "not on hand" in str(rr.json().get("detail", ""))
    # Untouched: still out with the customer at full quantity.
    item = (await client.get("/items/{}".format(item_id), headers=_h(token))).json()
    assert item["status"] == "memo_out" and item["quantity"] == 2.0


async def _sell_item(client, token: str, sku: str, unit_price: float) -> str:
    r = await client.post("/items", headers=_h(token),
                          json={"status": "available", "sku": sku, "name": sku, "quantity": 1, "sell_by": "piece"})
    assert r.status_code in {200, 201}, r.text
    item_id = r.json()["id"]
    r = await client.post("/docs", headers=_h(token), json={
        "doc_type": "invoice", "status": "draft",
        "line_items": [{"sku": sku, "name": sku, "quantity": 1, "unit_price": unit_price,
                        "line_total": unit_price, "entity_id": item_id}],
        "total": unit_price, "amount_outstanding": unit_price,
    })
    assert r.status_code in {200, 201}, r.text
    doc_id = r.json()["id"]
    assert (await client.post(f"/docs/{doc_id}/finalize", headers=_h(token))).status_code in {200, 201}
    r = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=_h(token), json={"line_entity_ids": [item_id]})
    assert r.status_code in {200, 201}, r.text
    return item_id


async def _revoke_documents(client, token: str) -> None:
    r = await client.patch("/companies/me/role-permissions", headers=_h(token),
                           json={"perm_key": "view_documents", "role_key": "owner", "granted": False})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_holdings_and_sold_figures_need_document_access(client, session):
    token = await _register(client)
    location_id = await _create_location(client, token)
    supplier = "contact:supNoDocs"
    await _consign_in_received(client, token, location_id, supplier, "CIN-NODOC-1", cost_price=250.0)
    sold_id = await _sell_item(client, token, "SOLD-NODOC-1", 120.0)
    assert (await client.get(f"/items/{sold_id}", headers=_h(token))).json()["sold_price"] == 120.0
    await _revoke_documents(client, token)

    for params in ({"consigned_from": supplier}, {"on_memo_to": "contact:custNoDocs"}):
        assert (await client.get("/items", headers=_h(token), params=params)).status_code == 403
        assert (await client.get("/items/valuation", headers=_h(token), params=params)).status_code == 403
        assert (await client.get("/items/export/csv", headers=_h(token), params=params)).status_code == 403

    sold = await client.get("/items", headers=_h(token), params={"status": "sold"})
    assert sold.status_code == 200, sold.text
    assert "sold_total" not in sold.json()
    assert all("sold_price" not in i for i in sold.json()["items"])
    detail = await client.get(f"/items/{sold_id}", headers=_h(token))
    assert detail.status_code == 200, detail.text
    assert "sold_price" not in detail.json()
