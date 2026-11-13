# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Stripe follow-ups (refunds, their reversals, disputes, releases) on a card charge that a late
success split across an agreement's invoices.

Initial conditions per test (as test_collection_settlement): a newly registered company, a payer
with a card on file, an agreement on Bill and collect automatically for 30.00 a month from 1 July
2026, and Celerp Cloud as a stand-in. Two split charges are used:

- the amendment split: an amendment effective 15 July credits 16.45 against July's invoice while
  its 30.00 charge cannot be cancelled; the charge succeeds and July takes 13.55, the amendment
  invoice 16.45 (spill order: July, then the amendment invoice);
- the excess split: 10.00 is paid by hand while July's 30.00 charge cannot be cancelled; the
  charge succeeds and July takes 20.00, the other 10.00 no invoice owes and is kept among the
  unmatched payments as ``<charge>:rest``.

A follow-up is applied over the charge's parts in reverse spill order: the rest first, then the
other invoices newest first, then the charge's own invoice, each part never past what it holds.
"""
from __future__ import annotations

import uuid

import pytest

from agreement_kit import tick
from test_collection import _attempt_outcome, _pay, cloud  # noqa: F401  (cloud: fixture)
from test_collection_settlement import _amend, _docs, _july, _unmatched

pytestmark = pytest.mark.asyncio

AT = "2026-07-20T10:00:00+00:00"


async def _kept_refunds(session) -> list[tuple[str, int]]:
    from celerp.services.payments import unmatched_refunds
    return sorted((r.reference, r.amount_minor) for r in await unmatched_refunds(session))


def _refund(cid, doc, ref, amount, *, rid="re_1", transition="applied", context=None):
    return {"context": context, "company_id": cid, "entity_id": doc, "reference": ref, "refund_id": rid,
            "transition": transition, "amount_minor": amount, "cycle": 1, "currency": "USD", "occurred_at": AT}


def _charge(cid, doc, ref, context, paid_at="2026-07-15T13:00:00+00:00"):
    return {"company_id": cid, "entity_id": doc, "reference": ref, "amount_minor": 3000, "currency": "USD",
            "paid_at": paid_at, "managed": True, "context": context}


def _dispute(cid, doc, ref, state, amount, outcome=None, did="dp_1"):
    return {"company_id": cid, "entity_id": doc, "reference": ref, "dispute_id": did, "state": state,
            "outcome": outcome, "amount_minor": amount, "currency": "usd", "occurred_at": AT}


def _stripe(doc: dict, ref: str) -> dict:
    [payment] = [p for p in doc.get("payments", []) if p.get("reference") == ref and p.get("status") != "deleted"]
    return payment


async def _amendment_split(client, session, monkeypatch, cloud, *, deliver=True):
    """(company id, agreement id, July, the amendment invoice, payment page books)."""
    from celerp.services import payments
    hd, cid, eid, july = await _july(client, session, monkeypatch, cloud)
    [sent] = cloud.sent("attempt")
    cloud.answers["cancel"] = (409, {"detail": "attempt_submitted"})
    new = (await _amend(client, monkeypatch, hd, eid)).json()["invoice_id"]
    await tick(session, cid, "2026-07-15")
    assert await _attempt_outcome(cid, july, 1, "succeeded") is True
    if deliver:
        assert await payments.receive_payment(_charge(cid, july, "pi_late", sent["context"]))
    return cid, eid, july, new, sent["context"]


async def _excess_split(client, session, monkeypatch, cloud):
    """(headers, company id, agreement id, July, payment page books)."""
    from celerp.services import payments
    hd, cid, eid, july = await _july(client, session, monkeypatch, cloud)
    [sent] = cloud.sent("attempt")
    cloud.answers["cancel"] = (409, {"detail": "attempt_submitted"})
    await _pay(client, hd, july, 10.0)
    await tick(session, cid, "2026-07-02")
    assert await payments.receive_payment(_charge(cid, july, "pi_over", sent["context"], "2026-07-02T13:00:00+00:00"))
    assert await _unmatched(session) == [("pi_over:rest", 1000)]
    return hd, cid, eid, july, sent["context"]


async def _owed(session, eid, *docs) -> tuple:
    state = await _docs(session, eid)
    return tuple(state[d]["amount_outstanding"] for d in docs)


async def test_a_full_refund_of_a_split_charge_gives_back_every_part(client, session, monkeypatch, cloud):
    """The amendment split, the whole 30.00 refunded in Stripe: the amendment invoice gives back its
    16.45 and owes 27.42 again, July gives back its 13.55 and owes it; nothing is kept."""
    from celerp.services import payments
    cid, eid, july, new, books = await _amendment_split(client, session, monkeypatch, cloud)
    assert await payments.receive_refund(_refund(cid, july, "pi_late", 3000, context=books))
    assert await _owed(session, eid, july, new) == (13.55, 27.42)
    assert await _kept_refunds(session) == []


async def test_a_partial_refund_of_a_split_charge_comes_off_the_newest_part_first(client, session, monkeypatch, cloud):
    """The amendment split, 20.00 refunded: the amendment invoice's 16.45 first, then 3.55 of July's part."""
    from celerp.services import payments
    cid, eid, july, new, books = await _amendment_split(client, session, monkeypatch, cloud)
    assert await payments.receive_refund(_refund(cid, july, "pi_late", 2000, context=books))
    assert await _owed(session, eid, july, new) == (3.55, 27.42)
    assert await _kept_refunds(session) == []


async def test_a_refund_of_the_excess_comes_off_the_rest(client, session, monkeypatch, cloud):
    """The excess split, 10.00 refunded: it is the rest's, so July stays paid (10 by hand + 20 of the
    charge) and the refund is kept with the rest among the unmatched payments, where recording the
    rest on an invoice gives it back with it."""
    from celerp.services import payments
    hd, cid, eid, july, books = await _excess_split(client, session, monkeypatch, cloud)
    for _ in range(2):
        assert await payments.receive_refund(_refund(cid, july, "pi_over", 1000, context=books))
    assert await _owed(session, eid, july) == (0,)
    assert (await _unmatched(session), await _kept_refunds(session)) == (
        [("pi_over:rest", 1000)], [("pi_over:rest", 1000)])


async def test_a_refund_past_the_rest_comes_off_the_charge_s_own_invoice(client, session, monkeypatch, cloud):
    """The excess split, 25.00 refunded: 10.00 off the rest (kept with it), 15.00 off July's 20.00 part."""
    from celerp.services import payments
    hd, cid, eid, july, books = await _excess_split(client, session, monkeypatch, cloud)
    assert await payments.receive_refund(_refund(cid, july, "pi_over", 2500, context=books))
    assert await _owed(session, eid, july) == (15.0,)
    assert await _kept_refunds(session) == [("pi_over:rest", 1000)]


