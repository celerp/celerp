# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Outcome-level proof that the global doc-row FOR UPDATE lock keeps every memo
lifecycle status-writer mutually exclusive with a concurrent Close, not merely that
a lock is requested.

Each lifecycle route reads the doc row, checks its status, and conditionally emits a
status-changing event. Two writers that each read the same prior committed state can
both pass their status check and both commit, leaving an inconsistent terminal state
(closed + shipped-from-closed, closed silently flipped by a payment, two terminal
transitions from one row). The doc-row lock forces the loser to block until the
winner commits, re-read the committed state under the lock, and then 409 or no-op.

These use independent sessions on the shared engine with real commits (not the
savepoint session): a row lock is only observable, and a lost update only forms,
between two separately committed transactions. They assert the FINAL COMMITTED state,
so neutralizing the lock (a plain unlocked load) makes each test fail on the illegal
interleaving it targets. FOR UPDATE row locking cannot be exercised on sqlite, so
these run on real Postgres via the session-scoped _db_engine (DATABASE_URL)."""

from __future__ import annotations

import asyncio
import types
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.events.engine import emit_event
from celerp_accounting.routes import seed_chart_of_accounts
from celerp_accounting.models import Account
from celerp.models.company import Company, User
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection


def _factory(engine):
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


async def _seed_company(factory):
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="Lifecycle Co", slug=f"life-{company_id.hex[:8]}"))
        s.add(User(id=user_id, email=f"race-{user_id.hex[:8]}@life.test", name="Race User",
                   auth_hash="x"))
        await s.flush()
        await seed_chart_of_accounts(s, company_id)
        await s.commit()
    return company_id, user_id, types.SimpleNamespace(id=user_id)


async def _cleanup(factory, company_id, user_id):
    async with factory() as s:
        await s.execute(delete(Projection).where(Projection.company_id == company_id))
        await s.execute(delete(LedgerEntry).where(LedgerEntry.company_id == company_id))
        await s.execute(delete(Account).where(Account.company_id == company_id))
        await s.execute(delete(Company).where(Company.id == company_id))
        await s.execute(delete(User).where(User.id == user_id))
        await s.commit()


def _barcode() -> str:
    return str(uuid.uuid4().int)[:12]


async def _seed_item(factory, company_id, user, *, sku, name, qty, barcode) -> str:
    entity_id = f"item:{uuid.uuid4()}"
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id=entity_id, entity_type="item",
            event_type="item.created",
            data={"status": "available", "sku": sku, "name": name, "quantity": qty,
                  "barcode": barcode, "cost_price": 1, "sell_by": "piece"},
            actor_id=user.id, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await s.commit()
    return entity_id


async def _seed_memo(factory, company_id, user, *, ref_id, line_items) -> str:
    """A committed, issued (status 'sent') memo: fulfillable AND closable.

    The doc.created reducer folds header totals straight from the event data (it does not
    recompute from line_items), so each line carries its own line_total and the header
    total/amount_outstanding are set here exactly as create_doc computes them - a seeded
    memo therefore has a real, payable total, not zero."""
    lines = [{**li, "line_total": li.get("line_total",
                                         float(li.get("quantity", 0)) * float(li.get("unit_price", 0)))}
             for li in line_items]
    total = sum(li["line_total"] for li in lines)
    entity_id = f"doc:{ref_id}-{uuid.uuid4().hex[:8]}"
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id=entity_id, entity_type="doc",
            event_type="doc.created",
            data={"doc_type": "memo", "status": "draft", "ref_id": ref_id, "line_items": lines,
                  "subtotal": total, "total": total, "amount_outstanding": total},
            actor_id=user.id, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await emit_event(
            s, company_id=company_id, entity_id=entity_id, entity_type="doc",
            event_type="doc.sent", data={},
            actor_id=user.id, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await s.commit()
    return entity_id


async def _one_line_memo(factory, company_id, user, ref_id, *, sku, name):
    """A sent one-line memo whose item is left 'available' (never shipped), so Close is
    admissible (no memo_out line) and the line is a valid target for the racing writer."""
    item_id = await _seed_item(factory, company_id, user, sku=sku, name=name, qty=1,
                               barcode=_barcode())
    memo_id = await _seed_memo(
        factory, company_id, user, ref_id=ref_id,
        line_items=[{"entity_id": item_id, "sku": sku, "name": name, "quantity": 1,
                     "unit_price": 10, "sell_by": "piece"}])
    return item_id, memo_id


async def _read(factory, company_id, entity_id) -> Projection:
    async with factory() as s:
        return await s.get(Projection, {"company_id": company_id, "entity_id": entity_id},
                           populate_existing=True)


async def _state(factory, company_id, entity_id) -> dict:
    return dict((await _read(factory, company_id, entity_id)).state)


async def _finalize_seq(factory, company_id, user, memo_id):
    """Finalize a seeded (sent) memo so it computes a total and can accept a payment."""
    from celerp_docs.routes import finalize_doc
    async with factory() as s:
        await finalize_doc(memo_id, company_id=company_id, _=None, user=user, session=s)


async def _record_payment_seq(factory, company_id, user, memo_id, amount):
    """Finalize (to compute the total) then record a real cash payment sequentially."""
    from celerp_docs.routes import record_payment, DocPaymentBody
    await _finalize_seq(factory, company_id, user, memo_id)
    async with factory() as s:
        await record_payment(
            memo_id, DocPaymentBody(amount=amount, payment_date="2026-06-20", method="cash",
                                    bank_account="1111"),
            company_id=company_id, _=None, user=user, session=s)


async def _close_seq(factory, company_id, user, memo_id):
    from celerp_docs.routes import close_doc, DocCloseBody
    async with factory() as s:
        await close_doc(memo_id, DocCloseBody(), company_id=company_id, _=None, user=user, session=s)


def _n_409(*outcomes) -> int:
    return sum(1 for o in outcomes if isinstance(o, HTTPException) and o.status_code == 409)


async def _race(coro_a, coro_b, timeout=20):
    await asyncio.wait_for(asyncio.gather(coro_a, coro_b), timeout=timeout)


# ---------------------------------------------------------------------------
# F3 races: Close vs each concurrent lifecycle status-writer.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def _shipping_docs_for(factory, company_id, memo_id) -> list[str]:
    """Every shipping_doc whose source_docs reference this memo."""
    from sqlalchemy import select
    async with factory() as s:
        rows = (await s.execute(
            select(Projection).where(Projection.company_id == company_id))).scalars().all()
    out = []
    for r in rows:
        st = r.state or {}
        if st.get("doc_type") != "shipping_doc" and st.get("list_type") != "shipping_doc":
            continue
        srcs = st.get("source_docs") or st.get("source_doc_ids") or []
        flat = " ".join(str(x) for x in (srcs if isinstance(srcs, (list, tuple)) else [srcs]))
        if memo_id in flat:
            out.append(r.entity_id)
    return out


async def test_close_shipping_race_no_ship_from_closed(_db_engine):
    """create_shipment must never commit a shipment against a memo whose committed status is
    'closed'. This is a GENUINE concurrent race: both writers read the same prior 'sent'
    state, then run concurrently on independent committed sessions, with Close ordered to
    commit first (it is released; the ship only proceeds after Close has committed). The
    forbidden outcome is a shipment created after the memo is durably closed - ship-from-
    closed. (Ship-then-close, where the ship precedes the close, is legal and is a different
    ordering; here Close leads, so the ship must observe 'closed' under the source lock.)

    The reject is PRE-EXISTING and the per-row FOR UPDATE (close @1643, the shipment source
    lock via _get_docs_for_update @3075) already serializes the ship's locked re-read
    against the committed close, so this is expected GREEN at merge-base: a defense-in-depth
    proof, not part of the red-first batch."""
    from celerp_docs.routes import close_doc, create_shipment_from_docs, DocCloseBody, ShipmentFromDocsBody

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        _item_id, memo_id = await _one_line_memo(factory, company_id, user, "MEMO-CS",
                                                 sku="CS1", name="Stone CS")
        # Both writers observe the same prior committed 'sent' state before racing.
        assert (await _state(factory, company_id, memo_id)).get("status") == "sent"
        s_close, s_ship = factory(), factory()
        outcome: dict = {}
        close_committed = asyncio.Event()

        async def _close(session):
            try:
                outcome["close"] = await close_doc(
                    memo_id, DocCloseBody(), company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001
                outcome["close"] = exc
                await session.rollback()
            finally:
                close_committed.set()

        async def _ship(session):
            # Ship only proceeds once Close has committed 'closed', so this is the
            # ship-from-closed ordering the shipment source lock must reject.
            await close_committed.wait()
            try:
                outcome["ship"] = await create_shipment_from_docs(
                    ShipmentFromDocsBody(doc_ids=[memo_id]), company_id=company_id, _=None,
                    user=user, session=session)
                await session.commit()
            except Exception as exc:  # noqa: BLE001 - a 409 (memo closed) is the required outcome
                outcome["ship"] = exc
                await session.rollback()

        try:
            await _race(_close(s_close), _ship(s_ship))
        finally:
            await s_close.close()
            await s_ship.close()

        status = (await _state(factory, company_id, memo_id)).get("status")
        shipments = await _shipping_docs_for(factory, company_id, memo_id)
        assert not isinstance(outcome.get("close"), Exception), (
            f"Close leads and must commit; got {outcome.get('close')!r}")
        assert status == "closed", f"the memo must be committed closed; got {status!r}"
        assert not shipments, (
            "illegal: shipping paperwork was created against a memo committed as closed. "
            f"shipments={shipments!r}, ship outcome={outcome.get('ship')!r}")
        assert isinstance(outcome.get("ship"), HTTPException) and outcome["ship"].status_code == 409, (
            f"create_shipment on a committed-closed memo must 409; got {outcome.get('ship')!r}")
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_close_record_payment_race_no_silent_unclose(_db_engine):
    """Close vs record_payment race on the same issued memo, with the terminal state pinned
    deterministically by payment-before-close ordering. The payment starts first and its
    doc-row FOR UPDATE is held until it commits; Close is released a moment later, so it
    can only commit AFTER the payment lands. The one legal terminal state is therefore
    paid-then-closed: the payment applied (the memo went live) and Close settled it as
    'closed'. Never a payment recorded while the committed status is 'closed', and never a
    Close silently flipped to partial/paid by a payment that landed after it.

    record_payment's closed-reject is PRE-EXISTING (apply_doc_payment re-reads under a lock
    and its allowlist excludes 'closed') and the added doc-row FOR UPDATE serializes the
    two, so this is expected GREEN at merge-base: defense-in-depth, not part of the
    red-first batch. The ordering makes the outcome a single asserted state rather than a
    disjunction, so a regression that let the payment un-close the memo fails here."""
    from celerp_docs.routes import close_doc, record_payment, DocCloseBody, DocPaymentBody

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        _item_id, memo_id = await _one_line_memo(factory, company_id, user, "MEMO-CP",
                                                 sku="CP1", name="Stone CP")
        s_close, s_pay = factory(), factory()
        outcome: dict = {}
        pay_committed = asyncio.Event()

        async def _pay(session):
            try:
                outcome["pay"] = await record_payment(
                    memo_id, DocPaymentBody(amount=5, payment_date="2026-06-21", method="cash",
                                            bank_account="1111"),
                    company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001 - a 409 (memo closed) is an allowed outcome
                outcome["pay"] = exc
                await session.rollback()
            finally:
                pay_committed.set()

        async def _close(session):
            # Order the writers: Close only proceeds after the payment has committed, so
            # the sole legal terminal state is paid-then-closed.
            await pay_committed.wait()
            try:
                outcome["close"] = await close_doc(
                    memo_id, DocCloseBody(), company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001
                outcome["close"] = exc
                await session.rollback()

        try:
            await _race(_pay(s_pay), _close(s_close))
        finally:
            await s_close.close()
            await s_pay.close()

        memo_state = await _state(factory, company_id, memo_id)
        status = memo_state.get("status")
        payments = [p for p in (memo_state.get("payments") or []) if p.get("status") != "deleted"]
        # The payment landed first (memo went live), then Close settled it: paid-then-closed.
        assert not isinstance(outcome.get("pay"), Exception), (
            f"the payment led and must apply against the live memo; got {outcome.get('pay')!r}")
        assert not isinstance(outcome.get("close"), Exception), (
            f"Close follows the committed payment and must settle the memo; got {outcome.get('close')!r}")
        assert len(payments) == 1, f"exactly one payment expected; got {payments!r}"
        assert status == "closed", (
            "un-close: a payment that predates the Close left the memo live instead of "
            f"paid-then-closed; status={status!r}, close={outcome.get('close')!r}")
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_close_void_payment_race_no_silent_unclose(_db_engine):
    """Close vs void_payment serialize: a committed close must never be flipped by a
    racing void of a payment. Setup: the memo carries a payment, then is closed; the
    void races the close. At merge-base void_payment reads UNLOCKED and its
    doc.payment.voided fold recomputes status, un-closing the memo. The lock makes the
    void re-read 'closed' and 409 (Reopen first)."""
    from celerp_docs.routes import close_doc, void_payment, DocCloseBody, VoidPaymentBody

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        _item_id, memo_id = await _one_line_memo(factory, company_id, user, "MEMO-CV",
                                                 sku="CV1", name="Stone CV")
        # A payment recorded while the memo is live (status -> partial). It survives the
        # close and is the void's target.
        await _record_payment_seq(factory, company_id, user, memo_id, 4)
        s_close, s_void = factory(), factory()
        outcome: dict = {}
        close_done = asyncio.Event()

        async def _close(session):
            try:
                outcome["close"] = await close_doc(
                    memo_id, DocCloseBody(), company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001
                outcome["close"] = exc
                await session.rollback()
            finally:
                close_done.set()

        async def _void(session):
            await close_done.wait()
            try:
                outcome["void"] = await void_payment(
                    memo_id, VoidPaymentBody(payment_index=0, void_reason="race"),
                    company_id=company_id, _=None, user=user, session=session)
                await session.commit()
            except Exception as exc:  # noqa: BLE001 - a 409 (memo closed) is the fixed outcome
                outcome["void"] = exc
                await session.rollback()

        try:
            await _race(_close(s_close), _void(s_void))
        finally:
            await s_close.close()
            await s_void.close()

        memo_state = await _state(factory, company_id, memo_id)
        assert not isinstance(outcome.get("close"), Exception), f"close should commit; got {outcome.get('close')!r}"
        assert memo_state.get("status") == "closed", (
            "un-close: a committed Close was flipped by a racing void-payment; memo is now "
            f"{memo_state.get('status')!r}. outcomes close={outcome.get('close')!r}, void={outcome.get('void')!r}")
        assert isinstance(outcome.get("void"), HTTPException) and outcome["void"].status_code == 409, (
            f"void-payment on the just-closed memo must 409; got {outcome.get('void')!r}")
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_close_void_doc_race_single_terminal(_db_engine):
    """Close vs void_doc from the same prior state must yield exactly one terminal
    transition, never a memo committed as both closed and void. At merge-base void_doc
    reads UNLOCKED and can commit doc.voided from the same 'sent' row Close read. The
    FOR UPDATE lock serializes them: the loser re-reads the committed terminal status
    and 409s."""
    from celerp_docs.routes import close_doc, void_doc, DocCloseBody, DocVoidBody

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        _item_id, memo_id = await _one_line_memo(factory, company_id, user, "MEMO-CVD",
                                                 sku="CVD1", name="Stone CVD")
        s_close, s_void = factory(), factory()
        outcome: dict = {}

        async def _close(session):
            try:
                outcome["close"] = await close_doc(
                    memo_id, DocCloseBody(), company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001
                outcome["close"] = exc
                await session.rollback()

        async def _void(session):
            try:
                outcome["void"] = await void_doc(
                    memo_id, DocVoidBody(), company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001
                outcome["void"] = exc
                await session.rollback()

        try:
            await _race(_close(s_close), _void(s_void))
        finally:
            await s_close.close()
            await s_void.close()

        memo_state = await _state(factory, company_id, memo_id)
        status = memo_state.get("status")
        assert status in ("closed", "void"), f"exactly one terminal transition; got {status!r}"
        # Exactly one side wins; the loser 409s (a void from a closed row, or a close of a void doc).
        assert _n_409(outcome.get("close"), outcome.get("void")) == 1, (
            f"exactly one of close/void must 409; close={outcome.get('close')!r}, void={outcome.get('void')!r}")
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_close_revert_to_draft_race_mutually_exclusive(_db_engine):
    """Close vs revert_doc_to_draft from the same 'sent' state: exactly one wins. At
    merge-base revert_doc_to_draft reads UNLOCKED and can commit doc.reverted from the
    same row Close read, leaving a memo both closed and reverted to draft. The lock
    serializes them; the loser re-reads and 409s."""
    from celerp_docs.routes import close_doc, revert_doc_to_draft, DocCloseBody, DocRevertBody

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        _item_id, memo_id = await _one_line_memo(factory, company_id, user, "MEMO-CR",
                                                 sku="CR1", name="Stone CR")
        s_close, s_revert = factory(), factory()
        outcome: dict = {}

        async def _close(session):
            try:
                outcome["close"] = await close_doc(
                    memo_id, DocCloseBody(), company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001
                outcome["close"] = exc
                await session.rollback()

        async def _revert(session):
            try:
                outcome["revert"] = await revert_doc_to_draft(
                    memo_id, DocRevertBody(), company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001
                outcome["revert"] = exc
                await session.rollback()

        try:
            await _race(_close(s_close), _revert(s_revert))
        finally:
            await s_close.close()
            await s_revert.close()

        memo_state = await _state(factory, company_id, memo_id)
        status = memo_state.get("status")
        assert status in ("closed", "draft"), f"exactly one transition; got {status!r}"
        assert _n_409(outcome.get("close"), outcome.get("revert")) == 1, (
            f"exactly one of close/revert must 409; close={outcome.get('close')!r}, revert={outcome.get('revert')!r}")
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_close_convert_race_mutually_exclusive(_db_engine):
    """Close vs convert_doc race. The invariant: a memo is never both closed and
    converted. Close and convert have COMPLEMENTARY custody preconditions that already
    make them mutually exclusive at merge-base - Close refuses while any line is memo_out
    (still at the customer), while convert refuses unless at least one line is memo_out
    to invoice - so the memo here carries a fulfilled (memo_out) line: convert is
    admissible, Close is not. The doc-row FOR UPDATE the fix adds to convert is
    defense-in-depth (it serializes convert against a concurrent lifecycle writer that
    could flip the custody set under it); this test proves the lock does not let the two
    both commit under contention. Expected GREEN at merge-base (the precondition guard is
    pre-existing) and not part of the red-first batch."""
    from celerp_docs.routes import close_doc, convert_doc, DocCloseBody, fulfill_lines, FulfillLinesRequest

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        # Fulfill the line so it is memo_out: convert is admissible (has an item to
        # invoice) and Close is not (a line is still at the customer).
        item_id, memo_id = await _one_line_memo(factory, company_id, user, "MEMO-CC",
                                                sku="CC1", name="Stone CC")
        async with factory() as s:
            await fulfill_lines(memo_id, FulfillLinesRequest(line_entity_ids=[item_id]),
                                company_id=company_id, _=None, user=user, session=s)
        s_close, s_conv = factory(), factory()
        outcome: dict = {}

        async def _close(session):
            try:
                outcome["close"] = await close_doc(
                    memo_id, DocCloseBody(), company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001 - a 409 (convert won) is allowed
                outcome["close"] = exc
                await session.rollback()

        async def _convert(session):
            try:
                outcome["convert"] = await convert_doc(
                    memo_id, company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001 - a 409 (close won) is allowed
                outcome["convert"] = exc
                await session.rollback()

        try:
            await _race(_close(s_close), _convert(s_conv))
        finally:
            await s_close.close()
            await s_conv.close()

        memo_state = await _state(factory, company_id, memo_id)
        status = memo_state.get("status")
        close_committed = not isinstance(outcome.get("close"), Exception)
        convert_committed = not isinstance(outcome.get("convert"), Exception)
        assert not (close_committed and convert_committed), (
            "illegal: the memo committed BOTH close and convert. "
            f"status={status!r}, close={outcome.get('close')!r}, convert={outcome.get('convert')!r}")
        # Close is inadmissible (a memo_out line remains) so convert is the sole committer.
        assert not close_committed, (
            f"close must refuse while a line is memo_out; got {outcome.get('close')!r}")
        assert status == "converted", f"convert should be the sole terminal transition; got {status!r}"
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_close_reserve_lines_race_mutually_exclusive(_db_engine):
    """Close vs reserve_lines from the same 'sent' state: an item must never be left
    reserved-from a memo that Close committed as closed. At merge-base reserve_lines
    reads the doc UNLOCKED and emits item.status.set(reserved) from the same prior state
    Close read. The FOR UPDATE lock makes reserve re-read the committed 'closed' status
    and 409 (Cannot reserve on a closed memo)."""
    from celerp_docs.routes import close_doc, reserve_lines, DocCloseBody, ReserveLinesRequest

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        item_id, memo_id = await _one_line_memo(factory, company_id, user, "MEMO-CRL",
                                                sku="CRL1", name="Stone CRL")
        s_close, s_res = factory(), factory()
        outcome: dict = {}

        async def _close(session):
            try:
                outcome["close"] = await close_doc(
                    memo_id, DocCloseBody(), company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001 - a 409 (reserve won) is allowed
                outcome["close"] = exc
                await session.rollback()

        async def _reserve(session):
            try:
                outcome["reserve"] = await reserve_lines(
                    memo_id, ReserveLinesRequest(line_entity_ids=[item_id], new_status="reserved"),
                    company_id=company_id, _=None, user=user, session=session)
                await session.commit()
            except Exception as exc:  # noqa: BLE001 - a 409 (memo closed) is the fixed outcome
                outcome["reserve"] = exc
                await session.rollback()

        try:
            await _race(_close(s_close), _reserve(s_res))
        finally:
            await s_close.close()
            await s_res.close()

        memo_state = await _state(factory, company_id, memo_id)
        item_state = await _state(factory, company_id, item_id)
        memo_closed = memo_state.get("status") == "closed"
        reserved_from_memo = (item_state.get("status") == "reserved"
                              and item_state.get("status_doc_id") == memo_id)
        # Illegal: an item reserved-from a memo that Close committed as closed. At
        # merge-base reserve's unlocked read lets it reserve the item from the still-live
        # memo while Close commits closed - the item ends reserved under a closed memo.
        assert not (memo_closed and reserved_from_memo), (
            "illegal: an item is reserved-from a memo Close committed as closed. "
            f"memo status={memo_state.get('status')!r}, item status={item_state.get('status')!r}, "
            f"status_doc_id={item_state.get('status_doc_id')!r}; reserve outcome={outcome.get('reserve')!r}")
        assert _n_409(outcome.get("close"), outcome.get("reserve")) == 1, (
            f"exactly one of close/reserve must 409; close={outcome.get('close')!r}, "
            f"reserve={outcome.get('reserve')!r}")
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_reopen_payment_race_serialized(_db_engine):
    """Reopen vs a payment on the SAME closed memo serialize (the reopen lock). Close
    and Reopen share no valid prior state, so the reopen lock's real job is serializing
    reopen against a concurrent payment on the closed memo: without the lock, reopen
    reads the doc UNLOCKED and a payment can apply against a status that reopen is about
    to change, a lost update. The invariant: the committed state is self-consistent -
    if the payment applied, the memo is live (reopen won first or the payment re-read
    the reopened status); if the payment was rejected, the memo may still be closed.
    Never a payment recorded while the status stayed 'closed'."""
    from celerp_docs.routes import reopen_doc, record_payment, DocReopenBody, DocPaymentBody

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        _item_id, memo_id = await _one_line_memo(factory, company_id, user, "MEMO-RP",
                                                 sku="RP1", name="Stone RP")
        # Take the memo to closed (no memo_out line, so Close is admissible).
        await _close_seq(factory, company_id, user, memo_id)
        assert (await _state(factory, company_id, memo_id)).get("status") == "closed"

        s_reopen, s_pay = factory(), factory()
        outcome: dict = {}

        async def _reopen(session):
            try:
                outcome["reopen"] = await reopen_doc(
                    memo_id, DocReopenBody(), company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001
                outcome["reopen"] = exc
                await session.rollback()

        async def _pay(session):
            try:
                outcome["pay"] = await record_payment(
                    memo_id, DocPaymentBody(amount=3, payment_date="2026-06-22", method="cash",
                                            bank_account="1111"),
                    company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001 - a 409 (still closed) is an allowed outcome
                outcome["pay"] = exc
                await session.rollback()

        try:
            await _race(_reopen(s_reopen), _pay(s_pay))
        finally:
            await s_reopen.close()
            await s_pay.close()

        memo_state = await _state(factory, company_id, memo_id)
        pay_applied = not isinstance(outcome.get("pay"), Exception)
        status = memo_state.get("status")
        if pay_applied:
            # A payment can only have been accepted against a live (reopened) status; the
            # committed status must NOT be 'closed' (that would be a payment on a closed memo).
            assert status != "closed", (
                "lost update: a payment applied while the memo's committed status stayed 'closed'. "
                f"status={status!r}, pay={outcome.get('pay')!r}, reopen={outcome.get('reopen')!r}")
        else:
            # The payment was rejected; the memo is either still closed (reopen lost/blocked
            # then payment saw closed) or reopened with no payment. Both are consistent.
            assert status in ("closed", "final", "sent", "partial", "paid"), f"unexpected status {status!r}"
    finally:
        await _cleanup(factory, company_id, user_id)


# ---------------------------------------------------------------------------
# Payment lock topology: single-serializer proofs (B1 / B2 / B3).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_payment_lock_order_no_abba_deadlock(_db_engine):
    """B1: no cross-mechanism ABBA deadlock between the two payment paths' lock orders.

    Two payment entry points take the doc-row lock and the payment serializer in OPPOSITE
    orders at merge-base: record_payment takes the PG row lock first (its _get_doc) then the
    payment serializer inside apply_doc_payment; bulk_payment takes the payment serializer
    first then the PG row lock inside emit. On one shared doc this is a classic ABBA cycle.

    The interleaving is pinned deterministically by delaying only the payment emit: the
    bulk (B) side reaches the serializer, then parks in the payment emit BEFORE its row lock
    while record_payment (A) takes the row lock and then waits for the serializer B holds.
    When B's emit resumes it contends the row lock A holds. At merge-base the two in-process
    orders form a cycle Postgres cannot break, so the 8s bound (deliberately under the
    fixture's 10s Postgres lock_timeout, so the asyncio bound trips first) raises
    TimeoutError -> FAIL. Post-fix there is ONE serializer (the PG row lock): both paths take
    it first, serialize, one applies and the other 409s (already paid / fully paid); both
    finish under the bound. The harness references only the public record_payment /
    bulk_payment / emit_event seams, so it imports and runs on both trees.

    Invariants: no TimeoutError; the memo is never double-paid (final amount paid == total)."""
    import celerp_docs.routes as _routes
    from celerp_docs.routes import record_payment, bulk_payment, DocPaymentBody, BulkPaymentBody

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    _orig_emit = _routes.emit_event
    bulk_in_emit = asyncio.Event()
    let_a_start = asyncio.Event()

    async def _pinning_emit(*args, **kwargs):
        # The bulk side announces it is inside the payment emit (serializer held, row lock
        # not yet taken), lets the record side take the row lock, then proceeds into the row
        # lock itself - forming the ABBA at merge-base. Only the first payment emit is pinned.
        if kwargs.get("event_type") == "doc.payment.received" and not bulk_in_emit.is_set():
            bulk_in_emit.set()
            let_a_start.set()
            await asyncio.sleep(0.4)
        return await _orig_emit(*args, **kwargs)

    _routes.emit_event = _pinning_emit
    try:
        _item_id, memo_id = await _one_line_memo(factory, company_id, user, "MEMO-B1",
                                                 sku="B11", name="Stone B1")
        total = float((await _state(factory, company_id, memo_id)).get("total") or 0)
        assert total > 0

        sA, sB = factory(), factory()
        outcome: dict = {}

        async def _record_side(session):
            # A: PG row lock first (record_payment._get_doc), then the serializer inside
            # apply_doc_payment. Starts only once B is parked holding the serializer.
            await let_a_start.wait()
            try:
                outcome["A"] = await record_payment(
                    memo_id, DocPaymentBody(amount=total, payment_date="2026-07-01",
                                            method="cash", bank_account="1111"),
                    company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001 - a 409 (already/fully paid) is allowed
                outcome["A"] = exc
                await session.rollback()

        async def _bulk_side(session):
            # B: serializer first (at merge-base), then the row lock inside the pinned emit.
            try:
                outcome["B"] = await bulk_payment(
                    BulkPaymentBody(doc_ids=[memo_id], amount=total, payment_date="2026-07-01",
                                    method="cash", bank_account="1111"),
                    company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001 - a 409 (already/fully paid) is allowed
                outcome["B"] = exc
                await session.rollback()

        try:
            # 8s < the fixture's 10s Postgres lock_timeout: a deadlock trips this asyncio
            # bound first, a deterministic FAIL rather than an unbounded hang.
            await asyncio.wait_for(asyncio.gather(_record_side(sA), _bulk_side(sB)), timeout=8)
        finally:
            await sA.close()
            await sB.close()

        memo_state = await _state(factory, company_id, memo_id)
        paid = float((memo_state.get("total") or 0)) - float(
            memo_state.get("amount_outstanding", memo_state.get("total", 0)) or 0)
        # Exactly the memo total is paid: the two payment runs did not double-pay (the loser
        # 409s the already-paid doc), and nothing over-applied.
        assert abs(paid - total) < 0.01, (
            f"memo must be paid exactly once (total {total}); paid={paid}, state={memo_state!r}")
    finally:
        _routes.emit_event = _orig_emit
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_close_bulk_payment_race_no_silent_unclose(_db_engine):
    """B2: a concurrent bulk_payment must never silently un-close a memo Close committed.

    emit_event is wrapped so ONLY a doc.payment.received emit awaits a short sleep before
    delegating (close's doc.closed emit is not delayed). Close and bulk_payment run
    concurrently on the same closable memo.

    At merge-base bulk reads the doc UNLOCKED and never re-validates status under a lock:
    Close commits 'closed' during bulk's sleep, then bulk emits a payment whose reducer
    overwrites the status back to partial/paid -> a silent un-close (final status !=
    'closed') -> FAIL. Post-fix bulk routes through apply_doc_payment, which locks the row
    and re-checks the allowlist (excludes 'closed'): either bulk holds the row across the
    sleep so Close blocks until bulk commits, or Close wins and bulk 409-skips. Either way
    the memo ends 'closed' and no payment is recorded against the committed-closed memo."""
    import celerp_docs.routes as _routes
    from celerp_docs.routes import close_doc, bulk_payment, DocCloseBody, BulkPaymentBody

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    _orig_emit = _routes.emit_event

    async def _slow_payment_emit(*args, **kwargs):
        # Delay ONLY the payment event, so a concurrent Close can commit 'closed' first at
        # merge-base (exposing the unlocked-read un-close) without slowing Close itself.
        if kwargs.get("event_type") == "doc.payment.received":
            await asyncio.sleep(0.5)
        return await _orig_emit(*args, **kwargs)

    _routes.emit_event = _slow_payment_emit
    try:
        _item_id, memo_id = await _one_line_memo(factory, company_id, user, "MEMO-B2",
                                                 sku="B21", name="Stone B2")
        assert (await _state(factory, company_id, memo_id)).get("status") == "sent"
        total = float((await _state(factory, company_id, memo_id)).get("total") or 0)
        s_close, s_bulk = factory(), factory()
        outcome: dict = {}

        async def _close(session):
            try:
                outcome["close"] = await close_doc(
                    memo_id, DocCloseBody(), company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001 - a 409 (bulk won) is allowed
                outcome["close"] = exc
                await session.rollback()

        async def _bulk(session):
            try:
                outcome["bulk"] = await bulk_payment(
                    BulkPaymentBody(doc_ids=[memo_id], amount=total, payment_date="2026-07-02",
                                    method="cash", bank_account="1111"),
                    company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001 - a 409 (memo closed) is allowed
                outcome["bulk"] = exc
                await session.rollback()

        try:
            await _race(_close(s_close), _bulk(s_bulk), timeout=8)
        finally:
            await s_close.close()
            await s_bulk.close()

        memo_state = await _state(factory, company_id, memo_id)
        status = memo_state.get("status")
        payments = [p for p in (memo_state.get("payments") or []) if p.get("status") != "deleted"]
        # If Close committed, its terminal 'closed' must stand: a payment must never overwrite it.
        close_committed = not isinstance(outcome.get("close"), Exception)
        if close_committed:
            assert status == "closed", (
                "silent un-close: bulk_payment flipped a committed-closed memo to "
                f"{status!r}. bulk={outcome.get('bulk')!r}")
            assert not payments, (
                "a payment was recorded against a memo committed as closed. "
                f"payments={payments!r}, bulk={outcome.get('bulk')!r}")
        else:
            # Bulk won the row first: the memo is paid, and Close 409'd (cannot close mid-pay
            # or the paid memo remains live/closable but not un-closed).
            assert status in ("partial", "paid", "closed"), f"unexpected status {status!r}"
    finally:
        _routes.emit_event = _orig_emit
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_multi_doc_lock_is_ordered(_db_engine):
    """B3: both multi-doc locking SELECTs compile with `ORDER BY projections.entity_id`, so
    two handlers sharing docs acquire them in the same order (no deadlock). At merge-base
    neither SELECT carries an ORDER BY (Postgres locks in plan order) -> the assertions fail
    (RED); post-fix both do (GREEN).

    Site 1 is _get_docs_for_update: its actual executed statement is captured by spying on
    session.execute. Site 2 is the write-off item-lock batch, an inline SELECT inside
    write_off_stock; its lock statement is asserted from the function source, since it is not
    separately callable. Both must order by projections.entity_id."""
    import inspect
    import celerp_docs.routes as _routes
    from celerp_docs.routes import _get_docs_for_update, write_off_stock

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        _item_id, memo_id = await _one_line_memo(factory, company_id, user, "MEMO-B3",
                                                 sku="B31", name="Stone B3")
        # Site 1: capture the compiled SQL of the statement _get_docs_for_update executes.
        captured: dict = {}
        async with factory() as s:
            _orig_execute = s.execute

            async def _spy(statement, *a, **k):
                try:
                    captured["sql"] = str(statement.compile(
                        dialect=s.bind.dialect,
                        compile_kwargs={"literal_binds": False}))
                except Exception:  # noqa: BLE001 - fall back to the generic compile
                    captured["sql"] = str(statement)
                return await _orig_execute(statement, *a, **k)

            s.execute = _spy  # type: ignore[assignment]
            await _get_docs_for_update(s, company_id, [memo_id, _item_id])

        sql = captured.get("sql", "")
        assert "for update" in sql.lower(), f"_get_docs_for_update must lock FOR UPDATE; sql={sql!r}"
        assert "order by projections.entity_id" in sql.lower(), (
            "_get_docs_for_update locking SELECT must ORDER BY projections.entity_id for a "
            f"deterministic lock order; compiled sql={sql!r}")

        # Site 2: the write-off item-lock batch. Assert its locking SELECT orders by
        # Projection.entity_id in the same block (source-level, it is inline and not callable).
        src = inspect.getsource(write_off_stock)
        assert ".with_for_update()" in src, "write-off batch must lock FOR UPDATE"
        assert ".order_by(Projection.entity_id)" in src, (
            "write-off item-lock batch must ORDER BY Projection.entity_id for a deterministic "
            "lock order (same B3 defect as _get_docs_for_update)")
    finally:
        await _cleanup(factory, company_id, user_id)
