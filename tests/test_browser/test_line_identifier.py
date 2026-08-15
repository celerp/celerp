# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser proof for the line item identifier setting (discussion #228).

The exact reported scenario: two physical pieces SHARE one SKU and differ only
by barcode. With the setting on Barcode + SKU, scanning both onto an invoice
must show the two distinct barcodes on their rows while building, and the
print view must carry them (stacked over the SKU) with the matching header.
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.browser


def _set_mode(api, mode: str) -> None:
    r = api.patch("/companies/me", json={"settings": {"line_item_identifier": mode}})
    assert r.status_code == 200, r.text


def _seed_two_lots_one_sku(api):
    tag = uuid.uuid4().hex[:6]
    sku = f"LOT-{tag}"
    bc_a, bc_b = str(uuid.uuid4().int)[:12], str(uuid.uuid4().int)[:12]
    api.post("/items", json={"status": "available", "sku": sku, "name": "Piece A", "sell_by": "piece",
                             "quantity": 1, "barcode": bc_a})
    api.post("/items", json={"status": "available", "sku": sku, "name": "Piece B", "sell_by": "piece",
                             "quantity": 1, "barcode": bc_b})
    return sku, bc_a, bc_b


def test_scanned_barcodes_stay_visible_and_print(page, ui_server, api):
    sku, bc_a, bc_b = _seed_two_lots_one_sku(api)
    _set_mode(api, "barcode_sku")
    try:
        doc_id = api.post("/docs", json={"doc_type": "invoice", "line_items": []}).json()["id"]

        page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
        page.wait_for_selector("#scan-bar-input", timeout=8000)

        # Scan both pieces; each row must show the barcode that was just scanned.
        for bc in (bc_a, bc_b):
            page.locator("#scan-bar-input").fill(bc)
            page.locator("#scan-bar-input").press("Enter")
            page.wait_for_selector(f'[data-name="barcode_display"]:has-text("{bc}")', timeout=8000)

        assert page.locator(f'[data-name="barcode_display"]:has-text("{bc_a}")').count() == 1
        assert page.locator(f'[data-name="barcode_display"]:has-text("{bc_b}")').count() == 1

        # The scan bar autosaves asynchronously; wait until both lines are stored
        # (with their barcodes) before rendering the print view.
        import time
        for _ in range(50):
            lines = api.get(f"/docs/{doc_id}").json().get("line_items") or []
            if sorted(li.get("barcode") or "" for li in lines) == sorted([bc_a, bc_b]):
                break
            time.sleep(0.2)
        else:
            raise AssertionError(f"scanned lines never persisted with barcodes: {lines!r}")

        # The print view carries both barcodes with the mode's column header.
        page.goto(f"{ui_server}/docs/{doc_id}/print", wait_until="domcontentloaded")
        body = page.locator("body").inner_text()
        assert "Internal Server Error" not in body, f"print page errored: {body!r}"
        assert bc_a in body and bc_b in body
        assert "Barcode / SKU" in body
        assert sku in body  # secondary line still shows the shared SKU
    finally:
        _set_mode(api, "sku")  # session-scoped company: leave other tests unaffected


def test_settings_tab_renders_and_default_prints_sku_only(page, ui_server, api):
    """The Settings > Sales > Line Items tab renders the dropdown + preview, and
    default mode keeps documents barcode-free."""
    page.goto(f"{ui_server}/settings/sales?tab=line-items", wait_until="domcontentloaded")
    page.wait_for_selector("#line-item-identifier", timeout=8000)
    opts = page.locator("#line-item-identifier option")
    assert opts.count() == 3

    sku, bc_a, _bc_b = _seed_two_lots_one_sku(api)
    doc_id = api.post("/docs", json={"doc_type": "invoice", "line_items": [
        {"sku": sku, "name": "Piece A", "quantity": 1, "unit_price": 5,
         "barcode": bc_a}]}).json()["id"]
    page.goto(f"{ui_server}/docs/{doc_id}/print", wait_until="domcontentloaded")
    body = page.locator("body").inner_text()
    assert sku in body
    assert bc_a not in body
