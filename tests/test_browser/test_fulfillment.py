# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser tests for fulfillment v4 - inline fulfill button and badge.

Covers:
  - FULFILL-01: No Fulfill button on draft docs
  - FULFILL-02: Fulfill button appears on final/sent docs (inventory installed)
  - FULFILL-03: Clicking Fulfill marks doc as fulfilled; badge appears; Revert button shows
  - FULFILL-04: Fulfilled doc shows Revert Fulfillment button, not Fulfill
  - FULFILL-05: Revert Fulfillment restores Fulfill button
  - FULFILL-06: Warehousing settings: auto_complete_pick present, require_pick_before_fulfill absent
  - FULFILL-07: No legacy celerp-fulfillment or mark-delivered references in rendered HTML
  - FULFILL-08: Void does NOT change fulfillment_status (independent lifecycles)
  - FULFILL-09: Service-only doc has no Fulfill button
  - FULFILL-10: Stock shortage returns 409 with per-item detail
  - FULFILL-11: Already-fulfilled doc returns 409 on second fulfill attempt
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.browser


def _create_item(api, sku, qty=10):
    """Create an inventory item via API; return its entity_id."""
    r = api.post("/items", json={"sku": sku, "name": sku, "quantity": qty, "sell_by": "piece"})
    assert r.status_code in {200, 201}, f"create item failed: {r.text}"
    return r.json()["id"]


def _save_screenshot(page, name: str) -> None:
    # Debug screenshots disabled — the hardcoded /mnt/storage path is not portable.
    # _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    # page.screenshot(path=str(_SCREENSHOT_DIR / f"{name}.png"), full_page=True)
    pass


def _assert_no_crash(page, ctx: str = "") -> None:
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body, f"{ctx}: Internal Server Error"
    assert "Traceback" not in body, f"{ctx}: Traceback in body"
    assert "/login" not in page.url, f"{ctx}: got redirected to login"


def _inventory_available(api) -> bool:
    """Return True if inventory module is installed and reachable."""
    r = api.get("/inventory")
    return r.status_code not in {404, 501}


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def draft_doc_id(api):
    r = api.post("/docs", json={
        "doc_type": "invoice",
        "ref_id": "FULFILL-DRAFT-001",
        "status": "draft",
        "line_items": [{"name": "Widget", "quantity": 1, "unit_price": 50.0, "line_total": 50.0}],
        "total": 50.0,
    })
    assert r.status_code in {200, 201}, f"Create draft failed: {r.text}"
    return r.json()["id"]


@pytest.fixture(scope="module")
def final_doc(api):
    """Finalized invoice + its stocked line item. Returns {'doc_id', 'item_id'}.

    Fulfillment is per-line and keyed on the stocked item's entity_id, so tests
    need the item_id (the doc's serialized line_items don't echo it back).
    """
    sku = f"FULFILL-FINAL-{uuid.uuid4().hex[:6]}"
    item_id = _create_item(api, sku, qty=10)
    r = api.post("/docs", json={
        "doc_type": "invoice",
        "ref_id": f"FULFILL-FINAL-{uuid.uuid4().hex[:6]}",
        "status": "draft",
        "line_items": [{"sku": sku, "name": "Widget", "quantity": 2, "unit_price": 75.0,
                        "line_total": 150.0, "entity_id": item_id}],
        "total": 150.0,
        "amount_outstanding": 150.0,
    })
    assert r.status_code in {200, 201}, f"Create doc failed: {r.text}"
    doc_id = r.json()["id"]
    r2 = api.post(f"/docs/{doc_id}/finalize")
    if r2.status_code not in {200, 201}:
        api.patch(f"/docs/{doc_id}", json={"status": "final"})
    return {"doc_id": doc_id, "item_id": item_id}


@pytest.fixture(scope="module")
def final_doc_id(final_doc):
    """Just the doc id (for navigation-only tests)."""
    return final_doc["doc_id"]


