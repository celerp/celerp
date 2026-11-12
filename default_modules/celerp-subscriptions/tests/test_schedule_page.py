# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The agreement page's schedule controls, its Periods table and what each action tells the
user, through the UI to the real API."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from agreement_kit import agreement, bill_early, freeze_day, generate_due, schedule, template
from ui.i18n import sentence, t

REPO = Path(__file__).resolve().parents[3]


def _toast(r) -> dict:
    return json.loads(r.headers.get("HX-Trigger") or "{}").get("celerpToast") or {}


def _schedule_controls(html: str) -> dict[str, str]:
    """Each schedule control on the page: the field it edits and the label it sits in."""
    soup = BeautifulSoup(html, "html.parser")
    found = {}
    for el in soup.select("[hx-patch*='/schedule/']"):
        label = el.find_parent("label")
        found[el["hx-patch"].rsplit("/", 1)[1]] = label.get_text(" ", strip=True) if label else ""
    return found


def _period_cells(html: str) -> dict[str, str]:
    """The Periods table's status text by bill date."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("#sub-periods")
    return {tds[1].get_text(strip=True): tds[2].get_text(" ", strip=True)
            for tds in (tr.find_all("td") for tr in table.select("tr")) if len(tds) > 2}


# --- Schedule controls -------------------------------------------------------

@pytest.mark.parametrize("activate", [False, True])
async def test_every_schedule_term_has_a_labelled_control(owner_ui, monkeypatch, activate):
    """Every term the schedule API takes is set on the page, each control named by its
    label, on a draft and on an active agreement."""
    from celerp_subscriptions.routes import ScheduleBody

    freeze_day(monkeypatch, "2026-03-15")
    eid = await agreement(owner_ui.api, {}, start="2026-03-01", activate=activate)
    controls = _schedule_controls((await owner_ui.get(f"/subscriptions/{eid}")).text)
    assert set(controls) == set(ScheduleBody.model_fields)
    assert all(controls.values()), controls
    assert t("label.first_bill_date", "en") in controls["anchor_date"]


async def test_timing_and_term_are_set_from_the_page(owner_ui, monkeypatch):
    freeze_day(monkeypatch, "2026-03-15")
    eid = await agreement(owner_ui.api, {}, start="2026-03-01")
    for field, value in (("billing_timing", "arrears"), ("term_end", "2026-12-31"), ("term_start", "2026-03-01")):
        r = await owner_ui.patch(f"/subscriptions/{eid}/schedule/{field}", data={"value": value})
        assert r.status_code == 200 and not _toast(r), (field, r.text[:300])
    state = await template(owner_ui.api, {}, eid)
    assert (state["billing_timing"], state["term_end"], state["term_start"]) == ("arrears", "2026-12-31", "2026-03-01")
    r = await owner_ui.patch(f"/subscriptions/{eid}/schedule/term_end", data={"value": ""})
    assert r.status_code == 200 and (await template(owner_ui.api, {}, eid)).get("term_end") is None


async def test_recognize_over_the_service_period_is_set_from_the_page(owner_ui, monkeypatch):
    """A sales agreement's Billing section sets whether its bills recognize over their service
    period, saved and undone from its Yes/No; a purchase agreement has no such control."""
    freeze_day(monkeypatch, "2026-03-15")
    eid = await agreement(owner_ui.api, {}, start="2026-03-01")
    controls = _schedule_controls((await owner_ui.get(f"/subscriptions/{eid}")).text)
    assert t("item.recognize_over_service_period", "en") in controls["recognize_over_service_period"]
    for value, stored in (("true", True), ("false", False)):
        r = await owner_ui.patch(f"/subscriptions/{eid}/schedule/recognize_over_service_period", data={"value": value})
        assert r.status_code == 200 and not _toast(r), r.text[:300]
        assert (await template(owner_ui.api, {}, eid))["recognize_over_service_period"] is stored
    purchase = await agreement(owner_ui.api, {}, start="2026-03-01", doc_type="subscription_po")
    assert "recognize_over_service_period" not in _schedule_controls((await owner_ui.get(f"/subscriptions/{purchase}")).text)


async def test_stop_at_period_end_is_set_and_undone_from_the_page(owner_ui, monkeypatch):
    freeze_day(monkeypatch, "2026-03-15")
    eid = await agreement(owner_ui.api, {}, start="2026-01-01")
    r = await owner_ui.patch(f"/subscriptions/{eid}/schedule/cancel_at_period_end", data={"value": "true"})
    assert r.status_code == 200, r.text[:300]
    assert (await template(owner_ui.api, {}, eid))["cancel_at_period_end"] == 2
    page = (await owner_ui.get(f"/subscriptions/{eid}")).text
    assert "2026-04-01" not in _period_cells(page)  # no period after the current one is listed
    await owner_ui.patch(f"/subscriptions/{eid}/schedule/cancel_at_period_end", data={"value": "false"})
    assert (await template(owner_ui.api, {}, eid)).get("cancel_at_period_end") is None


async def test_a_saved_first_bill_date_shows_at_once(owner_ui, monkeypatch):
    """Setting the first bill date answers with the schedule as saved, naming the date."""
    freeze_day(monkeypatch, "2026-03-15")
    eid = await agreement(owner_ui.api, {}, start="2026-03-01", activate=False)
    r = await owner_ui.patch(f"/subscriptions/{eid}/schedule/anchor_date", data={"value": "2026-05-01"})
    assert r.status_code == 200
    assert sentence("subscriptions.first_bill_on", "en", date="2026-05-01") in r.text
    assert 'value="2026-05-01"' in r.text


async def test_entering_custom_days_bills_on_that_interval(owner_ui, monkeypatch):
    """The days box is the custom frequency: entering a number of days chooses it."""
    freeze_day(monkeypatch, "2026-03-15")
    eid = await agreement(owner_ui.api, {}, start="2026-03-01", activate=False)
    r = await owner_ui.patch(f"/subscriptions/{eid}/schedule/custom_interval_days", data={"value": "10"})
    assert r.status_code == 200 and not _toast(r), r.text[:300]
    state = await template(owner_ui.api, {}, eid)
    assert (state["frequency"], state["custom_interval_days"]) == ("custom", 10)


@pytest.mark.parametrize("days", ["0", "40000"])
async def test_custom_days_outside_the_range_are_refused_by_the_modules_reason(owner_ui, monkeypatch, days):
    """Any number of days entered chooses the custom frequency, so one outside the range gets
    the schedule's own message with the range, never a stored value or a field name."""
    freeze_day(monkeypatch, "2026-03-15")
    eid = await agreement(owner_ui.api, {}, start="2026-03-01", activate=False)
    r = await owner_ui.patch(f"/subscriptions/{eid}/schedule/custom_interval_days", data={"value": days})
    assert _toast(r).get("message") == t("subscriptions.err_custom_interval", "en"), r.text[:300]
    state = await template(owner_ui.api, {}, eid)
    assert (state["frequency"], state.get("custom_interval_days")) == ("monthly", None)


