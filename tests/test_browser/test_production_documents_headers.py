"""On a phone or tablet, in German, the item's production documents table shows its column
headers in full; the table scrolls in its own box instead of cutting the headers short."""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("width", [390, 820])
def test_production_documents_headers_are_not_cut_short(page, ui_server, fresh_company, width):
    r = fresh_company.post("/items", json={"status": "available", "sku": f"PD-{uuid.uuid4().hex[:6]}",
                                           "sell_by": "piece", "name": "Ring", "quantity": 1})
    assert r.status_code in (200, 201), r.text
    page.context.add_cookies([{"name": "celerp_lang", "value": "de", "url": ui_server}])
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{ui_server}/inventory/{r.json()['id']}?tab=manufacturing", wait_until="load")
    page.wait_for_selector("table[id^=files-table]", timeout=10000)
    clipped = page.evaluate("""() => [...document.querySelectorAll('table[id^=files-table] th')]
        .filter(h => h.scrollWidth > h.clientWidth + 1).map(h => h.innerText.trim())""")
    assert page.locator("table[id^=files-table]").count() == 1
    assert not clipped, f"at {width}px headers cut short: {clipped}"