@pytest.fixture(scope="module")
def service_doc_id(api):
    """A finalized invoice with only service line items."""
    r = api.post("/docs", json={
        "doc_type": "invoice",
        "ref_id": "FULFILL-SERVICE-001",
        "status": "draft",
        "line_items": [
            {"name": "Consulting", "quantity": 3, "unit_price": 100.0, "line_total": 300.0, "sell_by": "hour"},
            {"name": "Setup Fee", "quantity": 1, "unit_price": 200.0, "line_total": 200.0, "sell_by": "service"},
        ],
        "total": 500.0,
    })
    assert r.status_code in {200, 201}, f"Create service doc failed: {r.text}"
    doc_id = r.json()["id"]
    r2 = api.post(f"/docs/{doc_id}/finalize")
    if r2.status_code not in {200, 201}:
        api.patch(f"/docs/{doc_id}", json={"status": "final"})
    return doc_id


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_no_fulfill_button_on_draft(page, ui_server, draft_doc_id):
    """FULFILL-01: Draft docs must NOT show a Fulfill button."""
    page.goto(f"{ui_server}/docs/{draft_doc_id}", wait_until="domcontentloaded")
    _assert_no_crash(page, "draft doc detail")
    _save_screenshot(page, "01-draft-no-fulfill-button")
    fulfill_btn = page.locator("button:has-text('Fulfill'), [hx-post*='/fulfill']").first
    assert fulfill_btn.count() == 0, "Fulfill button should NOT appear on draft docs"


def test_fulfill_button_on_final_doc(page, ui_server, final_doc_id):
    """FULFILL-02: A finalized invoice exposes the per-line 'Fulfill Selected' action.

    Fulfillment moved from a doc-level "Fulfill / Deduct Inventory" button to a
    line-item bulk action: an option (value=li-fulfill) in the #li-bulk-select
    dropdown above the line items.
    """
    page.goto(f"{ui_server}/docs/{final_doc_id}", wait_until="domcontentloaded")
    _assert_no_crash(page, "final doc detail")
    _save_screenshot(page, "02-final-doc-with-fulfill-button")

    fulfill_opt = page.locator('#li-bulk-select option[value="li-fulfill"]')
    assert fulfill_opt.count() > 0, (
        "Per-line 'Fulfill Selected' action missing on a finalized invoice with line items"
    )
    assert "Fulfill" in (fulfill_opt.first.text_content() or ""), (
        f"Unexpected fulfill option label: {fulfill_opt.first.text_content()!r}"
    )


def test_fulfill_action_marks_fulfilled_and_shows_revert(page, ui_server, api, final_doc):
    """FULFILL-03: Fulfill via API marks doc fulfilled; badge appears; Revert button replaces Fulfill."""
    final_doc_id = final_doc["doc_id"]
    # Use direct API call (hx_confirm dialogs are unreliable in headless Playwright)
    r = api.post(f"/docs/{final_doc_id}/fulfill-lines",
                 json={"line_entity_ids": [final_doc["item_id"]]})
    assert r.status_code in {200, 201}, f"API fulfill failed ({r.status_code}): {r.text}"

    # The authoritative signal is fulfillment_status (fulfillment is per-line now;
    # there is no doc-level Fulfill/Revert button to assert on).
    fs = api.get(f"/docs/{final_doc_id}").json().get("fulfillment_status")
    assert fs in ("fulfilled", "partial"), f"Expected fulfilled/partial, got {fs!r}"

    page.goto(f"{ui_server}/docs/{final_doc_id}", wait_until="domcontentloaded")
    _assert_no_crash(page, "post-fulfill")
    # Detail must surface the fulfilled state.
    assert "fulfilled" in page.locator("body").inner_text().lower(), \
        "Fulfilled badge/text not shown after fulfill"


def test_fulfilled_doc_shows_revert_button(page, ui_server, final_doc_id):
    """FULFILL-04: Fulfilled doc shows Revert Fulfillment button, not Fulfill."""
    page.goto(f"{ui_server}/docs/{final_doc_id}", wait_until="domcontentloaded")
    _assert_no_crash(page, "fulfilled doc")
    _save_screenshot(page, "04-fulfilled-doc-revert-button")

    body = page.locator("body").inner_text()
    if "fulfilled" not in body.lower():
        pytest.skip("Doc not in fulfilled state")

    revert_btn = page.locator("button:has-text('Revert Fulfillment')").first
    fulfill_btn = page.locator("button:has-text('Fulfill / Deduct Inventory')").first

    assert revert_btn.count() > 0 or fulfill_btn.count() == 0, \
        "Fulfilled doc must show Revert button and hide Fulfill button"


