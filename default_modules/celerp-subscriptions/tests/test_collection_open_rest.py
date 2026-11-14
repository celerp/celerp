# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""An invoice a recorded card charge leaves owing is asked for the rest, and no open invoice that
may be collected is ever left with nothing asked and nobody told.

Initial conditions per test (as test_collection_settlement): a newly registered company, a payer
with a card on file, an agreement on Bill and collect automatically for 30.00 a month from 1 July
2026, and Celerp Cloud as a stand-in.

The state-machine test drives one agreement through the app's real entry points (dispatcher
passes, Cloud's attempt and payment deliveries in any order, payments by hand and their voids,
other online payments on an invoice (paid on its page, or placed by a person from the unmatched
payments) at any point of an attempt, an amendment) under a seeded random story, with a stand-in
Cloud that charges, fails or cancels each attempt it was asked for. After every step, each open invoice whose collection may go on has an
attempt in progress, or its charge is on the way from Cloud, or the company was told why nothing
is asked: its charge was kept among the unmatched payments, or it came to owe again after its
collection ended. An invoice that comes to owe again outside a pass (a payment voided) is told by
the next pass, so between passes its ended collection still counts; right after a pass it never
does. And whenever an attempt is asked for, it and the invoice's other attempts Cloud may still
charge, or charged and whose payment is on the way, never ask more than the invoice owes: the payer
is never charged twice for the same money; and across the agreement, the money it took in (payments
recorded on its invoices and every charge Cloud made, recorded or kept among the unmatched) with
the charges Cloud may still make never comes to more than its invoices less its credit notes. A
payer's own payment while a charge is with Cloud may overlap it, so this is checked whenever an
attempt is asked for. A payment page opened before a payment by hand was voided may pay the
attempt's own amount; it is not the attempt's charge. The fixed seeds always run; COLLECTION_ORACLE_RANDOM
more (default 2) are drawn from COLLECTION_ORACLE_SEED, else at random. A failing case names its
seed: COLLECTION_ORACLE_SEED=<seed> with COLLECTION_ORACLE_RANDOM=1 replays it.
"""
from __future__ import annotations

import os
import random
import secrets
import uuid

import pytest
from sqlalchemy import select

from agreement_kit import tick
from test_collection import _attempt_outcome, _pay, cloud  # noqa: F401  (cloud: fixture)
from test_collection_settlement import _amend, _asked, _docs, _july

pytestmark = pytest.mark.asyncio

FIXED_SEEDS = [1, 2, 3, 5, 8, 13, 21, 34, 1004, 1006, 1016]
RANDOM_COUNT = int(os.environ.get("COLLECTION_ORACLE_RANDOM", "2"))
STEPS = 28


async def _deliver(cid, doc_id, ref, minor, context, request_key=None, day="2026-07-05") -> bool:
    """Cloud's delivery of an online payment: a collection charge carries its attempt's
    ``request_key``, a payment paid on the invoice's page none."""
    from celerp.services import payments
    return await payments.receive_payment({
        "company_id": cid, "entity_id": doc_id, "reference": ref, "amount_minor": minor, "currency": "USD",
        "paid_at": f"{day}T13:00:00+00:00", "managed": True, "context": context, "request_key": request_key})


async def _short_charge(client, session, monkeypatch, cloud, *, payment_first: bool):
    """July 30.00. A 10.00 bank transfer is recorded by hand, so the 30.00 charge is replaced by one
    for 20.00; the transfer is then voided (it bounced), so July owes 30.00 while the 20.00 is with
    Cloud. The 20.00 succeeds and is recorded on July, its payment delivered before or after
    Cloud's word that it succeeded: (headers, company id, agreement id, July)."""
    hd, cid, eid, july = await _july(client, session, monkeypatch, cloud)
    await _pay(client, hd, july, 10.0)
    for d in ("2026-07-02", "2026-07-03"):
        await tick(session, cid, d)
    assert _asked(cloud, july) == [3000, 2000]
    sent = cloud.sent("attempt")[-1]
    [hand] = [p for p in (await _docs(session, eid))[july]["payments"] if p.get("method") == "bank_transfer"]
    r = await client.post(f"/docs/{july}/void-payment", headers=hd, json={"payment_index": hand["index"]})
    assert r.status_code in (200, 201), r.text
    await tick(session, cid, "2026-07-04")
    steps = [lambda: _deliver(cid, july, "pi_20", 2000, sent["context"], sent["request_key"]),
             lambda: _attempt_outcome(cid, july, sent["attempt_number"], "succeeded")]
    for step in (steps if payment_first else steps[::-1]):
        assert await step() is True
    await tick(session, cid, "2026-07-05")
    return hd, cid, eid, july


