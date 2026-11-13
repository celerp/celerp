# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test: the Change fund panel on a posted journal from outside works in the page.

The panel arrives as a row inserted after the journal row (an afterend swap), outside the
element the request came from. Its Fund pickers are searchable comboboxes, which only open
once the page's script has set them up, so only a browser shows they can be used.

Proves:
1. A posted journal imported with a fund offers Change fund on its journal row.
2. The panel's pickers open their list and take a choice (Set all outside lines to
   Unrestricted), and that pick puts every line's picker on Unrestricted.
3. Saving with a reason moves every line of the entry off the fund.
"""
from __future__ import annotations

import uuid
from urllib.parse import quote

import pytest
from playwright.sync_api import expect

from celerp.services.business_time import business_today

from .conftest import css_class

pytestmark = pytest.mark.browser

_MEMO = "Imported for the fund panel"


@pytest.fixture(scope="module")
def outside_fund(ui_server, api):
    """Funds on, one fund, and a posted journal imported on it; yields the fund, funds off again after."""
    r = api.post("/accounting/chart/seed")
    assert r.status_code in (200, 409), r.text
    r = api.put("/accounting/funds/setting", json={"enabled": True})
    assert r.status_code == 200, r.text
    r = api.post("/accounting/funds", json={"request_key": uuid.uuid4().hex, "name": "Panel gift",
                                            "restricted": False, "purpose": None})
    assert r.status_code in (200, 201), r.text
    fund = r.json()["id"]
    today = business_today(api.get("/companies/me").json()["settings"].get("timezone")).isoformat()
    entries = [{"account": "1111", "debit": 30.0, "credit": 0}, {"account": "4100", "debit": 0, "credit": 30.0}]
    r = api.post("/accounting/import/batch", json={
        "records": [{"entity_id": f"je:{uuid.uuid4()}", "event_type": "acc.journal_entry.created", "source": "csv",
                     "idempotency_key": uuid.uuid4().hex,
                     # the company's today, the day the journal list's default range ends on
                     "data": {"status": "posted", "ts": today, "memo": _MEMO,
                              "entries": entries}}],
        "fund_choices": {"all": fund}})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
    yield fund
    api.put("/accounting/funds/setting", json={"enabled": False})


def test_change_fund_panel_pickers_open_and_save(page, ui_server, api, outside_fund):
    page.goto(f"{ui_server}/accounting?tab=journal&q={quote(_MEMO)}", wait_until="load")
    row = page.locator("tr", has_text=_MEMO).first
    row.get_by_role("button", name="Change fund").click()
    panel = page.locator("form", has=page.locator("input[name=reason]")).first
    expect(panel).to_be_visible(timeout=10000)

    set_all = panel.locator(".fund-set-all .combobox-input")
    set_all.click()
    expect(panel.locator(".fund-set-all .combobox-list")).to_have_class(css_class("open"), timeout=3000)
    set_all.fill("Unrestr")
    panel.locator(".fund-set-all .combobox-option", has_text="Unrestricted").first.click()
    expect(panel.locator("input[name=all]")).to_have_value("unrestricted")
    # The pick draws the panel again with every line's picker on that fund.
    expect(panel.locator("input[name=line_1]")).to_have_value("unrestricted")
    expect(panel.locator("input[name=line_2]")).to_have_value("unrestricted")

    panel.locator("input[name=reason]").fill("Not for this fund after all")
    panel.get_by_role("button", name="Save").click()
    expect(page.get_by_text("Fund changed.").first).to_be_visible(timeout=10000)
    expect(panel).to_have_count(0)
    accounts = api.get("/accounting/funds/balance-sheet").json()["accounts"]
    bank = next(row for row in accounts if row["code"] == "1111")
    assert not bank["amounts"].get(outside_fund), bank
