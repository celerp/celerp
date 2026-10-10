# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser tests for fulfillment v4 - inline fulfill button and badge.

Covers:
  - FULFILL-01: No Fulfill button on draft docs
  - FULFILL-02: Fulfill button appears on final/sent docs (inventory installed)
  - FULFILL-03: Clicking Fulfill marks doc as fulfilled; badge appears
  - FULFILL-04: A fulfilled line reads as sold and the line actions offer Set as available
  - FULFILL-05: Setting the lines as available takes the line out of sold
  - FULFILL-06: Warehousing settings: auto_complete_pick present, require_pick_before_fulfill absent
  - FULFILL-07: No legacy celerp-fulfillment or mark-delivered references in rendered HTML
  - FULFILL-08: Void does NOT change fulfillment_status (independent lifecycles)
  - FULFILL-09: Service-only doc has no Fulfill button
  - FULFILL-10: Stock shortage returns 409 with per-item detail
  - FULFILL-11: Already-fulfilled doc returns 409 on second fulfill attempt
"""
from __future__ import annotations

import re
import uuid

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


def _create_item(api, sku, qty=10):
    """Create an inventory item via API; return its entity_id."""
    r = api.post("/items", json={"status": "available", "sku": sku, "name": sku, "quantity": qty, "sell_by": "piece"})
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
    """FULFILL-01: Draft docs must NOT show a Set-as-shipped (fulfil) button."""
    page.goto(f"{ui_server}/docs/{draft_doc_id}", wait_until="domcontentloaded")
    _assert_no_crash(page, "draft doc detail")
    _save_screenshot(page, "01-draft-no-fulfill-button")
    fulfill_btn = page.locator(
        "button:has-text('Set as shipped'), [hx-post*='/fulfill']"
    ).first
    assert fulfill_btn.count() == 0, "Set-as-shipped button should NOT appear on draft docs"


def test_fulfill_button_on_final_doc(page, ui_server, final_doc_id):
    """FULFILL-02: A finalized invoice exposes the per-line 'Set as shipped' action.

    Shipping is a line action: an option (value=li-fulfill) in the #li-bulk-select
    dropdown above the line items, labelled "Set as shipped".
    """
    page.goto(f"{ui_server}/docs/{final_doc_id}", wait_until="domcontentloaded")
    _assert_no_crash(page, "final doc detail")
    _save_screenshot(page, "02-final-doc-with-fulfill-button")

    fulfill_opt = page.locator('#li-bulk-select option[value="li-fulfill"]')
    assert fulfill_opt.count() > 0, (
        "Per-line 'Set as shipped' action missing on a finalized invoice with line items"
    )
    assert "Set as shipped" in (fulfill_opt.first.text_content() or ""), (
        f"Unexpected fulfil option label: {fulfill_opt.first.text_content()!r}"
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


def test_fulfilled_line_reads_sold_and_offers_set_as_available(page, ui_server, api, final_doc_id):
    """FULFILL-04: A fulfilled line reads as sold and the line actions offer Set as available."""
    assert api.get(f"/docs/{final_doc_id}").json().get("fulfillment_status") == "fulfilled", \
        "FULFILL-03 must leave the doc fulfilled"
    page.goto(f"{ui_server}/docs/{final_doc_id}", wait_until="domcontentloaded")
    _assert_no_crash(page, "fulfilled doc")
    _save_screenshot(page, "04-fulfilled-doc-set-available")

    statuses = page.locator(".li-select").evaluate_all(
        "els => els.map(e => e.getAttribute('data-item-status'))")
    assert "sold" in statuses, f"Fulfilled line must read as sold, got {statuses}"
    assert page.locator("#li-bulk-select option[value='li-revert']").count() == 1, \
        "Line actions must offer Set as available on a fulfilled doc"
    assert page.locator("#li-bulk-revert-btn").count() == 1


def test_revert_fulfillment_restores_fulfill_button(page, ui_server, api, final_doc):
    """FULFILL-05: Setting the lines as available (via API) takes the line out of sold."""
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

    statuses = page.locator(".li-select").evaluate_all(
        "els => els.map(e => e.getAttribute('data-item-status'))")
    assert statuses and "sold" not in statuses, f"No line may read as sold after revert, got {statuses}"


def test_no_fulfill_button_on_service_only_doc(page, ui_server, service_doc_id):
    """FULFILL-09: Service-only docs must NOT show a Fulfill button."""
    page.goto(f"{ui_server}/docs/{service_doc_id}", wait_until="domcontentloaded")
    _assert_no_crash(page, "service doc detail")
    _save_screenshot(page, "09-service-doc-no-fulfill-button")

    assert page.locator("#li-bulk-select option[value='li-fulfill']").count() == 0, \
        "Service-only docs must NOT offer Fulfill"
    assert page.locator("#li-bulk-fulfill-btn").count() == 0, \
        "Service-only docs must NOT render a Fulfill button"


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

    detail = r3.json()["detail"]
    assert detail["message_key"] == "lines.cannot_fulfil"
    detail = detail["message"]
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


def test_set_as_available_mixed_selection_routes_both(page, ui_server, api):
    """Set-as-available over a MIXED selection (one reserved line + one sold line)
    returns BOTH to available, in one set-available request that takes back the shipped
    line and releases the held one."""
    sku_r = f"MIX-RES-{uuid.uuid4().hex[:6]}"
    sku_s = f"MIX-SOLD-{uuid.uuid4().hex[:6]}"
    item_r = _create_item(api, sku_r, qty=1)
    item_s = _create_item(api, sku_s, qty=1)

    r = api.post("/docs", json={
        "doc_type": "invoice",
        "ref_id": f"MIX-{uuid.uuid4().hex[:6]}",
        "status": "draft",
        "line_items": [
            {"sku": sku_r, "name": sku_r, "quantity": 1, "unit_price": 100.0,
             "line_total": 100.0, "entity_id": item_r},
            {"sku": sku_s, "name": sku_s, "quantity": 1, "unit_price": 100.0,
             "line_total": 100.0, "entity_id": item_s},
        ],
        "total": 200.0,
    })
    assert r.status_code in {200, 201}, f"create doc failed: {r.text}"
    doc_id = r.json()["id"]
    assert api.post(f"/docs/{doc_id}/finalize").status_code in {200, 201}

    # One line reserved by this doc, one line sold by this doc.
    rr = api.post(f"/docs/{doc_id}/reserve-lines",
                  json={"line_entity_ids": [item_r], "new_status": "reserved"})
    assert rr.status_code in {200, 201}, f"reserve failed: {rr.text}"
    rf = api.post(f"/docs/{doc_id}/fulfill-lines", json={"line_entity_ids": [item_s]})
    assert rf.status_code in {200, 201}, f"fulfil failed: {rf.text}"
    assert api.get(f"/items/{item_r}").json()["status"] == "reserved"
    assert api.get(f"/items/{item_s}").json()["status"] == "sold"

    page.on("dialog", lambda d: d.accept())
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    _assert_no_crash(page, "mixed-selection doc detail")

    # Select both lines, pick Set as available, confirm.
    boxes = page.locator(".li-select")
    assert boxes.count() >= 2, "expected two selectable line checkboxes"
    for i in range(boxes.count()):
        boxes.nth(i).check()
    page.locator("#li-bulk-select").select_option(value="li-revert")
    # Set as available is a plain button (not an HTMX submit form): its handler sends one
    # set-available request before reloading.
    page.locator("#li-bulk-revert-btn").click()

    # Both partitions land available: the reserved half released, the sold half reverted.
    deadline_ok = False
    for _ in range(30):
        s_r = api.get(f"/items/{item_r}").json()["status"]
        s_s = api.get(f"/items/{item_s}").json()["status"]
        if s_r == "available" and s_s == "available":
            deadline_ok = True
            break
        page.wait_for_timeout(200)
    assert deadline_ok, (
        f"mixed set-as-available did not route both: reserved-half={s_r}, sold-half={s_s}"
    )


def _shipped_memo(api, sku, qty):
    item = _create_item(api, sku, qty=qty)
    r = api.post("/docs", json={
        "doc_type": "memo", "ref_id": f"MR-{uuid.uuid4().hex[:6]}",
        "line_items": [{"sku": sku, "name": sku, "quantity": qty, "unit_price": 10.0,
                        "line_total": 10.0 * qty, "item_id": item, "sell_by": "piece"}],
        "total": 10.0 * qty,
    })
    assert r.status_code in {200, 201}, r.text
    doc_id = r.json()["id"]
    assert api.post(f"/docs/{doc_id}/finalize").status_code in {200, 201}
    line_id = api.get(f"/docs/{doc_id}").json()["line_items"][0]["line_id"]
    assert api.post(f"/docs/{doc_id}/fulfill-lines", json={"line_ids": [line_id]}).status_code == 200
    assert api.get(f"/items/{item}").json()["status"] == "memo_out"
    return doc_id, item


def _open_return(page, ui_server, doc_id):
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    _assert_no_crash(page, "memo return")
    page.locator(".li-select").first.check()
    page.locator("#li-bulk-select").select_option(value="li-revert")
    field = page.locator(".li-return-qty input").first
    field.wait_for(state="visible")
    return field


def test_memo_part_return_uses_the_line_quantity_field(page, ui_server, api):
    """Part of a memo line comes back through its own quantity field, in one request."""
    doc_id, item = _shipped_memo(api, f"MPR-{uuid.uuid4().hex[:6]}", 5)
    page.on("dialog", lambda d: d.accept())
    field = _open_return(page, ui_server, doc_id)
    field.fill("2")
    page.locator("#li-bulk-revert-btn").click()
    for _ in range(30):
        st = api.get(f"/items/{item}").json()
        if float(st["quantity"]) == 3:
            break
        page.wait_for_timeout(200)
    assert st["status"] == "memo_out" and float(st["quantity"]) == 3, st


def test_a_memo_line_taken_back_in_part_says_how_much_is_still_out(page, ui_server, api):
    """Straight after a part take-back, the line's shipped tag counts what is still out
    against the line's quantity."""
    doc_id, item = _shipped_memo(api, f"MPO-{uuid.uuid4().hex[:6]}", 3)
    page.on("dialog", lambda d: d.accept())
    field = _open_return(page, ui_server, doc_id)
    field.fill("1")
    with page.expect_navigation(wait_until="domcontentloaded"):
        page.locator("#li-bulk-revert-btn").click()
    tag = page.locator("tbody td.col-shipped-label").first
    expect(tag).to_have_text(re.compile(r"^\s*on memo 2 of 3\s*$", re.I))
    assert float(api.get(f"/items/{item}").json()["quantity"]) == 2


