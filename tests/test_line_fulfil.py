# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Shipping chosen lines: a line ships its own holds and free stock, never a lot
another line of the same document holds, and only the chosen lines move."""
from __future__ import annotations

import pytest

from line_actions_support import doc, h, item, line, line_ids, lot, state  # noqa: F401

pytestmark = pytest.mark.asyncio


async def test_ship_one_of_two_lines_on_the_same_lot(client, h):
    a = await lot(client, h, "FL-1", 11)
    d = await doc(client, h, [line(a, 4, sku="FL-1"), line(a, 7, sku="FL-1")])
    _l0, l1 = await line_ids(client, h, d)
    r = await client.post(f"/docs/{d}/fulfill-lines", headers=h, json={"line_ids": [l1]})
    assert r.status_code == 200, r.text
    lines = (await state(client, h, d))["line_items"]
    assert lines[0]["item_id"] == a
    shipped = await item(client, h, lines[1]["item_id"])
    assert shipped["status"] == "sold" and float(shipped["quantity"]) == 7.0
    left = await item(client, h, a)
    assert left["status"] == "available" and float(left["quantity"]) == 4.0


async def test_ship_never_takes_another_lines_hold(client, h):
    a = await lot(client, h, "FL-2", 10)
    b = await lot(client, h, "FL-2", 2)
    d3 = await lot(client, h, "FL-2", 3)
    d = await doc(client, h, [line(a, 12, sku="FL-2"), line(b, 5, sku="FL-2")])
    l0, l1 = await line_ids(client, h, d)
    r = await client.post(f"/docs/{d}/reserve-lines", headers=h, json={"line_ids": [l1], "new_status": "reserved"})
    assert r.status_code == 200, r.text
    assert (await item(client, h, d3))["status_line_entity_id"] == l1
    short = await client.post(f"/docs/{d}/fulfill-lines", headers=h, json={"line_ids": [l0]})
    assert short.status_code == 409, short.text
    e = await lot(client, h, "FL-2", 5)
    r = await client.post(f"/docs/{d}/fulfill-lines", headers=h, json={"line_ids": [l0]})
    assert r.status_code == 200, r.text
    assert (await item(client, h, d3))["status"] == "reserved"
    assert (await item(client, h, a))["status"] == "sold"
    assert float((await item(client, h, e))["quantity"]) == 3.0


async def test_ship_a_reserved_line_ships_its_hold(client, h):
    a = await lot(client, h, "FL-3", 10)
    b = await lot(client, h, "FL-3", 15)
    d = await doc(client, h, [line(a, 20, sku="FL-3")])
    (l0,) = await line_ids(client, h, d)
    assert (await client.post(f"/docs/{d}/reserve-lines", headers=h,
                              json={"line_ids": [l0], "new_status": "reserved"})).status_code == 200
    r = await client.post(f"/docs/{d}/fulfill-lines", headers=h, json={"line_ids": [l0]})
    assert r.status_code == 200, r.text
    assert len(r.json()["fulfilled"]) == 2
    left = await item(client, h, b)
    assert left["status"] == "available" and float(left["quantity"]) == 5.0
