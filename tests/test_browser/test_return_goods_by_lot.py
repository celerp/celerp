# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Returning a line's goods by lot from the bill's page.

When the server cannot work out which of a line's lots make up the quantity, the refusal
opens that line's lots on the Return Goods form. Each lot is ticked with its own quantity
and the form sends those lots.
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.browser


def _two_lots(api) -> tuple[str, str, str]:
    """A bill line of 8 received as a lot of 5 that may not be split, then a lot of 3:
    (bill id, first lot, second lot)."""
    sku = f"RBL-{uuid.uuid4().hex[:6].upper()}"
    r = api.post("/docs", json={"doc_type": "bill", "contact_id": "supplier:1", "line_items": [
        {"sku": sku, "name": sku, "quantity": 8, "unit_price": 2.0}]})
    assert r.status_code in {200, 201}, r.text
    bill = r.json()["id"]
    assert api.post(f"/docs/{bill}/finalize").status_code == 200
    for qty in (5, 3):
        r = api.post(f"/docs/{bill}/receive", json={"location_id": "", "received_items": [
            {"po_line_index": 0, "sku": sku, "name": sku, "quantity_received": qty, "receive_as": "stock"}]})
        assert r.status_code == 200, r.text
    first, second = api.get(f"/docs/{bill}").json()["received_item_ids"]
    r = api.patch(f"/items/{first}", json={"fields_changed": {"allow_splitting": {"old": None, "new": False}}})
    assert r.status_code == 200, r.text
    return bill, first, second


def test_a_line_refused_by_line_goes_back_by_lot(page, ui_server, api, monkeypatch):
    from celerp_docs import routes

    bill, first, second = _two_lots(api)
    # Hold the server's search to no work, so the return by line is refused.
    monkeypatch.setattr(routes, "_LINE_TAKES_RANGES", 0, raising=False)
    monkeypatch.setattr(routes, "_LINE_TAKES_BITS", 0, raising=False)

    page.on("dialog", lambda d: d.accept())
    page.goto(f"{ui_server}/docs/{bill}", wait_until="domcontentloaded")
    page.wait_for_selector("#li-select-all", timeout=8000)
    page.check("#li-select-all")
    page.select_option("#li-bulk-select", "li-revert")
    row = page.locator("#li-bulk-revert-btn fieldset.receive-row:visible").first
    row.wait_for(timeout=5000)
    lots = row.locator("details.return-lots")
    assert not lots.evaluate("d => d.open")

    row.locator("input[name='qty_0']").fill("7")
    with page.expect_response(lambda r: r.url.endswith(f"/docs/{bill}/return-goods"), timeout=10000):
        page.click("#li-bulk-revert-btn button[type=submit]")
    page.wait_for_selector(".toast-container .toast", timeout=10000)
    assert "Return the goods by lot instead." in page.locator(".toast-container .toast").first.inner_text()
    # The refusal opened the line's lots; the line's own quantity is not sent while they are open.
    page.wait_for_function("document.querySelector('#li-bulk-revert-btn details.return-lots').open", timeout=5000)
    # The first lot's tick box takes the focus, so the keyboard carries on from the list.
    page.wait_for_function("document.activeElement && document.activeElement.name === 'lot_0_0'", timeout=5000)
    assert row.locator("input[name='qty_0']").is_disabled()
    assert row.locator("input[name='lot_qty_0_0']").is_disabled(), "a lot sends nothing until ticked"

    row.locator("input[name='lot_0_0']").check()
    row.locator("input[name='lot_0_1']").check()
    row.locator("input[name='lot_qty_0_1']").fill("2")
    row.locator("input[name='lot_qty_0_1']").press("Escape")
    with page.expect_response(lambda r: r.url.endswith(f"/docs/{bill}/return-goods"), timeout=10000) as resp:
        page.click("#li-bulk-revert-btn button[type=submit]")
    assert resp.value.status == 204, resp.value.headers.get("hx-trigger")

    returned = api.get(f"/docs/{bill}").json()["returned_items"]
    assert [(x["item_id"], x["quantity_returned"]) for x in returned] == [(first, 5), (second, 2)]
    assert api.get(f"/items/{second}").json()["quantity"] == 1