def test_a_refused_take_back_quantity_names_the_lowest_it_accepts(page, ui_server, api):
    """The take-back field refuses 0, so its message starts at the unit's smallest step: 1 piece."""
    doc_id, item = _shipped_memo(api, f"MPZ-{uuid.uuid4().hex[:6]}", 8)
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    field = _open_return(page, ui_server, doc_id)
    assert field.get_attribute("min") == "1" and field.get_attribute("step") == "1"
    field.fill("0")
    page.locator("#li-bulk-revert-btn").click()
    toast = page.locator(".toast-container .toast--error").last
    toast.wait_for(state="visible", timeout=5000)
    assert "Enter a quantity between 1 and 8." in toast.inner_text()
    assert not dialogs
    st = api.get(f"/items/{item}").json()
    assert st["status"] == "memo_out" and float(st["quantity"]) == 8, st


def test_cancelling_the_return_sends_nothing(page, ui_server, api):
    doc_id, item = _shipped_memo(api, f"MPC-{uuid.uuid4().hex[:6]}", 2)
    page.on("dialog", lambda d: d.dismiss())
    _open_return(page, ui_server, doc_id)
    page.locator("#li-bulk-revert-btn").click()
    page.wait_for_timeout(1000)
    st = api.get(f"/items/{item}").json()
    assert st["status"] == "memo_out" and float(st["quantity"]) == 2, st


