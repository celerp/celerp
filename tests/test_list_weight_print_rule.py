# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""The Lists table's weight total follows the print's rule: each line's weight is resolved from
its item (a weight-sold line weighs its quantity in its sell_by unit, a whole-lot line takes the
item's weight, a line with no unit of its own takes the item's), then summed only when every line
shares one unit. Built from a demo company's own items."""

from __future__ import annotations

import pytest

from test_tax_acceptance_matrix import _demo_company


def _items(body):
    return body["items"] if isinstance(body, dict) else body


async def _table_row(client, h, list_id: str) -> dict:
    rows = _items((await client.get("/lists?limit=100", headers=h)).json())
    return next(r for r in rows if r["id"] == list_id)


@pytest.mark.asyncio
async def test_weight_sold_lines_total_in_their_sell_by_unit(client, session):
    auth, _ = await _demo_company(client, session, "USD")
    h = auth["headers"]
    from celerp.services.demo import seed_demo_items
    await seed_demo_items(session, auth["company_id"], auth["user_id"], vertical="gemstones")
    await session.flush()
    items = _items((await client.get("/items?limit=200", headers=h)).json())
    lots = [i for i in items if i.get("sell_by") == "carat" and float(i.get("quantity") or 0) > 0][:2]
    assert len(lots) == 2
    ids = [i.get("entity_id") or i.get("id") for i in lots]
    carats = sum(float(i["quantity"]) for i in lots)

    r = await client.post("/lists/writeoff", headers=h, json={"entity_ids": ids})
    assert r.status_code == 200, r.text
    writeoff = r.json()["id"]
    r = await client.post("/lists", headers=h, json={"list_type": "quotation", "line_items": [
        {"item_id": i, "sku": p["sku"], "name": p["name"], "quantity": float(p["quantity"]), "unit_price": 10}
        for i, p in zip(ids, lots)]})
    assert r.status_code == 200, r.text
    quotation = r.json()["id"]

    for list_id in (writeoff, quotation):
        row = await _table_row(client, h, list_id)
        assert row["has_weight"] is True
        assert row["weight_unit"] == "carat"
        assert row["total_weight"] == pytest.approx(carats)


@pytest.mark.asyncio
async def test_a_line_without_its_own_unit_takes_the_item_unit(client, session):
    auth, _ = await _demo_company(client, session, "USD")
    h = auth["headers"]
    items = _items((await client.get("/items?limit=200", headers=h)).json())
    hub, chair = (next(i for i in items if i["sku"] == sku) for sku in ("ELEC-003", "FURN-001"))
    for item, weight in ((hub, 2.5), (chair, 12)):
        eid = item.get("entity_id") or item.get("id")
        r = await client.patch(f"/items/{eid}", headers=h, json={"fields_changed": {"weight": {"new": weight}, "weight_unit": {"new": "kg"}}})
        assert r.status_code == 200, r.text

    def line(item, weight, qty=1):
        # What the line-item picker saves: the item's weight, and no unit of its own.
        return {"item_id": item.get("entity_id") or item.get("id"), "sku": item["sku"], "name": item["name"],
                "quantity": qty, "unit_price": 10, "weight": weight}

    r = await client.post("/lists", headers=h, json={"list_type": "quotation", "line_items": [line(hub, 2.5)]})
    assert r.status_code == 200, r.text
    one = await _table_row(client, h, r.json()["id"])
    assert (one["total_weight"], one["weight_unit"], one["has_weight"]) == (pytest.approx(2.5), "kg", True)

    r = await client.post("/lists", headers=h, json={"list_type": "quotation",
                                                      "line_items": [line(hub, 2.5), line(chair, 12)]})
    assert r.status_code == 200, r.text
    two = await _table_row(client, h, r.json()["id"])
    assert (two["total_weight"], two["weight_unit"]) == (pytest.approx(14.5), "kg")

    # A part of a lot with no weight on the line weighs an unknown amount: no total, never a guess.
    r = await client.post("/lists", headers=h, json={"list_type": "quotation", "line_items": [
        {**line(hub, None, qty=2), "weight": None}, line(chair, 12)]})
    assert r.status_code == 200, r.text
    part = await _table_row(client, h, r.json()["id"])
    assert (part["total_weight"], part["has_weight"]) == (None, True)
