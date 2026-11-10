# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A refused change made in an always-open control goes back to the saved value.

The agreement's Billing section edits its settings in selects and number fields that save on
change. When the server refuses a change, the toast says why and the control must show what
is saved again; left on the refused value, the page reads as if the change had been made.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser

CATCHUP = "#sub-billing input[hx-patch$='/catchup_periods']"
SEND = "#sub-billing select[hx-patch$='/auto_send']"


def _agreement(api, doc_type: str) -> str:
    r = api.post("/docs", json={
        "doc_type": doc_type, "frequency": "monthly", "start_date": "2026-01-01",
        "line_items": [{"description": "Service", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}],
    })
    assert r.status_code in {200, 201}, r.text
    eid = r.json()["id"]
    r = api.post(f"/subscriptions/{eid}/activate")
    assert r.status_code == 200, r.text
    return eid


def _saved(api, eid: str) -> dict:
    return api.get(f"/docs/{eid}").json()


def _refused(page, action) -> None:
    with page.expect_response(lambda r: r.request.method == "PATCH" and "/schedule/" in r.url):
        action()
    expect(page.locator(".toast").first).to_be_visible()


def test_a_refused_catch_up_window_shows_the_saved_window_again(page, ui_server, fresh_company):
    eid = _agreement(fresh_company, "subscription_invoice")
    page.goto(f"{ui_server}/subscriptions/{eid}", wait_until="domcontentloaded")
    field = page.locator(CATCHUP)
    field.wait_for(state="visible", timeout=8000)
    field.fill("13")
    _refused(page, lambda: field.press("Tab"))
    expect(field).to_have_value(str(_saved(fresh_company, eid).get("catchup_periods") or 0))


def test_a_refused_send_setting_shows_the_saved_setting_again(page, ui_server, fresh_company):
    eid = _agreement(fresh_company, "subscription_po")
    page.goto(f"{ui_server}/subscriptions/{eid}", wait_until="domcontentloaded")
    select = page.locator(SEND)
    select.wait_for(state="visible", timeout=8000)
    _refused(page, lambda: select.select_option("true"))
    assert not _saved(fresh_company, eid).get("auto_send")
    expect(select).to_have_value("false")
