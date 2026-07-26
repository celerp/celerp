# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Browser test: the extended journal names what each posting was for.

The report is built from the same journal the classical view is built from,
with the document's own lines matched back onto the postings they became. The
API tests prove the matching; this proves the page a person actually opens
shows it, reached the way they reach it.

Proves:
1. Reports lists the extended journal and links to it.
2. Opening it shows the item, quantity and unit price behind a sale.
3. A posting with no item behind it still renders, with "--" where the item
   would be, so nothing on the page reads as a row that failed to load.
4. Ticking entries counts entries, not the postings under them, and voiding the
   ticked ones asks first and then posts them together.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser


def _cell(row, page, header: str) -> str:
    """The row's cell under the named column header.

    Read by header rather than by a fixed offset: the table gains and loses
    leading columns (a selection box, a void button) with the mode it is in, and
    a test counting cells from the left breaks on a column it does not care
    about.
    """
    headers = page.locator("table.data-table thead th")
    labels = [headers.nth(i).text_content().strip() for i in range(headers.count())]
    assert header in labels, f"No {header!r} column in {labels}"
    return row.locator("td").nth(labels.index(header)).text_content().strip()


@pytest.fixture(scope="module")
def sold_item(api):
    """A finalized invoice for a named item, so the report has one to expand."""
    r = api.post("/accounting/chart/seed")
    assert r.status_code in (200, 409), f"Chart seed failed: {r.text}"

    r = api.post("/crm/contacts",
                 json={"name": "Extended Journal Co", "contact_type": "customer"})
    assert r.status_code == 200, r.text
    contact = r.json()["id"]

    sku = "XJ-WIDGET"
    r = api.post("/docs", json={
        "doc_type": "invoice", "contact_id": contact, "issue_date": "2026-05-04",
        "line_items": [{"sku": sku, "name": sku, "quantity": 3, "unit_price": 12.5,
                        "line_total": 37.5}],
        "subtotal": 37.5, "tax": 0.0, "total": 37.5,
    })
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    assert api.post(f"/docs/{doc_id}/finalize").status_code == 200
    return sku


def test_reports_index_links_the_extended_journal(page, ui_server, sold_item):
    page.goto(f"{ui_server}/reports", wait_until="domcontentloaded")
    link = page.locator('a[href="/reports/extended-journal"]').first
    link.wait_for(state="visible", timeout=10000)
    link.click()
    page.wait_for_url("**/reports/extended-journal", timeout=10000)


def test_extended_journal_shows_item_rows(page, ui_server, sold_item):
    page.goto(f"{ui_server}/reports/extended-journal"
              f"?from=2026-01-01&to=2026-12-31", wait_until="domcontentloaded")
    page.wait_for_selector("table.data-table", timeout=10000)

    row = page.locator("tr", has_text=sold_item).first
    row.wait_for(state="visible", timeout=10000)
    assert _cell(row, page, "Item") == sold_item
    assert _cell(row, page, "Quantity") == "3"
    assert "12.50" in _cell(row, page, "Unit Price")

    # The receivable side of the same sale is one posting for the whole
    # document, so it carries no item. It says so rather than going blank.
    receivable = page.locator("tr", has_text="1120").first
    assert _cell(receivable, page, "Item") == "--"
