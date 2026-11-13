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

import json
import uuid

import pytest

from agreement_kit import agreement, billed_docs, contact, freeze_day, generate_due, member, schedule, template
from test_helpers import ui_signed_in
from ui.components.operation_key import FIELD
from income_books import _account, _category, _item, _set_category_account

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
    """The API refused with ``key`` and ``params`` (its message shows a date in the company's format)."""
    detail = r.json().get("detail")
    return isinstance(detail, dict) and detail.get("message_key") == key and detail.get("params") == params


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
    assert line["item_id"] == team and float(line["quantity"]) == 5 and float(line["unit_price"]) == 20.0
    assert sub["trial_end"] == "2026-03-15" and sub["anchor_date"] == "2026-03-15"
    listed = await owner_ui.get("/subscribers", params={"status": "draft"})
    assert f"/subscriptions/{eid}" in listed.text


@pytest.mark.parametrize(("seats", "plan"), [("0", True), ("-1", True), ("2.5", True), ("\u00b2", True), ("", True),
                                            (" ", True), ("3", False), ("3", None)])
async def test_new_subscriber_refuses_bad_seats_or_plan(owner_ui, monkeypatch, seats, plan):
    """Seats are a whole number of at least one and the plan a SaaS plan item: anything else is
    refused naming it (blank seats or no plan chosen asks for one, never naming "--"), and no
    agreement is made."""
    from ui.i18n import t

    freeze_day(monkeypatch, "2026-03-01")
    await _plan_category(owner_ui.api)
    chosen = "" if plan is None else await (
        _plan(owner_ui.api, "Team plan", 20.0) if plan else _item(owner_ui.api, _AUTH, "Not a plan"))
    payer = await contact(owner_ui.api, {}, f"sub-{uuid.uuid4().hex[:6]}@test.test")
    before = (await owner_ui.api.get("/subscriptions", params={"limit": 500})).json()["total"]
    r = await owner_ui.post("/subscriptions/new", params={"direction": "sales", "kind": "subscription"},
                            data={"contact_id": payer, "plan_id": chosen, "seats": seats})
    assert r.status_code != 204 and "HX-Redirect" not in r.headers, r.text[:300]
    said = json.loads(r.headers["HX-Trigger"])["celerpToast"]
    expected = (t("subscriptions.err_no_plan") if plan is None
                else t("subscriptions.err_not_plan", name="Not a plan") if plan is False
                else t("subscriptions.err_seats_blank") if not seats.strip()
                else t("subscriptions.err_seats", seats=seats))
    assert said["type"] == "error" and said["message"] == expected, said
    assert "--" not in said["message"], said
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
    saas = {"subscribers", "plans", "revenue"}
    assert (saas <= keys) is shown and (shown or not (saas & keys))


# ── Amendments: a seat change from an effective date ────────────────────────

SEATS = {"description": "Team plan", "quantity": 3, "unit_price": 10.0, "line_total": 30.0}


async def _amend(api, eid: str, effective: str, quantities: dict, key: str | None = None):
    return await api.post(f"/subscriptions/{eid}/amend", json={
        "effective": effective, "quantities": quantities, "request_key": key or str(uuid.uuid4())})


async def _line_id(api, eid: str) -> str:
    return (await template(api, {}, eid))["line_items"][0]["line_id"]


def _of(docs: list[dict], doc_type: str) -> list[dict]:
    return [d for d in docs if d["doc_type"] == doc_type]


async def test_amendment_validation(owner_ui, monkeypatch):
    """An effective date before the start of the last billed period is refused naming that
    start; a quantity below 0, a line the agreement does not have, a change that changes
    nothing and a missing request key are refused, and nothing is written."""
    freeze_day(monkeypatch, "2026-03-01")
    eid = await agreement(owner_ui.api, {}, start="2026-03-01", line_items=[dict(SEATS)])
    line = await _line_id(owner_ui.api, eid)
    assert (await generate_due(owner_ui.api, {}, eid)).status_code == 200
    freeze_day(monkeypatch, "2026-04-01")
    assert (await generate_due(owner_ui.api, {}, eid)).status_code == 200
    r = await _amend(owner_ui.api, eid, "2026-03-20", {line: 5})
    assert r.status_code == 409 and _says(r, "subscriptions.err_amend_before_billed", day="2026-04-01"), r.text
    r = await _amend(owner_ui.api, eid, "2026-04-10", {line: -1})
    assert r.status_code == 422 and _says(r, "subscriptions.err_amend_quantity"), r.text
    r = await _amend(owner_ui.api, eid, "2026-04-10", {"line:missing": 2})
    assert r.status_code == 422 and _says(r, "subscriptions.err_amend_line"), r.text
    r = await _amend(owner_ui.api, eid, "2026-04-10", {line: 3})
    assert r.status_code == 422 and _says(r, "subscriptions.err_amend_unchanged"), r.text
    r = await owner_ui.api.post(f"/subscriptions/{eid}/amend", json={"effective": "2026-04-10", "quantities": {line: 4}})
    assert r.status_code == 422, r.text
    assert not (await template(owner_ui.api, {}, eid)).get("amendments")