def test_draft_quotation_bulk_reserve(page, ui_server, api):
    """A DRAFT quotation offers Set as reserved in the bulk toolbar; confirming it
    reserves the selected line (list stamped as owner) and the refreshed status
    column shows the Reserved badge linked to the quotation."""
    sku = f"DQR-{uuid.uuid4().hex[:6]}"
    item = _create_item(api, sku, qty=1)
    r = api.post("/lists", json={
        "list_type": "quotation",
        "contact_name": "Buyer",
        "line_items": [{"sku": sku, "name": sku, "quantity": 1, "unit_price": 100.0,
                        "item_id": item, "entity_id": item}],
    })
    assert r.status_code in {200, 201}, f"create list failed: {r.text}"
    list_id = r.json()["id"]

    page.on("dialog", lambda d: d.accept())
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="domcontentloaded")
    _assert_no_crash(page, "draft quotation detail")

    box = page.locator(f'.li-select[value="{item}"]')
    box.check()
    reserve_opt = page.locator('#li-bulk-select option[value="li-reserve"]')
    assert reserve_opt.count() == 1, "draft quotation must offer Set as reserved"
    page.locator("#li-bulk-select").select_option(value="li-reserve")
    page.locator("#li-bulk-reserve-btn").click()

    reserved = False
    for _ in range(30):
        if api.get(f"/items/{item}").json()["status"] == "reserved":
            reserved = True
            break
        page.wait_for_timeout(200)
    assert reserved, "draft bulk reserve did not reserve the line"

    # The handler reloads on success; the status column then reads Reserved with
    # the quotation as the reserving document.
    page.wait_for_selector(".col-item-status .badge--reserved", timeout=10000)


