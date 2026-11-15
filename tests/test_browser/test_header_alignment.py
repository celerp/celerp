# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Playwright: on every main page at a desktop width, a column of figures reads as one.

A header over figures sits right, over its own numbers, and a header over text sits centered; a number
box typed into sits right like the figures it edits, and so do its cell and header; and no header is cut short."""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

from .conftest import css_class
from .test_mobile_reflow import _MAIN_PAGES, seed_main_pages, sweep_main_pages

pytestmark = pytest.mark.browser

# Per visible table: every column whose header and first full row disagree on sitting right,
# every number box that does not sit right, and every header cut short.
_ALIGNMENT_JS = """() => {
  const FIGURES = '.cell--money, .cell--rate, .cell--weight, .cell--number, .cell--paired:not(.cell--conversion)';
  const right = el => ['right', 'end'].includes(getComputedStyle(el).textAlign);
  // A cell typed into as a number: a visible number box and no text box or select beside it.
  const numberColumn = td => [...td.querySelectorAll('input[type=number]')].some(i => i.offsetParent)
    && !td.querySelector('input:not([type=number]):not([type=hidden]):not([type=checkbox]), select, textarea');
  const out = [];
  for (const t of document.querySelectorAll('.main-content table')) {
    if (!t.offsetParent) continue;
    const head = [...t.querySelectorAll('thead tr')].pop();
    if (!head) continue;
    const ths = [...head.children];
    const row = [...t.querySelectorAll('tbody tr')].find(r => r.offsetParent && r.children.length === ths.length);
    ths.forEach((th, i) => {
      const label = th.innerText.trim();
      if (!label) return;
      if (th.scrollWidth > th.clientWidth + 1) out.push(`header cut short: ${label}`);
      if (!right(th) && getComputedStyle(th).textAlign !== 'center')
        out.push(`${label}: header over text sits ${getComputedStyle(th).textAlign}, not centered`);
      const td = row && row.children[i];
      if (!td) return;
      if (right(th) !== right(td)) out.push(`${label}: header ${getComputedStyle(th).textAlign}, cells ${getComputedStyle(td).textAlign}`);
      else if ((td.matches(FIGURES) || numberColumn(td)) && !right(td))
        out.push(`${label}: figures sit ${getComputedStyle(td).textAlign}`);
    });
    for (const inp of t.querySelectorAll('input[type=number]'))
      if (inp.offsetParent && !right(inp)) out.push(`number box ${inp.name || inp.id || inp.tagName} sits ${getComputedStyle(inp).textAlign}`);
  }
  return out;
}"""


# The money pages beyond the main ones: a paid document, the payments, a journal entry being written, the
# books and a bank's matching.
_MONEY_PAGES = ("/docs/{issued}", "/payments", "/accounting/journal/new", "/accounting/chart", "/accounting/trial-balance",
                "/accounting/general-ledger", "/accounting/bank-accounts", "/accounting/reconcile/start")


@pytest.mark.timeout(400)
def test_every_header_sits_over_its_figures(page, ui_server, fresh_company):
    api = fresh_company
    ids = seed_main_pages(api)
    issued = api.post("/docs", json={"doc_type": "invoice", "contact_id": ids["contact"], "line_items": [
        {"description": "Polished stock lot", "quantity": 2, "unit_price": 100.0, "line_total": 200.0}]}).json()["id"]
    assert api.post(f"/docs/{issued}/finalize").status_code == 200
    # A payment, so the document's payments and the payments page show their amounts.
    assert api.post(f"/docs/{issued}/payment", json={
        "amount": 50.0, "method": "transfer", "payment_date": "2026-04-26", "bank_account": "1111"}).status_code == 200
    page.set_viewport_size({"width": 1440, "height": 900})
    wrong = [f"{url}: {m}" for url in sweep_main_pages(page, ui_server, {**ids, "issued": issued},
                                                       _MAIN_PAGES + _MONEY_PAGES)
             for m in page.evaluate(_ALIGNMENT_JS)]
    assert not wrong, "\n".join(wrong)


# The draft line editor: every header and select whole, every number box wide enough to read,
# and on a phone the lines keep their widths and scroll in their own box, its hidden edge faded.
_LINE_EDITOR_JS = """() => {
  const out = [];
  const t = document.querySelector('table.doc-lines'), box = t.closest('.table-scroll-wrap');
  for (const el of t.querySelectorAll('thead th, tbody select'))
    if (el.offsetParent && el.scrollWidth > el.clientWidth + 1) out.push(`cut short: ${el.tagName === 'SELECT' ? el.selectedOptions[0].text : el.innerText.trim() || el.tagName}`);
  for (const inp of t.querySelectorAll('tbody input[type=number]'))
    if (inp.offsetParent && inp.getBoundingClientRect().width < 40) out.push(`number box ${inp.name} too narrow`);
  const scrolls = box.scrollWidth > box.clientWidth;
  return {wrong: out, scrolls};
}"""


@pytest.mark.parametrize("width,lang", [(390, "en"), (1440, "es")])
def test_the_line_editor_reads_whole_on_a_phone_and_in_spanish(page, ui_server, fresh_company, width, lang):
    ids = seed_main_pages(fresh_company)
    page.context.add_cookies([{"name": "celerp_lang", "value": lang, "url": ui_server}])
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{ui_server}/docs/{ids['doc']}", wait_until="load")
    page.wait_for_timeout(600)
    seen = page.evaluate(_LINE_EDITOR_JS)
    assert not seen["wrong"], "\n".join(seen["wrong"])
    if width < 800:
        assert seen["scrolls"], seen
        box = page.locator(".table-scroll-wrap:has(table.doc-lines)")
        expect(box).to_have_class(css_class("scroll-more-right"))
        # Scrolled to its end, the right edge no longer fades.
        box.evaluate("b => b.scrollLeft = b.scrollWidth")
        expect(box).not_to_have_class(css_class("scroll-more-right"))