async def test_seat_change_prorates_advance_via_credit_and_debit(owner_ui, session, monkeypatch):
    """Billed in advance for March (3 seats at 10), a change to 5 seats from 15 March credits the
    old terms for the 17 days left of 31 (16.45) against the March invoice line and bills the new
    terms for them (27.42), both linked to the agreement and the period; the credit is applied to
    the invoice. The same request repeats the same answer; changing back credits the debit invoice.
    April bills 5 seats."""
    freeze_day(monkeypatch, "2026-03-01")
    eid = await agreement(owner_ui.api, {}, start="2026-03-01", line_items=[dict(SEATS)])
    line = await _line_id(owner_ui.api, eid)
    assert (await generate_due(owner_ui.api, {}, eid)).status_code == 200
    [march] = await billed_docs(session, eid)
    freeze_day(monkeypatch, "2026-03-15")
    r = await _amend(owner_ui.api, eid, "2026-03-15", {line: 5}, key="seats-5")
    assert r.status_code == 200, r.text
    again = await _amend(owner_ui.api, eid, "2026-03-15", {line: 5}, key="seats-5")
    assert again.status_code == 200 and again.json() == r.json(), again.text
    docs = await billed_docs(session, eid)
    [credit], [debit] = _of(docs, "credit_note"), [d for d in _of(docs, "invoice") if d["entity_id"] != march["entity_id"]]
    assert (credit["total"], debit["total"]) == (16.45, 27.42)
    for doc in (credit, debit):
        assert doc["status"] not in ("draft", "void")
        assert (doc["period_index"], doc["service_start"], doc["service_end"]) == (0, "2026-03-15", "2026-03-31")
    assert credit["original_doc_id"] == march["entity_id"]
    assert credit["line_items"][0]["source_line_id"] == march["line_items"][0]["line_id"]
    assert (credit["line_items"][0]["quantity"], debit["line_items"][0]["quantity"]) == (3, 5)
    assert {r.json()["credit_note_id"], r.json()["invoice_id"]} == {credit["entity_id"], debit["entity_id"]}
    [march_now] = [d for d in await billed_docs(session, eid) if d["entity_id"] == march["entity_id"]]
    assert march_now["amount_outstanding"] == 13.55
    back = await _amend(owner_ui.api, eid, "2026-03-15", {line: 3})
    assert back.status_code == 200, back.text
    [undo] = [d for d in _of(await billed_docs(session, eid), "credit_note") if d["entity_id"] != credit["entity_id"]]
    assert undo["original_doc_id"] == debit["entity_id"] and undo["total"] == 27.42
    freeze_day(monkeypatch, "2026-04-01")
    assert (await generate_due(owner_ui.api, {}, eid)).status_code == 200
    [april] = [d for d in await billed_docs(session, eid) if d.get("period_index") == 1]
    assert [(li["quantity"], li["line_total"]) for li in april["line_items"]] == [(3, 30.0)]


async def test_seat_change_page_names_the_documents_numbers(owner_ui, session, monkeypatch):
    """The Quantities form's answer names the credit note and the invoice by the numbers the
    documents show, as every list and document page does, never by their storage keys."""
    freeze_day(monkeypatch, "2026-03-01")
    eid = await agreement(owner_ui.api, {}, start="2026-03-01", line_items=[dict(SEATS)])
    line = await _line_id(owner_ui.api, eid)
    assert (await generate_due(owner_ui.api, {}, eid)).status_code == 200
    freeze_day(monkeypatch, "2026-03-15")
    r = await owner_ui.post(f"/subscriptions/{eid}/amend", data={
        "effective": "2026-03-15", "quantities": json.dumps({line: 5}), FIELD: str(uuid.uuid4())})
    assert r.status_code == 204, r.text
    message = json.loads(r.headers["HX-Trigger"])["celerpToast"]["message"]
    docs = await billed_docs(session, eid)
    [credit] = _of(docs, "credit_note")
    [debit] = [d for d in _of(docs, "invoice") if d.get("service_start") == "2026-03-15"]
    for doc in (credit, debit):
        assert doc["ref_id"] in message, message
    assert debit["entity_id"].removeprefix("doc:") not in message, message
    page = (await owner_ui.get(f"/subscriptions/{eid}")).text
    changes = page[page.index('id="sub-quantities"'):page.index('id="sub-billing"')]
    for doc in (credit, debit):
        assert f'>{doc["ref_id"]}</a>' in changes, changes[:3000]
        key = doc["entity_id"].removeprefix("doc:")
        assert key == doc["ref_id"] or f'>{key}</a>' not in changes
    assert f'>{credit["ref_id"]}</a>, <a' in changes, changes[:3000]
    assert debit["entity_id"].removeprefix("doc:") != debit["ref_id"]


async def test_agreement_page_shows_no_outstanding_of_its_own(owner_ui, monkeypatch):
    """An agreement is a template, not a bill: its page shows no Outstanding amount, before or
    after a period is billed (what is owed is on the documents it issues)."""
    freeze_day(monkeypatch, "2026-03-01")
    eid = await agreement(owner_ui.api, {}, start="2026-03-01", line_items=[dict(SEATS)])
    assert 'id="doc-outstanding"' not in (await owner_ui.get(f"/subscriptions/{eid}")).text
    assert (await generate_due(owner_ui.api, {}, eid)).status_code == 200
    assert 'id="doc-outstanding"' not in (await owner_ui.get(f"/subscriptions/{eid}")).text