# --- Period status -----------------------------------------------------------

async def test_periods_say_overdue_due_today_and_upcoming(owner_ui, monkeypatch):
    freeze_day(monkeypatch, "2026-03-01")
    eid = await agreement(owner_ui.api, {}, start="2026-01-01")
    cells = _period_cells((await owner_ui.get(f"/subscriptions/{eid}")).text)
    assert cells["2026-01-01"] == cells["2026-02-01"] == t("subscriptions.period_overdue", "en")
    assert cells["2026-03-01"] == t("subscriptions.period_due", "en")
    assert cells["2026-04-01"] == t("subscriptions.period_upcoming", "en")


async def test_a_voided_period_does_not_read_billed_and_a_reopened_one_says_so(owner_ui, monkeypatch):
    freeze_day(monkeypatch, "2026-01-15")
    eid = await agreement(owner_ui.api, {}, start="2026-01-01")
    billed = (await generate_due(owner_ui.api, {}, eid)).json()
    assert (await owner_ui.api.post(f"/docs/{billed['doc_id']}/void", json={"reason": "Wrong amount"})).status_code == 200
    cell = _period_cells((await owner_ui.get(f"/subscriptions/{eid}")).text)["2026-01-01"]
    assert cell == t("subscriptions.period_voided", "en")
    assert (await owner_ui.post(f"/subscriptions/{eid}/periods/0/reopen")).status_code == 204
    cell = _period_cells((await owner_ui.get(f"/subscriptions/{eid}")).text)["2026-01-01"]
    assert cell == f'{t("subscriptions.period_overdue", "en")} {t("subscriptions.period_reopened", "en")}'