@pytest.mark.parametrize("payment_first", [False, True], ids=["outcome-first", "payment-first"])
async def test_a_recorded_charge_that_leaves_the_invoice_owing_asks_for_the_rest(
        client, session, monkeypatch, cloud, payment_first):
    hd, cid, eid, july = await _short_charge(client, session, monkeypatch, cloud, payment_first=payment_first)
    doc = (await _docs(session, eid))[july]
    assert doc["amount_outstanding"] == 10.0
    assert _asked(cloud, july) == [3000, 2000, 1000]
    assert doc["collection"]["state"] == "submitted" and doc["collection"]["attempt_number"] == 3


async def test_the_rest_paid_by_hand_ends_the_collection(client, session, monkeypatch, cloud):
    """The person then records July's last 10.00 by hand: July is paid, so the charge asked for it is
    cancelled and nothing more is asked."""
    hd, cid, eid, july = await _short_charge(client, session, monkeypatch, cloud, payment_first=False)
    await _pay(client, hd, july, 10.0)
    await tick(session, cid, "2026-07-06")
    doc = (await _docs(session, eid))[july]
    assert doc["amount_outstanding"] == 0
    assert _asked(cloud, july) == [3000, 2000, 1000]
    assert doc["collection"]["state"] in ("canceled", "stopped")


# ── the state machine under seeded random stories ────────────────────────────

class _Story:
    """Cloud's side of each attempt, (invoice, attempt number) -> open, charged, failed or
    canceled, and the deliveries it still owes the app."""

    def __init__(self, rng: random.Random, cloud):
        self.rng, self.cloud = rng, cloud
        self.attempts: dict[tuple[str, int], str] = {}
        self.amounts: dict[tuple[str, int], tuple[int, dict, str]] = {}
        self.payments: list[tuple[str, str, int, dict, str]] = []
        self.outcomes: list[tuple[str, int, str]] = []
        cloud.answers["attempt"] = self._attempt
        cloud.answers["cancel"] = self._cancel

    def _attempt(self, p):
        key = (p["entity_id"], p["attempt_number"])
        self.attempts.setdefault(key, "open")
        self.amounts[key] = (p["amount_minor"], p["context"], p["request_key"])
        state = self.rng.choice(["submitted", "processing", "requires_action", "unknown"])
        return 200, {"state": state, "attempt_number": p["attempt_number"]}

    def _cancel(self, p):
        doc = p["entity_id"]
        key = max((k for k in self.attempts if k[0] == doc), key=lambda k: k[1], default=None)
        if key is None or self.attempts[key] == "canceled":
            return 200, {"state": "canceled"}
        if self.attempts[key] == "open" and self.rng.random() < 0.5:
            self.attempts[key] = "canceled"
            return 200, {"state": "canceled"}
        return 409, {"detail": "attempt_submitted"}

    def open(self) -> list[tuple[str, int]]:
        return sorted(k for k, v in self.attempts.items() if v == "open")

    def charge(self, key) -> None:
        self.attempts[key] = "charged"
        minor, context, request_key = self.amounts[key]
        self.payments.append((key[0], self.reference(key), minor, context, request_key))
        self.outcomes.append((key[0], key[1], "succeeded"))

    @staticmethod
    def reference(key) -> str:
        return f"pi_{key[0][-6:]}_{key[1]}"

    def fail(self, key) -> None:
        self.attempts[key] = "failed"
        self.outcomes.append((key[0], key[1], "failed"))


async def _told(session, name: str) -> bool:
    """The company was told once of a notice naming ``name`` (a payment's reference or an invoice's number)."""
    from celerp.models.notification import Notification
    rows = (await session.execute(select(Notification.title).where(Notification.title.contains(name)))).all()
    return len(rows) == 1


# The oracle's own reading of an attempt in progress: with Cloud, or failed and retried when due.
IN_PROGRESS = frozenset({"pending", "submitted", "processing", "requires_action", "unknown", "failed"})


def _minor(doc: dict) -> int:
    return round((doc.get("amount_outstanding") or 0) * 100)


def _ended(c: dict) -> bool:
    return c.get("state") == "paid" or (c.get("state") in ("stopped", "canceled")
                                        and c.get("reason") == "invoice_settled")