async def test_arrears_period_splits_effective_segments(owner_ui, session, monkeypatch):
    """Billed in arrears, a change from 1 to 3 seats from 11 March bills March on 1 April as two
    lines, one per effective segment, each prorated by its days of 31, and issues no credit or
    debit document."""
    freeze_day(monkeypatch, "2026-03-01")
    eid = await agreement(owner_ui.api, {}, start="2026-03-01", billing_timing="arrears",
                          line_items=[dict(SEATS, quantity=1, unit_price=100.0, line_total=100.0)])
    line = await _line_id(owner_ui.api, eid)
    freeze_day(monkeypatch, "2026-03-10")
    assert (await _amend(owner_ui.api, eid, "2026-03-11", {line: 3})).status_code == 200
    assert await billed_docs(session, eid) == []
    freeze_day(monkeypatch, "2026-04-01")
    assert (await generate_due(owner_ui.api, {}, eid)).status_code == 200
    [march] = await billed_docs(session, eid)
    assert [(li["quantity"], li["line_total"], li["service_start"], li["service_end"]) for li in march["line_items"]] == [
        (1, 32.26, "2026-03-01", "2026-03-10"), (3, 203.23, "2026-03-11", "2026-03-31")]
    assert march["total"] == 235.49


async def test_invoice_snapshot_unchanged_after_amendment(owner_ui, session, monkeypatch):
    """An issued invoice never changes: an amendment from a later period leaves the March invoice
    exactly as it was, and one inside March leaves its lines and totals as issued (only the
    applied credit moves what it still owes)."""
    freeze_day(monkeypatch, "2026-03-01")
    eid = await agreement(owner_ui.api, {}, start="2026-03-01", line_items=[dict(SEATS)])
    line = await _line_id(owner_ui.api, eid)
    assert (await generate_due(owner_ui.api, {}, eid)).status_code == 200
    [march] = await billed_docs(session, eid)
    assert (await _amend(owner_ui.api, eid, "2026-04-05", {line: 4})).status_code == 200
    [same] = await billed_docs(session, eid)
    assert same == march
    assert (await _amend(owner_ui.api, eid, "2026-03-20", {line: 6})).status_code == 200
    [issued] = [d for d in await billed_docs(session, eid) if d["entity_id"] == march["entity_id"]]
    fixed = ("line_items", "subtotal", "tax", "total", "issue_date", "service_start", "service_end", "ref_id")
    assert {k: issued.get(k) for k in fixed} == {k: march.get(k) for k in fixed}


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
async def test_amendment_refuses_non_finite_quantity(owner_ui, monkeypatch, raw):
    """A quantity that is not a finite number (JSON allows NaN and Infinity) is refused at the
    request body, and nothing is written."""
    freeze_day(monkeypatch, "2026-03-01")
    eid = await agreement(owner_ui.api, {}, start="2026-03-01", line_items=[dict(SEATS)])
    line = await _line_id(owner_ui.api, eid)
    assert (await generate_due(owner_ui.api, {}, eid)).status_code == 200
    body = '{"effective": "2026-03-10", "quantities": {%s: %s}, "request_key": "%s"}' % (
        json.dumps(line), raw, uuid.uuid4())
    r = await owner_ui.api.post(f"/subscriptions/{eid}/amend", content=body,
                                headers={"content-type": "application/json"})
    assert r.status_code == 422 and r.json()["detail"][0]["type"] == "finite_number", r.text
    assert not (await template(owner_ui.api, {}, eid)).get("amendments")


async def test_seat_change_to_zero_on_a_paid_period(owner_ui, session, monkeypatch):
    """March billed in advance and paid in full: a change to 0 seats from 15 March issues the
    credit note for the 17 days left (16.45) against the paid invoice, which stays paid, and a
    document for the new terms with nothing to bill; April bills 0 seats."""
    freeze_day(monkeypatch, "2026-03-01")
    eid = await agreement(owner_ui.api, {}, start="2026-03-01", line_items=[dict(SEATS)])
    line = await _line_id(owner_ui.api, eid)
    assert (await generate_due(owner_ui.api, {}, eid)).status_code == 200
    [march] = await billed_docs(session, eid)
    r = await owner_ui.api.post(f"/docs/{march['entity_id']}/payment", json={
        "amount": 30.0, "payment_date": "2026-03-02", "bank_account": "1111", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    freeze_day(monkeypatch, "2026-03-15")
    r = await _amend(owner_ui.api, eid, "2026-03-15", {line: 0})
    assert r.status_code == 200, r.text
    docs = await billed_docs(session, eid)
    [credit] = _of(docs, "credit_note")
    assert credit["total"] == 16.45 and credit["status"] not in ("draft", "void")
    [march_now] = [d for d in docs if d["entity_id"] == march["entity_id"]]
    assert march_now["amount_outstanding"] == 0
