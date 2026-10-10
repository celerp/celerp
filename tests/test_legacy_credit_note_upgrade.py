# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The one-time settlement of credit notes an earlier release issued, from every state an
earlier release could leave them in.

An earlier release took a credit note's amount off its invoice's balance when it was made,
posted nothing when it was issued, and every later payment recomputed the balance as total
less paid, wiping the reduction. Each test builds that state, runs the settlement and
checks, to the cent: 1120 == open invoices less open credit note balances, the customer
owes exactly total less paid less credited, no credit is spendable twice, the owner is told
what changed, and running it again changes nothing."""
from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import select

from celerp.events.engine import emit_event
from celerp.models.ledger import LedgerEntry
from celerp.models.notification import Notification
from celerp.models.projections import Projection
from test_cost_restatement import _state
from test_credit_note_settlement_owned import (
    _ar, _backfill, _cn, _events, _legacy_cn, _pay, _post, _svc_invoice,
)
from test_set_aside_older_paths import _net

pytestmark = pytest.mark.asyncio


async def _s(session, auth, doc) -> tuple:
    session.expire_all()
    s = await _state(session, auth, doc)
    return (s.get("status"), float(s.get("amount_outstanding") or 0), float(s.get("amount_paid") or 0),
            float(s.get("credited") or 0))


async def _tie(session, auth) -> float:
    gl, sub = await _ar(session, auth)
    assert gl == sub, f"1120 {gl} != open invoices less open credit note balances {sub}"
    return gl


async def _raw(session, auth, doc, event_type, data=None, **meta):
    """An event as an earlier release wrote it."""
    await emit_event(session, company_id=auth["company_id"], entity_id=doc, entity_type="doc",
                     event_type=event_type, data=data or {}, actor_id=auth["user_id"], location_id=None,
                     source="api", idempotency_key=f"main-{uuid.uuid4().hex}", metadata_=meta or None)
    await session.commit()


async def _reduce(session, auth, inv, cn, new: float):
    """An earlier release's reduction of the invoice's balance for a credit note."""
    state = await _state(session, auth, inv)
    await _raw(session, auth, inv, "doc.updated",
               {"fields_changed": {"amount_outstanding": {"old": state["amount_outstanding"], "new": new}}},
               source_credit_note=cn)


async def _notice(session, auth) -> str:
    rows = (await session.execute(select(Notification).where(
        Notification.company_id == auth["company_id"], Notification.category == "system"))).scalars().all()
    return " ".join(f"{n.title}: {n.body}" for n in rows)


async def _number(session, auth, doc) -> str:
    return str((await _state(session, auth, doc)).get("ref_id"))


async def _refund(client, auth, cn, amount):
    return await _post(client, auth, f"/docs/{cn}/cn-refund",
                       {"amount": amount, "date": "2026-10-09", "bank_account": "1111"})


async def _rerun_changes_nothing(session, auth):
    before = await _events(session, auth)
    again = await _backfill(session, auth)
    assert again == {"settled": 0, "restored": 0, "errored": 0, "staged": False}, again
    assert await _events(session, auth) == before


async def test_paid_after_the_credit_note_owes_nothing_and_takes_no_second_payment(client, session, auth):
    inv = await _svc_invoice(client, auth, 80.0)
    cn = await _legacy_cn(client, session, auth, inv, 40.0)
    assert (await _pay(client, auth, inv, 40.0)).status_code == 200
    out = await _backfill(session, auth)
    assert out["settled"] == 1, out
    assert await _s(session, auth, inv) == ("paid", 0.0, 40.0, 40.0)
    assert await _s(session, auth, cn) == ("paid", 0.0, 0.0, 40.0)
    assert await _tie(session, auth) == 0.0
    assert (await _pay(client, auth, inv, 1.0)).status_code != 200, "a second payment on a settled invoice"
    await _rerun_changes_nothing(session, auth)


async def test_paid_in_full_after_the_credit_note_keeps_the_credit_open(client, session, auth):
    inv = await _svc_invoice(client, auth, 80.0)
    cn = await _legacy_cn(client, session, auth, inv, 40.0)
    for _ in range(2):  # the second payment is the one an earlier release took for the wiped 40
        assert (await _pay(client, auth, inv, 40.0)).status_code == 200
    await _backfill(session, auth)
    assert await _s(session, auth, inv) == ("paid", 0.0, 80.0, 0.0)
    assert await _s(session, auth, cn) == ("final", 40.0, 0.0, 0.0)
    assert await _tie(session, auth) == -40.0
    assert await _net(session, auth, "4100", prefix="je:") == -40.0
    notice = await _notice(session, auth)
    assert await _number(session, auth, cn) in notice and "40.00" in notice, notice
    assert (await _refund(client, auth, cn, 40.0)).status_code == 200
    assert await _tie(session, auth) == 0.0
    await _rerun_changes_nothing(session, auth)


async def test_refunded_in_cash_gives_the_invoice_back_what_it_reduced(client, session, auth):
    inv = await _svc_invoice(client, auth, 80.0)
    cn = await _legacy_cn(client, session, auth, inv, 40.0)
    assert (await _refund(client, auth, cn, 40.0)).status_code == 200
    await _backfill(session, auth)
    assert await _s(session, auth, inv) == ("final", 80.0, 0.0, 0.0)
    assert await _s(session, auth, cn) == ("paid", 0.0, 40.0, 0.0)
    assert await _tie(session, auth) == 80.0
    assert await _net(session, auth, "4100", prefix="je:") == -40.0
    notice = await _notice(session, auth)
    assert await _number(session, auth, cn) in notice and "40.00" in notice, notice
    await _rerun_changes_nothing(session, auth)


async def test_refunded_in_part_settles_only_what_is_left(client, session, auth):
    inv = await _svc_invoice(client, auth, 80.0)
    cn = await _legacy_cn(client, session, auth, inv, 40.0)
    assert (await _refund(client, auth, cn, 10.0)).status_code == 200
    await _backfill(session, auth)
    assert await _s(session, auth, inv) == ("partial", 50.0, 0.0, 30.0)
    assert await _s(session, auth, cn) == ("paid", 0.0, 10.0, 30.0)
    assert await _tie(session, auth) == 50.0
    notice = await _notice(session, auth)
    assert await _number(session, auth, cn) in notice and "10.00" in notice, notice
    await _rerun_changes_nothing(session, auth)


async def test_applied_to_its_own_invoice_is_not_counted_twice(client, session, auth):
    inv = await _svc_invoice(client, auth, 80.0)
    cn = await _legacy_cn(client, session, auth, inv, 40.0)
    a = await _post(client, auth, f"/docs/{cn}/apply-to-invoice", {"target_doc_id": inv, "amount": 40.0})
    assert a.status_code == 200, a.text
    await _backfill(session, auth)
    assert (await _s(session, auth, inv))[1] == 40.0
    assert await _tie(session, auth) == 40.0
    await _rerun_changes_nothing(session, auth)


async def test_voided_credit_note_gives_the_reduction_back(client, session, auth):
    inv = await _svc_invoice(client, auth, 80.0)
    cn = await _legacy_cn(client, session, auth, inv, 40.0)
    await _raw(session, auth, cn, "doc.voided", {"pre_void_status": "final"})
    out = await _backfill(session, auth)
    assert out["restored"] == 1, out
    assert (await _s(session, auth, inv))[:2] == ("final", 80.0)
    assert await _tie(session, auth) == 80.0
    assert await _number(session, auth, cn) in await _notice(session, auth)
    await _rerun_changes_nothing(session, auth)
    u = await _post(client, auth, f"/docs/{cn}/unvoid")
    assert u.status_code == 200, u.text
    assert (await _s(session, auth, inv))[:2] == ("partial", 40.0)
    assert await _tie(session, auth) == 40.0


async def test_deleted_draft_credit_note_gives_the_reduction_back(client, session, auth):
    inv = await _svc_invoice(client, auth, 80.0)
    cn = await _legacy_cn(client, session, auth, inv, 40.0, issued=False)
    number = await _number(session, auth, cn)
    await session.execute(sa.delete(Projection).where(Projection.company_id == auth["company_id"],
                                                       Projection.entity_id == cn))
    await session.execute(sa.delete(LedgerEntry).where(LedgerEntry.company_id == auth["company_id"],
                                                        LedgerEntry.entity_id == cn))
    await session.commit()
    out = await _backfill(session, auth)
    assert out["restored"] == 1, out
    assert (await _s(session, auth, inv))[:2] == ("final", 80.0)
    assert await _tie(session, auth) == 80.0
    assert number in await _notice(session, auth)
    await _rerun_changes_nothing(session, auth)


async def test_draft_reduced_then_paid_owes_what_payments_leave(client, session, auth):
    inv = await _svc_invoice(client, auth, 80.0)
    await _legacy_cn(client, session, auth, inv, 40.0, issued=False)
    assert (await _pay(client, auth, inv, 40.0)).status_code == 200
    await _backfill(session, auth)
    assert (await _s(session, auth, inv))[:2] == ("partial", 40.0)
    assert await _tie(session, auth) == 40.0


async def test_credit_beyond_the_balance_settles_what_was_owed(client, session, auth):
    inv = await _svc_invoice(client, auth, 80.0)
    assert (await _pay(client, auth, inv, 60.0)).status_code == 200
    cn = await _legacy_cn(client, session, auth, inv, 40.0)
    await _backfill(session, auth)
    assert await _tie(session, auth) == -20.0
    assert await _net(session, auth, "4100", prefix="je:") == -40.0
    assert (await _refund(client, auth, cn, 20.0)).status_code == 200
    assert await _tie(session, auth) == 0.0
    await _rerun_changes_nothing(session, auth)


async def test_invoice_voided_voids_its_credit_note(client, session, auth):
    inv = await _svc_invoice(client, auth, 80.0)
    cn = await _cn(client, auth, inv, 40.0, finalize=False)
    await _reduce(session, auth, inv, cn, 40.0)
    assert (await _post(client, auth, f"/docs/{inv}/void")).status_code == 200
    await _raw(session, auth, cn, "doc.finalized")
    await _backfill(session, auth)
    assert (await _s(session, auth, cn))[:2] == ("void", 0.0)
    assert await _tie(session, auth) == 0.0
    assert await _number(session, auth, cn) in await _notice(session, auth)
    r = await _refund(client, auth, cn, 40.0)
    assert r.status_code == 409, r.text
    assert (await _post(client, auth, f"/docs/{cn}/unvoid")).status_code == 409
    assert await _tie(session, auth) == 0.0
    await _rerun_changes_nothing(session, auth)


async def test_nothing_is_refunded_or_applied_from_a_credit_note_on_a_void_invoice(client, session, auth):
    inv = await _svc_invoice(client, auth, 80.0)
    other = await _svc_invoice(client, auth, 50.0)
    cn = await _cn(client, auth, inv, 40.0, finalize=False)
    assert (await _post(client, auth, f"/docs/{inv}/void")).status_code == 200
    await _raw(session, auth, cn, "doc.finalized")
    r = await _refund(client, auth, cn, 10.0)
    assert r.status_code == 409 and "void" in r.text, r.text
    a = await _post(client, auth, f"/docs/{cn}/apply-to-invoice", {"target_doc_id": other, "amount": 10.0})
    assert a.status_code == 409 and "void" in a.text, a.text


async def test_credit_note_at_another_rate_posts_at_the_invoices(client, session, auth):
    inv = await _svc_invoice(client, auth, 100.0, currency="EUR", conversion_rate=1.1)
    cn = await _cn(client, auth, inv, 40.0, finalize=False)
    await _raw(session, auth, cn, "doc.updated", {"fields_changed": {"conversion_rate": {"old": 1.1, "new": 1.3}}})
    await _reduce(session, auth, inv, cn, 60.0)
    await _raw(session, auth, cn, "doc.finalized")
    await _backfill(session, auth)
    assert float((await _state(session, auth, cn))["conversion_rate"]) == 1.1
    assert await _tie(session, auth) == 66.0
    assert (await _pay(client, auth, inv, 60.0, conversion_rate=1.1)).status_code == 200
    assert await _tie(session, auth) == 0.0
    await _rerun_changes_nothing(session, auth)


async def test_credit_note_posted_at_another_rate_is_trued_up_to_the_invoices(client, session, auth):
    from celerp.services import auto_je

    inv = await _svc_invoice(client, auth, 100.0, currency="EUR", conversion_rate=1.1)
    cn = await _cn(client, auth, inv, 40.0, finalize=False)
    await _raw(session, auth, cn, "doc.updated", {"fields_changed": {"conversion_rate": {"old": 1.1, "new": 1.3}}})
    await auto_je.create_for_credit_note_finalized(session, company_id=auth["company_id"], user_id=auth["user_id"],
                                                   doc_id=cn, doc=await _state(session, auth, cn))
    await session.commit()
    await _reduce(session, auth, inv, cn, 60.0)
    await _raw(session, auth, cn, "doc.finalized")
    await _backfill(session, auth)
    assert await _net(session, auth, "6960", prefix="je:") == -8.0
    assert await _tie(session, auth) == 66.0
    assert (await _pay(client, auth, inv, 60.0, conversion_rate=1.1)).status_code == 200
    assert await _tie(session, auth) == 0.0
    v = await _post(client, auth, f"/docs/{cn}/void")
    assert v.status_code == 200, v.text
    assert await _tie(session, auth) == 44.0
    await _rerun_changes_nothing(session, auth)


async def test_void_and_unvoid_after_the_settlement_move_every_leg(client, session, auth):
    inv = await _svc_invoice(client, auth, 80.0)
    cn = await _legacy_cn(client, session, auth, inv, 40.0)
    await _backfill(session, auth)
    for path, owed in (("void", 80.0), ("unvoid", 40.0), ("void", 80.0)):
        r = await _post(client, auth, f"/docs/{cn}/{path}")
        assert r.status_code == 200, r.text
        assert await _tie(session, auth) == owed
    await _rerun_changes_nothing(session, auth)
    assert await _net(session, auth, "4100", prefix="je:") == -80.0
    assert (await _s(session, auth, inv))[:2] == ("final", 80.0)


async def test_two_credit_notes_never_credit_more_than_the_invoice(client, session, auth):
    inv = await _svc_invoice(client, auth, 80.0)
    c1 = await _legacy_cn(client, session, auth, inv, 60.0)
    c2 = await _legacy_cn(client, session, auth, inv, 40.0)
    out = await _backfill(session, auth)
    assert out["settled"] == 2, out
    assert await _s(session, auth, inv) == ("paid", 0.0, 0.0, 80.0)
    assert await _s(session, auth, c1) == ("paid", 0.0, 0.0, 60.0)
    assert await _s(session, auth, c2) == ("partial", 20.0, 0.0, 20.0)
    assert await _tie(session, auth) == -20.0
    await _rerun_changes_nothing(session, auth)
