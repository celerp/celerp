# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""An invoice's card charge while what it owes changes: an amendment's credit, a payment by hand,
Cloud withdrawing collection, a success delivered late.

Initial conditions per test (as test_collection): a newly registered company, a payer with a card
on file, an agreement on Bill and collect automatically for 30.00 a month from 1 July 2026, and
Celerp Cloud as a stand-in that answers what the test sets. An amendment effective 15 July (17 of
July's 31 days) taking the line from 1 to 1.6667 credits 16.45 against July's invoice and issues
an invoice of 27.42 for the new terms: 30.00 - 16.45 + 27.42 = 40.97 is what the payer owes.
"""
from __future__ import annotations

import uuid

import pytest

from agreement_kit import agreement, billed_docs, company_id, freeze_day, register, schedule, template, tick
from test_collection import _attempt_outcome, _bind, _flag, _pay, _payer, cloud  # noqa: F401  (cloud: fixture)

pytestmark = pytest.mark.asyncio

PLAN = {"description": "Plan", "quantity": 1, "unit_price": 30.0, "line_total": 30.0}


async def _july(client, session, monkeypatch, cloud, answer: str = "submitted"):
    """July's invoice billed and its charge asked for, Cloud answering ``answer``:
    (headers, company id, agreement id, July's invoice id)."""
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid, payer = await company_id(client, hd), await _payer(client, hd)
    eid = await agreement(client, hd, start="2026-07-01", contact_id=payer, line_items=[dict(PLAN)])
    assert await _bind(cid, payer)
    assert (await schedule(client, hd, eid, generation_mode="auto_invoice_collect")).status_code == 200
    cloud.answers["attempt"] = lambda p: (200, {"state": answer, "attempt_number": p["attempt_number"]})
    await tick(session, cid, "2026-07-01")
    [doc] = await billed_docs(session, eid)
    assert (doc["total"], doc["collection"]["state"]) == (30.0, answer)
    return hd, cid, eid, doc["entity_id"]


async def _amend(client, monkeypatch, hd, eid, *, quantity: float = 1.6667, key: str | None = None):
    freeze_day(monkeypatch, "2026-07-15")
    line = (await template(client, hd, eid))["line_items"][0]["line_id"]
    return await client.post(f"/subscriptions/{eid}/amend", headers=hd, json={
        "effective": "2026-07-15", "quantities": {line: quantity}, "request_key": key or uuid.uuid4().hex})


async def _docs(session, eid) -> dict[str, dict]:
    return {d["entity_id"]: d for d in await billed_docs(session, eid)}


def _asked(cloud, doc_id: str) -> list[int]:
    return [p["amount_minor"] for p in cloud.sent("attempt") if p["entity_id"] == doc_id]


async def _unmatched(session) -> list[tuple[str, int]]:
    from celerp.services.payments import unmatched_payments
    return [(p.reference, p.amount_minor) for p in await unmatched_payments(session)]


async def test_a_late_charge_on_an_invoice_paid_in_part_by_hand_is_kept_whole_among_the_unmatched(
        client, session, monkeypatch, cloud):
    """A payment by hand of 10.00 while the 30.00 is with Cloud, the cancel refused, the 30.00 succeeds:
    it no longer fits July's 20.00, so it is kept whole among the unmatched payments for a person to
    place, the company is told once, and July still owes 20.00. The same delivery again changes
    nothing."""
    from celerp.services import payments
    hd, cid, eid, july = await _july(client, session, monkeypatch, cloud)
    [sent] = cloud.sent("attempt")
    cloud.answers["cancel"] = (409, {"detail": "attempt_submitted"})
    await _pay(client, hd, july, 10.0)
    await tick(session, cid, "2026-07-02")
    delivery = {"company_id": cid, "entity_id": july, "reference": "pi_over", "amount_minor": 3000,
                "currency": "USD", "paid_at": "2026-07-02T13:00:00+00:00", "managed": True,
                "context": sent["context"]}
    assert await payments.receive_payment(delivery)
    assert await payments.receive_payment(delivery)
    assert (await _docs(session, eid))[july]["amount_outstanding"] == 20.0
    assert await _unmatched(session) == [("pi_over", 3000)]
    from sqlalchemy import select
    from celerp.models.notification import Notification
    told = (await session.execute(select(Notification.body, Notification.action_url).where(
        Notification.title == "Card payment pi_over not matched to an invoice"))).all()
    assert len(told) == 1 and "30.00" in told[0].body and told[0].action_url == "/settings/payments"


async def test_a_late_charge_on_an_invoice_paid_by_hand_is_kept_whole_and_the_credit_goes_to_the_amendment(
        client, session, monkeypatch, cloud):
    """July is paid 30.00 by hand while its charge is with Cloud, the cancel refused, then the
    amendment's 16.45 credit is issued at once: July owes nothing, so all of it goes to the
    amendment's 27.42, which then owes 10.97. The 30.00 succeeds and is kept whole among the
    unmatched payments."""
    from celerp.services import payments
    hd, cid, eid, july = await _july(client, session, monkeypatch, cloud)
    [sent] = cloud.sent("attempt")
    cloud.answers["cancel"] = (409, {"detail": "attempt_submitted"})
    await _pay(client, hd, july, 30.0)
    await tick(session, cid, "2026-07-02")
    new = (await _amend(client, monkeypatch, hd, eid)).json()["invoice_id"]
    assert await _attempt_outcome(cid, july, 1, "succeeded") is True
    assert await payments.receive_payment({
        "company_id": cid, "entity_id": july, "reference": "pi_next", "amount_minor": 3000, "currency": "USD",
        "paid_at": "2026-07-15T13:00:00+00:00", "managed": True, "context": sent["context"]})
    await tick(session, cid, "2026-07-16")
    docs = await _docs(session, eid)
    assert (docs[july]["amount_outstanding"], docs[new]["amount_outstanding"]) == (0, 10.97)
    assert await _unmatched(session) == [("pi_next", 3000)]


async def test_the_owner_example_a_late_charge_after_the_amendment_is_placed_by_a_person(
        client, session, monkeypatch, cloud):
    """The amendment's 16.45 credit is issued at once while July's 30.00 is with Cloud, so July owes
    13.55 and the amendment's invoice is asked its 27.42. The 30.00 then succeeds: it no longer fits
    July, so it is kept whole among the unmatched payments, the company is told once and July is not
    asked its 13.55 while the 30.00 waits. A person
    voids the credit note, records the 30.00 on July, restores the note and applies it to the
    amendment's invoice, which then owes 10.97: 30.00 + 10.97 = 40.97, and the books stay balanced."""
    from sqlalchemy import select
    from celerp.models.notification import Notification
    from celerp.services import payments
    hd, cid, eid, july = await _july(client, session, monkeypatch, cloud)
    [sent] = cloud.sent("attempt")
    cloud.answers["cancel"] = (409, {"detail": "attempt_submitted"})
    r = await _amend(client, monkeypatch, hd, eid)
    assert r.status_code == 200, r.text
    note, new = r.json()["credit_note_id"], r.json()["invoice_id"]
    await tick(session, cid, "2026-07-15")
    assert _asked(cloud, new) == [2742]
    delivery = {"company_id": cid, "entity_id": july, "reference": "pi_late", "amount_minor": 3000,
                "currency": "USD", "paid_at": "2026-07-15T13:00:00+00:00", "managed": True,
                "context": sent["context"]}
    assert await payments.receive_payment(delivery)
    assert await payments.receive_payment(delivery)
    await tick(session, cid, "2026-07-16")
    docs = await _docs(session, eid)
    assert (docs[july]["amount_outstanding"], docs[new]["amount_outstanding"]) == (13.55, 27.42)
    assert await _unmatched(session) == [("pi_late", 3000)]
    told = (await session.execute(select(Notification.action_url).where(
        Notification.title == "Card payment pi_late not matched to an invoice"))).all()
    assert told == [("/settings/payments",)]
    assert _asked(cloud, july) == [3000]

    assert (await client.post(f"/docs/{note}/void", headers=hd, json={})).status_code == 200
    r = await client.post("/payments/unmatched/record", headers=hd, json={"reference": "pi_late", "entity_id": july})
    assert r.status_code == 200, r.text
    assert (await client.post(f"/docs/{note}/unvoid", headers=hd, json={})).status_code == 200
    r = await client.post(f"/docs/{note}/apply-to-invoice", headers=hd, json={"target_doc_id": new, "amount": 16.45})
    assert r.status_code == 200, r.text
    await tick(session, cid, "2026-07-17")
    docs = await _docs(session, eid)
    assert (docs[july]["amount_outstanding"], docs[note]["amount_outstanding"], docs[new]["amount_outstanding"]) \
        == (0, 0, 10.97)
    assert await _unmatched(session) == []
    trial = (await client.get("/accounting/trial-balance", headers=hd)).json()
    assert trial["balanced"] is True and trial["total_debit"] > 0


async def test_an_amendment_after_a_successful_charge_charges_only_what_its_credit_leaves(
        client, session, monkeypatch, cloud):
    """July's 30.00 is paid before the amendment, so its 16.45 credit cannot settle July: it is applied
    to the amendment's 27.42 before that is charged, which leaves 10.97 (40.97 in all)."""
    from celerp.services import payments
    hd, cid, eid, july = await _july(client, session, monkeypatch, cloud)
    [sent] = cloud.sent("attempt")
    assert await payments.receive_payment({
        "company_id": cid, "entity_id": july, "reference": "pi_1", "amount_minor": 3000, "currency": "USD",
        "paid_at": "2026-07-01T13:00:00+00:00", "managed": True, "context": sent["context"]})
    r = await _amend(client, monkeypatch, hd, eid)
    assert r.status_code == 200, r.text
    note, new = r.json()["credit_note_id"], r.json()["invoice_id"]
    docs = await _docs(session, eid)
    assert _asked(cloud, new) == [1097]
    assert docs[new]["amount_outstanding"] == 10.97
    assert [(p["method"], p.get("source_doc_id")) for p in docs[new]["payments"]] == [("credit_note", note)]
    assert docs[note]["amount_outstanding"] == 0


async def test_a_partial_payment_by_hand_while_the_charge_is_submitted_cancels_it(client, session, monkeypatch, cloud):
    """A payment by hand of 10.00 while the 30.00 is with Cloud: the charge is cancelled at Cloud and,
    once cancelled, the invoice is charged the 20.00 it owes."""
    hd, cid, eid, july = await _july(client, session, monkeypatch, cloud)
    await _pay(client, hd, july, 10.0)
    await tick(session, cid, "2026-07-02")
    assert [p["entity_id"] for p in cloud.sent("cancel")] == [july]
    await tick(session, cid, "2026-07-03")
    assert _asked(cloud, july) == [3000, 2000]


async def test_cloud_withdrawing_collection_keeps_a_queued_cancel_going(client, session, monkeypatch, cloud):
    """A cancel queued and not yet answered when Cloud withdraws collection is still asked for until
    Cloud answers; nothing new is charged."""
    hd, cid, eid, july = await _july(client, session, monkeypatch, cloud)
    await _pay(client, hd, july, 30.0)
    cloud.answers["cancel"] = None
    await tick(session, cid, "2026-07-02")
    _flag(None)
    await tick(session, cid, "2026-07-03")
    cloud.answers["cancel"] = (200, {"state": "canceled"})
    await tick(session, cid, "2026-07-04")
    assert [p["entity_id"] for p in cloud.sent("cancel")] == [july] * 3
    assert len(cloud.sent("attempt")) == 1
    assert (await _docs(session, eid))[july]["collection"]["state"] == "canceled"


async def test_cloud_withdrawing_collection_still_cancels_a_charge_the_invoice_no_longer_owes(
        client, session, monkeypatch, cloud):
    """Collection withdrawn while the 30.00 is with Cloud, then the invoice paid by hand: the charge is
    cancelled at Cloud, and no charge follows the cancel while collection is off."""
    hd, cid, eid, july = await _july(client, session, monkeypatch, cloud)
    _flag(None)
    await tick(session, cid, "2026-07-02")
    assert cloud.sent("cancel") == []  # an attempt already with Cloud runs to its outcome
    await _pay(client, hd, july, 10.0)
    await tick(session, cid, "2026-07-03")
    await tick(session, cid, "2026-07-04")
    assert [p["entity_id"] for p in cloud.sent("cancel")] == [july]
    assert _asked(cloud, july) == [3000]
    c = (await _docs(session, eid))[july]["collection"]
    assert c["state"] == "canceled"


async def test_a_late_success_after_the_charge_was_cancelled_for_a_smaller_balance_is_taken(
        client, session, monkeypatch, cloud):
    """Cloud delivers the 30.00 as succeeded after the app queued its cancel for the amendment's credit:
    the outcome is recorded (a known outcome is not lost) and the amendment's invoice, its credit
    issued at once, is asked its 27.42 without waiting for the July charge."""
    hd, cid, eid, july = await _july(client, session, monkeypatch, cloud)
    cloud.answers["cancel"] = None
    new = (await _amend(client, monkeypatch, hd, eid)).json()["invoice_id"]
    await tick(session, cid, "2026-07-15")
    assert await _attempt_outcome(cid, july, 1, "succeeded") is True
    await tick(session, cid, "2026-07-16")
    assert _asked(cloud, new) == [2742]


async def test_the_same_amend_request_key_with_another_quantity_is_refused(client, session, monkeypatch, cloud):
    """L4: the same request key sent again with another quantity is refused and the first stands."""
    hd, cid, eid, july = await _july(client, session, monkeypatch, cloud)
    key = uuid.uuid4().hex
    assert (await _amend(client, monkeypatch, hd, eid, quantity=2, key=key)).status_code == 200
    r = await _amend(client, monkeypatch, hd, eid, quantity=3, key=key)
    assert r.status_code == 409, r.text
    assert [a["line_items"][0]["quantity"] for a in (await template(client, hd, eid))["amendments"]] == [2]
