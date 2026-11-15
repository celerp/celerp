# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A SaaS subscriber collected from a card on file, through the pages and API the app uses.

Initial conditions: a newly registered company signed in to the UI (owner_ui), its API calls
answered in process, and a payer contact with an email address. Celerp Cloud is the in-process
stand-in of test_collection: it records each request the app sends and answers what the test
sets, and its deliveries (collection.binding, collection.attempt, invoice.payment,
invoice.dispute) are handed to the app's delivery handlers, whose sessions join the test's
transaction. The agreement bills 3 seats of the Team plan at 10.00 a month from 1 July 2026,
in advance. One dispatcher pass runs per step on a frozen business day.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from agreement_kit import agreement, billed_docs, company_id, freeze_day, schedule, tick
from test_collection import LINK, _at, _attempt_outcome, _bind, _flag, _payer, _says, stand_in

pytestmark = pytest.mark.asyncio

SEATS = {"description": "Team plan", "quantity": 3, "unit_price": 10.0, "line_total": 30.0}
DISPUTED = "1170"


def _period(docs: list[dict], index: int) -> dict:
    """The invoice billing period ``index`` (a seat change's invoice starts later in it)."""
    return next(d for d in docs if d.get("period_index") == index)


async def _invoices(session, eid: str) -> list[dict]:
    return sorted((d for d in await billed_docs(session, eid) if d["doc_type"] == "invoice"),
                  key=lambda d: (d.get("period_index") or 0, d.get("service_start") or ""))


async def _deliver_payment(sent: dict, reference: str, paid_at: str) -> bool:
    from celerp.services import payments
    return await payments.receive_payment({
        "company_id": sent["company_id"], "entity_id": sent["entity_id"], "reference": reference,
        "amount_minor": sent["amount_minor"], "currency": sent["currency"], "paid_at": paid_at,
        "managed": True, "context": sent["context"], "request_key": sent["request_key"]})


async def _deliver_dispute(sent: dict, reference: str, state: str, occurred_at: str | None,
                           outcome: str | None = None) -> bool:
    from celerp.services import payments
    return await payments.receive_dispute({
        "company_id": sent["company_id"], "entity_id": sent["entity_id"], "reference": reference,
        "dispute_id": "dp_1", "state": state, "outcome": outcome, "amount_minor": sent["amount_minor"],
        "currency": sent["currency"].lower(), "occurred_at": occurred_at, "context": sent["context"],
        "delivery_id": uuid.uuid4().hex})


async def _disputed_balance(session, entity_id: str) -> Decimal:
    """Net debit on Disputed payments over the invoice's posted dispute entries."""
    rows = (await session.execute(text("SELECT state FROM projections WHERE entity_id LIKE :j"),
                                  {"j": f"je:auto:{entity_id}:dispute:%"})).scalars().all()
    total = Decimal(0)
    for je in rows:
        if je.get("status") == "posted":
            for line in je["entries"]:
                if line["account"] == DISPUTED:
                    total += Decimal(str(line.get("debit") or 0)) - Decimal(str(line.get("credit") or 0))
    return total


class _Calendar:
    """The business day of each step. Moving on dates every ledger row the last day wrote at its
    noon UTC, as had the step happened then, so the revenue metrics replay the history in order."""

    def __init__(self, session, monkeypatch, company: str):
        self.session, self.monkeypatch, self.company = session, monkeypatch, uuid.UUID(company)
        self.day = self.mark = None

    async def move(self, day: str) -> None:
        await self.close()
        freeze_day(self.monkeypatch, day)
        self.day = day

    async def close(self) -> None:
        if self.day is not None:
            from celerp.services.company_lock import lock_company
            await lock_company(self.session, self.company)  # taken before the ledger rows, as every writer does
            await self.session.execute(text("UPDATE ledger SET ts = :ts WHERE id > :id"), {
                "ts": datetime.fromisoformat(self.day).replace(hour=12, tzinfo=timezone.utc), "id": self.mark})
            await self.session.flush()
        self.mark = (await self.session.execute(text("SELECT coalesce(max(id), 0) FROM ledger"))).scalar()