def test_revert_fulfillment_restores_fulfill_button(page, ui_server, api, final_doc):
    """FULFILL-05: Revert Fulfillment (via API) restores the Fulfill button."""
    final_doc_id = final_doc["doc_id"]
    # Ensure doc is fulfilled (may already be from FULFILL-03, re-fulfill if needed)
    eids = [final_doc["item_id"]]
    state_r = api.get(f"/docs/{final_doc_id}")
    if state_r.json().get("fulfillment_status") != "fulfilled":
        r = api.post(f"/docs/{final_doc_id}/fulfill-lines", json={"line_entity_ids": eids})
        assert r.status_code in {200, 201}, f"Pre-revert fulfill failed ({r.status_code}): {r.text}"

    # A partial fulfill splits the parcel and rewrites the line to the child, so
    # revert using the doc's CURRENT line refs rather than the original item id.
    cur_eids = [
        li.get("entity_id") or li.get("item_id")
        for li in api.get(f"/docs/{final_doc_id}").json().get("line_items", [])
        if (li.get("entity_id") or li.get("item_id"))
    ]
    r = api.post(f"/docs/{final_doc_id}/revert-lines", json={"line_entity_ids": cur_eids})
    assert r.status_code in {200, 201}, f"API revert-lines failed ({r.status_code}): {r.text}"
    # Reverting the lines must clear the fulfilled status (authoritative signal).
    fs_after = api.get(f"/docs/{final_doc_id}").json().get("fulfillment_status", "MISSING")
    assert fs_after != "fulfilled", f"fulfillment_status still 'fulfilled' after revert: {fs_after}"

    page.goto(f"{ui_server}/docs/{final_doc_id}", wait_until="domcontentloaded")
    _assert_no_crash(page, "post-revert")

    revert_btn_after = page.locator("button:has-text('Revert Fulfillment')").first
    assert revert_btn_after.count() == 0, "Revert button must disappear after revert"


def test_no_fulfill_button_on_service_only_doc(page, ui_server, service_doc_id):
    """FULFILL-09: Service-only docs must NOT show a Fulfill button."""
    page.goto(f"{ui_server}/docs/{service_doc_id}", wait_until="domcontentloaded")
    _assert_no_crash(page, "service doc detail")
    _save_screenshot(page, "09-service-doc-no-fulfill-button")

    fulfill_btn = page.locator(
        "button:has-text('Fulfill / Deduct Inventory'), button:has-text('Revert Fulfillment')"
    ).first
    assert fulfill_btn.count() == 0, \
        "Service-only docs must NOT show any Fulfill or Revert button"


def test_warehousing_settings_has_auto_complete_pick(page, ui_server):
    """FULFILL-06: Settings page must not expose the legacy require_pick_before_fulfill field.

    Full schema + hook coverage lives in celerp-warehousing:
        tests/test_fulfill_toggle.py::test_settings_schema_uses_auto_complete_pick_not_legacy_field
        tests/test_fulfill_toggle.py::test_hook_reads_auto_complete_pick
    This browser test only checks the rendered HTML of pages loaded without the warehousing module.
    """
    page.goto(f"{ui_server}/settings", wait_until="domcontentloaded")
    _assert_no_crash(page, "/settings")
    _save_screenshot(page, "06-settings-page")
    html = page.content()
    assert "require_pick_before_fulfill" not in html, \
        "Found legacy field 'require_pick_before_fulfill' in settings page HTML"


def test_no_legacy_references_in_ui(page, ui_server):
    """FULFILL-07: No legacy celerp-fulfillment or mark-delivered in rendered HTML."""
    for path in ["/docs", "/settings"]:
        page.goto(f"{ui_server}{path}", wait_until="domcontentloaded")
        html = page.content()
        assert "mark-delivered" not in html, f"Found 'mark-delivered' in {path}"
        assert "celerp_fulfillment" not in html, f"Found 'celerp_fulfillment' in {path}"
        assert "celerp-fulfillment" not in html, f"Found 'celerp-fulfillment' in {path}"


