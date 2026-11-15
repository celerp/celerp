# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
""""All time" on every page with the date filter bar: the page passes no bound for an open
end, and each API behind it answers an open range (missing from = the first entry, missing to
= the business today) while still refusing a malformed or backwards date.

Initial conditions per test: a newly registered company signed in to the UI (owner_ui), its
API calls answered in process. The recognition tests add a deferred revenue invoice
(test_revenue_recognition helpers); the donors test adds two donors' paid gifts
(income_books._statement_donors)."""
from __future__ import annotations

import pytest

from income_books import _statement_donors
from test_revenue_recognition import _deferred_item, _freeze, _invoice, _service
from ui.i18n import t

pytestmark = pytest.mark.asyncio

AUTH = {"headers": {}}
ROLL_FORWARD = "/accounting/recognition/roll-forward"

# Every page that draws _date_filter_bar, one address per call site.
PRESET_PAGES = [
    "/accounting",
    "/donors",
    "/accounting/recognition",
    "/reports/pnl",
    "/reports/trial-balance",
    "/reports/general-ledger",
    "/reports/cash-flow",
    "/reports/extended-journal",
    "/reports/ledger/1111",
    "/reports/statement",
    "/reports/funds/activity",
    "/reports/ar-aging",
    "/reports/sales",
    "/reports/purchases",
]


async def _recognition_books(api, monkeypatch) -> None:
    _freeze(monkeypatch, "2026-01-20")
    plan = await _deferred_item(api, AUTH)
    await _invoice(api, AUTH, [_service(plan, 900, start="2026-01-01", end="2026-03-31")])
    _freeze(monkeypatch, "2026-02-02")


async def test_recognition_all_time_shows_roll_forward(owner_ui, monkeypatch):
    """All time shows the roll-forward from the first entry to today: everything deferred is in
    the period, so nothing opens and the closing equals what was deferred less recognized."""
    await _recognition_books(owner_ui.api, monkeypatch)
    r = await owner_ui.get("/accounting/recognition", params={"preset": "all"})
    assert r.status_code == 200, r.text[:300]
    assert t("recognition.roll_forward") in r.text and 'class="error-banner"' not in r.text

    roll = await owner_ui.api.get(ROLL_FORWARD)
    assert roll.status_code == 200, roll.text
    body = roll.json()
    assert float(body["opening"]) == 0 and float(body["deferred"]) == 900
    bounded = (await owner_ui.api.get(ROLL_FORWARD, params={"date_from": "2026-01-01",
                                                            "date_to": "2026-02-02"})).json()
    assert bounded["closing"] == body["closing"]


@pytest.mark.parametrize("params", [
    {"date_from": "2026-13-01"},
    {"date_to": "20260105"},
    {"date_from": "2026-03-01", "date_to": "2026-01-01"},
])
async def test_recognition_refuses_a_malformed_date(owner_ui, monkeypatch, params):
    await _recognition_books(owner_ui.api, monkeypatch)
    r = await owner_ui.api.get(ROLL_FORWARD, params=params)
    assert r.status_code == 422, r.text


async def test_donors_all_time_lists_every_gift_and_its_statement(owner_ui):
    """All time lists every donor's gifts, and the statement links and the send name the period
    the API resolved, so the send and the PDF cover what the page shows."""
    donors = await _statement_donors(owner_ui.api, AUTH)
    r = await owner_ui.get("/donors", params={"preset": "all"})
    assert r.status_code == 200, r.text[:300]
    assert 'class="error-banner"' not in r.text and "Cy Donor" in r.text and "75.00" in r.text
    preview = (await owner_ui.api.get("/donors/statements/preview")).json()
    period = preview["period"]
    assert period["from"] and period["to"]
    assert f"statement.pdf?from={period['from']}&amp;to={period['to']}" in r.text
    pdf = await owner_ui.api.get(f"/donors/{donors['email']}/statement.pdf")
    assert pdf.status_code == 200 and pdf.headers["content-type"].startswith("application/pdf")
    bad = await owner_ui.api.get("/donors/statements/preview", params={"from": "2026-02-30"})
    assert bad.status_code == 422


@pytest.mark.parametrize("page", PRESET_PAGES)
async def test_every_date_preset_page_accepts_all_time(owner_ui, monkeypatch, page):
    """Each page answers preset=all with its screen, never an error, with books that reach the
    roll-forward and the donors list."""
    await _recognition_books(owner_ui.api, monkeypatch)
    await _statement_donors(owner_ui.api, AUTH)
    r = await owner_ui.get(page, params={"preset": "all"})
    assert r.status_code == 200, r.text[:300]
    assert 'class="error-banner"' not in r.text, page
    assert "preset-btn--active" in r.text and "preset=all" in r.text
