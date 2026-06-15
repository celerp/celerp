# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A freight charge line (inventory_type=freight) behaves like a service line on a document: it has no
stock, so fulfilling a document that references it must not be blocked by a stock check."""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@frtline.test"
    r = await client.post("/auth/register", json={"company_name": "FrtLine Co", "email": addr, "name": "A", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.mark.asyncio
async def test_fulfill_mixed_stock_and_freight(client):
    """A document mixing a stocked item and a freight line fulfils both: stock is drawn, the freight
    charge is marked done without a stock check and is itself untouched."""
    t = await _register(client)
    stock = (await client.post("/items", headers=_h(t), json={
        "sku": "STK-F1", "name": "Widget", "quantity": 5, "sell_by": "piece"})).json()["id"]
    frt = (await client.post("/items", headers=_h(t), json={
        "sku": "FRT-L1", "name": "Sea freight", "quantity": 0, "sell_by": "piece",
        "inventory_type": "freight", "landed_cost_kind": "freight"})).json()["id"]
    doc = (await client.post("/docs", headers=_h(t), json={"doc_type": "invoice", "line_items": [
        {"entity_id": stock, "sku": "STK-F1", "name": "Widget", "quantity": 5, "unit_price": 10, "sell_by": "piece"},
        {"entity_id": frt, "sku": "FRT-L1", "name": "Sea freight", "quantity": 1, "unit_price": 50, "sell_by": "piece"}],
        "total": 100})).json()["id"]
    assert (await client.post(f"/docs/{doc}/finalize", headers=_h(t))).status_code == 200

    r = await client.post(f"/docs/{doc}/fulfill-lines", headers=_h(t), json={"line_entity_ids": [stock, frt]})
    assert r.status_code == 200, r.text
    assert (await client.get(f"/docs/{doc}", headers=_h(t))).json().get("fulfillment_status") == "fulfilled"
    # Stocked item sold; freight charge untouched (not picked from stock).
    assert (await client.get(f"/items/{stock}", headers=_h(t))).json().get("status") in ("sold", "memo_out")
    frt_state = (await client.get(f"/items/{frt}", headers=_h(t))).json()
    assert frt_state.get("status") == "available" and float(frt_state.get("quantity") or 0) == 0
