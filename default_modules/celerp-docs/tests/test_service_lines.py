# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Service items on documents: a service (inventory_type=service) has no stock, so fulfilling a
document line that references it must not be blocked by a stock-availability check."""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@svc.test"
    r = await client.post("/auth/register", json={"company_name": "Svc Co", "email": addr, "name": "A", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.mark.asyncio
async def test_fulfill_service_line_not_blocked_by_stock(client):
    """A service line (sell_by not a service unit, e.g. 'piece') with zero stock must still fulfill."""
    token = await _register(client)
    svc = (await client.post("/items", headers=_h(token), json={
        "sku": "SVC-1", "name": "Consulting", "quantity": 0, "sell_by": "piece",
        "inventory_type": "service"})).json()["id"]
    doc = (await client.post("/docs", headers=_h(token), json={"doc_type": "invoice", "line_items": [
        {"entity_id": svc, "sku": "SVC-1", "name": "Consulting", "quantity": 2, "unit_price": 100, "sell_by": "piece"}],
        "total": 200})).json()["id"]
    assert (await client.post(f"/docs/{doc}/finalize", headers=_h(token))).status_code == 200

    # Previously 409 "insufficient stock"; a service has no stock to draw, so this must succeed.
    r = await client.post(f"/docs/{doc}/fulfill-lines", headers=_h(token), json={"line_entity_ids": [svc]})
    assert r.status_code == 200, r.text

    doc_state = (await client.get(f"/docs/{doc}", headers=_h(token))).json()
    assert doc_state.get("fulfillment_status") == "fulfilled"
    # The service item itself is untouched - not consumed/sold, quantity unchanged.
    svc_state = (await client.get(f"/items/{svc}", headers=_h(token))).json()
    assert float(svc_state.get("quantity") or 0) == 0
    assert svc_state.get("status") == "available"


@pytest.mark.asyncio
async def test_fulfill_mixed_service_and_stock(client):
    """A document mixing a stocked item and a service fulfils both: stock is drawn, service is marked
    done without a stock check."""
    token = await _register(client)
    stock = (await client.post("/items", headers=_h(token), json={
        "sku": "STK-1", "name": "Widget", "quantity": 5, "sell_by": "piece"})).json()["id"]
    svc = (await client.post("/items", headers=_h(token), json={
        "sku": "SVC-2", "name": "Install", "quantity": 0, "sell_by": "piece", "inventory_type": "service"})).json()["id"]
    doc = (await client.post("/docs", headers=_h(token), json={"doc_type": "invoice", "line_items": [
        {"entity_id": stock, "sku": "STK-1", "name": "Widget", "quantity": 5, "unit_price": 10, "sell_by": "piece"},
        {"entity_id": svc, "sku": "SVC-2", "name": "Install", "quantity": 1, "unit_price": 50, "sell_by": "piece"}],
        "total": 100})).json()["id"]
    assert (await client.post(f"/docs/{doc}/finalize", headers=_h(token))).status_code == 200
    r = await client.post(f"/docs/{doc}/fulfill-lines", headers=_h(token), json={"line_entity_ids": [stock, svc]})
    assert r.status_code == 200, r.text
    assert (await client.get(f"/docs/{doc}", headers=_h(token))).json().get("fulfillment_status") == "fulfilled"
    # The stocked item is sold; the service is untouched.
    assert (await client.get(f"/items/{stock}", headers=_h(token))).json().get("status") in ("sold", "memo_out")
    assert (await client.get(f"/items/{svc}", headers=_h(token))).json().get("status") == "available"