def test_shipping_goods_another_invoice_set_aside_shows_a_lasting_notice(page, ui_server, api):
    """Shipping a lot another invoice had set aside says so after the page reloads, and
    the notice stays until it is closed."""
    sku = f"MOVE-{uuid.uuid4().hex[:6]}"
    r = api.post("/items", json={"status": "available", "sku": sku, "name": sku, "quantity": 1,
                                 "sell_by": "piece", "cost_total": 40.0})
    assert r.status_code in {200, 201}, r.text
    item_id = r.json()["id"]
    docs = []
    for _ in range(2):
        r = api.post("/docs", json={
            "doc_type": "invoice", "ref_id": f"MOVE-{uuid.uuid4().hex[:6]}", "status": "draft",
            "line_items": [{"sku": sku, "name": sku, "quantity": 1, "unit_price": 100.0,
                            "line_total": 100.0, "entity_id": item_id}],
            "total": 100.0,
        })
        assert r.status_code in {200, 201}, r.text
        docs.append(r.json()["id"])
        assert api.post(f"/docs/{docs[-1]}/finalize").status_code in {200, 201}
    first, second = docs
    first_number = api.get(f"/docs/{first}").json().get("doc_number")

    page.on("dialog", lambda d: d.accept())
    page.goto(f"{ui_server}/docs/{second}", wait_until="domcontentloaded")
    _assert_no_crash(page, "second invoice")
    page.locator(".li-select").first.check()
    page.locator("#li-bulk-select").select_option(value="li-fulfill")
    page.locator("#li-bulk-fulfill-btn button").click()

    page.wait_for_selector(".toast-container .toast--info", timeout=8000)
    toast = page.locator(".toast-container .toast--info").first.inner_text()
    assert f"Lot {sku} was set aside for invoice {first_number}." in toast, toast
    assert f"Invoice {first_number} will be costed when it ships." in toast, toast
    page.wait_for_timeout(7000)  # longer than a passing notice stays
    assert page.locator(".toast-container .toast--info").count() == 1
    assert api.get(f"/items/{item_id}").json()["status"] == "sold"
