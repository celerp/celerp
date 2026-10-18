# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A refusal says what is wrong in plain words and, where there is one, the way on: one
sentence per rule, figures as money, dates the company's own, and a link to the page that
fixes it."""
from __future__ import annotations

import html
from datetime import timedelta

import pytest

from fa_fixtures import FA
from test_runs import _contributed, _managed_from, _run
from test_sales import _finalize, _sell

pytestmark = pytest.mark.asyncio


def _message(response) -> str:
    detail = response.json()["detail"]
    return detail["message"] if isinstance(detail, dict) else detail


def _params(response) -> dict:
    detail = response.json()["detail"]
    return detail.get("params") or {} if isinstance(detail, dict) else {}


async def test_a_price_at_or_below_zero_gets_one_sentence(fa: FA):
    await _managed_from(fa)
    car = await _contributed(fa)
    said = set()
    for price in ("-5", "0"):
        r = await fa.post(f"/assets/{car}/sell", {"date": "2026-05-20", "price": price,
                                                  "contact_id": await fa.buyer(), "preview": True, "op_key": "p"})
        assert r.status_code == 422, r.text
        said.add(_message(r))
    assert said == {"The price must be more than zero."}


async def test_a_missing_or_unreadable_date_or_month_says_what_to_enter(fa: FA):
    await _managed_from(fa)
    car = await _contributed(fa)
    blank = await fa.post(f"/assets/{car}/dispose", {"date": "", "reason": "lost", "preview": True, "op_key": "d"})
    assert _message(blank) == "Enter the disposal date."
    wrong = await fa.post(f"/assets/{car}/dispose", {"date": "soon", "reason": "lost", "preview": True,
                                                     "op_key": "d"})
    assert _message(wrong) == "The disposal date soon is not a date. Enter it as YYYY-MM-DD, such as 2026-09-30."
    month = await fa.post("/runs", {"month": "soon", "preview": True, "op_key": "m"})
    assert _message(month) == "Enter the month as YYYY-MM, such as 2026-09."


async def test_a_month_already_posted_links_its_run(fa: FA):
    await _managed_from(fa)
    await _contributed(fa)
    await _run(fa, "2026-03")
    again = await fa.post("/runs", {"month": "2026-03", "preview": True, "op_key": "again"})
    assert again.status_code == 409
    assert _message(again) == "Depreciation for 2026-03 is already posted. Open its run to see it or reverse it."
    assert _params(again) == {"href": "/fixed-assets/runs/2026-03", "action": "fixed_assets.action.open_run"}


async def test_amounts_in_a_refusal_read_as_money(fa: FA):
    await _managed_from(fa)
    car = await _contributed(fa)
    r = await fa.post(f"/assets/{car}/impair", {"date": "2026-06-30", "recoverable_amount": "9999999",
                                                "preview": True, "op_key": "i"})
    assert r.status_code == 422
    assert "9,999,999.00" in _message(r) and "9999999.00" not in _message(r)


async def test_a_date_after_today_names_the_company_today_and_where_it_is_set(fa: FA):
    await _managed_from(fa)
    car = await _contributed(fa)
    today = await fa.today()
    ahead = (today + timedelta(days=1)).isoformat()
    r = await fa.post(f"/assets/{car}/dispose", {"date": ahead, "reason": "lost", "preview": True, "op_key": "d"})
    assert r.status_code == 422
    assert _message(r) == (f"The disposal date {ahead} is after the company's today, {today.isoformat()} "
                           "(time zone UTC).")
    assert _params(r) == {"href": "/settings/general?tab=company", "action": "settings.company_details"}


async def test_a_date_before_a_later_change_names_the_change(fa: FA):
    await _managed_from(fa)
    car = await _contributed(fa)
    await fa.operate(f"/assets/{car}/impair", {"date": "2026-06-30", "recoverable_amount": "8000"}, key="imp")
    r = await fa.post(f"/assets/{car}/sell", {"date": "2026-05-20", "price": "9000", "contact_id": await fa.buyer(),
                                              "preview": True, "op_key": "s"})
    number = (await fa.asset(car))["number"]
    assert _message(r) == f"{number} was changed on 2026-06-30 (Impairment). Date the sale on or after that date."


async def test_undo_registration_on_the_asset_page_points_at_its_correct(fa: FA):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-02")
    r = await fa.post(f"/assets/{car}/undo", {"preview": True, "op_key": "u"})
    assert r.status_code == 409
    number = (await fa.asset(car))["number"]
    assert _message(r) == f"{number} has changed since it was registered. Use Correct instead."


async def test_reverse_run_refusal_gives_each_asset_its_own_way_and_link(fa: FA):
    await _managed_from(fa)
    car = await _contributed(fa)
    van = await _contributed(fa, "Van")
    await _run(fa, "2026-03")
    await fa.operate(f"/assets/{car}/impair", {"date": "2026-03-31", "recoverable_amount": "8000"}, key="imp")
    invoice = await _sell(fa, van, on="2026-03-31", price="9000")
    await _finalize(fa, invoice)
    r = await fa.post("/runs/2026-03/reverse", {"preview": True, "op_key": "r"})
    assert r.status_code == 409
    car_no, van_no = (await fa.asset(car))["number"], (await fa.asset(van))["number"]
    ref = (await fa.state(invoice))["doc_number"]
    message = _message(r)
    assert message.startswith("The 2026-03 run can no longer be reversed.")
    assert f"{car_no} changed since this run. Use Correct on its page to change its depreciation." in message
    assert f"{van_no} was sold on invoice {ref}." in message
    assert _params(r)["links"] == [{"label": car_no, "href": f"/fixed-assets/assets/{car}"},
                                   {"label": van_no, "href": f"/fixed-assets/assets/{van}"}]
    page = await fa.submit("/fixed-assets/op/reverse-run/review", {"month": "2026-03", "idempotency_key": "rv"})
    assert f'href="/fixed-assets/assets/{car}"' in page.text and f'href="/fixed-assets/assets/{van}"' in page.text


async def test_undo_disposal_refusal_names_what_changed_and_the_way_on(fa: FA):
    await _managed_from(fa)
    car = await _contributed(fa)
    await fa.operate(f"/assets/{car}/dispose", {"date": "2026-02-10", "reason": "lost", "percent": "40"}, key="pd")
    await _run(fa, "2026-03")
    r = await fa.post(f"/assets/{car}/undo-disposal", {"preview": True, "op_key": "u"})
    assert r.status_code == 409
    number = (await fa.asset(car))["number"]
    assert _message(r) == (f"{number} has changed since it was disposed of (Depreciation), so the disposal can no "
                           "longer be undone. Use Correct to change its figures.")


_HOME_CRUMB = '<a href="/fixed-assets" class="breadcrumb-link">'


async def test_a_missing_record_keeps_the_breadcrumb_and_a_way_back(fa: FA):
    await _managed_from(fa)
    for path in ("/fixed-assets/assets/nope", "/fixed-assets/assets/nope/dispose", "/fixed-assets/corrections/nope",
                 "/fixed-assets/imports/nope"):
        page = (await fa.page(path)).text
        assert _HOME_CRUMB in page, path
        assert 'class="empty-state-cta-msg">No such' in page, path


async def test_a_missing_permission_on_a_print_reads_as_on_screen_without_a_link_the_reader_cannot_use(fa: FA):
    from celerp.services.permissions import missing_permission_text

    await _managed_from(fa)
    viewer = await fa.role_headers("viewer")
    for path in ("/fixed-assets/reports/schedule", "/fixed-assets/reports/schedule/print",
                 "/fixed-assets/year-end/print"):
        r = await fa.page(path, headers=viewer)
        page = html.unescape(r.text)
        assert missing_permission_text("view_fixed_assets") in page, path
        assert _HOME_CRUMB in r.text, path
        assert "tab=users" not in page, path


async def test_an_unknown_run_or_class_names_no_crumb_it_does_not_have(fa: FA):
    await _managed_from(fa)
    run = (await fa.page("/fixed-assets/runs/soon")).text
    assert "soon" not in _crumbs(run) and "soon" not in run.split("<h1", 1)[1].split("</h1>", 1)[0]
    for path in ("/fixed-assets/classes/nope/fix-account", "/fixed-assets/classes/nope/revalue",
                 "/fixed-assets/classes/nope/apply"):
        assert "--" not in _crumbs((await fa.page(path)).text), path


def _crumbs(page: str) -> str:
    return page[page.index('class="breadcrumbs"'):].split("</div>", 1)[0]