async def test_a_refund_of_a_split_charge_delivered_again_changes_nothing(client, session, monkeypatch, cloud):
    from celerp.services import payments
    cid, eid, july, new, books = await _amendment_split(client, session, monkeypatch, cloud)
    for _ in range(2):
        assert await payments.receive_refund(_refund(cid, july, "pi_late", 2000, context=books))
    assert await _owed(session, eid, july, new) == (3.55, 27.42)
    assert await payments.receive_payment(_charge(cid, july, "pi_late", books))
    assert await _owed(session, eid, july, new) == (3.55, 27.42)
    assert await _kept_refunds(session) == []


async def test_a_reversed_refund_of_a_split_charge_puts_every_part_back(client, session, monkeypatch, cloud):
    """The excess split, 25.00 refunded then reversed by Stripe: July is paid again and the rest's
    kept refund is undone by its kept reversal (recorded together, they cancel)."""
    from celerp.services import payments
    hd, cid, eid, july, books = await _excess_split(client, session, monkeypatch, cloud)
    assert await payments.receive_refund(_refund(cid, july, "pi_over", 2500, context=books))
    for _ in range(2):
        assert await payments.receive_refund(_refund(cid, july, "pi_over", 2500, transition="reversed", context=books))
    assert await _owed(session, eid, july) == (0,)
    assert await _kept_refunds(session) == [("pi_over:rest", 1000), ("pi_over:rest", 1000)]