async def _left_alone(session, cid, eid, story: _Story | None, *, passed: bool) -> list[str]:
    """Each open invoice of the agreement whose collection may go on and that has no attempt in
    progress, no charge on the way (``story``: still owed by Cloud) and nothing the company was told
    of: a kept charge, or owing again after its collection ended. Unless a pass just ran
    (``passed``), an ended collection waits for the next pass to tell it."""
    session.expire_all()
    alone = []
    for doc_id, doc in (await _docs(session, eid)).items():
        c = doc.get("collection") or {}
        if doc.get("doc_type") != "invoice" or not c or _minor(doc) <= 0 or doc.get("status") in ("draft", "void"):
            continue
        if c.get("state") in IN_PROGRESS or (c.get("state") == "canceled" and c.get("replace") is True):
            continue
        if c.get("state") == "succeeded" and c.get("kept") and await _told(session, c["kept"]):
            continue
        if c.get("state") == "stopped" and c.get("reason") == "owes_again" and await _told(session, doc["ref_id"]):
            continue
        if c.get("state") == "succeeded" and story and any(p[0] == doc_id for p in story.payments):
            continue
        if _ended(c) and not passed:
            continue
        alone.append(f"{doc.get('ref_id')} owes {_minor(doc)} collection {c}")
    return alone


def _overasked(story: _Story, docs: dict[str, dict], asked: set[tuple[str, int]]) -> list[str]:
    """Each attempt asked for since ``asked`` whose amount, with the invoice's other attempts Cloud
    may still charge or charged with the payment on the way, is more than the invoice owes."""
    over = []
    for p in story.cloud.sent("attempt"):
        key = (p["entity_id"], p["attempt_number"])
        if key in asked:
            continue
        asked.add(key)
        others = sum(story.amounts[k][0] for k, v in story.attempts.items() if k[0] == key[0] and k != key and (
            v == "open" or (v == "charged" and any(pay[1] == story.reference(k) for pay in story.payments))))
        if p["amount_minor"] + others > _minor(docs[key[0]]):
            over.append(f"{docs[key[0]].get('ref_id')} asked {p['amount_minor']} with {others} on the way, "
                        f"owes {_minor(docs[key[0]])}")
    return over


def _cents(amount) -> int:
    return round((amount or 0) * 100)


def _overcommitted(story: _Story, docs: dict[str, dict]) -> list[str]:
    """Across the agreement, the money taken in (payments recorded on its invoices, credit notes
    applied aside, and every charge Cloud made, recorded or kept among the unmatched) plus the charges Cloud may still make, when it
    is more than the agreement's invoices less its credit notes."""
    live = [d for d in docs.values() if d.get("status") not in ("draft", "void")]
    owed = sum(_cents(d.get("total")) * (-1 if d.get("doc_type") == "credit_note" else 1)
               for d in live if d.get("doc_type") in ("invoice", "credit_note"))
    charges = {story.reference(k) for k in story.amounts}
    paid = sum(_cents(p.get("amount")) - _cents(p.get("refunded")) for d in live if d.get("doc_type") == "invoice"
               for p in d.get("payments") or [] if p.get("status") == "active" and p.get("method") != "credit_note" and p.get("reference") not in charges)
    charged = sum(story.amounts[k][0] for k, v in story.attempts.items() if v in ("open", "charged"))
    if paid + charged > owed:
        return [f"paid {paid} with {charged} charged or with Cloud, the agreement owes {owed}: "
                + "; ".join(f"{d.get('ref_id')} owes {_minor(d)} collection {d.get('collection')}"
                            for d in live if d.get("doc_type") == "invoice")]
    return []