def test_every_period_status_has_a_label():
    """Class guard: each status the period list can report is shown by its own label."""
    from celerp_subscriptions.billing import PERIOD_STATUSES
    from ui.i18n import t_or

    assert {"billed", "voided", "skipped", "unresolved", "overdue", "due", "upcoming"} <= set(PERIOD_STATUSES)
    for status in (*PERIOD_STATUSES, "reopened"):
        assert t_or(f"subscriptions.period_{status}", "") != "", status


# --- What each action says ---------------------------------------------------

async def test_billing_names_the_document_and_its_period(owner_ui, monkeypatch):
    freeze_day(monkeypatch, "2026-01-15")
    eid = await agreement(owner_ui.api, {}, start="2026-01-01")
    r = await owner_ui.post(f"/subscriptions/{eid}/generate-due", data={"idempotency_key": "bill-0"})
    assert r.headers["HX-Redirect"] == f"/subscriptions/{eid}"
    ref = next(iter((await template(owner_ui.api, {}, eid))["periods"].values()))["doc_id"]
    ref = (await owner_ui.api.get(f"/docs/{ref}")).json()["ref_id"]
    assert _toast(r) == {**_toast(r), "type": "success", "message": sentence(
        "subscriptions.done_billed", "en", ref=ref, start="2026-01-01", end="2026-01-31")}
    r = await owner_ui.post(f"/subscriptions/{eid}/bill-early", data={"idempotency_key": "bill-1", "period_index": "1"})
    assert "1 Feb 2026" in _toast(r)["message"] and _toast(r)["type"] == "success"


@pytest.mark.parametrize("action", ["activate", "pause", "resume", "cancel", "generate-due", "bill-early",
                                    "skip", "unskip", "reopen"])
async def test_every_agreement_action_says_what_it_did(owner_ui, monkeypatch, action):
    """Class guard: each lifecycle, billing and period action answers with a message, and a
    period action names the period's dates."""
    import ui.api_client as api

    assert set(api.SUBSCRIPTION_ACTIONS) | (set(api.PERIOD_ACTIONS) - {"send"}) == {
        "activate", "pause", "resume", "cancel", "generate-due", "bill-early", "skip", "unskip", "reopen"}
    freeze_day(monkeypatch, "2026-01-15")
    eid = await agreement(owner_ui.api, {}, start="2026-01-01", activate=action != "activate")
    if action == "resume":
        await owner_ui.api.post(f"/subscriptions/{eid}/pause")
    if action in ("unskip",):
        await owner_ui.api.post(f"/subscriptions/{eid}/periods/0/skip", json={"reason": "Closed"})
    if action == "reopen":
        doc = (await generate_due(owner_ui.api, {}, eid)).json()["doc_id"]
        await owner_ui.api.post(f"/docs/{doc}/void", json={"reason": "Wrong amount"})
    if action == "bill-early":
        await generate_due(owner_ui.api, {}, eid)
    path = (f"/subscriptions/{eid}/periods/0/{action}" if action in api.PERIOD_ACTIONS
            else f"/subscriptions/{eid}/{action}")
    r = await owner_ui.post(path, data={"reason": "Closed", "idempotency_key": f"say-{action}"})
    assert r.status_code == 204, r.text[:300]
    toast = _toast(r)
    assert toast.get("type") == "success" and toast.get("message"), r.headers
    if action in api.PERIOD_ACTIONS:
        assert "1 Jan 2026" in toast["message"] and "31 Jan 2026" in toast["message"]