async def test_a_refund_larger_than_the_whole_split_charge_is_kept_unapplied(client, session, monkeypatch, cloud):
    from celerp.services import payments
    cid, eid, july, new, books = await _amendment_split(client, session, monkeypatch, cloud)
    assert await payments.receive_refund(_refund(cid, july, "pi_late", 3001, context=books))
    assert await _owed(session, eid, july, new) == (0, 10.97)
    assert await _kept_refunds(session) == [("pi_late", 3001)]


async def test_a_refund_delivered_before_the_late_success_applies_over_the_split(client, session, monkeypatch, cloud):
    """The refund arrives first and is kept; the late success then splits the charge and the kept
    refund applies over its parts in the same transaction."""
    from celerp.services import payments
    cid, eid, july, new, books = await _amendment_split(client, session, monkeypatch, cloud, deliver=False)
    assert await payments.receive_refund(_refund(cid, july, "pi_late", 3000, context=books))
    assert await _kept_refunds(session) == [("pi_late", 3000)]
    assert await payments.receive_payment(_charge(cid, july, "pi_late", books))
    assert await _owed(session, eid, july, new) == (13.55, 27.42)
    assert await _kept_refunds(session) == []


async def test_a_lost_dispute_of_a_split_charge_is_recorded_on_every_part(client, session, monkeypatch, cloud):
    """The amendment split, the 30.00 disputed and lost: the amendment invoice's payment carries
    16.45 of it and July's 13.55, each through every step, delivered twice."""
    from celerp.services import payments
    cid, eid, july, new, books = await _amendment_split(client, session, monkeypatch, cloud)
    for state, outcome in (("created", None), ("funds_withdrawn", None), ("closed", "lost")):
        for _ in range(2):
            assert await payments.receive_dispute(_dispute(cid, july, "pi_late", state, 3000, outcome))
    docs = await _docs(session, eid)
    parts = {d: list((_stripe(docs[d], "pi_late").get("disputes") or {}).values()) for d in (july, new)}
    assert {d: [(x["outcome"], x["amount_minor"]) for x in v] for d, v in parts.items()} == {
        july: [("lost", 1355)], new: [("lost", 1645)]}


async def test_a_dispute_larger_than_the_whole_split_charge_is_not_recorded(client, session, monkeypatch, cloud):
    from celerp.services import payments
    cid, eid, july, new, books = await _amendment_split(client, session, monkeypatch, cloud)
    assert await payments.receive_dispute(_dispute(cid, july, "pi_late", "created", 3001))
    docs = await _docs(session, eid)
    assert [_stripe(docs[d], "pi_late").get("disputes") for d in (july, new)] == [None, None]


async def test_a_dispute_of_a_split_charge_in_another_currency_is_not_recorded(client, session, monkeypatch, cloud):
    from celerp.services import payments
    cid, eid, july, new, books = await _amendment_split(client, session, monkeypatch, cloud)
    assert await payments.receive_dispute({**_dispute(cid, july, "pi_late", "created", 1000), "currency": "eur"})
    docs = await _docs(session, eid)
    assert [_stripe(docs[d], "pi_late").get("disputes") for d in (july, new)] == [None, None]


async def test_a_dispute_or_release_delivered_before_its_charge_waits_for_it(client, session, monkeypatch, cloud):
    """July's charge succeeded but its payment is not delivered yet: neither a dispute nor a
    release is taken, so each is delivered again once the payment is on July."""
    from celerp.services import payments
    cid, eid, july, new, books = await _amendment_split(client, session, monkeypatch, cloud, deliver=False)
    assert await payments.receive_dispute(_dispute(cid, july, "pi_late", "created", 1000)) is False
    assert await payments.receive_release({"company_id": cid, "entity_id": july, "reference": "pi_late",
                                           "released_at": AT}) is False


