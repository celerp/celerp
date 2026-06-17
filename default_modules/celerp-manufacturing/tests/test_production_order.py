# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The internal Production Order document type (invoice-to-self demand).

The single most important correctness requirement: a production order is NOT a sale, so it must be
excluded from revenue, AR aging, sales summaries and dashboard sales everywhere - while still
feeding the manufacturing To-Make board as demand once finalized. Also covers PRD- numbering,
no-customer-required, and that it never takes a payment.
"""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@prodorder.test"
    r = await client.post("/auth/register", json={"company_name": "PO Co", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _ring(client, token):
    """A manufacturable product (has a recipe) plus its component."""
    gold = (await client.post("/items", headers=_h(token),
                              json={"sku": "PO-GOLD", "name": "Gold", "quantity": 100, "sell_by": "gram",
                                    "cost_total": 8000})).json()["id"]
    ring = (await client.post("/items", headers=_h(token),
                              json={"sku": "PO-RING", "name": "Ring", "quantity": 0, "sell_by": "piece"})).json()["id"]
    await client.put(f"/manufacturing/items/{ring}/recipe", headers=_h(token),
                     json={"output_qty": 1, "components": [{"item_id": gold, "quantity": 5}], "labor": [], "overhead": []})
    return gold, ring


async def _production_order(client, token, ring, qty=3, *, finalize=False, **extra) -> dict:
    body = {"doc_type": "production_order",
            "line_items": [{"item_id": ring, "sku": "PO-RING", "name": "Ring", "quantity": qty, "unit_price": 900}],
            "total": 2700, **extra}
    created = (await client.post("/docs", headers=_h(token), json=body)).json()
    doc_id = created["id"]
    if finalize:
        assert (await client.post(f"/docs/{doc_id}/finalize", headers=_h(token))).status_code == 200
    doc = (await client.get(f"/docs/{doc_id}", headers=_h(token))).json()
    doc.setdefault("id", doc_id)
    return doc


@pytest.mark.asyncio
async def test_production_order_numbering_and_no_customer(client):
    """A production order needs no customer and is numbered from its own PRD- sequence."""
    token = await _register(client)
    _gold, ring = await _ring(client, token)
    doc = await _production_order(client, token, ring)  # no contact_id supplied
    assert doc["doc_type"] == "production_order"
    assert str(doc["ref_id"]).startswith("PRD-"), doc["ref_id"]


@pytest.mark.asyncio
async def test_production_order_excluded_from_revenue_ar_and_sales(client):
    """The exclusion gate: a finalized production order is invisible to every money surface."""
    token = await _register(client)
    _gold, ring = await _ring(client, token)
    doc = await _production_order(client, token, ring, finalize=True)
    assert doc["status"] == "final"

    # Sales report: no revenue, no lines.
    sales = (await client.get("/reports/sales", headers=_h(token), params={"group_by": "item"})).json()
    assert sales["total_revenue"] == 0 and sales["lines"] == []

    # AR aging: nothing outstanding.
    aging = (await client.get("/reports/ar-aging", headers=_h(token))).json()
    assert aging["lines"] == []

    # Dashboard: zero revenue and AR.
    kpis = (await client.get("/dashboard/kpis", headers=_h(token))).json()
    assert kpis["sales"]["revenue_ytd"] == 0 and kpis["sales"]["ar_outstanding"] == 0

    # No auto journal entry was posted for the production order.
    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(token))).json()["items"]
    assert not any(doc["id"] in (e["data"].get("memo") or "") for e in ledger)


@pytest.mark.asyncio
async def test_production_order_feeds_demand_only_when_finalized(client):
    """A draft production order is still being composed (not demand); finalizing releases it."""
    token = await _register(client)
    _gold, ring = await _ring(client, token)

    draft = await _production_order(client, token, ring, qty=3)
    board = {r["item_id"]: r for r in (await client.get("/manufacturing/to-make", headers=_h(token))).json()["items"]}
    assert ring not in board  # draft = not yet active demand

    assert (await client.post(f"/docs/{draft['id']}/finalize", headers=_h(token))).status_code == 200
    board = {r["item_id"]: r for r in (await client.get("/manufacturing/to-make", headers=_h(token))).json()["items"]}
    assert ring in board and board[ring]["demand"] == 3.0 and board[ring]["to_make"] == 3.0


@pytest.mark.asyncio
async def test_production_order_rejects_payment(client):
    """A production order carries no money, so recording a payment is rejected."""
    token = await _register(client)
    _gold, ring = await _ring(client, token)
    doc = await _production_order(client, token, ring, finalize=True)
    r = await client.post(f"/docs/{doc['id']}/payment", headers=_h(token),
                          json={"amount": 100, "payment_date": "2026-06-14", "bank_account": "1010"})
    assert r.status_code == 409
