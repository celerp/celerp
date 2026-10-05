# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every button that imports a spreadsheet carries the same spreadsheet icon on its
left, from one shared source, and the dashboard's "Bring in your data" card says
"Import from a spreadsheet:" over its three buttons, which carry that icon too.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.browser

# Each icon is checked against the first one seen, so one icon serves every button.
_ICON_JS = """(el) => {
  const icon = el.querySelector(':scope > .import-icon');
  if (!icon || el.firstElementChild !== icon) return null;
  const svg = icon.querySelector('svg');
  if (!svg) return null;
  const i = icon.getBoundingClientRect();
  const text = [...el.childNodes].filter(n => n.nodeType === 3 && n.textContent.trim());
  const left = text.length ? (() => { const r = document.createRange(); r.selectNode(text[0]);
                                     return r.getBoundingClientRect().left; })() : i.right;
  return {markup: svg.outerHTML, left_of_text: i.right <= left + 0.5, width: i.width};
}"""

# The list pages' Import buttons and the settings pages that import a CSV.
_PAGES = [
    ("/inventory", "a[href='/inventory/import']"),
    ("/contacts/customers", "a[href='/crm/import/contacts']"),
    ("/contacts/vendors", "a[href='/crm/import/contacts']"),
    ("/docs", "a[href='/docs/import']"),
    ("/lists", "a[href='/lists/import']"),
    ("/subscriptions?direction=sales", "a[href='/subscriptions/import']"),
    ("/subscriptions?direction=purchasing", "a[href='/subscriptions/import']"),
    ("/settings/sales?tab=taxes", "a[href='/settings/import/taxes']"),
    ("/settings/inventory?tab=locations", "a[href='/settings/import/locations']"),
    ("/settings/accounting?tab=chart", "a[href='/accounting/import/chart']"),
]


def _icon(page: Page, locator) -> dict:
    expect(locator).to_be_visible()
    got = locator.evaluate(_ICON_JS)
    assert got, f"{locator} has no spreadsheet icon as its first child"
    assert got["left_of_text"], f"{locator}: the icon is not on the left of the label"
    assert got["width"] > 0
    return got


def _upload_locations(page: Page, csv: bytes) -> None:
    page.goto("/settings/import/locations")
    page.locator("input[type='file']").first.set_input_files(
        {"name": "locations.csv", "mimeType": "text/csv", "buffer": csv})
    page.locator("#csv-preview-btn").click()
    page.get_by_role("button", name="Continue to Preview").click()


def test_every_import_button_has_the_spreadsheet_icon(page: Page, fresh_company):
    page.set_viewport_size({"width": 1280, "height": 900})
    seen: set[str] = set()
    for path, sel in _PAGES:
        page.goto(path)
        seen.add(_icon(page, page.locator(sel).first)["markup"])

    # A draft document's line-item CSV import (an icon-only button).
    r = fresh_company.post("/docs", json={"doc_type": "invoice", "status": "draft", "line_items": [
        {"name": "Alpha", "quantity": 1, "unit_price": 5.0, "line_total": 5.0}], "total": 5.0})
    assert r.status_code in (200, 201), r.text
    page.goto(f"/docs/{r.json()['id']}")
    btn = page.locator("button[onclick*='csv-import-input']")
    expect(btn).to_be_visible()
    icon = btn.locator(":scope > .import-icon svg")
    expect(icon).to_have_count(1)
    seen.add(icon.evaluate("e => e.outerHTML"))

    # The shared CSV import steps: fix-and-import, import-all, import-more.
    _upload_locations(page, b"name,type\nIcon Warehouse,warehouse\n,warehouse\n")
    seen.add(_icon(page, page.locator("button", has_text="Fix & Import"))["markup"])
    _upload_locations(page, b"name,type\nIcon Warehouse,warehouse\n")
    import_all = page.locator("button", has_text="Import All 1 Rows")
    seen.add(_icon(page, import_all)["markup"])
    import_all.click()
    seen.add(_icon(page, page.locator("a", has_text="Import more"))["markup"])

    assert len(seen) == 1, "every import button uses the one shared icon"


@pytest.mark.parametrize("width", [390, 1280])
def test_card_says_import_from_a_spreadsheet_over_one_icon(page: Page, fresh_company, width):
    from ui.i18n import t
    page.set_viewport_size({"width": width, "height": 900})
    page.goto("/dashboard")
    card = page.locator("#getting-started-card")
    expect(card).to_be_visible()
    lead = card.locator(".getting-started-lead")
    expect(lead).to_have_text(t("dashboard.getting_started_from_spreadsheet"))
    links = card.locator(".getting-started-link")
    expect(links).to_have_count(3)
    lead_box = lead.bounding_box()
    marks = set()
    for i in range(3):
        link = links.nth(i)
        assert link.bounding_box()["y"] >= lead_box["y"] + lead_box["height"], "the buttons sit under the line"
        marks.add(_icon(page, link)["markup"])
    assert len(marks) == 1, "the three buttons share one icon"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