# --- Refusals are never empty ------------------------------------------------

@pytest.mark.parametrize("typed", ["13", "-1", "2.5"])
async def test_an_invalid_catchup_window_says_how_to_fix_it(owner_ui, monkeypatch, typed):
    freeze_day(monkeypatch, "2026-03-15")
    eid = await agreement(owner_ui.api, {}, start="2026-03-01")
    r = await owner_ui.patch(f"/subscriptions/{eid}/schedule/catchup_periods", data={"value": typed})
    assert _toast(r)["message"] == t("subscriptions.err_catchup_window", "en")


async def test_skipping_without_a_reason_says_a_reason_is_needed(owner_ui, monkeypatch):
    freeze_day(monkeypatch, "2026-01-15")
    eid = await agreement(owner_ui.api, {}, start="2026-01-01")
    r = await owner_ui.post(f"/subscriptions/{eid}/periods/0/skip", data={"reason": "  "})
    assert _toast(r)["message"] == t("subscriptions.err_skip_reason", "en")
    r = await owner_ui.api.post(f"/subscriptions/{eid}/periods/0/skip", json={"reason": ""})
    assert r.status_code == 422 and r.json()["detail"]["message_key"] == "subscriptions.err_skip_reason"


async def test_catch_up_skip_without_a_reason_names_why(owner_ui, monkeypatch):
    freeze_day(monkeypatch, "2026-06-15")
    eid = await agreement(owner_ui.api, {}, start="2026-01-01")
    await schedule(owner_ui.api, {}, eid, generation_mode="auto_invoice")
    r = await owner_ui.post("/subscriptions/catch-up/skip", data={"item": json.dumps([f"{eid}|0"]), "reason": ""})
    assert t("subscriptions.err_skip_reason", "en") in r.text


async def test_a_request_validation_refusal_is_never_an_empty_toast(owner_ui, monkeypatch):
    """Class guard: a refusal that carries only the API's field-by-field validation list
    still reads as a sentence."""
    from unittest.mock import AsyncMock, patch
    from ui.api_client import APIError, _api_error

    freeze_day(monkeypatch, "2026-03-15")
    eid = await agreement(owner_ui.api, {}, start="2026-03-01")
    body = {"detail": [{"loc": ["body", "auto_send"], "msg": "Input should be a valid boolean", "type": "bool_parsing"}]}
    refused = _api_error(422, body, "")
    assert isinstance(refused, APIError)
    with patch("ui.api_client.update_subscription_schedule", AsyncMock(side_effect=refused)):
        r = await owner_ui.patch(f"/subscriptions/{eid}/schedule/auto_send", data={"value": "true"})
    assert _toast(r)["message"].strip()


def test_no_page_renders_an_api_refusal_that_can_be_empty():
    """Class guard: refusal_text of a caught error's data falls back to a sentence
    (error_message, or an explicit `or`), so a validation list never renders as nothing."""
    bare = re.compile(r"refusal_text\((\w+)\.data or \1\.detail\)(?!\s*or\b)")
    offenders = [f"{p.relative_to(REPO)}:{n}" for p in (REPO / "ui").rglob("*.py")
                 for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if bare.search(line)]
    assert offenders == []


# --- Stop at period end after an early bill ----------------------------------

async def test_stop_at_period_end_keeps_a_period_billed_early(owner_ui, monkeypatch):
    freeze_day(monkeypatch, "2026-01-15")
    eid = await agreement(owner_ui.api, {}, start="2026-01-01")
    assert (await generate_due(owner_ui.api, {}, eid)).status_code == 200
    assert (await bill_early(owner_ui.api, {}, eid, "early-1")).status_code == 200
    r = await schedule(owner_ui.api, {}, eid, cancel_at_period_end=True)
    assert r.status_code == 200, r.text
    assert r.json()["next_bill_date"] is None
    assert (await template(owner_ui.api, {}, eid))["cancel_at_period_end"] == 1
