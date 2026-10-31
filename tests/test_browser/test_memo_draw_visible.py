# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""What a user sees on a memo invoiced in part, checked on screen at 1280 px: every badge and
link the memo and its invoices carry lies whole inside its table cell, the memo says what is
still on it, and after its Rate is saved every money figure on the page agrees."""
from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import expect

from .conftest import css_class
from .test_fulfillment import _assert_no_crash, _create_item, _shipped_memo

pytestmark = pytest.mark.browser

# Every badge, link and label a document line cell shows, with its cell's box and its own, and
# whether each word of it fits the cell unbroken: laid out once more breaking only between words, it
# takes no fewer lines, so no word was split to fit.
_CLIPPED = """() => [...document.querySelectorAll('.doc-lines td .badge, .doc-lines td .drawn-from a')]
    .map(el => { const td = el.closest('td'), r = el.getBoundingClientRect(), c = td.getBoundingClientRect();
                 el.style.overflowWrap = 'normal';
                 const unbroken = el.getBoundingClientRect().height >= r.height - 1;
                 el.style.overflowWrap = '';
                 return {text: el.textContent.trim(), cell: td.className, width: r.width,
                         inside: r.width > 0 && r.left >= c.left - 1 && r.right <= c.right + 1
                                 && r.top >= c.top - 1 && r.bottom <= c.bottom + 1 && unbroken}; })"""


def _draw(api, memo: str, units: int) -> str:
    lid = api.get(f"/docs/{memo}").json()["line_items"][0]["line_id"]
    body = {"line_ids": [lid], "quantities": {lid: units}}
    preview = api.post(f"/docs/{memo}/invoice-lines", json={**body, "dry_run": True}).json()
    r = api.post(f"/docs/{memo}/invoice-lines", json={**body, "binding": preview["binding"],
                                                    "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    return r.json()["target_doc_id"]


def _shown_whole(page, url: str, expected: list[str]) -> list[dict]:
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(url, wait_until="domcontentloaded")
    _assert_no_crash(page, url)
    page.locator(".doc-lines").first.wait_for()
    shown = page.evaluate(_CLIPPED)
    for text in expected:
        assert any(text in s["text"] for s in shown), (text, shown)
    assert [s for s in shown if not s["inside"]] == [], shown
    return shown


def test_a_memo_invoiced_in_part_shows_every_badge_and_link_whole(page, ui_server, api):
    """Draw 1 of 4 onto a draft, sell 1, sell 1 and take it back: at 1280 px the memo line reads
    each outcome and links each invoice, the invoice line links its memo, and nothing is cut off."""
    memo, _ = _shipped_memo(api, f"VB-{uuid.uuid4().hex[:6]}", 4)
    draft = _draw(api, memo, 1)
    sold = _draw(api, memo, 1)
    returned = _draw(api, memo, 1)
    for invoice in (sold, returned):
        assert api.post(f"/docs/{invoice}/finalize").status_code == 200
    back = api.get(f"/docs/{returned}").json()["line_items"][0]["line_id"]
    assert api.post(f"/docs/{returned}/revert-lines", json={"line_ids": [back]}).status_code == 200
    numbers = {i: api.get(f"/docs/{i}").json()["ref_id"] for i in (draft, sold)}

    _shown_whole(page, f"{ui_server}/docs/{memo}", ["1 of 4", numbers[draft], numbers[sold], "Returned"])
    links = page.locator(".doc-lines td.col-shipped-label a.badge")
    assert sorted(links.evaluate_all("els => els.map(a => a.getAttribute('href'))")) == sorted(
        [f"/docs/{draft}", f"/docs/{sold}"])
    expect(page.locator("#doc-still-on-memo")).to_be_visible()

    memo_number = api.get(f"/docs/{memo}").json()["ref_id"]
    for invoice in (draft, sold):
        _shown_whole(page, f"{ui_server}/docs/{invoice}", [memo_number])


def test_a_memo_never_invoiced_and_one_drawn_without_fixed_amounts_say_what_is_still_on_them(page, ui_server, api):
    page.set_viewport_size({"width": 1280, "height": 900})
    for drawn in (0, 1):
        memo, _ = _shipped_memo(api, f"VS-{uuid.uuid4().hex[:6]}", 4)
        if drawn:
            _draw(api, memo, drawn)
        page.goto(f"{ui_server}/docs/{memo}", wait_until="domcontentloaded")
        still = page.locator("#doc-still-on-memo")
        expect(still).to_be_visible()
        expect(still).to_have_text(f"${10.0 * (4 - drawn):.2f}")


def test_after_a_memo_rate_is_saved_outstanding_equals_the_total(page, ui_server, api):
    """The Rate save changes the memo's total; every money figure on the page is redrawn with it,
    so Outstanding never shows the issued amount beside the new total."""
    sku = f"VO-{uuid.uuid4().hex[:6]}"
    item = _create_item(api, sku, qty=2)
    memo = api.post("/docs", json={"doc_type": "memo", "ref_id": f"VO-{uuid.uuid4().hex[:6]}", "tax": 35.0,
                                   "line_items": [{"sku": sku, "name": sku, "quantity": 2, "unit_price": 100.0,
                                                   "line_total": 200.0, "item_id": item, "sell_by": "piece"}]}).json()["id"]
    assert api.post(f"/docs/{memo}/finalize").status_code in {200, 201}
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{ui_server}/docs/{memo}", wait_until="domcontentloaded")
    expect(page.locator("#doc-outstanding")).to_have_text("$235.00")
    page.locator("[hx-get*='/field/tax_rate']").first.click()
    expect(page.locator("form.editable-cell")).not_to_have_class(css_class("htmx-added"))
    page.locator("form.editable-cell input[name=value]").fill("8")
    expect(page.locator("form.editable-cell .memo-rate-preview")).to_contain_text("$16.00")
    page.locator("form.editable-cell button[type=submit]").click()
    expect(page.locator(".toast-container .toast").last).to_contain_text("$35.00 became $16.00")
    expect(page.locator("#doc-total")).to_have_text("$216.00")
    expect(page.locator("#doc-outstanding")).to_have_text("$216.00")
    expect(page.locator("form.editable-cell")).to_have_count(0)
    stored = api.get(f"/docs/{memo}").json()
    assert (stored["total"], stored["amount_outstanding"]) == (216.0, 216.0)