async def test_e2e4_saas_subscriber_collected_from_a_card_on_file(owner_ui, session, monkeypatch):
    from ui.i18n import sentence, t
    cloud = stand_in(monkeypatch, session)
    api = owner_ui.api
    cid = await company_id(api, {})
    cal = _Calendar(session, monkeypatch, cid)
    await cal.move("2026-06-15")
    payer = await _payer(api, {})
    eid = await agreement(api, {}, start="2026-07-01", contact_id=payer, line_items=[dict(SEATS)])

    # Setup link emailed to the payer; the payer consents on Cloud's hosted page.
    r = await api.post(f"/subscriptions/{eid}/collection/setup-link", json={"delivery": "email"})
    assert r.status_code == 200 and r.json()["link"] == LINK, r.text
    [asked] = cloud.sent("setup-link")
    assert asked["payer_contact_id"] == payer
    assert await _bind(cid, payer)
    view = (await api.get(f"/subscriptions/{eid}/collection")).json()
    assert view["binding"]["state"] == "active" and view["binding"]["card_last4"] == "4242"
    r = await schedule(api, {}, eid, generation_mode="auto_invoice_collect")
    assert r.status_code == 200, r.text

    # July is billed and charged off session; the payment posts its receipt.
    await cal.move("2026-07-01")
    await tick(session, cid, "2026-07-01")
    july = _period(await _invoices(session, eid), 0)
    [july_charge] = cloud.sent("attempt")
    assert (july_charge["entity_id"], july_charge["amount_minor"]) == (july["entity_id"], 3000)
    assert await _deliver_payment(july_charge, "pi_jul", "2026-07-01T13:00:00+00:00")
    july = _period(await _invoices(session, eid), 0)
    assert july["status"] == "paid" and [(p["method"], p["reference"]) for p in july["payments"]] == [("stripe", "pi_jul")]

    # August's charge fails; the invoice page names the retry date. Before it nothing is asked;
    # on it the app asks again for what the invoice owes then, and the retry succeeds.
    await cal.move("2026-08-01")
    _at(monkeypatch, datetime(2026, 8, 1, 9, tzinfo=timezone.utc))
    await tick(session, cid, "2026-08-01")
    august = _period(await _invoices(session, eid), 1)
    eligible = datetime(2026, 8, 4, 12, tzinfo=timezone.utc)
    assert await _attempt_outcome(cid, august["entity_id"], 1, "failed", failure_code="card_declined",
                                  retry_eligible_at=eligible.isoformat())
    page = (await owner_ui.get(f"/docs/{august['entity_id']}")).text
    assert sentence("collection.status_failed_retry", date="2026-08-04") in page
    await cal.move("2026-08-04")
    _at(monkeypatch, datetime(2026, 8, 4, 11, tzinfo=timezone.utc))
    await tick(session, cid, "2026-08-04")
    assert len(cloud.sent("attempt")) == 2
    _at(monkeypatch, eligible)
    await tick(session, cid, "2026-08-04")
    retry = cloud.sent("attempt")[-1]
    assert (retry["entity_id"], retry["attempt_number"], retry["amount_minor"]) == (august["entity_id"], 2, 3000)
    assert await _deliver_payment(retry, "pi_aug", "2026-08-04T12:30:00+00:00")
    assert _period(await _invoices(session, eid), 1)["status"] == "paid"

    # September's charge needs the payer's action: shown with the bank's link, not as a failure.
    # Checkout opened meanwhile is refused (Cloud holds it while the attempt is open); a payment by
    # hand settles the invoice, Cloud can no longer cancel, and the late charge parks unmatched.
    await cal.move("2026-09-01")
    _at(monkeypatch, datetime(2026, 9, 1, 9, tzinfo=timezone.utc))
    await tick(session, cid, "2026-09-01")
    september = _period(await _invoices(session, eid), 2)
    sept_charge = cloud.sent("attempt")[-1]
    assert sept_charge["entity_id"] == september["entity_id"]
    assert await _attempt_outcome(cid, september["entity_id"], 1, "requires_action", definitive=False,
                                  action_url="https://bank.test/3ds")
    page = (await owner_ui.get(f"/docs/{september['entity_id']}")).text
    assert t("collection.status_requires_action") in page and 'href="https://bank.test/3ds"' in page
    token = (await api.post(f"/docs/{september['entity_id']}/share")).json()["token"]
    checkouts = []

    async def answer(path, payload):
        checkouts.append(path)
        return 409, {"detail": "attempt_in_progress"}
    monkeypatch.setattr("celerp.services.payments._cloud_answer", answer)
    r = await api.get(f"/pay/{token}", follow_redirects=False)
    assert r.status_code == 502 and "location" not in r.headers, r.text
    assert checkouts == ["/billing/connect/checkout"]
    r = await api.post(f"/docs/{september['entity_id']}/payment", json={
        "amount": 30.0, "method": "bank_transfer", "payment_date": "2026-09-02", "bank_account": "1111"})
    assert r.status_code in (200, 201), r.text
    cloud.answers["cancel"] = (409, {"detail": "attempt_submitted"})
    await tick(session, cid, "2026-09-02")
    await tick(session, cid, "2026-09-03")
    assert [p["entity_id"] for p in cloud.sent("cancel")] == [september["entity_id"]]
    assert await _deliver_payment(sept_charge, "pi_sep_late", "2026-09-03T08:00:00+00:00")
    from celerp.models.payment_closure import UnmatchedPayment
    assert await session.get(UnmatchedPayment, "pi_sep_late") is not None
    september = _period(await _invoices(session, eid), 2)
    assert september["status"] == "paid" and [p["reference"] for p in september["payments"] if p.get("reference")] == []

    # Five seats from 15 September: the 16 days left of 30 are credited at 3 seats and billed at 5.
    await cal.move("2026-09-15")
    line = (await api.get(f"/docs/{eid}")).json()["line_items"][0]["line_id"]
    r = await api.post(f"/subscriptions/{eid}/amend", json={
        "effective": "2026-09-15", "quantities": {line: 5}, "request_key": "seats-5"})
    assert r.status_code == 200, r.text
    docs = {d["entity_id"]: d for d in await billed_docs(session, eid)}
    credit, debit = docs[r.json()["credit_note_id"]], docs[r.json()["invoice_id"]]
    assert (credit["total"], debit["total"]) == (16.0, 26.67)
    assert credit["original_doc_id"] == september["entity_id"]
    # September is paid and its late card charge is kept among the unmatched payments, so it can take
    # nothing more: the 16.00 credit is applied to the new invoice at once, which leaves 10.67 to charge.
    # The operator voids the payment by hand and records the parked charge on September instead.
    assert debit["amount_outstanding"] == 10.67
    [by_hand] = [p for p in september["payments"] if p.get("method") == "bank_transfer"]
    r = await api.post(f"/docs/{september['entity_id']}/void-payment", json={"payment_index": by_hand["index"]})
    assert r.status_code == 200, r.text
    r = await api.post("/payments/unmatched/record", json={"reference": "pi_sep_late",
                                                           "entity_id": september["entity_id"]})
    assert r.status_code == 200, r.text
    assert _period(await _invoices(session, eid), 2)["collection"]["state"] == "paid"
    await tick(session, cid, "2026-09-15")
    assert [p["amount_minor"] for p in cloud.sent("attempt") if p["entity_id"] == debit["entity_id"]] == [1067]

    # October bills 5 seats and is collected; the July payment is disputed, its money moves to
    # Disputed payments, and comes back when the dispute is won.
    await cal.move("2026-10-01")
    _at(monkeypatch, datetime(2026, 10, 1, 9, tzinfo=timezone.utc))
    await tick(session, cid, "2026-10-01")
    october = _period(await _invoices(session, eid), 3)
    oct_charge = next(p for p in cloud.sent("attempt") if p["entity_id"] == october["entity_id"])
    assert oct_charge["amount_minor"] == 5000
    assert await _deliver_payment(oct_charge, "pi_oct", "2026-10-01T13:00:00+00:00")
    assert await _deliver_dispute(july_charge, "pi_jul", "created", "2026-10-03T09:00:00+00:00")
    assert await _deliver_dispute(july_charge, "pi_jul", "funds_withdrawn", "2026-10-03T09:00:00+00:00")
    assert await _disputed_balance(session, july["entity_id"]) == Decimal("30")
    assert await _deliver_dispute(july_charge, "pi_jul", "funds_reinstated", "2026-10-20T09:00:00+00:00")
    assert await _deliver_dispute(july_charge, "pi_jul", "closed", None, outcome="won")
    assert await _disputed_balance(session, july["entity_id"]) == Decimal("0")
    july = _period(await _invoices(session, eid), 0)
    [paid] = [p for p in july["payments"] if p.get("reference") == "pi_jul"]
    assert (paid["disputes"]["dp_1"]["state"], paid["disputes"]["dp_1"]["outcome"]) == ("closed", "won")
    assert july["status"] == "paid"

    # The payer removes the card through the receipt link: November is billed and not charged.
    assert await _bind(cid, payer, "revoked", card_brand=None, card_last4=None)
    await cal.move("2026-11-01")
    _at(monkeypatch, datetime(2026, 11, 1, 9, tzinfo=timezone.utc))
    asked = len(cloud.sent("attempt"))
    await tick(session, cid, "2026-11-01")
    november = _period(await _invoices(session, eid), 4)
    assert len(cloud.sent("attempt")) == asked
    assert november["collection"]["state"] == "stopped" and november["collection"]["reason"] == "no_card_on_file"

    # The merchant removes Stripe access: the binding reads access_removed, Cloud withdraws the
    # capability, the company is told, and no agreement can be set to collect again.
    assert await _bind(cid, payer, "access_removed", card_brand=None, card_last4=None)
    _flag(None)
    assert (await api.get(f"/subscriptions/{eid}/collection")).json()["binding"]["state"] == "access_removed"
    notes = (await api.get("/notifications")).json()
    assert "Automatic collection stopped: Stripe access was removed" in str(notes)
    r = await schedule(api, {}, eid, generation_mode="auto_invoice")
    assert r.status_code == 200, r.text
    r = await schedule(api, {}, eid, generation_mode="auto_invoice_collect")
    assert r.status_code == 409 and _says(r, "subscriptions.err_collection_off"), r.text

    # Revenue: 30.00 a month until the seat change, 50.00 after it.
    await cal.close()
    r = await api.get("/subscriptions/metrics", params={"from": "2026-07-01", "to": "2026-11-30"})
    assert r.status_code == 200, r.text
    months = {m["month"]: m for m in r.json()["months"]}
    assert [months[m]["mrr"] for m in ("2026-07", "2026-08", "2026-09", "2026-10", "2026-11")] == [
        30.0, 30.0, 50.0, 50.0, 50.0]
    assert (months["2026-07"]["new_mrr"], months["2026-09"]["expansion_mrr"]) == (30.0, 20.0)
    assert months["2026-11"]["arr"] == 600.0 and months["2026-11"]["active"] == 1
