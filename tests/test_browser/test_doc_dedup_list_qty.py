# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Client-side guards for document item dedup and list-quantity integrity.

Two customer-reported defects, both surfacing in the document/list line editor:

1. A non-splittable physical item scanned or picked twice onto one document must
   not create a second line. The client rejects the duplicate at the fill step,
   shows a translated message, and never appends a row or autosaves; after a
   reload the server holds the item exactly once.

2. A meaningful list row whose quantity is blank/missing must NOT silently
   coerce to 1 and save. The save aborts before any request, shows a translated
   error, and persists nothing. A quantity explicitly set to 0 is sent as 0
   rather than rewritten to 1; the per-unit quantity rule then decides it, and
   for a stocked line that rejects zero the stored value is left unchanged.
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.browser

# The English value of the new shared dedup key. The tests assert the rendered,
# translated copy (en locale in the browser context) rather than match against a
# hardcoded English literal elsewhere.
_DUP_MSG = "Item is already on this document."


def _sku_row_count(page, sku: str) -> int:
    """Count line rows whose SKU input holds *sku* as its live VALUE PROPERTY.

    A scanned/filled row sets the input's `.value` property, never the HTML
    `value` attribute, so an attribute selector (`[value="..."]`) never matches a
    client-appended row. Read the property in the page instead."""
    return page.evaluate(
        """(sku) => Array.from(document.querySelectorAll('#line-body [data-name=\"sku\"]'))"""
        """.filter(e => e.value === sku).length""",
        sku,
    )


def _wait_for_sku_row(page, sku: str, timeout: int = 8000) -> None:
    page.wait_for_function(
        """(sku) => Array.from(document.querySelectorAll('#line-body [data-name=\"sku\"]'))"""
        """.some(e => e.value === sku)""",
        arg=sku, timeout=timeout,
    )


def _nonsplittable_item(api, tag: str) -> dict:
    """Seed one non-splittable (allow_splitting False) stock item and return it."""
    sku = f"DUP-{tag}"
    barcode = str(uuid.uuid4().int)[:12]
    r = api.post("/items", json={
        "status": "available",
        "sku": sku,
        "name": f"Unique Widget {tag}",
        "sell_by": "piece",
        "quantity": 5,
        "barcode": barcode,
        "allow_splitting": False,
    })
    assert r.status_code in {200, 201}, f"item create failed: {r.text}"
    item = r.json()
    return {"id": item.get("id") or item.get("entity_id"), "sku": sku, "barcode": barcode}


# ── Defect 1: physical-duplicate guard (document scan) ──────────────────────────


def test_duplicate_scan_blocked_and_persisted(page, ui_server, api):
    """Scanning a non-splittable item that is already on the invoice shows the
    translated message, appends no second row, does not autosave, and after a
    reload the server holds the item on exactly one line."""
    tag = uuid.uuid4().hex[:6]
    item = _nonsplittable_item(api, tag)
    doc_id = api.post("/docs", json={"doc_type": "invoice", "status": "draft"}).json()["id"]

    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    page.wait_for_selector("#scan-bar-input", timeout=8000)

    inp = page.locator("#scan-bar-input")
    # First scan appends one line.
    inp.click()
    inp.fill(item["barcode"])
    inp.press("Enter")
    _wait_for_sku_row(page, item["sku"])
    assert _sku_row_count(page, item["sku"]) == 1

    # Second scan of the same non-splittable item is rejected: no second row is
    # appended (the primary invariant, red at merge-base where it appends a
    # duplicate), nothing new is autosaved, and the translated message shows.
    inp.click()
    inp.fill(item["barcode"])
    inp.press("Enter")
    page.wait_for_timeout(1500)
    assert _sku_row_count(page, item["sku"]) == 1, "the duplicate scan appended a second row"
    assert _DUP_MSG in (page.locator("#scan-bar-status").text_content() or ""), \
        "the duplicate-item message did not show"

    # Authoritative: the server holds the item on exactly one line.
    line_items = api.get(f"/docs/{doc_id}").json().get("line_items", [])
    matches = [li for li in line_items if li.get("sku") == item["sku"]]
    assert len(matches) == 1, f"server must hold one line for the item, got {line_items}"


