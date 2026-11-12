# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Retainers and Members: pages over the agreement list, scoped by the agreement's kind.

Initial conditions per test: a newly registered company signed in to the UI (owner_ui), its
API calls answered in process; agreements are made through POST /docs and activated through
the subscriptions API; a member's standing is read from GET /subscriptions/members on a frozen
business day. No payment provider is configured, so payments are recorded by hand.
"""
from __future__ import annotations

import uuid

import pytest

from agreement_kit import agreement, billed_docs, contact, freeze_day, generate_due, member, template
from test_helpers import ui_signed_in

pytestmark = pytest.mark.asyncio


async def _paid(api, doc: str, amount: float, day: str):
    r = await api.post(f"/docs/{doc}/payment", json={
        "amount": amount, "payment_date": day, "bank_account": "1111", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text


async def _members(api) -> dict[str, dict]:
    r = await api.get("/subscriptions/members")
    assert r.status_code == 200, r.text
    return {row.get("entity_id") or row.get("doc_id"): row for row in r.json()["items"]}


async def _membership(api, session, *, grace: int = 14, start: str = "2026-03-01") -> tuple[str, str]:
    """An activated annual membership billed for its first period: (agreement, its invoice)."""
    payer = await contact(api, {}, f"payer-{uuid.uuid4().hex[:6]}@test.test")
    beneficiary = await contact(api, {}, f"member-{uuid.uuid4().hex[:6]}@test.test")
    eid = await agreement(api, {}, start=start, contact_id=payer, frequency="annually", kind="membership",
                          beneficiary_contact_id=beneficiary, grace_days=grace)
    assert (await generate_due(api, {}, eid)).status_code == 200
    [doc] = await billed_docs(session, eid)
    return eid, doc["entity_id"]


async def test_retainers_page_lists_retainer_kind_only(owner_ui, monkeypatch):
    freeze_day(monkeypatch, "2026-03-01")
    retainer = await agreement(owner_ui.api, {}, start="2026-03-01", kind="retainer")
    plain = await agreement(owner_ui.api, {}, start="2026-03-01")
    assert (await template(owner_ui.api, {}, plain)).get("kind") in (None, "subscription")
    r = await owner_ui.api.get("/subscriptions", params={"kind": "retainer"})
    assert r.status_code == 200, r.text
    assert [i.get("id") or i.get("entity_id") for i in r.json()["items"]] == [retainer]
    page = await owner_ui.get("/retainers")
    assert page.status_code == 200, page.text[:300]
    assert f"/subscriptions/{retainer}" in page.text and f"/subscriptions/{plain}" not in page.text


async def test_new_retainer_without_stripe(owner_ui, session, monkeypatch):
    """New retainer on the Retainers page makes a retainer agreement; it bills and is paid by
    bank with no payment provider set up."""
    freeze_day(monkeypatch, "2026-03-01")
    page = await owner_ui.get("/retainers")
    assert page.status_code == 200 and "New retainer" in page.text, page.text[:300]
    r = await owner_ui.post("/subscriptions/new", params={"direction": "sales", "kind": "retainer"})
    assert r.status_code == 200, r.text[:300]
    eid = r.headers["HX-Redirect"].rsplit("/", 1)[-1]
    assert (await template(owner_ui.api, {}, eid)).get("kind") == "retainer"
    payer = await contact(owner_ui.api, {}, f"client-{uuid.uuid4().hex[:6]}@test.test")
    retainer = await agreement(owner_ui.api, {}, start="2026-03-01", contact_id=payer, kind="retainer")
    assert (await generate_due(owner_ui.api, {}, retainer)).status_code == 200
    [doc] = await billed_docs(session, retainer)
    await _paid(owner_ui.api, doc["entity_id"], 100.0, "2026-03-02")
    paid = (await owner_ui.api.get(f"/docs/{doc['entity_id']}")).json()
    assert paid["status"] == "paid" and float(paid["amount_outstanding"]) == 0


async def test_members_standing_derived_from_balances_grace(owner_ui, session, monkeypatch):
    """Current until the bill falls due, in grace for grace_days after, then lapsed; paid or
    fully credited reads current again, with no stored paid flag."""
    freeze_day(monkeypatch, "2026-03-01")
    eid, doc = await _membership(owner_ui.api, session, grace=14)
    credited, note_target = await _membership(owner_ui.api, session, grace=14)
    assert (await _members(owner_ui.api))[eid]["standing"] == "current"
    freeze_day(monkeypatch, "2026-03-10")
    assert (await _members(owner_ui.api))[eid]["standing"] == "in_grace"
    freeze_day(monkeypatch, "2026-03-16")
    rows = await _members(owner_ui.api)
    assert rows[eid]["standing"] == "lapsed" and rows[credited]["standing"] == "lapsed"
    await _paid(owner_ui.api, doc, 100.0, "2026-03-16")
    r = await owner_ui.api.post("/docs", json={
        "doc_type": "credit_note", "line_items": [{"name": "Dues waived", "quantity": 1, "unit_price": 100.0,
                                                   "line_total": 100.0}], "total": 100.0})
    assert r.status_code == 200, r.text
    note = r.json()["id"]
    assert (await owner_ui.api.post(f"/docs/{note}/finalize")).status_code == 200
    r = await owner_ui.api.post(f"/docs/{note}/apply-to-invoice", json={
        "target_doc_id": note_target, "amount": 100.0, "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    rows = await _members(owner_ui.api)
    assert rows[eid]["standing"] == "current" and rows[credited]["standing"] == "current"
    assert "paid" not in (await template(owner_ui.api, {}, eid))
    page = await owner_ui.get("/members")
    assert page.status_code == 200 and f"/subscriptions/{eid}" in page.text


async def test_lifetime_membership_one_time_invoice(owner_ui, monkeypatch):
    """A one-time invoice with a lifetime membership item lists its member as lifetime once
    its balance is settled, and not before."""
    freeze_day(monkeypatch, "2026-03-01")
    api = owner_ui.api
    assert (await api.post("/companies/me/apply-preset", params={"vertical": "nonprofit"})).status_code == 200
    r = await api.post("/items", json={"name": "Lifetime membership", "category": "membership", "sell_by": "piece",
                                       "inventory_type": "service", "quantity": 0, "status": "available",
                                       "attributes": {"membership_term": "Lifetime"}})
    assert r.status_code == 200, r.text
    item = r.json()["id"]
    who = await contact(api, {}, f"life-{uuid.uuid4().hex[:6]}@test.test")
    r = await api.post("/docs", json={"doc_type": "invoice", "contact_id": who, "total": 500.0, "line_items": [
        {"item_id": item, "name": "Lifetime membership", "quantity": 1, "unit_price": 500.0, "line_total": 500.0}]})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    assert (await api.post(f"/docs/{doc}/finalize")).status_code == 200
    assert doc not in await _members(api)
    await _paid(api, doc, 500.0, "2026-03-02")
    row = (await _members(api))[doc]
    assert row["standing"] == "lifetime" and row["contact_id"] == who


async def test_page_scoped_labels_no_global_customer_swap(owner_ui, session, monkeypatch):
    from ui.i18n import t

    freeze_day(monkeypatch, "2026-03-01")
    await _membership(owner_ui.api, session)
    members = await owner_ui.get("/members")
    assert members.status_code == 200 and ">Member<" in members.text.replace(" ", ""), members.text[:300]
    assert t("enum.contact_type.customer") == "Customer" and t("th.customer") == "Customer"
    contacts = await owner_ui.get("/contacts")
    assert contacts.status_code == 200 and "Member" not in contacts.text


@pytest.mark.parametrize(("path", "empty", "new"), [
    ("/retainers", "No retainers yet.", "New retainer"),
    ("/members", "No members yet. Add a membership tier, then New membership.", "New membership"),
    ("/donors", "No gifts recorded yet.", None),
])
async def test_vertical_pages_empty_states(owner_ui, path, empty, new):
    r = await owner_ui.get(path)
    assert r.status_code == 200, r.text[:300]
    assert empty in r.text
    if new:
        assert new in r.text


@pytest.mark.parametrize(("role", "path"), [
    ("viewer", "/retainers"), ("viewer", "/members"), ("operator", "/donors"),
])
async def test_vertical_pages_permissions(owner_ui, session, role, path):
    """Retainers and Members need view_subscriptions; Donors needs view_contacts and
    view_payments, so a role without one is sent away from the page and refused by its API."""
    headers, _uid = await member(owner_ui.api, session, {}, role)
    async with ui_signed_in(headers["Authorization"].split()[1]) as ui:
        r = await ui.get(path)
        assert r.status_code in (302, 303), (path, r.status_code)
        api = {"/retainers": "/subscriptions?kind=retainer", "/members": "/subscriptions/members",
               "/donors": "/donors"}[path]
        assert (await ui.api.get(api)).status_code == 403
