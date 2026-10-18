# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A cell with nothing in it reads "--", never blank and never a zero nothing moved, and a
hint says only what holds for the page it is on."""
from __future__ import annotations

import html
import re

import pytest

from fa_fixtures import FA
from test_disposal import _dispose
from test_runs import _contributed, _managed_from, _run
from test_sales import _doc, _finalize, _sell
from test_transfer import _location, _move

pytestmark = pytest.mark.asyncio

_DASH = '<span class="cell-empty">--</span>'


def _tight(page: str) -> str:
    """The page as read, without the whitespace around its tags."""
    return html.unescape(re.sub(r"\s*([<>])\s*", r"\1", page))


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
    corrections = html.unescape((await fa.page("/fixed-assets/corrections")).text)
    assert "Changes made with Correct on an asset's page" in corrections and "after it was added" not in corrections
    recon = re.sub(r">\s+<", "><", (await fa.page("/fixed-assets/reconciliation")).text)
    assert '<h2 class="section-title">Accounts</h2><div class="table-scroll-wrap"><table' in recon


async def test_a_movement_an_operation_recalculates_reads_as_recalculated(fa: FA):
    from ui.i18n import t

    assert t("fixed_assets.movement.depreciation") == "Depreciation recalculated"


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


async def test_a_move_review_says_what_stays_and_the_undo_review_where_the_asset_goes_back_to(fa: FA):
    await _managed_from(fa)
    car = await _contributed(fa)
    store = await _location(fa, "North store")
    moving = await fa.submit("/fixed-assets/op/move/review", {"asset_id": car, "date": "2026-02-01",
                                                               "location_id": store, "idempotency_key": "mv"})
    moving = _tight(moving.text)
    assert '<th scope="row">Location</th><td>North store</td>' in moving
    assert '<th scope="row">Custodian</th><td>Unchanged</td>' in moving
    await _move(fa, car, date="2026-02-01", location_id=store)
    undo = await fa.submit("/fixed-assets/op/undo-move/review", {"asset_id": car, "idempotency_key": "um"})
    undo = _tight(undo.text)
    assert "<td>Owner's car</td>" in undo
    assert '<th scope="row">Location</th><td>North store, back to --</td>' in undo
    assert '<th scope="row">Custodian</th><td>Unchanged</td>' in undo


async def test_a_partial_disposal_says_it_is_partial_and_what_share_and_cost_it_took_off(fa: FA):
    await _managed_from(fa)
    car = await _contributed(fa)
    review = _tight((await fa.submit("/fixed-assets/op/dispose/review", {
        "asset_id": car, "date": "2026-07-20", "reason": "scrapped", "percent": "40",
        "idempotency_key": "pd"})).text)
    assert '<th scope="row">Percent disposed of</th><td>40%</td>' in review
    await _dispose(fa, car, "2026-07-20", percent="40")
    page = _tight((await fa.page(f"/fixed-assets/assets/{car}")).text)
    assert '<h2 class="section-title">Partial disposal (40%)</h2>' in page
    assert re.search(r'<th scope="row">Cost taken off</th><td class="cell--number">\$4,800\.00</td>', page)
