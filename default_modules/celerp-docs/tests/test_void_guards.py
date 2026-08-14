# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Voiding is blocked while goods are out (or in) on the document.

A voided document loses its fulfillment toolbar, so voiding a memo with stones
still out (or an invoice with fulfilled lines, or a bill holding received
parcels) stranded the stock records with no UI path back. Void now requires
the goods to be settled first, mirroring the revert-to-draft guards; the 409
message surfaces in the standard error popup when the Void button is clicked.
"""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@voidguard.test"
    r = await client.post("/auth/register", json={
        "company_name": "VoidGuard Co", "email": addr, "name": "A", "password": "pw"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _finalized_doc_with_stock(client, t, doc_type: str) -> tuple[str, str]:
    sku = f"VG-{uuid.uuid4().hex[:6]}"
    item = (await client.post("/items", headers=_h(t), json={
        "status": "available", "sku": sku, "name": "Stone", "quantity": 5, "sell_by": "piece"})).json()["id"]
    doc = (await client.post("/docs", headers=_h(t), json={"doc_type": doc_type, "line_items": [
        {"entity_id": item, "sku": sku, "name": "Stone", "quantity": 5, "unit_price": 10,
         "sell_by": "piece"}], "total": 50})).json()["id"]
    assert (await client.post(f"/docs/{doc}/finalize", headers=_h(t))).status_code == 200
    return doc, item


@pytest.mark.asyncio
async def test_void_blocked_while_memo_stones_are_out(client):
    """The reported bug: a memo with memo_out stones could be voided, stranding
    the stones. Now: void 409s until fulfillment is reverted, then succeeds."""
    t = await _register(client)
    doc, item = await _finalized_doc_with_stock(client, t, "memo")

    r = await client.post(f"/docs/{doc}/fulfill-lines", headers=_h(t), json={"line_entity_ids": [item]})
    assert r.status_code == 200, r.text
    assert (await client.get(f"/items/{item}", headers=_h(t))).json().get("status") == "memo_out"

    r = await client.post(f"/docs/{doc}/void", headers=_h(t), json={})
    assert r.status_code == 409
    assert "revert fulfillment" in r.json()["detail"].lower()

    # Bring the stones back, then voiding works.
    r = await client.post(f"/docs/{doc}/revert-lines", headers=_h(t), json={"line_entity_ids": [item]})
    assert r.status_code == 200, r.text
    assert (await client.get(f"/items/{item}", headers=_h(t))).json().get("status") == "available"
    r = await client.post(f"/docs/{doc}/void", headers=_h(t), json={})
    assert r.status_code == 200, r.text
    assert (await client.get(f"/docs/{doc}", headers=_h(t))).json().get("status") == "void"


@pytest.mark.asyncio
async def test_void_blocked_on_fulfilled_invoice(client):
    """Same class of bug on invoices: sold stock must be reverted before void."""
    t = await _register(client)
    doc, item = await _finalized_doc_with_stock(client, t, "invoice")
    r = await client.post(f"/docs/{doc}/fulfill-lines", headers=_h(t), json={"line_entity_ids": [item]})
    assert r.status_code == 200, r.text

    r = await client.post(f"/docs/{doc}/void", headers=_h(t), json={})
    assert r.status_code == 409
    assert "fulfilled" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_void_blocked_on_received_bill(client):
    """Inbound flavor: a bill holding received parcels cannot be voided."""
    t = await _register(client)
    loc = (await client.post("/companies/me/locations", headers=_h(t), json={
        "name": "Main", "type": "warehouse"})).json()["id"]
    sku = f"VG-{uuid.uuid4().hex[:6]}"
    doc = (await client.post("/docs", headers=_h(t), json={"doc_type": "bill", "line_items": [
        {"sku": sku, "name": "Parcel", "quantity": 3, "unit_price": 10, "sell_by": "piece"}],
        "total": 30})).json()["id"]
    assert (await client.post(f"/docs/{doc}/finalize", headers=_h(t))).status_code == 200
    r = await client.post(f"/docs/{doc}/receive", headers=_h(t), json={
        "location_id": str(loc),
        "received_items": [{"sku": sku, "name": "Parcel", "quantity_received": 3, "cost_price": 10}]})
    assert r.status_code == 200, r.text

    r = await client.post(f"/docs/{doc}/void", headers=_h(t), json={})
    assert r.status_code == 409
    assert "return the goods" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_void_still_works_on_unfulfilled_docs(client):
    """No over-blocking: a finalized doc with nothing drawn voids as before."""
    t = await _register(client)
    doc, _item = await _finalized_doc_with_stock(client, t, "memo")
    r = await client.post(f"/docs/{doc}/void", headers=_h(t), json={})
    assert r.status_code == 200, r.text
