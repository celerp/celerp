# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Retainers and Members: pages over the agreement list, scoped by the agreement's kind.

Initial conditions per test: a newly registered company signed in to the UI (owner_ui), its
API calls answered in process; agreements are made through POST /docs and activated through
the subscriptions API; a member's standing is read from GET /subscriptions/members on a frozen
business day. No payment provider is configured, so payments are recorded by hand.
"""
from __future__ import annotations

import re
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


async def test_an_empty_view_with_agreements_says_nothing_matches(owner_ui, monkeypatch):
    """A list view whose status or search leaves nothing out of the agreements that exist says
    nothing matches and how to widen it, never that there are none yet: All issued leaves the
    only draft out."""
    from ui.i18n import t
    freeze_day(monkeypatch, "2026-03-01")
    page = (await owner_ui.get("/retainers")).text
    assert t("subscriptions.retainers_empty") in page
    await agreement(owner_ui.api, {}, start="2026-03-01", kind="retainer", activate=False)
    page = (await owner_ui.get("/retainers")).text
    assert t("subscriptions.none_in_view") in page and t("subscriptions.retainers_empty") not in page
    page = (await owner_ui.get("/retainers?status=draft")).text
    assert t("subscriptions.none_in_view") not in page


async def test_new_retainer_without_stripe(owner_ui, session, monkeypatch):
    """New retainer on the Retainers page makes a retainer agreement; it bills and is paid by
    bank with no payment provider set up."""
    freeze_day(monkeypatch, "2026-03-01")
    page = await owner_ui.get("/retainers")
    assert page.status_code == 200 and "New retainer" in page.text, page.text[:300]
    r = await owner_ui.post("/subscriptions/new", params={"direction": "sales", "kind": "retainer"})
    assert r.status_code == 204, r.text[:300]
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


async def _lifetime_sale(api, extra: list[dict] = ()) -> tuple[str, str, str]:
    """An issued one-time invoice selling one lifetime membership (500) plus ``extra`` lines:
    (invoice, lifetime item, member)."""
    assert (await api.post("/companies/me/apply-preset", params={"vertical": "nonprofit"})).status_code == 200
    r = await api.post("/items", json={"name": "Lifetime membership", "category": "membership", "sell_by": "piece",
                                       "inventory_type": "service", "quantity": 0, "status": "available",
                                       "attributes": {"membership_term": "Lifetime"}})
    assert r.status_code == 200, r.text
    item = r.json()["id"]
    who = await contact(api, {}, f"life-{uuid.uuid4().hex[:6]}@test.test")
    lines = [{"item_id": item, "name": "Lifetime membership", "quantity": 1, "unit_price": 500.0, "line_total": 500.0},
             *extra]
    # The invoice names its price list, so the line keeps its price rather than the contact's list's.
    r = await api.post("/docs", json={"doc_type": "invoice", "contact_id": who, "price_list": "Retail",
                                      "total": sum(li["line_total"] for li in lines), "line_items": lines})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    assert (await api.post(f"/docs/{doc}/finalize")).status_code == 200
    return doc, item, who


async def _credit(api, invoice: str, who: str, lines: list[dict]) -> None:
    """An issued credit note against ``invoice`` crediting ``lines``."""
    r = await api.post("/docs", json={"doc_type": "credit_note", "original_doc_id": invoice, "contact_id": who,
                                      "total": sum(li["line_total"] for li in lines), "line_items": lines})
    assert r.status_code == 200, r.text
    assert (await api.post(f"/docs/{r.json()['id']}/finalize")).status_code == 200, r.text


async def test_lifetime_membership_one_time_invoice(owner_ui, monkeypatch):
    """A one-time invoice with a lifetime membership item lists its member as lifetime once
    its balance is settled, and not before."""
    freeze_day(monkeypatch, "2026-03-01")
    api = owner_ui.api
    doc, _, who = await _lifetime_sale(api)
    assert doc not in await _members(api)
    await _paid(api, doc, 500.0, "2026-03-02")
    row = (await _members(api))[doc]
    assert row["standing"] == "lifetime" and row["contact_id"] == who


async def test_a_lifetime_sale_credited_back_in_full_makes_no_member(owner_ui, monkeypatch):
    """Lifetime holds only while the lifetime line is kept: credit notes against the invoice
    that credit it back in full end it; crediting another line of the sale does not."""
    freeze_day(monkeypatch, "2026-03-01")
    api = owner_ui.api
    pin = {"name": "Pin", "quantity": 1, "unit_price": 20.0, "line_total": 20.0}
    kept, _, kept_who = await _lifetime_sale(api, [pin])
    await _paid(api, kept, 520.0, "2026-03-02")
    await _credit(api, kept, kept_who, [pin])
    assert (await _members(api))[kept]["standing"] == "lifetime"
    doc, item, who = await _lifetime_sale(api)
    await _paid(api, doc, 500.0, "2026-03-02")
    assert doc in await _members(api)
    await _credit(api, doc, who, [{"item_id": item, "name": "Lifetime membership", "quantity": 1,
                                   "unit_price": 500.0, "line_total": 500.0}])
    assert doc not in await _members(api)


async def _note(api, who: str, amount: float, *, against: str | None = None, item: str | None = None) -> str:
    """An issued credit note for ``amount``: against the invoice ``against`` (original_doc_id) or
    standalone, crediting the lifetime ``item`` (one, at ``amount``) or a general credit."""
    line = {"item_id": item, "name": "Lifetime membership"} if item else {"name": "Goodwill credit"}
    r = await api.post("/docs", json={
        "doc_type": "credit_note", "contact_id": who, "price_list": "Retail", "total": amount,
        "line_items": [{**line, "quantity": 1, "unit_price": amount, "line_total": amount}],
        **({"original_doc_id": against} if against else {})})
    assert r.status_code == 200, r.text
    assert (await api.post(f"/docs/{r.json()['id']}/finalize")).status_code == 200, r.text
    return r.json()["id"]


async def _apply(api, note: str, invoice: str, amount: float) -> None:
    r = await api.post(f"/docs/{note}/apply-to-invoice", json={
        "target_doc_id": invoice, "amount": amount, "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text


# Each way credit reaches a lifetime sale of 500, given the partial amount p (0 < p < 500):
# (what is credited, and whether its member stays lifetime).
async def _linked_full(api, doc, item, who, p):
    await _paid(api, doc, 500.0, "2026-03-02")
    await _note(api, who, 500.0, against=doc, item=item)


async def _linked_partial(api, doc, item, who, p):
    await _note(api, who, p, against=doc)
    await _paid(api, doc, 500.0 - p, "2026-03-02")


async def _standalone_full(api, doc, item, who, p):
    await _apply(api, await _note(api, who, 500.0), doc, 500.0)


async def _standalone_partial(api, doc, item, who, p):
    await _apply(api, await _note(api, who, p), doc, p)
    await _paid(api, doc, 500.0 - p, "2026-03-02")


async def _mixed(api, doc, item, who, p):
    await _note(api, who, p, against=doc)
    await _apply(api, await _note(api, who, 500.0 - p, item=item), doc, 500.0 - p)


async def _unapplied(api, doc, item, who, p):
    await _paid(api, doc, 500.0, "2026-03-02")
    await _note(api, who, 500.0)


_CREDIT_PATHS = {_linked_full: False, _linked_partial: True, _standalone_full: False, _standalone_partial: True,
                 _mixed: False, _unapplied: True}


# Credit applied without any note issued against an invoice: each applied note is read on its own.
_APPLIED_ONLY = (_standalone_full, _standalone_partial, _unapplied)


@pytest.mark.parametrize("paths", [tuple(_CREDIT_PATHS), _APPLIED_ONLY], ids=["every", "applied_only"])
@pytest.mark.parametrize("seed", [11, 4026])
async def test_lifetime_reads_every_credit_note_that_credits_its_sale(owner_ui, monkeypatch, seed, paths):
    """Lifetime reads what credit notes credited back on the sale, however they reached it: a
    note issued against the invoice or one issued on its own and applied to it, in full or in
    part, or both; a note that names the lifetime item credits that quantity, one that does not
    credits the sale pro rata to what it applies. Settled entirely by credit, the sale makes no
    member; credited in part it still does; a note never applied to it changes nothing."""
    import random
    freeze_day(monkeypatch, "2026-03-01")
    api, rng = owner_ui.api, random.Random(seed)
    sales = {}
    for path in paths:
        doc, item, who = await _lifetime_sale(api)
        await path(api, doc, item, who, rng.randrange(1, 500) * 1.0)
        sales[path.__name__] = doc
    members = await _members(api)
    assert {name: doc in members for name, doc in sales.items()} == {
        path.__name__: _CREDIT_PATHS[path] for path in paths}


async def test_a_cancelled_or_expired_membership_reads_ended_and_a_void_one_is_not_listed(owner_ui, session, monkeypatch):
    """Standing is read as of today from the agreement as well as its bill: a cancelled
    membership, or one whose term ended, is no longer current however its last bill stands;
    a voided agreement never made anyone a member. The table shows each agreement's status."""
    freeze_day(monkeypatch, "2026-03-01")
    api = owner_ui.api
    cancelled, doc = await _membership(api, session)
    await _paid(api, doc, 100.0, "2026-03-01")
    ending, ending_doc = await _membership(api, session)
    await _paid(api, ending_doc, 100.0, "2026-03-01")
    r = await api.patch(f"/subscriptions/{ending}/schedule", json={"term_end": "2027-03-01"})
    assert r.status_code == 200, r.text
    assert (await api.post(f"/subscriptions/{cancelled}/cancel")).status_code == 200
    voided, voided_doc = await _membership(api, session)
    assert (await api.post(f"/docs/{voided_doc}/void", json={"reason": "Wrong amount"})).status_code == 200
    r = await api.post(f"/docs/{voided}/void", json={"reason": "Entered by mistake", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    rows = await _members(api)
    assert voided not in rows
    assert (rows[cancelled]["standing"], rows[cancelled]["status"]) == ("ended", "cancelled")
    assert rows[ending]["standing"] == "current"
    freeze_day(monkeypatch, "2027-03-01")
    assert (await _members(api))[ending]["standing"] == "ended"
    from ui.i18n import t

    page = await owner_ui.get("/members")
    assert page.status_code == 200
    assert t("enum.member_standing.ended") in page.text and t("enum.subscription_status.cancelled") in page.text


async def test_an_amendment_bill_is_the_one_standing_reads(owner_ui, session, monkeypatch):
    """A paid membership raised mid-term bills the change on a new invoice; until that is
    settled the member's standing follows it like any bill."""
    freeze_day(monkeypatch, "2026-03-01")
    api = owner_ui.api
    eid, doc = await _membership(api, session, grace=14)
    await _paid(api, doc, 100.0, "2026-03-01")
    line = (await template(api, {}, eid))["line_items"][0]["line_id"]
    freeze_day(monkeypatch, "2026-05-01")
    r = await api.post(f"/subscriptions/{eid}/amend", json={
        "effective": "2026-05-01", "quantities": {line: 2}, "request_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    amendment = r.json()["invoice_id"]
    row = (await _members(api))[eid]
    assert (row["standing"], row["bill_id"]) == ("current", amendment)
    freeze_day(monkeypatch, "2026-06-30")
    assert (await _members(api))[eid]["standing"] == "lapsed"


async def test_standing_reads_the_oldest_bill_still_owing(owner_ui, session, monkeypatch):
    """A newer bill paid does not clear an older one still owing: standing runs from the
    oldest unpaid bill's due date and grace, and reads current once nothing is owed."""
    freeze_day(monkeypatch, "2026-03-01")
    api = owner_ui.api
    eid, doc = await _membership(api, session, grace=14)
    line = (await template(api, {}, eid))["line_items"][0]["line_id"]
    freeze_day(monkeypatch, "2026-03-05")
    r = await api.post(f"/subscriptions/{eid}/amend", json={
        "effective": "2026-03-05", "quantities": {line: 2}, "request_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    amendment = r.json()["invoice_id"]
    await _paid(api, amendment, float((await template(api, {}, amendment)).get("amount_outstanding") or 0), "2026-03-05")
    freeze_day(monkeypatch, "2026-03-10")
    row = (await _members(api))[eid]
    assert (row["standing"], row["bill_id"]) == ("in_grace", doc)
    freeze_day(monkeypatch, "2026-03-16")
    assert (await _members(api))[eid]["standing"] == "lapsed"
    await _paid(api, doc, float((await template(api, {}, doc))["amount_outstanding"]), "2026-03-16")
    assert (await _members(api))[eid]["standing"] == "current"


@pytest.mark.parametrize("edit", ["lifetime_sale_then_item_changed", "item_made_lifetime_after_the_sale",
                                  "item_made_lifetime_while_draft"])
async def test_lifetime_membership_is_what_was_sold(owner_ui, monkeypatch, edit):
    """Lifetime is fixed by the item as it stood when the invoice was issued: editing the item
    afterwards neither takes lifetime from a member who bought it nor gives it to one who bought
    something else, and an edit while the invoice is a draft counts."""
    freeze_day(monkeypatch, "2026-03-01")
    api = owner_ui.api
    assert (await api.post("/companies/me/apply-preset", params={"vertical": "nonprofit"})).status_code == 200
    sold_as_lifetime = edit != "item_made_lifetime_after_the_sale"
    first = "Lifetime" if edit == "lifetime_sale_then_item_changed" else "Annual"
    r = await api.post("/items", json={
        "name": "Membership", "category": "membership", "sell_by": "piece", "inventory_type": "service",
        "quantity": 0, "status": "available", "attributes": {"membership_term": first}})
    assert r.status_code == 200, r.text
    item = r.json()["id"]

    async def set_term(old: str, new: str) -> None:
        r = await api.patch(f"/items/{item}", json={"fields_changed": {"attributes": {
            "old": {"membership_term": old}, "new": {"membership_term": new}}}})
        assert r.status_code == 200, r.text
        r = await api.get(f"/items/{item}")
        assert r.status_code == 200 and r.json().get("membership_term") == new, r.text

    who = await contact(api, {}, f"life-{uuid.uuid4().hex[:6]}@test.test")
    r = await api.post("/docs", json={"doc_type": "invoice", "contact_id": who, "price_list": "Retail", "total": 500.0,
                                      "line_items": [{"item_id": item, "name": "Membership", "quantity": 1,
                                                      "unit_price": 500.0, "line_total": 500.0}]})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    if edit == "item_made_lifetime_while_draft":
        await set_term("Annual", "Lifetime")
    assert (await api.post(f"/docs/{doc}/finalize")).status_code == 200
    await _paid(api, doc, 500.0, "2026-03-02")
    sold = "Lifetime" if sold_as_lifetime else "Annual"
    await set_term(sold, "Annual" if sold_as_lifetime else "Lifetime")
    assert (doc in await _members(api)) is sold_as_lifetime


@pytest.mark.parametrize("how", ["data", "fields_changed"])
async def test_no_writer_rewrites_what_an_invoice_sold(owner_ui, session, monkeypatch, how):
    """What an issued invoice sold is written once, at issue: an event that names it, at the top
    level or as a changed field, is passed on without it."""
    from sqlalchemy import select
    from celerp.events.engine import emit_event
    from celerp.models.projections import Projection

    freeze_day(monkeypatch, "2026-03-01")
    api = owner_ui.api
    r = await api.post("/items", json={"name": "Membership", "category": "membership", "sell_by": "piece",
                                       "inventory_type": "service", "quantity": 0, "status": "available",
                                       "attributes": {"membership_term": "Lifetime"}})
    assert r.status_code == 200, r.text
    item = r.json()["id"]
    who = await contact(api, {}, f"sold-{uuid.uuid4().hex[:6]}@test.test")
    r = await api.post("/docs", json={"doc_type": "invoice", "contact_id": who, "price_list": "Retail", "total": 50.0,
                                      "line_items": [{"item_id": item, "name": "Membership", "quantity": 1,
                                                      "unit_price": 50.0, "line_total": 50.0}]})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    assert (await api.post(f"/docs/{doc}/finalize")).status_code == 200
    row = (await session.execute(select(Projection).where(Projection.entity_id == doc))).scalar_one()
    sold = row.state["items_at_sale"]
    assert sold == {item: {"category": "membership", "attributes": {"membership_term": "Lifetime"}}}
    other = {item: {"category": "membership", "attributes": {"membership_term": "Annual"}}}
    data = ({"items_at_sale": other, "fields_changed": {}} if how == "data"
            else {"fields_changed": {"items_at_sale": {"old": sold, "new": other}}})
    await emit_event(session, company_id=row.company_id, entity_id=doc, entity_type="doc",
                     event_type="doc.updated", data=data, actor_id=None, location_id=None, source="test",
                     idempotency_key=uuid.uuid4().hex)
    await session.commit()
    await session.refresh(row)
    assert row.state["items_at_sale"] == sold


async def test_page_scoped_labels_no_global_customer_swap(owner_ui, session, monkeypatch):
    from ui.i18n import t

    freeze_day(monkeypatch, "2026-03-01")
    await _membership(owner_ui.api, session)
    members = await owner_ui.get("/members")
    assert members.status_code == 200 and ">Member<" in members.text.replace(" ", ""), members.text[:300]
    assert t("enum.contact_type.customer") == "Customer" and t("th.customer") == "Customer"
    contacts = await owner_ui.get("/contacts/customers")
    # The page's own words (headings, column headers, sidebar) stay Customer; the agreement kit's
    # contacts are named "Member", so their rows may say it.
    assert contacts.status_code == 200
    assert not re.search(r"<(th|h1|title)[^>]*>\s*Members?\b", contacts.text), contacts.text[:300]
    assert 'href="/members"' not in contacts.text


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


async def test_agreement_kind_member_grace_checked_and_changed_on_schedule(owner_ui, monkeypatch):
    """Kind, member and grace days are checked by every writer, refused by Documents edit and
    changed through the agreement's schedule; an unknown kind in the list filter is refused."""
    from ui.i18n import t

    freeze_day(monkeypatch, "2026-03-01")
    api = owner_ui.api
    for bad, key in (({"kind": "club"}, "err_kind"), ({"grace_days": 366}, "err_grace_days"),
                     ({"grace_days": -1}, "err_grace_days"), ({"beneficiary_contact_id": "  "}, "err_beneficiary")):
        r = await api.post("/docs", json={"doc_type": "subscription_invoice", "contact_id": "contact:test-001",
                                          "frequency": "monthly", "start_date": "2026-03-01", **bad,
                                          "line_items": [{"name": "Dues", "quantity": 1, "unit_price": 10.0,
                                                          "line_total": 10.0}], "total": 10.0})
        assert r.status_code == 422 and t(f"subscriptions.{key}") in r.text, (bad, r.text)
    eid = await agreement(api, {}, start="2026-03-01", kind="membership", grace_days=7)
    r = await api.patch(f"/docs/{eid}", json={"fields_changed": {"grace_days": {"new": 30}}})
    assert r.status_code == 422, r.text
    r = await api.patch(f"/subscriptions/{eid}/schedule", json={"grace_days": 30, "kind": "retainer"})
    assert r.status_code == 200, r.text
    state = await template(api, {}, eid)
    assert state["grace_days"] == 30 and state["kind"] == "retainer"
    r = await api.patch(f"/subscriptions/{eid}/schedule", json={"kind": "club"})
    assert r.status_code == 422 and "subscription, retainer or membership" in r.text, r.text
    assert (await api.get("/subscriptions", params={"kind": "club"})).status_code == 422


async def test_the_member_must_be_a_contact_of_the_company(owner_ui, monkeypatch):
    """A member that names no contact of the company (a made-up id, or a document's) is refused
    by every writer: Documents' create and the agreement's schedule; a real contact stands."""
    from ui.i18n import t

    freeze_day(monkeypatch, "2026-03-01")
    api = owner_ui.api
    refused = t("subscriptions.err_beneficiary")
    for bad in ("contact:nobody", "doc:INV-0001"):
        r = await api.post("/docs", json={"doc_type": "subscription_invoice", "contact_id": "contact:test-001",
                                          "frequency": "monthly", "start_date": "2026-03-01",
                                          "beneficiary_contact_id": bad,
                                          "line_items": [{"name": "Dues", "quantity": 1, "unit_price": 10.0,
                                                          "line_total": 10.0}], "total": 10.0})
        assert r.status_code == 422 and refused in r.text, (bad, r.text)
    eid = await agreement(api, {}, start="2026-03-01", kind="membership")
    r = await api.patch(f"/subscriptions/{eid}/schedule", json={"beneficiary_contact_id": "contact:nobody"})
    assert r.status_code == 422 and refused in r.text, r.text
    member = await contact(api, {}, "member-one@example.com")
    r = await api.patch(f"/subscriptions/{eid}/schedule", json={"beneficiary_contact_id": member})
    assert r.status_code == 200, r.text
    assert (await template(api, {}, eid))["beneficiary_contact_id"] == member


@pytest.mark.parametrize(("vertical", "shown", "hidden"), [
    ("consulting", "retainers", "members"), ("nonprofit", "members", "retainers"), (None, None, "members"),
])
async def test_retainers_and_members_in_the_sidebar_of_their_business_type_only(vertical, shown, hidden):
    """Retainers is a consulting firm's page and Members a nonprofit's: each shows in the sidebar
    of its business type, and neither in any other, nor in the menu editor's list."""
    from ui.components.shell import module_nav

    keys = {item.get("key") for item in module_nav({"vertical": vertical} if vertical else {})}
    assert "subscriptions_sales" in keys and hidden not in keys and (shown is None or shown in keys)
    if vertical is None:
        assert "retainers" not in keys