def test_overlapping_lookup_race_no_duplicate(page, ui_server, api):
    """Two racing scans of the same non-splittable item append only one line. The
    second catalog-lookup response is held until after the first has appended its
    row, so the guard (which reads the live DOM) sees the first line and rejects
    the second append. After a reload the server holds exactly one line."""
    tag = uuid.uuid4().hex[:6]
    item = _nonsplittable_item(api, tag)
    doc_id = api.post("/docs", json={"doc_type": "invoice", "status": "draft"}).json()["id"]

    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    page.wait_for_selector("#scan-bar-input", timeout=8000)

    # Delay only the SECOND catalog-lookup so the first scan has fully appended its
    # row (and its fill has run) before the second scan's fill evaluates the guard.
    seen = {"count": 0}

    def _delay_second_lookup(route):
        if "catalog-lookup" in route.request.url:
            seen["count"] += 1
            if seen["count"] == 2:
                page.wait_for_timeout(600)
        route.continue_()

    page.route("**/docs/catalog-lookup*", _delay_second_lookup)

    inp = page.locator("#scan-bar-input")
    inp.click()
    inp.fill(item["barcode"])
    inp.press("Enter")
    _wait_for_sku_row(page, item["sku"])

    # Fire the second scan while the first line is already on the page.
    inp.click()
    inp.fill(item["barcode"])
    inp.press("Enter")

    page.wait_for_timeout(1500)
    assert _sku_row_count(page, item["sku"]) == 1, \
        "a racing duplicate scan appended a second row"

    line_items = api.get(f"/docs/{doc_id}").json().get("line_items", [])
    matches = [li for li in line_items if li.get("sku") == item["sku"]]
    assert len(matches) == 1, f"server must hold one line for the item, got {line_items}"


# ── Defect 2: list-quantity read fail-close ─────────────────────────────────────


def _seed_list_with_line(api, tag: str, quantity) -> tuple[str, str]:
    """Create a quotation list carrying one stock line at the given quantity."""
    sku = f"LQ-{tag}"
    barcode = str(uuid.uuid4().int)[:12]
    api.post("/items", json={"status": "available", "sku": sku, "name": f"List Widget {tag}",
             "sell_by": "piece", "quantity": 20, "barcode": barcode})
    list_id = api.post("/lists", json={
        "list_type": "quotation",
        "line_items": [{"sku": sku, "description": f"List Widget {tag}", "quantity": quantity,
                        "unit_price": 3.0, "barcode": barcode}],
    }).json()["id"]
    return list_id, sku


def test_blank_list_quantity_aborts_save(page, ui_server, api):
    """A meaningful list row whose quantity is cleared to blank aborts the whole
    save: nothing is sent, a translated error shows, and the server state is
    unchanged after a reload (the original quantity survives, never coerced to 1)."""
    tag = uuid.uuid4().hex[:6]
    list_id, sku = _seed_list_with_line(api, tag, quantity=7)

    page.goto(f"{ui_server}/lists/{list_id}", wait_until="domcontentloaded")
    page.wait_for_selector('#line-body [data-name="quantity"]', timeout=8000)

    qty = page.locator('#line-body [data-name="quantity"]').first
    qty.click()
    qty.fill("")
    qty.blur()

    # Let the debounced autosave either fire its request or abort.
    page.wait_for_timeout(1500)

    # Primary invariant (red at merge-base): the abort prevented any request, so
    # the server still holds the original quantity (7). At merge-base the blank
    # coerces to 1 and saves, so the server quantity becomes 1 and this fails.
    line_items = api.get(f"/lists/{list_id}").json().get("line_items", [])
    matches = [li for li in line_items if li.get("sku") == sku]
    assert len(matches) == 1, f"expected one seeded line, got {line_items}"
    assert float(matches[0].get("quantity")) == 7.0, \
        f"blank quantity must not persist (or coerce to 1); server qty is {matches[0].get('quantity')}"

    # A translated error shows on the save-status element (no success tick).
    status = page.locator("#save-status").text_content() or ""
    assert status.strip() and "✓" not in status, \
        f"expected a visible abort error on save-status, got {status!r}"


def test_list_quantity_zero_not_coerced_to_one(page, ui_server, api):
    """A zero entered on a stocked line is sent as zero, never rewritten to one.

    The client no longer coerces the value to one before saving, so the entry
    reaches the backend as zero and the existing per-unit quantity rule decides
    it. For a stocked line that rule rejects zero, so the save does not succeed
    and the stored quantity is left at its previous value, not one and not zero.
    """
    tag = uuid.uuid4().hex[:6]
    list_id, sku = _seed_list_with_line(api, tag, quantity=4)

    page.goto(f"{ui_server}/lists/{list_id}", wait_until="domcontentloaded")
    page.wait_for_selector('#line-body [data-name="quantity"]', timeout=8000)

    qty = page.locator('#line-body [data-name="quantity"]').first
    qty.click()
    qty.fill("0")
    qty.blur()

    # The save request fires and is rejected by the backend; no success tick.
    page.wait_for_timeout(2000)
    status = page.locator("#save-status").text_content() or ""
    assert "✓" not in status, \
        f"stocked zero must not save successfully; save-status was {status!r}"

    # Stored state is untouched: still the seeded value, never coerced to one.
    line_items = api.get(f"/lists/{list_id}").json().get("line_items", [])
    matches = [li for li in line_items if li.get("sku") == sku]
    assert len(matches) == 1, f"expected one seeded line, got {line_items}"
    assert float(matches[0].get("quantity")) == 4.0, \
        "a rejected zero must leave the stored quantity unchanged, not 1 or 0; " \
        f"server qty is {matches[0].get('quantity')}"