async def _run_story(seed, client, session, monkeypatch, cloud):
    rng = random.Random(seed)
    hd, cid, eid, july = await _july(client, session, monkeypatch, cloud)
    story = _Story(rng, cloud)
    story.attempts[(july, 1)] = "open"
    [first] = cloud.sent("attempt")
    story.amounts[(july, 1)] = (first["amount_minor"], first["context"], first["request_key"])
    day, amended, trail, asked, foreign, stale = 1, False, [], {(july, 1)}, 0, False

    async def step(name):
        nonlocal day, amended, foreign, stale
        docs = await _docs(session, eid)
        invoices = [d for d in docs.values() if d.get("doc_type") == "invoice"]
        if name == "tick":
            day += 1
            await tick(session, cid, f"2026-07-{day:02d}")
        elif name == "charge":
            story.charge(rng.choice(story.open()))
        elif name == "fail":
            story.fail(rng.choice(story.open()))
        elif name == "deliver_payment":
            doc, ref, minor, context, request_key = story.payments.pop(rng.randrange(len(story.payments)))
            assert await _deliver(cid, doc, ref, minor, context, request_key) is True
        elif name == "deliver_outcome":
            doc, n, state = story.outcomes.pop(rng.randrange(len(story.outcomes)))
            fields = {"retry_eligible_at": "2026-07-01T00:00:00+00:00"} if state == "failed" else {}
            assert await _attempt_outcome(cid, doc, n, state, **fields) is True
        elif name == "hand_pay":
            doc = rng.choice([d for d in invoices if (d.get("amount_outstanding") or 0) >= 1])
            await _pay(client, hd, doc["entity_id"], min(float(doc["amount_outstanding"]), rng.choice([5.0, 10.0])))
        elif name in ("page_pay", "place"):
            doc = rng.choice([d for d in invoices if _minor(d) >= 100])
            attempt = (doc.get("collection") or {}).get("amount_minor") or 500
            minor, foreign = min(_minor(doc), attempt if stale else rng.choice([500, 1000, attempt])), foreign + 1
            stale = False
            ref, on = f"pi_other_{foreign}", doc["entity_id"] if name == "page_pay" else f"doc:{uuid.uuid4().hex}"
            assert await _deliver(cid, on, ref, minor, first["context"]) is True
            if name == "place":
                from celerp.services.payments import record_unmatched_payment
                await record_unmatched_payment(session, uuid.UUID(cid), ref, doc["entity_id"])
                await session.commit()
        elif name == "void_hand":
            doc, p = rng.choice([(d, p) for d in invoices for p in d.get("payments") or []
                                 if p.get("method") == "bank_transfer" and p.get("status") == "active"])
            r = await client.post(f"/docs/{doc['entity_id']}/void-payment", headers=hd,
                                  json={"payment_index": p["index"]})
            assert r.status_code in (200, 201), r.text
            stale = True  # a payment page opened before the void may now pay the attempt's amount
        elif name == "amend":
            r = await _amend(client, monkeypatch, hd, eid)
            assert r.status_code == 200, r.text
            amended, day = True, 15

    def enabled(docs) -> list[str]:
        invoices = [d for d in docs.values() if d.get("doc_type") == "invoice"]
        names = ["tick"] if day < 28 else []
        names += ["charge", "charge", "fail"] if story.open() else []
        names += ["deliver_payment", "deliver_payment"] if story.payments else []
        names += ["deliver_outcome", "deliver_outcome"] if story.outcomes else []
        names += ["hand_pay", "page_pay", "place"] + ["page_pay"] * 2 * stale \
            if any(_minor(d) >= 100 for d in invoices) else []
        names += ["void_hand"] if any(p.get("method") == "bank_transfer" and p.get("status") == "active"
                                      for d in invoices for p in d.get("payments") or []) else []
        names += ["amend"] if not amended and day < 15 else []
        return names

    for _ in range(STEPS):
        names = enabled(await _docs(session, eid))
        if not names:
            break
        name = rng.choice(names)
        trail.append(name)
        await step(name)
        alone = await _left_alone(session, cid, eid, story, passed=name == "tick")
        assert not alone, f"seed {seed} after {trail}: {alone}"
        docs, before = await _docs(session, eid), len(asked)
        over = _overasked(story, docs, asked)
        if len(asked) > before:
            over += _overcommitted(story, docs)
        assert not over, f"seed {seed} after {trail}: {over}"
    while story.outcomes or story.payments:
        name = "deliver_payment" if story.payments else "deliver_outcome"
        trail.append(name)
        await step(name)
    trail.append("tick")
    await step("tick")
    alone = await _left_alone(session, cid, eid, None, passed=True)
    assert not alone, f"seed {seed} after {trail}: {alone}"


def _random_seed(i: int) -> int:
    base = os.environ.get("COLLECTION_ORACLE_SEED")
    return int(base) + i if base else secrets.randbelow(2**31)


@pytest.mark.parametrize("seed", FIXED_SEEDS)
async def test_no_open_invoice_is_left_with_nothing_asked_and_nobody_told(seed, client, session, monkeypatch, cloud):
    await _run_story(seed, client, session, monkeypatch, cloud)


@pytest.mark.parametrize("i", range(RANDOM_COUNT), ids=lambda i: f"random-{i}")
async def test_random_stories_leave_no_open_invoice_alone(i, client, session, monkeypatch, cloud):
    seed = _random_seed(i)
    print(f"COLLECTION_ORACLE_SEED={seed}")
    await _run_story(seed, client, session, monkeypatch, cloud)
