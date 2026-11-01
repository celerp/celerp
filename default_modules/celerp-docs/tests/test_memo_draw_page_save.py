# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The document page saves an invoice by sending back every row it shows. A row drawn from a
memo comes back the way the page builds it: the lot under ``entity_id`` (no ``item_id``), no
``drawn_from``, and the measure the quantity is (carat weight, piece count) filled in from the
quantity. That save changes nothing the draw owns, so the invoice saves and issues; a real change
of product or quantity is still refused."""
from __future__ import annotations

import uuid

import pytest

from memo_draw_support import auth_full, draft, drawn, drawn_lines, key_of, ok, owner, service_line, state

pytestmark = pytest.mark.asyncio


async def _memo_out(client, h, sell_by: str, qty: float, **measures) -> tuple[str, str, str]:
    """An issued memo of one whole lot sold by *sell_by*, shipped. Returns memo, its line, the lot."""
    sku = f"PG-{uuid.uuid4().hex[:6]}"
    lot = (await ok(await client.post("/items", headers=h, json={
        "sku": sku, "name": sku, "quantity": qty, "sell_by": sell_by, "status": "available",
        "cost_total": 100.0 * qty, "retail_price": 150.0, **measures})))["id"]
    memo = (await ok(await client.post("/docs", headers=h, json={"doc_type": "memo", "line_items": [{
        "item_id": lot, "sku": sku, "description": sku, "quantity": qty, "unit_price": 150.0,
        "sell_by": sell_by}]})))["id"]
    await ok(await client.post(f"/docs/{memo}/finalize", headers=h))
    lid = (await state(client, h, memo))["line_items"][0]["line_id"]
    await ok(await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_ids": [lid]}))
    return memo, lid, lot


def _page_row(li: dict, measure: str | None) -> dict:
    """One row as the page's line save builds it from what it renders."""
    row = {"line_id": li["line_id"], "description": li.get("description") or "", "sku": li.get("sku") or "",
           "quantity": li["quantity"], "unit": li.get("unit") or li.get("sell_by") or "",
           "unit_price": li.get("unit_price") or 0, "discount_pct": li.get("discount_pct") or 0,
           "tax_rate": li.get("tax_rate") or 0, "taxes": li.get("taxes") or [],
           "line_total": li.get("line_total") or 0, "allow_splitting": True}
    if li.get("item_id") or li.get("entity_id"):
        row["entity_id"] = li.get("item_id") or li.get("entity_id")
    if measure:
        row[measure] = li["quantity"]
    return row


async def _page_save(client, h, doc_id: str, rows: list[dict]):
    st = await state(client, h, doc_id)
    return await client.patch(f"/docs/{doc_id}", headers=h, json={
        "expected_version": st["version"], "fields_changed": {"line_items": {"new": rows}}})


CASES = {"carat": ("carat", 10.0, {"weight": 10.0}, "weight"), "piece": ("piece", 1.0, {"pieces": 1}, "pieces")}


@pytest.mark.parametrize("kind", list(CASES))
async def test_page_save_of_drawn_invoice_then_issue(client, session, kind):
    sell_by, qty, measures, measure = CASES[kind]
    h = (await auth_full(session))["headers"]
    memo, lid, lot = await _memo_out(client, h, sell_by, qty, **measures)
    pf = await drawn(client, h, memo, {lid: qty}, target=await draft(client, h, lines=[service_line()]))
    doc = await state(client, h, pf)
    stored = drawn_lines(doc)[0]

    # The page saves after an edit of the other line's price.
    rows = [_page_row(li, measure if li.get("drawn_from") else None) for li in doc["line_items"]]
    plain = next(row for row, li in zip(rows, doc["line_items"]) if not li.get("drawn_from"))
    plain.update(unit_price=25.0, line_total=25.0)
    r = await _page_save(client, h, pf, rows)
    assert r.status_code == 200, r.text
    kept = drawn_lines(await state(client, h, pf))
    assert len(kept) == 1
    assert {k: kept[0].get(k) for k in ("item_id", "drawn_from", "quantity", "weight", "pieces")} \
        == {k: stored.get(k) for k in ("item_id", "drawn_from", "quantity", "weight", "pieces")}

    await ok(await client.post(f"/docs/{pf}/finalize", headers=h))
    assert (await state(client, h, pf))["status"] != "draft"
    assert (await owner(client, h, lot))[1] == pf


@pytest.mark.parametrize("change", ["quantity", "product"])
async def test_page_save_still_refuses_a_real_change(client, session, change):
    h = (await auth_full(session))["headers"]
    memo, lid, lot = await _memo_out(client, h, "carat", 10.0, weight=10.0)
    pf = await drawn(client, h, memo, {lid: 10.0})
    doc = await state(client, h, pf)
    rows = [_page_row(li, "weight") for li in doc["line_items"]]
    if change == "quantity":
        rows[0].update(quantity=4.0, weight=4.0)
    else:
        rows[0]["entity_id"] = None
    before = await state(client, h, pf)
    r = await _page_save(client, h, pf, rows)
    assert key_of(r) == "docs.drawn_line_locked", r.text
    assert (await state(client, h, pf))["version"] == before["version"]
