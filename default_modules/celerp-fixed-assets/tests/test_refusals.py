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
    assert _message(r) == f"{number} (Depreciation) changed since it was registered. Use Correct on its page instead."
    assert _params(r)["links"] == [{"label": number, "href": f"/fixed-assets/assets/{car}"}]


async def test_reverse_run_refusal_gives_each_asset_its_own_way_and_link(fa: FA):
    """The assets still in use share one sentence naming what changed on each and pointing at
    their Correct; a sold asset is told how to end the sale first. Each number is a link
    where the message names it."""
    await _managed_from(fa)
    car = await _contributed(fa)
    van = await _contributed(fa, "Van")
    bike = await _contributed(fa, "Bike")
    await _run(fa, "2026-03")
    await fa.operate(f"/assets/{car}/impair", {"date": "2026-03-31", "recoverable_amount": "8000"}, key="imp")
    await fa.operate(f"/assets/{bike}/impair", {"date": "2026-03-31", "recoverable_amount": "9000"}, key="imp2")
    invoice = await _sell(fa, van, on="2026-03-31", price="9000")
    await _finalize(fa, invoice)
    r = await fa.post("/runs/2026-03/reverse", {"preview": True, "op_key": "r"})
    assert r.status_code == 409
    car_no, van_no, bike_no = [(await fa.asset(a))["number"] for a in (car, van, bike)]
    ref = (await fa.state(invoice))["doc_number"]
    message = _message(r)
    assert message == (f"The 2026-03 run can no longer be reversed. {car_no} (Impairment) and {bike_no} (Impairment) "
                       "changed since this run. Use Correct on each one's page to change its depreciation. "
                       f"{van_no} was sold on invoice {ref}. Void that invoice and cancel the sale on {van_no}'s "
                       "page, then use Correct there.")
    assert _params(r)["links"] == [{"label": n, "href": f"/fixed-assets/assets/{a}"}
                                   for n, a in sorted([(car_no, car), (van_no, van), (bike_no, bike)])]
    page = await fa.submit("/fixed-assets/op/reverse-run/review", {"month": "2026-03", "idempotency_key": "rv"})
    for asset_id, number in ((car, car_no), (bike, bike_no)):
        assert f'<a href="/fixed-assets/assets/{asset_id}">{number}</a> (Impairment)' in page.text
    assert f'<a href="/fixed-assets/assets/{van}">{van_no}</a> was sold' in page.text


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



async def test_the_asset_page_says_why_each_undo_would_be_refused_beside_its_button(fa: FA):
    from bs4 import BeautifulSoup
    from ui.i18n import t

    await _managed_from(fa)
    car = await _contributed(fa)
    await fa.operate(f"/assets/{car}/dispose", {"date": "2026-02-10", "reason": "lost", "percent": "40"}, key="pd")
    await _run(fa, "2026-03")
    number = (await fa.asset(car))["number"]
    soup = BeautifulSoup((await fa.page(f"/fixed-assets/assets/{car}")).text, "html.parser")
    told = {form.select_one("button[hx-post]")["hx-post"].split("/")[3]:
            [w.get_text(" ", strip=True) for w in form.select(".flash--warning")]
            for form in soup.select("form") if form.select(".flash--warning")}
    assert told["undo-disposal"] == [f"{number} has changed since it was disposed of (Depreciation), so the disposal "
                                     "can no longer be undone. Use Correct to change its figures."], told
    assert told["undo-asset"] == [f"{number} (Depreciation recalculated, Disposed of, Depreciation) changed since it was registered. "
                                  "Use Correct on its page instead."], told
    assert t("fixed_assets.undo_disposal") in soup.text and t("fixed_assets.undo") in soup.text

    fresh = await _contributed(fa, name="Van")
    page = await fa.page(f"/fixed-assets/assets/{fresh}")
    assert "flash--warning" not in page.text and t("fixed_assets.undo") in page.text


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
    for path, key in (("/fixed-assets/reports/schedule", "view_fixed_assets"),
                      ("/fixed-assets/reports/schedule/print", "view_fixed_assets"),
                      ("/fixed-assets/year-end/print", "view_fixed_assets"),
                      ("/fixed-assets/checks/not-seen/print", "check_fixed_assets")):
        r = await fa.page(path, headers=viewer)
        page = html.unescape(r.text)
        assert missing_permission_text(key) in page, path
        assert _HOME_CRUMB in r.text, path
        assert "tab=users" not in page, path


