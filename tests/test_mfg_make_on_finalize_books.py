# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Making a product when its invoice is finalized, then shipping it.

With work orders created and completed on finalize, the run makes the product into a
lot of its own on the account for made goods. The invoice is costed from that lot, so
its cost of sale comes off the account the made stock is on, and fulfilling the line
ships the made lot and closes the invoice, leaving every inventory account carrying
exactly the stock it holds.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection
from stock_books import assert_settled

pytestmark = pytest.mark.asyncio


async def _item(client, h, sku, **kw) -> str:
    r = await client.post("/items", headers=h, json={"status": "available", "sku": sku, "name": sku,
                                                     "sell_by": "piece", **kw})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _finalize_entries(session, company_id) -> list[tuple]:
    row = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "journal_entry",
        Projection.entity_id.like("je:auto:doc:%:fin")))).scalar_one()
    return sorted((e["account"], e.get("debit") or 0.0, e.get("credit") or 0.0) for e in row.state["entries"])


async def test_an_invoice_for_a_product_made_on_finalize_is_costed_from_and_ships_the_made_lot(client, session):
    r = await client.post("/auth/register", json={"company_name": "Make Co", "email": f"a-{uuid.uuid4().hex[:8]}@mk.test",
                                                  "name": "Admin", "password": "validpass1"})
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    company_id = uuid.UUID((await client.get("/companies/me", headers=h)).json()["id"])
    auth = {"company_id": str(company_id), "headers": h}

    gold = await _item(client, h, "GOLDMK", quantity=100, cost_total=8000)  # 80 each
    ring = await _item(client, h, "RINGMK", quantity=0)
    r = await client.put(f"/manufacturing/items/{ring}/recipe", headers=h,
                         json={"output_qty": 1, "components": [{"item_id": gold, "quantity": 5}],
                               "labor": [], "overhead": []})
    assert r.status_code == 200, r.text
    r = await client.patch("/companies/me", headers=h, json={"settings": {"manufacturing": {
        "auto_create_work_orders": True, "auto_complete_work_orders": True}}})
    assert r.status_code == 200, r.text

    r = await client.post("/docs", headers=h, json={"doc_type": "invoice", "total": 0, "line_items": [
        {"item_id": ring, "sku": "RINGMK", "name": "RINGMK", "quantity": 2, "unit_price": 100}]})
    assert r.status_code in (200, 201), r.text
    doc = r.json()["id"]
    assert (await client.post(f"/docs/{doc}/finalize", headers=h)).status_code == 200

    # Cost of sale comes off the made goods account the run put the rings on.
    assert await _finalize_entries(session, company_id) == [
        ("1120", 200.0, 0.0), ("1130-P", 0.0, 800.0), ("4100", 0.0, 200.0), ("5100", 800.0, 0.0)]

    r = await client.post(f"/docs/{doc}/fulfill-lines", headers=h, json={"line_entity_ids": [ring]})
    assert r.status_code == 200, r.text
    assert r.json()["fulfillment_status"] == "fulfilled"
    assert (await client.get(f"/docs/{doc}", headers=h)).json()["fulfillment_status"] == "fulfilled"
    session.expire_all()
    await assert_settled(client, session, auth)
