# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A document that names a ship-from location ships only stock at that location.

Its own holds and the lot a line names are no exception: stock elsewhere is left alone,
and a line the location cannot cover is refused naming the location. A document with no
ship-from location draws from every location as before.
"""
from __future__ import annotations

import pytest


def _msg(r) -> str:
    detail = r.json()["detail"]
    return detail["message"] if isinstance(detail, dict) else detail


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _setup(client) -> tuple[dict, dict, dict]:
    r = await client.post("/auth/register", json={
        "company_name": "Ship From Co", "email": "shipfrom@test.example", "name": "Admin", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    h = _h(r.json()["access_token"])
    loc = {}
    for name in ("Front Shop", "Back Store"):
        r = await client.post("/companies/me/locations", headers=h, json={"name": name, "type": "warehouse"})
        assert r.status_code == 200, r.text
        loc[name] = r.json()["id"]
    lot = {}
    for name, qty in (("Front Shop", 3), ("Back Store", 10)):
        r = await client.post("/items", headers=h, json={
            "status": "available", "sku": "TWO-LOC", "name": "Two Places", "sell_by": "piece",
            "quantity": qty, "location_id": loc[name]})
        assert r.status_code == 200, r.text
        lot[name] = r.json()["id"]
    return h, loc, lot


async def _invoice(client, h, item_id: str, qty: float, from_location_id: str | None) -> str:
    body = {"doc_type": "invoice", "line_items": [{
        "sku": "TWO-LOC", "name": "Two Places", "item_id": item_id, "quantity": qty,
        "unit_price": 10, "line_total": 10 * qty}]}
    if from_location_id is not None:
        body["from_location_id"] = from_location_id
    r = await client.post("/docs", headers=h, json=body)
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    r = await client.post(f"/docs/{doc_id}/finalize", headers=h)
    assert r.status_code == 200, r.text
    return doc_id


async def _qty(client, h, item_id: str) -> float:
    return float((await client.get(f"/items/{item_id}", headers=h)).json()["quantity"])


async def _status(client, h, item_id: str) -> str:
    return (await client.get(f"/items/{item_id}", headers=h)).json()["status"]


@pytest.mark.asyncio
async def test_a_shortfall_at_the_ship_from_location_is_refused_naming_it(client):
    h, loc, lot = await _setup(client)
    doc_id = await _invoice(client, h, lot["Front Shop"], 5, loc["Front Shop"])
    line_id = (await client.get(f"/docs/{doc_id}", headers=h)).json()["line_items"][0]["line_id"]
    r = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=h, json={"line_ids": [line_id]})
    assert r.status_code == 409, r.text
    assert "Front Shop" in _msg(r)
    assert await _qty(client, h, lot["Back Store"]) == 10 and await _status(client, h, lot["Back Store"]) == "available"
    assert await _qty(client, h, lot["Front Shop"]) == 3 and await _status(client, h, lot["Front Shop"]) == "available"


@pytest.mark.asyncio
async def test_a_line_naming_a_lot_elsewhere_ships_from_the_ship_from_location(client):
    """The line names the Back Store lot, the document ships from Front Shop: two of
    Front Shop's three ship and the Back Store lot is untouched."""
    h, loc, lot = await _setup(client)
    doc_id = await _invoice(client, h, lot["Back Store"], 2, loc["Front Shop"])
    line_id = (await client.get(f"/docs/{doc_id}", headers=h)).json()["line_items"][0]["line_id"]
    r = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=h, json={"line_ids": [line_id]})
    assert r.status_code == 200, r.text
    assert await _qty(client, h, lot["Back Store"]) == 10 and await _status(client, h, lot["Back Store"]) == "available"
    assert await _qty(client, h, lot["Front Shop"]) == 1
    shipped = r.json()["fulfilled"]
    assert len(shipped) == 1 and lot["Back Store"] not in shipped
    part = (await client.get(f"/items/{shipped[0]}", headers=h)).json()
    assert part["location_id"] == loc["Front Shop"] and float(part["quantity"]) == 2 and part["status"] == "sold"


@pytest.mark.asyncio
async def test_a_hold_elsewhere_is_not_shipped_from_a_ship_from_location(client):
    """The line holds the whole Back Store lot; shipping from Front Shop never ships the
    hold, so the shortfall at Front Shop is refused and the hold stays as it was."""
    h, loc, lot = await _setup(client)
    doc_id = await _invoice(client, h, lot["Back Store"], 10, loc["Front Shop"])
    line_id = (await client.get(f"/docs/{doc_id}", headers=h)).json()["line_items"][0]["line_id"]
    r = await client.post(f"/docs/{doc_id}/reserve-lines", headers=h,
                          json={"line_ids": [line_id], "new_status": "reserved"})
    assert r.status_code == 200, r.text
    assert await _status(client, h, lot["Back Store"]) == "reserved"
    r = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=h, json={"line_ids": [line_id]})
    assert r.status_code == 409, r.text
    assert "Front Shop" in _msg(r)
    assert await _status(client, h, lot["Back Store"]) == "reserved"
    assert await _qty(client, h, lot["Back Store"]) == 10


@pytest.mark.asyncio
async def test_with_no_ship_from_location_a_line_draws_from_every_location(client):
    h, _loc, lot = await _setup(client)
    doc_id = await _invoice(client, h, lot["Front Shop"], 5, None)
    line_id = (await client.get(f"/docs/{doc_id}", headers=h)).json()["line_items"][0]["line_id"]
    r = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=h, json={"line_ids": [line_id]})
    assert r.status_code == 200, r.text
    assert await _status(client, h, lot["Front Shop"]) == "sold"
    assert await _qty(client, h, lot["Back Store"]) == 8


@pytest.mark.asyncio
async def test_a_ship_from_location_that_is_not_the_companys_is_refused(client):
    h, loc, lot = await _setup(client)
    line = {"sku": "TWO-LOC", "name": "Two Places", "item_id": lot["Front Shop"], "quantity": 1, "unit_price": 10}
    for bad in ("not-a-location", "00000000-0000-0000-0000-000000000001"):
        r = await client.post("/docs", headers=h, json={"doc_type": "invoice", "line_items": [line],
                                                         "from_location_id": bad})
        assert r.status_code == 422, r.text
        assert "not one of this company's locations" in _msg(r)
    r = await client.post("/docs", headers=h, json={"doc_type": "invoice", "line_items": [line],
                                                     "from_location_id": loc["Front Shop"]})
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    r = await client.patch(f"/docs/{doc_id}", headers=h,
                           json={"fields_changed": {"from_location_id": {"new": "00000000-0000-0000-0000-000000000001"}}})
    assert r.status_code == 422, r.text
    assert (await client.get(f"/docs/{doc_id}", headers=h)).json()["from_location_id"] == loc["Front Shop"]
