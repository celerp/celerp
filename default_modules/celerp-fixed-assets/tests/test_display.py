# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A cell with nothing in it reads "--", never blank and never a zero nothing moved, and a
hint says only what holds for the page it is on."""
from __future__ import annotations

import html
import re

import pytest

from fa_fixtures import FA
from test_runs import _contributed, _managed_from, _run
from test_sales import _doc, _finalize, _sell

pytestmark = pytest.mark.asyncio

_DASH = '<span class="cell-empty">--</span>'


def _row(page: str, label: str) -> str:
    """The table row holding ``label``, without the whitespace between its tags."""
    row = page[:page.index(label)].rsplit("<tr", 1)[1] + page[page.index(label):].split("</tr>", 1)[0]
    return re.sub(r">\s+<", "><", row).strip()


async def test_depreciation_by_period_marks_an_asset_row_with_nothing_skipped(fa: FA):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-02")
    number = (await fa.asset(car))["number"]
    for path in ("/fixed-assets/reports/depreciation", "/fixed-assets/reports/depreciation/print"):
        page = (await fa.page(path, date_from="2026-01-01", date_to="2026-02-28")).text
        row = _row(page, f">{number}</a>")
        assert row.endswith(f"<td>{_DASH}</td>"), (path, row)


async def test_a_movement_marks_the_columns_it_does_not_move(fa: FA):
    from ui.i18n import t

    await _managed_from(fa)
    car = await _contributed(fa)
    await fa.operate(f"/assets/{car}/impair", {"date": "2026-06-30", "recoverable_amount": "8000"}, key="imp")
    page = (await fa.page(f"/fixed-assets/assets/{car}")).text
    row = _row(page, f"<td>{t('fixed_assets.movement.impairment')}</td>")
    assert row.count(_DASH) == 2, row


async def test_hints_hold_for_the_page_they_are_on(fa: FA):
    await _managed_from(fa)
    car = await _contributed(fa)
    revalue = html.unescape((await fa.page(f"/fixed-assets/assets/{car}/remeasure")).text)
    assert "uses the cost model, so its assets are not revalued" in revalue
    year_end = html.unescape((await fa.page("/fixed-assets/year-end")).text)
    assert "no tax" in year_end.lower() and "tax year closes" not in year_end


async def test_a_void_sale_invoice_shows_no_journal_it_would_post(fa: FA):
    from ui.i18n import t

    await _managed_from(fa)
    car = await _contributed(fa)
    invoice = await _sell(fa, car, on="2026-05-20", price="9000")
    await _finalize(fa, invoice)
    await fa.ok(await _doc(fa, invoice, "void"))
    page = (await fa.page(f"/fixed-assets/assets/{car}")).text
    assert t("fixed_assets.sale_invoice_void", invoice=(await fa.state(invoice))["doc_number"]) in page
    assert t("fixed_assets.sale_journal_pending") not in page
