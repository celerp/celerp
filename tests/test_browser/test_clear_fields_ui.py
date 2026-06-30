# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser regression for https://github.com/celerp/celerp/issues/202.

An optional field that is set must be clearable from the UI (not just the API): double-click the cell,
blank the input (or pick the blank option for a select), and the field unsets. Today the numeric cell
errors on empty, and a text/barcode/select clear is rejected or stored as "", so the value can't be
removed.

Covers a numeric field (retail_price), a text field (barcode), and a SELECT field (a category
attribute). The cleared state is polled through the API (htmx PATCH is async; avoids networkidle,
which never settles on a page with a live event stream). FAILS before the fix.
"""
import time
import uuid

import pytest

pytestmark = pytest.mark.browser


@pytest.fixture()
def item_id(api):
    barcode = str(900000 + (uuid.uuid4().int % 90000))  # unique per item
    r = api.post("/items", json={
        "sku": f"CLR-UI-{uuid.uuid4().hex[:6]}",
        "sell_by": "piece",
        "name": "Clear UI Item",
        "quantity": 1,
        "category": "book",
        "barcode": barcode,
        "retail_price": 12,
    })
    assert r.status_code in {200, 201}, r.text
    return r.json()["id"]


def _poll_cleared(api, eid: str, field: str, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if api.get(f"/items/{eid}").json().get(field) is None:
            return True
        time.sleep(0.25)
    return False


def _open_editor(page, ui_server, eid: str, field: str, td_only: bool = True):
    page.goto(f"{ui_server}/inventory/{eid}", wait_until="domcontentloaded")
    # Core fields render as <td> cells; category-schema attributes render in the attributes card
    # (not a <td>), so allow a broader selector for those.
    sel = f'td[hx-get$="/field/{field}/edit"]' if td_only else f'[hx-get$="/field/{field}/edit"]'
    cell = page.locator(sel).first
    cell.wait_for(state="visible", timeout=8000)
    cell.dblclick()                     # cells edit on double-click


def test_clear_barcode_via_ui(page, ui_server, api, item_id):
    assert api.get(f"/items/{item_id}").json().get("barcode") is not None  # sanity: set
    _open_editor(page, ui_server, item_id, "barcode")
    inp = page.locator('[name="value"]').first
    inp.wait_for(state="visible", timeout=5000)
    inp.fill("")
    page.keyboard.press("Enter")
    assert _poll_cleared(api, item_id, "barcode"), "barcode not cleared from UI"


def test_clear_retail_price_via_pricing_tab(page, ui_server, api, item_id):
    """Prices are edited on the Pricing tab (autosave form) — blanking the input must clear it."""
    assert api.get(f"/items/{item_id}").json().get("retail_price") == 12  # sanity: set
    page.goto(f"{ui_server}/inventory/{item_id}?tab=pricing", wait_until="domcontentloaded")
    inp = page.locator('input[name="retail_price"]').first
    inp.wait_for(state="visible", timeout=8000)
    inp.fill("")          # blank the price
    inp.blur()            # autosaves on change
    assert _poll_cleared(api, item_id, "retail_price"), "retail_price not cleared from Pricing tab"


def test_clear_select_field_via_ui(page, ui_server, api):
    """A SELECT (dropdown) field is cleared by choosing the blank '(clear)' option."""
    r = api.patch("/companies/me/category-schema/book", json={"fields": [
        {"key": "format", "label": "Format", "type": "select",
         "options": ["Hardcover", "Paperback"], "required": False},
    ]})
    assert r.status_code in {200, 201}, r.text
    eid = api.post("/items", json={
        "sku": f"CLR-SEL-{uuid.uuid4().hex[:6]}", "sell_by": "piece",
        "name": "Sel Item", "quantity": 1, "category": "book",
    }).json()["id"]
    r = api.patch(f"/items/{eid}", json={"fields_changed": {"format": {"old": None, "new": "Hardcover"}}})
    assert r.status_code == 200, r.text
    assert api.get(f"/items/{eid}").json().get("format") == "Hardcover"  # sanity: set

    _open_editor(page, ui_server, eid, "format", td_only=False)
    sel = page.locator('select[name="value"]').first
    sel.wait_for(state="visible", timeout=5000)
    sel.select_option(value="")          # the blank "(clear)" option (issue #202)
    assert _poll_cleared(api, eid, "format"), "select field not cleared from UI"