async def test_a_print_is_refused_for_exactly_who_its_screen_refuses(fa: FA):
    """The print fetches what its screen fetches, so the same roles reach each: the schedule and
    the other reports need financial reports, and the year end, a checklist whose amounts the
    role's money setting hides, needs only fixed assets."""
    from celerp.services.permissions import missing_permission_text
    from celerp_fixed_assets.ui_reports import REPORTS

    await _managed_from(fa)
    refusals = [missing_permission_text(p) for p in ("view_fixed_assets", "view_financial_reports")]
    pages = [f"/fixed-assets/reports/{r.key}" for r in REPORTS] + ["/fixed-assets/year-end"]
    for role in ("viewer", "operator", "manager", "owner"):
        headers = await fa.role_headers(role) if role != "owner" else fa.h
        for path in pages:
            refused = []
            for url in (path, f"{path}/print"):
                page = html.unescape((await fa.page(url, headers=headers)).text)
                refused.append(next((r for r in refusals if r in page), None))
            assert refused[0] == refused[1], (role, path, refused)
    operator = await fa.role_headers("operator")
    for path, refusal in (("/fixed-assets/reports/schedule/print", refusals[1]), ("/fixed-assets/year-end/print", None)):
        page = html.unescape((await fa.page(path, headers=operator)).text)
        assert (refusal in page) if refusal else not any(r in page for r in refusals), path


async def test_every_print_is_a_document_under_the_core_report_header_without_the_app_around_it(fa: FA):
    from bs4 import BeautifulSoup
    from celerp_fixed_assets.ui_reports import REPORTS
    from ui.i18n import t

    await _managed_from(fa)
    await _contributed(fa)
    company = (await fa.client.get("/companies/me", headers=fa.h)).json()["name"]
    prints = {f"/fixed-assets/reports/{r.key}/print": r.title_key for r in REPORTS}
    prints |= {"/fixed-assets/year-end/print": "fixed_assets.year_end_title",
               "/fixed-assets/checks/not-seen/print": "fixed_assets.not_seen_title"}
    for path, title in prints.items():
        soup = BeautifulSoup((await fa.page(path, since="2026-01-01")).text, "html.parser")
        header = soup.select_one(".report-header")
        assert header and company in header.text and t(title) in header.text, path
        assert not soup.select(".breadcrumb, nav, input, select, .btn"), path


async def test_an_unknown_run_or_class_names_no_crumb_it_does_not_have(fa: FA):
    await _managed_from(fa)
    run = (await fa.page("/fixed-assets/runs/soon")).text
    assert "soon" not in _crumbs(run) and "soon" not in run.split("<h1", 1)[1].split("</h1>", 1)[0]
    for path in ("/fixed-assets/classes/nope/fix-account", "/fixed-assets/classes/nope/revalue",
                 "/fixed-assets/classes/nope/apply"):
        assert "--" not in _crumbs((await fa.page(path)).text), path


def _crumbs(page: str) -> str:
    return page[page.index('class="breadcrumbs"'):].split("</div>", 1)[0]


async def test_a_percent_out_of_range_says_what_to_enter(fa: FA):
    await _managed_from(fa)
    car = await _contributed(fa)
    for percent in ("0", "120"):
        r = await fa.post(f"/assets/{car}/dispose", {"date": "2026-05-01", "reason": "lost", "percent": percent,
                                                     "preview": True, "op_key": "pc"})
        assert _message(r) == "Enter a percent disposed of above 0 and up to 100.", r.text