async def test_a_release_of_a_split_charge_releases_every_part(client, session, monkeypatch, cloud):
    """The excess split, Stripe disconnected: July's part and the rest both stop being linked to
    Stripe, so a refund Stripe makes afterwards applies to neither and is kept."""
    from celerp.services import payments
    from celerp.models.payment_closure import UnmatchedPayment
    hd, cid, eid, july, books = await _excess_split(client, session, monkeypatch, cloud)
    release = {"company_id": cid, "entity_id": july, "reference": "pi_over", "released_at": "2026-07-10T00:00:00+00:00"}
    for _ in range(2):
        assert await payments.receive_release(release)
    docs = await _docs(session, eid)
    assert _stripe(docs[july], "pi_over").get("stripe_released_at")
    rest = await session.get(UnmatchedPayment, "pi_over:rest", populate_existing=True)
    assert rest.released_at is not None
    assert await payments.receive_refund(_refund(cid, july, "pi_over", 2500, context=books))
    assert await _owed(session, eid, july) == (0,)
    assert await _kept_refunds(session) == [("pi_over", 2500)]


async def test_a_refund_follows_the_rest_a_person_recorded_on_another_invoice(client, session, monkeypatch, cloud):
    """The excess split; a person records the 10.00 rest on the agreement's next invoice (August).
    The whole 30.00 is then refunded: the rest gives back its 10.00 on August, July its 20.00."""
    from celerp.services import payments
    hd, cid, eid, july, books = await _excess_split(client, session, monkeypatch, cloud)
    await tick(session, cid, "2026-08-01")
    august = next(d for d, s in (await _docs(session, eid)).items() if d != july and s.get("doc_type") == "invoice")
    await payments.record_unmatched_payment(session, uuid.UUID(cid), "pi_over:rest", august)
    await session.commit()
    before = (await _owed(session, eid, august))[0]
    assert await payments.receive_refund(_refund(cid, july, "pi_over", 3000, context=books))
    assert await _owed(session, eid, july, august) == (20.0, before + 10.0)
    assert await _kept_refunds(session) == []


async def test_control_follow_ups_of_a_charge_that_was_not_split(client, session, monkeypatch, cloud):
    """July's 30.00 succeeds untouched: a 5.00 refund applies on July, a refund past the charge is
    kept, a dispute is recorded whole on July's payment and a release releases it."""
    from celerp.services import payments
    hd, cid, eid, july = await _july(client, session, monkeypatch, cloud)
    [sent] = cloud.sent("attempt")
    assert await _attempt_outcome(cid, july, 1, "succeeded") is True
    assert await payments.receive_payment(_charge(cid, july, "pi_ok", sent["context"], "2026-07-02T13:00:00+00:00"))
    assert await payments.receive_refund(_refund(cid, july, "pi_ok", 500, context=sent["context"]))
    assert await payments.receive_refund(_refund(cid, july, "pi_ok", 2600, rid="re_2", context=sent["context"]))
    assert await _owed(session, eid, july) == (5.0,)
    assert await _kept_refunds(session) == [("pi_ok", 2600)]
    assert await payments.receive_dispute(_dispute(cid, july, "pi_ok", "created", 2500))
    payment = _stripe((await _docs(session, eid))[july], "pi_ok")
    assert [(k, v["amount_minor"]) for k, v in payment["disputes"].items()] == [("dp_1", 2500)]
    assert await payments.receive_release({"company_id": cid, "entity_id": july, "reference": "pi_ok",
                                           "released_at": "2026-07-25T00:00:00+00:00"})
    assert _stripe((await _docs(session, eid))[july], "pi_ok").get("stripe_released_at")