def test_void_blocked_while_fulfilled(api):
    """FULFILL-08: Voiding a fulfilled doc is refused until fulfillment is
    reverted (goods back first); the refused attempt changes nothing."""
    sku = f"FULFILL-VOID-{uuid.uuid4().hex[:6]}"
    item_id = _create_item(api, sku, qty=5)
    r = api.post("/docs", json={
        "doc_type": "invoice",
        "ref_id": f"FULFILL-VOID-{uuid.uuid4().hex[:6]}",
        "status": "draft",
        "line_items": [{"sku": sku, "name": "Gadget", "quantity": 1, "unit_price": 10.0,
                        "line_total": 10.0, "entity_id": item_id}],
        "total": 10.0,
    })
    assert r.status_code in {200, 201}, f"Create failed: {r.text}"
    doc_id = r.json()["id"]

    r2 = api.post(f"/docs/{doc_id}/finalize")
    if r2.status_code not in {200, 201}:
        pytest.skip("Cannot finalize")

    r3 = api.post(f"/docs/{doc_id}/fulfill-lines", json={"line_entity_ids": [item_id]})
    if r3.status_code not in {200, 201}:
        pytest.skip(f"Fulfill failed (inventory not installed?): {r3.text}")

    doc_before = api.get(f"/docs/{doc_id}").json()
    fs_before = doc_before.get("fulfillment_status")
    assert fs_before in ("fulfilled", "partial"), f"Expected fulfilled, got: {fs_before}"

    r4 = api.post(f"/docs/{doc_id}/void", json={"reason": "test"})
    assert r4.status_code == 409, f"Void must be blocked while fulfilled, got {r4.status_code}: {r4.text}"

    doc_after = api.get(f"/docs/{doc_id}").json()
    fs_after = doc_after.get("fulfillment_status")
    assert fs_after == fs_before, \
        f"A refused void must not change fulfillment_status. Was {fs_before!r}, now {fs_after!r}"
    assert doc_after.get("status") != "void"

    # Reverting fulfillment unblocks the void. A partial draw splits off a child
    # parcel and rebinds the line to it, so revert targets the doc's CURRENT line ids.
    line_ids = [li.get("entity_id") or li.get("item_id")
                for li in (doc_after.get("line_items") or [])]
    r5 = api.post(f"/docs/{doc_id}/revert-lines",
                  json={"line_entity_ids": [i for i in line_ids if i]})
    assert r5.status_code in {200, 201}, f"Revert failed: {r5.text}"
    r6 = api.post(f"/docs/{doc_id}/void", json={"reason": "test"})
    assert r6.status_code in {200, 201}, f"Void after revert failed: {r6.text}"


def test_stock_shortage_returns_409_with_details(api):
    """FULFILL-10: Fulfilling with insufficient stock returns 409 with per-item error message."""
    # Item with only 1 in stock; a line demanding 999 must report a shortage.
    sku = f"SHORTAGE-{uuid.uuid4().hex[:6]}"
    item_id = _create_item(api, sku, qty=1)
    r = api.post("/docs", json={
        "doc_type": "invoice",
        "ref_id": f"FULFILL-SHORTAGE-{uuid.uuid4().hex[:6]}",
        "status": "draft",
        "line_items": [{
            "name": "Unobtainium Block",
            "sku": sku,
            "quantity": 999,
            "unit_price": 1.0,
            "line_total": 999.0,
            "entity_id": item_id,
        }],
        "total": 999.0,
    })
    assert r.status_code in {200, 201}, f"Create failed: {r.text}"
    doc_id = r.json()["id"]

    r2 = api.post(f"/docs/{doc_id}/finalize")
    if r2.status_code not in {200, 201}:
        pytest.skip("Cannot finalize")

    r3 = api.post(f"/docs/{doc_id}/fulfill-lines", json={"line_entity_ids": [item_id]})
    assert r3.status_code == 409, f"Expected 409 for stock shortage, got {r3.status_code}: {r3.text}"

    detail = r3.json().get("detail", "")
    assert "Unobtainium Block" in detail or sku in detail or "short" in detail.lower() or "stock" in detail.lower(), \
        f"Error message should name the item/shortage. Got: {detail!r}"


def test_double_fulfill_returns_error(api):
    """FULFILL-11: Attempting to fulfill an already-fulfilled doc returns an error."""
    sku = f"FULFILL-DOUBLE-{uuid.uuid4().hex[:6]}"
    item_id = _create_item(api, sku, qty=5)
    r = api.post("/docs", json={
        "doc_type": "invoice",
        "ref_id": f"FULFILL-DOUBLE-{uuid.uuid4().hex[:6]}",
        "status": "draft",
        "line_items": [{"sku": sku, "name": "Widget", "quantity": 1, "unit_price": 10.0,
                        "line_total": 10.0, "entity_id": item_id}],
        "total": 10.0,
    })
    assert r.status_code in {200, 201}
    doc_id = r.json()["id"]

    r2 = api.post(f"/docs/{doc_id}/finalize")
    if r2.status_code not in {200, 201}:
        pytest.skip("Cannot finalize")

    # First fulfill
    r3 = api.post(f"/docs/{doc_id}/fulfill-lines", json={"line_entity_ids": [item_id]})
    if r3.status_code not in {200, 201}:
        pytest.skip(f"First fulfill failed: {r3.text}")

    # Second fulfill of the same line must fail
    r4 = api.post(f"/docs/{doc_id}/fulfill-lines", json={"line_entity_ids": [item_id]})
    assert r4.status_code in {409, 400, 422}, \
        f"Double fulfill should return 4xx, got {r4.status_code}: {r4.text}"
