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


@pytest.mark.parametrize("other_type,out_status", [("invoice", "sold"), ("memo", "memo_out")])
async def test_ship_a_line_whose_lot_went_out_on_another_record_is_refused(client, h, other_type, out_status):
    """The line names a lot another record shipped: nothing ships, no other lot stands in
    for it, and the refusal names that record and what the lot is now, in plain words."""
    from ui import i18n
    a = await lot(client, h, "FL-4", 3)
    b = await lot(client, h, "FL-4", 3)
    d = await doc(client, h, [line(a, 3, sku="FL-4")])
    other = await doc(client, h, [line(a, 3, sku="FL-4")], doc_type=other_type)
    r = await client.post(f"/docs/{other}/fulfill-lines", headers=h, json={"line_ids": await line_ids(client, h, other)})
    assert r.status_code == 200, r.text
    number = (await item(client, h, a))["status_doc_number"]
    assert number
    r = await client.post(f"/docs/{d}/fulfill-lines", headers=h, json={"line_ids": await line_ids(client, h, d)})
    assert r.status_code == 422, r.text
    left = await item(client, h, b)
    assert left["status"] == "available" and float(left["quantity"]) == 3.0
    assert (await item(client, h, a))["status"] == out_status
    detail = r.json()["detail"]
    try:
        for code, status_label in (("en", "Sold" if out_status == "sold" else "Memo Out"),
                                   ("es", "Vendido" if out_status == "sold" else "Consignación saliente")):
            i18n.set_lang(code)
            text = i18n.refusal_text(detail)
            assert number in text and status_label in text and "FL-4" in text, text
            assert out_status not in text and "'" not in text, text
    finally:
        i18n.set_lang("en")


async def test_ship_on_a_void_invoice_names_its_status_plainly(client, h):
    from ui import i18n
    a = await lot(client, h, "FL-5", 1)
    d = await doc(client, h, [line(a, 1, sku="FL-5")])
    assert (await client.post(f"/docs/{d}/void", headers=h, json={})).status_code == 200
    r = await client.post(f"/docs/{d}/fulfill-lines", headers=h, json={"line_ids": await line_ids(client, h, d)})
    assert r.status_code == 409, r.text
    try:
        for code, label in (("en", "Void"), ("es", "Anulado")):
            i18n.set_lang(code)
            text = i18n.refusal_text(r.json()["detail"])
            assert label in text and "'void'" not in text and " a invoice" not in text, text
    finally:
        i18n.set_lang("en")
