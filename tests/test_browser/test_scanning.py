# Copyright (c) 2026 Noah Severs. All rights reserved.
"""Group 10: Scanning page — loads, barcode form works."""
import pytest

pytestmark = pytest.mark.browser

# The scanning page renders an input with name="code" and id="scan-input".
_BARCODE_SELECTOR = "input#scan-input, input[name='code']"


def _skip_if_disabled(page, ui_server):
    """Skip test if celerp-inventory is not enabled in this environment."""
    resp = page.goto(f"{ui_server}/scanning", wait_until="domcontentloaded")
    if resp and resp.status == 404:
        pytest.skip("/scanning not registered (celerp-inventory not enabled)")
    return resp


def test_scanning_page_loads(page, ui_server):
    """SCAN-01: /scanning loads without 500 and renders the barcode input."""
    resp = _skip_if_disabled(page, ui_server)
    assert resp.status != 500, "/scanning returned HTTP 500"
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body
    assert "Traceback" not in body
    assert "/login" not in page.url
    # The barcode input must be present
    assert page.locator(_BARCODE_SELECTOR).count() > 0, \
        "Barcode input (name='code') not found on /scanning"


def test_scanning_barcode_submit(page, ui_server, api):
    """SCAN-02: Known barcode submits → response renders without error."""
    api.post("/items", json={
        "sku": "SCAN-TEST-001",
        "sell_by": "piece",
        "name": "Scan Test Item",
        "barcode": "1234567890128",
        "quantity": 5,
    })

    _skip_if_disabled(page, ui_server)
    page.locator(_BARCODE_SELECTOR).fill("1234567890128")
    page.keyboard.press("Enter")
    page.wait_for_load_state("networkidle", timeout=8000)

    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body
    assert "Traceback" not in body


def test_scanning_unknown_barcode(page, ui_server):
    """SCAN-03: Unknown barcode → graceful not-found response, no crash."""
    _skip_if_disabled(page, ui_server)
    page.locator(_BARCODE_SELECTOR).fill("BARCODE-DOES-NOT-EXIST-99999")
    page.keyboard.press("Enter")
    page.wait_for_load_state("networkidle", timeout=8000)

    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body
    assert "Traceback" not in body
    assert "500" not in page.url
