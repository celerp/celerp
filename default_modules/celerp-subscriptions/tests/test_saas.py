# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""SaaS: plans, subscribers, seats and trials.

Initial conditions per test: a newly registered company signed in to the UI (owner_ui), its
API calls answered in process. A plan is a service item in the company's "saas_plan" category,
whose income account is a revenue account made for the test, priced on the Retail list.
Agreements are made through POST /docs or the New subscriber form on /subscribers and activated
through the subscriptions API on a frozen business day.
"""
from __future__ import annotations

import uuid

import pytest

from agreement_kit import agreement, billed_docs, contact, freeze_day, generate_due, member, schedule, template
from test_helpers import ui_signed_in
from test_income_accounts import _account, _category, _item, _set_category_account

pytestmark = pytest.mark.asyncio

_AUTH = {"headers": {}}  # owner_ui.api already carries the owner's token
PLAN_ACCOUNT = "4190"


async def _plan_category(api) -> str:
    await _account(api, _AUTH, PLAN_ACCOUNT)
    key = await _category(api, _AUTH, "saas_plan")
    assert key == "saas_plan"
    assert (await _set_category_account(api, _AUTH, key, PLAN_ACCOUNT)).status_code == 200
    return key


async def _plan(api, name: str, price: float) -> str:
    return await _item(api, _AUTH, name, category="saas_plan", retail_price=price)


def _says(r, key: str, **params) -> bool:
    from ui.i18n import t

    return t(key, **params) in r.text


async def test_plans_empty_state(owner_ui):
    """No plan yet: the Plans page says what a plan is and offers New plan, which makes a service
    item in the SaaS plan category."""
    r = await owner_ui.get("/plans")
    assert r.status_code == 200, r.text[:300]
    assert "No plans yet. A plan is a service item in the SaaS plan category." in r.text
    assert "New plan" in r.text


async def test_plans_list_saas_plan_items_with_price_and_subscribers(owner_ui, monkeypatch):
    freeze_day(monkeypatch, "2026-03-01")
    await _plan_category(owner_ui.api)
    team = await _plan(owner_ui.api, "Team plan", 20.0)
    other = await _item(owner_ui.api, _AUTH, "Setup service")
    payer = await contact(owner_ui.api, {}, f"sub-{uuid.uuid4().hex[:6]}@test.test")
    await agreement(owner_ui.api, {}, start="2026-03-01", contact_id=payer,
                    line_items=[{"entity_id": team, "description": "Team plan", "quantity": 3, "unit_price": 20.0}])
    r = await owner_ui.get("/plans")
    assert r.status_code == 200, r.text[:300]
    assert f"/inventory/{team}" in r.text and f"/inventory/{other}" not in r.text
    row = r.text[r.text.index(f"/inventory/{team}"):]
    row = row[:row.index("</tr>")]
    assert "20.00" in row and ">1<" in row  # its price and one subscriber


async def test_subscribers_empty_state_and_permissions(owner_ui, session):
    r = await owner_ui.get("/subscribers")
    assert r.status_code == 200, r.text[:300]
    assert "No subscribers yet." in r.text and "New subscriber" in r.text
    headers, _uid = await member(owner_ui.api, session, {}, "viewer")
    async with ui_signed_in(headers["Authorization"].split()[1]) as ui:
        for path in ("/subscribers", "/plans"):
            assert (await ui.get(path)).status_code in (302, 303), path


async def test_new_subscriber_plan_quantity_trial(owner_ui, monkeypatch):
    """New subscriber stores the plan as the agreement's line at the plan's price, the seats as
    its quantity and the trial end as the first bill date; the Subscribers page lists it."""
    freeze_day(monkeypatch, "2026-03-01")
    await _plan_category(owner_ui.api)
    team = await _plan(owner_ui.api, "Team plan", 20.0)
    payer = await contact(owner_ui.api, {}, f"sub-{uuid.uuid4().hex[:6]}@test.test")
    page = await owner_ui.get("/subscribers")
    assert page.status_code == 200 and "New subscriber" in page.text, page.text[:300]
    assert team in page.text  # the plan is offered by the form
    r = await owner_ui.post("/subscriptions/new", params={"direction": "sales", "kind": "subscription"},
                            data={"contact_id": payer, "plan_id": team, "seats": "5", "trial_end": "2026-03-15"})
    assert r.status_code == 204, r.text[:300]
    eid = r.headers["HX-Redirect"].rsplit("/", 1)[-1]
    sub = await template(owner_ui.api, {}, eid)
    assert sub.get("kind") == "subscription" and sub["contact_id"] == payer
    [line] = sub["line_items"]
    assert line["entity_id"] == team and float(line["quantity"]) == 5 and float(line["unit_price"]) == 20.0
    assert sub["trial_end"] == "2026-03-15" and sub["anchor_date"] == "2026-03-15"
    listed = await owner_ui.get("/subscribers", params={"status": "draft"})
    assert f"/subscriptions/{eid}" in listed.text


@pytest.mark.parametrize(("seats", "plan"), [("0", True), ("-1", True), ("2.5", True), ("3", False)])
async def test_new_subscriber_refuses_bad_seats_or_plan(owner_ui, monkeypatch, seats, plan):
    """Seats are a whole number of at least one and the plan a SaaS plan item: anything else is
    refused naming it, and no agreement is made."""
    freeze_day(monkeypatch, "2026-03-01")
    await _plan_category(owner_ui.api)
    chosen = await (_plan(owner_ui.api, "Team plan", 20.0) if plan else _item(owner_ui.api, _AUTH, "Not a plan"))
    payer = await contact(owner_ui.api, {}, f"sub-{uuid.uuid4().hex[:6]}@test.test")
    before = (await owner_ui.api.get("/subscriptions", params={"limit": 500})).json()["total"]
    r = await owner_ui.post("/subscriptions/new", params={"direction": "sales", "kind": "subscription"},
                            data={"contact_id": payer, "plan_id": chosen, "seats": seats})
    assert r.status_code != 204 and "HX-Redirect" not in r.headers, r.text[:300]
    assert ("seat" in r.text.lower()) if plan else ("plan" in r.text.lower())
    assert (await owner_ui.api.get("/subscriptions", params={"limit": 500})).json()["total"] == before


async def test_trial_first_bill_at_trial_end(owner_ui, session, monkeypatch):
    """A trial ending on 15 March starts the first period then: nothing is due before it, its
    first bill covers 15 March to 14 April, and once billed the trial end is fixed."""
    freeze_day(monkeypatch, "2026-03-01")
    eid = await agreement(owner_ui.api, {}, start="2026-03-01", trial_end="2026-03-15")
    sub = await template(owner_ui.api, {}, eid)
    assert sub["anchor_date"] == "2026-03-15" and sub["trial_end"] == "2026-03-15"
    freeze_day(monkeypatch, "2026-03-10")
    assert (await generate_due(owner_ui.api, {}, eid)).status_code in (200, 409)
    assert await billed_docs(session, eid) == []
    freeze_day(monkeypatch, "2026-03-15")
    assert (await generate_due(owner_ui.api, {}, eid)).status_code == 200
    [doc] = await billed_docs(session, eid)
    assert (doc["service_start"], doc["service_end"]) == ("2026-03-15", "2026-04-14")
    r = await schedule(owner_ui.api, {}, eid, trial_end="2026-03-20")
    assert r.status_code == 409, r.text


async def test_trial_end_checked_by_every_writer(owner_ui, monkeypatch):
    """A trial end that is not a date, or a first bill date other than the trial end, is refused
    with its own message by Documents and by the schedule; setting a trial on the schedule moves
    the first bill date to it."""
    freeze_day(monkeypatch, "2026-03-01")
    r = await owner_ui.api.post("/docs", json={"doc_type": "subscription_invoice", "contact_id": "contact:test-001",
                                               "frequency": "monthly", "trial_end": "soon"})
    assert r.status_code == 422 and _says(r, "subscriptions.err_schedule_date", day="soon"), r.text
    r = await owner_ui.api.post("/docs", json={"doc_type": "subscription_invoice", "contact_id": "contact:test-001",
                                               "frequency": "monthly", "anchor_date": "2026-03-01",
                                               "trial_end": "2026-03-15"})
    assert r.status_code == 422 and _says(r, "subscriptions.err_trial_first_bill", day="2026-03-15"), r.text
    eid = await agreement(owner_ui.api, {}, start="2026-03-01")
    r = await schedule(owner_ui.api, {}, eid, trial_end="2026-03-20")
    assert r.status_code == 200, r.text
    sub = await template(owner_ui.api, {}, eid)
    assert (sub["trial_end"], sub["anchor_date"]) == ("2026-03-20", "2026-03-20")
    r = await schedule(owner_ui.api, {}, eid, anchor_date="2026-03-25")
    assert r.status_code == 422 and _says(r, "subscriptions.err_trial_first_bill", day="2026-03-20"), r.text


@pytest.mark.parametrize(("vertical", "shown"), [("saas", True), ("consulting", False), (None, False)])
async def test_subscribers_and_plans_in_the_saas_sidebar_only(vertical, shown):
    from ui.components.shell import module_nav

    keys = {item.get("key") for item in module_nav({"vertical": vertical} if vertical else {})}
    assert ({"subscribers", "plans"} <= keys) is shown and (shown or not ({"subscribers", "plans"} & keys))
