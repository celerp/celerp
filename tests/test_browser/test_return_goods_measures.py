# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Returning part of a received parcel to the supplier with the weight of what goes back.

A parcel sold by the piece that also carries a weight offers an optional weight field on its
Return Goods row. A weight given there is what leaves; the parcel keeps the rest. Left blank,
the weight of both parts is unknown, never 0.
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.browser


def _received_parcel(api, *, weight=15) -> tuple[str, str, str]:
    """A finalized bill that received one parcel of 5 pieces weighing ``weight`` carat:
    (bill id, parcel id, sku)."""
    sku = f"RGM-{uuid.uuid4().hex[:6].upper()}"
    r = api.post("/items", json={"sku": sku, "name": sku, "status": "available", "inventory_type": "stocked",
                                 "sell_by": "piece", "allow_splitting": True, "quantity": 0})
    assert r.status_code in {200, 201}, r.text
    r = api.post("/docs", json={"doc_type": "bill", "contact_id": "supplier:1", "line_items": [
        {"sku": sku, "name": sku, "quantity": 5, "unit_price": 10.0}]})
    assert r.status_code in {200, 201}, r.text
    bill = r.json()["id"]
    assert api.post(f"/docs/{bill}/finalize").status_code == 200
    r = api.post(f"/docs/{bill}/receive", json={"location_id": "", "received_items": [
        {"po_line_index": 0, "sku": sku, "name": sku, "quantity_received": 5, "receive_as": "stock"}]})
    assert r.status_code == 200, r.text
    parcel = api.get(f"/docs/{bill}").json()["received_item_ids"][0]
    r = api.patch(f"/items/{parcel}", json={"fields_changed": {
        "weight": {"old": None, "new": weight}, "weight_unit": {"old": None, "new": "carat"}}})
    assert r.status_code == 200, r.text
    return bill, parcel, sku


def _parts(api, parcel: str, sku: str) -> tuple[dict, dict]:
    """(the parcel as kept, the part split off and sent back)."""
    lots = api.get("/items", params={"q": sku, "status": "all", "limit": 50}).json()["items"]
    kept = api.get(f"/items/{parcel}").json()
    [back] = [i for i in lots if i.get("split_from") == parcel]
    return kept, back


def _open_return_row(page, ui_server: str, bill: str):
    page.on("dialog", lambda d: d.accept())
    page.goto(f"{ui_server}/docs/{bill}", wait_until="domcontentloaded")
    page.wait_for_selector("#li-select-all", timeout=8000)
    page.check("#li-select-all")
    page.select_option("#li-bulk-select", "li-revert")
    page.wait_for_selector("#li-bulk-revert-btn:visible", timeout=5000)
    return page.locator("#li-bulk-revert-btn fieldset.receive-row:visible").first


def _submit_return(page, bill: str) -> None:
    with page.expect_response(lambda r: r.url.endswith(f"/docs/{bill}/return-goods"), timeout=10000) as resp:
        page.click("#li-bulk-revert-btn button[type=submit]")
    assert resp.value.status == 204, resp.value.headers.get("hx-trigger")


def test_a_weight_given_on_return_reaches_both_parts(page, ui_server, api):
    bill, parcel, sku = _received_parcel(api)
    row = _open_return_row(page, ui_server, bill)
    weight = row.locator("input.li-measure-input[name='weight_0']")
    # Optional and blank by default; a piece-sold parcel offers no pieces field (its quantity is its pieces).
    assert weight.input_value() == ""
    assert row.locator("input[name='pieces_0']").count() == 0
    assert row.locator(".receive-row__hint").inner_text().strip()
    row.locator("input.li-qty-input").fill("2")
    weight.fill("6")
    _submit_return(page, bill)

    kept, back = _parts(api, parcel, sku)
    assert (kept["quantity"], kept["weight"]) == (3, 9)
    assert (back["quantity"], back["weight"], back["status"]) == (2, 6, "disposed")


def test_a_blank_weight_leaves_both_parts_unknown(page, ui_server, api):
    bill, parcel, sku = _received_parcel(api)
    row = _open_return_row(page, ui_server, bill)
    row.locator("input.li-qty-input").fill("2")
    _submit_return(page, bill)

    kept, back = _parts(api, parcel, sku)
    assert kept["quantity"] == 3 and back["quantity"] == 2
    assert kept.get("weight") is None and back.get("weight") is None
    # The kept parcel's page shows its weight as not known, never 0.
    page.goto(f"{ui_server}/inventory/{parcel}", wait_until="domcontentloaded")
    weight = page.locator("td[data-col='weight'] .paired-primary")
    weight.wait_for(timeout=8000)
    assert weight.inner_text().strip() == "--"


def test_a_weight_the_server_refuses_shows_as_a_toast(page, ui_server, api):
    bill, parcel, sku = _received_parcel(api)
    row = _open_return_row(page, ui_server, bill)
    row.locator("input.li-qty-input").fill("2")
    row.locator("input.li-measure-input[name='weight_0']").fill("40")
    with page.expect_response(lambda r: r.url.endswith(f"/docs/{bill}/return-goods"), timeout=10000):
        page.click("#li-bulk-revert-btn button[type=submit]")
    page.wait_for_selector(".toast-container .toast", timeout=10000)
    assert api.get(f"/items/{parcel}").json()["quantity"] == 5
