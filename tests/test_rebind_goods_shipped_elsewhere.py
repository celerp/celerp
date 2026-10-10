# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An invoice whose goods another invoice shipped goes back to uncosted and is costed
when it ships. Shipping the gone lot says where it went and what to do; the invoice can
be reverted and its line changed to other stock, which it then ships at that stock's
cost. A line whose goods the invoice itself shipped still cannot be dropped. Both
refusals are the one ``lines.shipped_elsewhere`` key, which names the document the goods
went out on and the next step, in every catalog."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from celerp.models.projections import Projection

from stock_books import assert_settled
from test_cost_follows_goods import _doc_number, _invoice, _lots, _ship
from test_invoice_unshipped_books import _lot, _ok
from test_set_aside_goods_follow_the_lot import _per_doc

pytestmark = pytest.mark.asyncio

_LOCALES = sorted((Path(__file__).resolve().parents[1] / "ui" / "locales").glob("*.json"))


def _line(lot: str, sku: str) -> list[dict]:
    return [{"entity_id": lot, "sku": sku, "name": "Lot", "quantity": 1, "unit_price": 40.0, "line_total": 40.0}]


async def _rebind(client, auth, doc: str, new: list[dict]):
    return await client.patch(f"/docs/{doc}", headers=auth["headers"], json={
        "fields_changed": {"line_items": {"old": None, "new": new}}})


async def test_an_invoice_whose_goods_shipped_elsewhere_ships_other_stock(client, session, auth):
    sku, (L,) = await _lots(client, auth, 20.0)
    a = await _invoice(client, auth, [(L, sku, 1)])
    b = await _invoice(client, auth, [(L, sku, 1)])
    await _ship(client, auth, b, L)
    K = await _lot(client, auth, sku, 1, 25.0)
    r = await client.post(f"/docs/{a}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": [L]})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    [reason] = detail["params"]["reasons"]
    assert reason["message_key"] == "lines.shipped_elsewhere"
    nb = await _doc_number(session, auth, b)
    assert reason["params"]["docs"] == nb
    assert [w["params"] for w in reason["params"]["went"]] == [{"lines": sku, "doc": nb}]
    assert "Revert fulfillment first" in detail["message"]
    await _ok(client, auth, f"/docs/{a}/revert-to-draft")
    r = await _rebind(client, auth, a, _line(K, sku))
    assert r.status_code == 200, r.text
    await _ok(client, auth, f"/docs/{a}/finalize")
    await _ship(client, auth, a, K)
    assert await _per_doc(session, auth) == {a: 25.0, b: 20.0}
    await assert_settled(client, session, auth)


async def test_a_line_whose_goods_this_document_sent_out_cannot_be_dropped(client, session, auth):
    sku, (L, K) = await _lots(client, auth, 20.0, 25.0)
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "memo", "ref_id": "MEMO-REBIND", "line_items": _line(K, sku) + _line(L, sku), "total": 80.0})
    assert r.status_code == 200, r.text
    memo = r.json()["id"]
    await _ok(client, auth, f"/docs/{memo}/finalize")
    await _ship(client, auth, memo, L)
    session.expire_all()
    lines = (await session.get(Projection, {"company_id": auth["company_id"], "entity_id": memo})).state["line_items"]
    r = await _rebind(client, auth, memo, lines[:1])
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "lines.shipped_elsewhere"
    n = await _doc_number(session, auth, memo)
    assert detail["params"]["docs"] == n
    assert [w["params"] for w in detail["params"]["went"]] == [{"lines": sku, "doc": n}]
    assert "Revert fulfillment first" in detail["message"]


async def test_dropping_several_shipped_lines_names_each_of_them(client, session, auth):
    """A write dropping every line a memo shipped is refused once, naming all of them."""
    sku, (L,) = await _lots(client, auth, 20.0)
    other = f"{sku}-B"
    K = await _lot(client, auth, other, 1, 25.0)
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "memo", "ref_id": "MEMO-REBIND-2", "line_items": _line(L, sku) + _line(K, other), "total": 80.0})
    assert r.status_code == 200, r.text
    memo = r.json()["id"]
    await _ok(client, auth, f"/docs/{memo}/finalize")
    await _ship(client, auth, memo, L)
    await _ship(client, auth, memo, K)
    r = await _rebind(client, auth, memo, [])
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    n = await _doc_number(session, auth, memo)
    assert detail["message_key"] == "lines.shipped_elsewhere"
    assert [w["params"] for w in detail["params"]["went"]] == [{"lines": f"{sku}, {other}", "doc": n}]
    assert detail["message"].endswith(f"set the goods as available on {n}.")


async def test_shipping_goods_two_invoices_shipped_names_both_with_their_lines(client, session, auth):
    """Fulfilling lines whose goods went out on two other invoices names each invoice
    with its own lines, and the refusal ends with the next step."""
    sku, (L, K) = await _lots(client, auth, 20.0, 25.0)
    a = await _invoice(client, auth, [(L, sku, 1), (K, sku, 1)])
    b = await _invoice(client, auth, [(L, sku, 1)])
    c = await _invoice(client, auth, [(K, sku, 1)])
    await _ship(client, auth, b, L)
    await _ship(client, auth, c, K)
    r = await client.post(f"/docs/{a}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": [L, K]})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    nb, nc = await _doc_number(session, auth, b), await _doc_number(session, auth, c)
    [reason] = detail["params"]["reasons"]
    assert reason["message_key"] == "lines.shipped_elsewhere"
    assert sorted((w["params"]["doc"], w["params"]["lines"]) for w in reason["params"]["went"]) == sorted(
        [(nb, sku), (nc, sku)])
    assert detail["message"].endswith(".") and "Revert fulfillment first" in detail["message"]
    assert nb in detail["message"].rsplit("Revert fulfillment first", 1)[1]
    assert nc in detail["message"].rsplit("Revert fulfillment first", 1)[1]


@pytest.mark.parametrize("path", _LOCALES, ids=lambda p: p.stem)
async def test_the_shipped_elsewhere_refusal_names_each_document_and_the_next_step_in_every_catalog(path):
    catalog = json.loads(path.read_text(encoding="utf-8"))
    text = catalog["lines.shipped_elsewhere"]
    assert text.startswith("{went}") and "{docs}" in text and "{doc}" not in text
    went = catalog["lines.went_out_on"]
    assert "{lines}" in went and "{doc}" in went
    assert "line.protected_shipped" not in catalog