async def test_a_stale_run_names_the_asset_added_since_its_review(fa: FA):
    await _managed_from(fa)
    await _contributed(fa)
    preview = await fa.ok(await fa.post("/runs", {"month": "2026-03", "preview": True, "op_key": "st"}))
    second = await _contributed(fa, name="Second car")
    r = await fa.post("/runs", {"month": "2026-03", "preview": False, "op_key": "st",
                                "fingerprint": preview["fingerprint"]})
    assert r.status_code == 409 and _message(r) == (
        "FA-0002 (Contributed) changed since you reviewed it. Press Review again to see it as it stands now."), r.text
    assert _params(r)["links"] == [{"label": "FA-0002", "href": f"/fixed-assets/assets/{second}"}]


async def test_a_stale_review_with_no_asset_changed_says_what_may_have(fa: FA):
    await _managed_from(fa)
    await _contributed(fa)
    preview = await fa.ok(await fa.post("/runs", {"month": "2026-03", "preview": True, "op_key": "st"}))
    await fa.journal("2026-02-10", [{"account": "6400", "debit": 10}, {"account": "1220", "credit": 10}])
    r = await fa.post("/runs", {"month": "2026-03", "preview": False, "op_key": "st",
                                "fingerprint": preview["fingerprint"]})
    assert r.status_code == 409 and _message(r) == (
        "An asset, account or posting on this review changed since you reviewed it. "
        "Press Review again to see it as it stands now."), r.text


async def test_forms_on_an_asset_out_of_use_say_its_state_and_the_way_back_before_any_field(fa: FA):
    """Every operation on a disposed, sold or undone asset refuses on the asset's state first,
    however empty the form, naming how it left use and the way back with a link to it; its
    form pages say the same above the form, and Revalue offers no class change for it."""
    from test_disposal import _dispose
    from test_files_tags import _registered_van

    await _managed_from(fa)
    printer = await _contributed(fa, name="Printer")
    await _dispose(fa, printer, "2026-03-20")
    car = await _contributed(fa)
    invoice = await _sell(fa, car, on="2026-04-10")
    await _finalize(fa, invoice)
    van, _ = await _registered_van(fa)
    await fa.operate(f"/assets/{van}/undo", {}, key="undo-van")
    numbers = {a: (await fa.asset(a))["number"] for a in (printer, car, van)}
    left = {
        printer: f"{numbers[printer]} was disposed of on 2026-03-20{{so}}. Undo the disposal on its page first.",
        car: f"{numbers[car]} was sold on 2026-04-10{{so}}. Void its invoice and cancel the sale on its page first.",
        van: f"{numbers[van]}'s registration was undone{{so}}. Its bill line is back in Register from bills, "
             "to register it again."}
    cannot = {"sell": "it cannot be sold", "dispose": "it cannot be disposed of", "move": "it cannot be moved",
              "add": "nothing can be added to it", "revalue": "it cannot be remeasured",
              "impair": "it cannot be remeasured", "reclassify": "it cannot be reclassified",
              "correct": "it cannot be corrected"}
    for asset_id, text in left.items():
        links = [{"label": numbers[asset_id], "href": f"/fixed-assets/assets/{asset_id}"}]
        if asset_id == van:
            links.append({"label": "Register from bills", "href": "/fixed-assets/intake"})
        for op, clause in cannot.items():
            r = await fa.post(f"/assets/{asset_id}/{op}", {"preview": True, "op_key": f"x-{op}"})
            assert (op, r.status_code, _message(r)) == (op, 409, text.format(so=f", so {clause}")), r.text
            assert _params(r)["links"] == links
        assert (await fa.asset(asset_id))["left_use"] == {"message": text.format(so=""), "links": links}
        for form in ("sell", "dispose", "move", "add", "remeasure", "reclassify", "correct"):
            page = html.unescape((await fa.page(f"/fixed-assets/assets/{asset_id}/{form}")).text)
            assert 'id="fa-left-use"' in page, (form, asset_id)
            assert f'<a href="/fixed-assets/assets/{asset_id}">{numbers[asset_id]}</a>' in page, form
        page = (await fa.page(f"/fixed-assets/assets/{asset_id}/remeasure")).text
        assert "Change class to revaluation" not in page
    r = await fa.post("/move", {"asset_ids": [printer], "preview": True, "op_key": "x-many"})
    assert _message(r) == left[printer].format(so=", so it cannot be moved")
    assert (await fa.asset(await _contributed(fa, name="Bike")))["left_use"] is None
