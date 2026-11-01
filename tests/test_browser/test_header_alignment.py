# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Playwright: on every main page at a desktop width, a column of figures reads as one.

A header over figures sits right, over its own numbers, and a header over text does not; a number
box typed into sits right like the figures it edits; and no header is cut short."""
from __future__ import annotations

import pytest

from .test_mobile_reflow import _MAIN_PAGES, seed_main_pages, sweep_main_pages

pytestmark = pytest.mark.browser

# Per visible table: every column whose header and first full row disagree on sitting right,
# every number box that does not sit right, and every header cut short.
_ALIGNMENT_JS = """() => {
  const FIGURES = '.cell--money, .cell--rate, .cell--weight, .cell--number, .cell--paired:not(.cell--conversion)';
  const right = el => ['right', 'end'].includes(getComputedStyle(el).textAlign);
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
      const td = row && row.children[i];
      if (!td) return;
      if (right(th) !== right(td)) out.push(`${label}: header ${getComputedStyle(th).textAlign}, cells ${getComputedStyle(td).textAlign}`);
      else if (td.matches(FIGURES) && !right(td)) out.push(`${label}: figures sit ${getComputedStyle(td).textAlign}`);
    });
    for (const inp of t.querySelectorAll('input[type=number]'))
      if (inp.offsetParent && !right(inp)) out.push(`number box ${inp.name || inp.className} sits ${getComputedStyle(inp).textAlign}`);
  }
  return out;
}"""


# The money pages beyond the main ones: a journal entry being written, the books and a bank's matching.
_MONEY_PAGES = ("/docs/{issued}", "/accounting/journal/new", "/accounting/chart", "/accounting/trial-balance",
                "/accounting/general-ledger", "/accounting/bank-accounts", "/accounting/reconcile/start")


@pytest.mark.timeout(400)
def test_every_header_sits_over_its_figures(page, ui_server, fresh_company):
    api = fresh_company
    ids = seed_main_pages(api)
    issued = api.post("/docs", json={"doc_type": "invoice", "contact_id": ids["contact"], "line_items": [
        {"description": "Polished stock lot", "quantity": 2, "unit_price": 100.0, "line_total": 200.0}]}).json()["id"]
    assert api.post(f"/docs/{issued}/finalize").status_code == 200
    page.set_viewport_size({"width": 1440, "height": 900})
    wrong = [f"{url}: {m}" for url in sweep_main_pages(page, ui_server, {**ids, "issued": issued},
                                                       _MAIN_PAGES + _MONEY_PAGES)
             for m in page.evaluate(_ALIGNMENT_JS)]
    assert not wrong, "\n".join(wrong)
