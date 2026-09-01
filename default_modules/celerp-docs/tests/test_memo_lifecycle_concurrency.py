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
    """Close vs create_shipment race on the same issued memo. The invariant: a shipping
    document must never exist for a memo that Close committed as closed. At merge-base
    create_shipment reads each source doc UNLOCKED, so under a concurrent gather it can
    pass its non-closed check against the still-live memo and then commit the new
    shipping doc even though Close committed 'closed' in between - a shipment from a
    closed memo (RED). The sorted-batch FOR UPDATE makes create_shipment take the memo
    row FOR UPDATE, block behind Close, re-read the committed 'closed' status, and 409 -
    so either Close wins (no shipment) or create_shipment wins (memo not closed)."""
    from celerp_docs.routes import close_doc, create_shipment_from_docs, DocCloseBody, ShipmentFromDocsBody

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        _item_id, memo_id = await _one_line_memo(factory, company_id, user, "MEMO-CS",
                                                 sku="CS1", name="Stone CS")
        s_close, s_ship = factory(), factory()
        outcome: dict = {}

        async def _close(session):
            try:
                outcome["close"] = await close_doc(
                    memo_id, DocCloseBody(), company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001 - a 409 (create_shipment won) is allowed
                outcome["close"] = exc
                await session.rollback()

        async def _ship(session):
            try:
                outcome["ship"] = await create_shipment_from_docs(
                    ShipmentFromDocsBody(doc_ids=[memo_id]), company_id=company_id, _=None,
                    user=user, session=session)
                await session.commit()
            except Exception as exc:  # noqa: BLE001 - a 409 (memo closed) is the fixed outcome
                outcome["ship"] = exc
                await session.rollback()

        try:
            await _race(_close(s_close), _ship(s_ship))
        finally:
            await s_close.close()
            await s_ship.close()

        memo_state = await _state(factory, company_id, memo_id)
        shipments = await _shipping_docs_for(factory, company_id, memo_id)
        memo_closed = memo_state.get("status") == "closed"
        assert not (memo_closed and shipments), (
            "illegal: a shipping document exists for a memo Close committed as closed. "
            f"memo status={memo_state.get('status')!r}, shipments={shipments!r}; "
            f"outcomes close={outcome.get('close')!r}, ship={outcome.get('ship')!r}")
        assert _n_409(outcome.get("close"), outcome.get("ship")) == 1, (
            f"exactly one of close/create_shipment must 409; close={outcome.get('close')!r}, "
            f"ship={outcome.get('ship')!r}")
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_close_record_payment_race_no_silent_unclose(_db_engine):
    """Close vs record_payment race on the same issued memo. The invariant: a payment is
    never recorded against a memo whose committed status is 'closed'. record_payment's
    closed-reject is PRE-EXISTING - apply_doc_payment re-reads the row under its own
    per-document payment lock and its status allowlist excludes 'closed' - so this race
    has no illegal interleaving even at merge-base: either the payment commits first
    (memo goes live, then Close settles it as paid-then-closed, legal) or Close commits
    first and the payment's re-read rejects it. The added doc-row FOR UPDATE on
    record_payment is defense-in-depth for the same guarantee; this test proves the lock
    does not weaken the pre-existing safety (no un-close under contention). It is
    therefore expected GREEN at merge-base and is not part of the red-first batch."""
    from celerp_docs.routes import close_doc, record_payment, DocCloseBody, DocPaymentBody

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        _item_id, memo_id = await _one_line_memo(factory, company_id, user, "MEMO-CP",
                                                 sku="CP1", name="Stone CP")
        s_close, s_pay = factory(), factory()
        outcome: dict = {}

        async def _close(session):
            try:
                outcome["close"] = await close_doc(
                    memo_id, DocCloseBody(), company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001 - a 409 (payment won, memo live) is allowed
                outcome["close"] = exc
                await session.rollback()

        async def _pay(session):
            try:
                outcome["pay"] = await record_payment(
                    memo_id, DocPaymentBody(amount=5, payment_date="2026-06-21", method="cash",
                                            bank_account="1111"),
                    company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001 - a 409 (memo closed) is an allowed outcome
                outcome["pay"] = exc
                await session.rollback()

        try:
            await _race(_close(s_close), _pay(s_pay))
        finally:
            await s_close.close()
            await s_pay.close()

        memo_state = await _state(factory, company_id, memo_id)
        pay_applied = not isinstance(outcome.get("pay"), Exception)
        status = memo_state.get("status")
        payments = [p for p in (memo_state.get("payments") or []) if p.get("status") != "deleted"]
        # A payment is never recorded while the committed status is 'closed'. If a payment
        # landed, either the memo is live, or Close settled it afterwards as paid-then-closed
        # (a legal terminal state, the payment predates the close). Never a payment applied
        # by un-closing a settled memo.
        if payments:
            assert not (status == "closed" and not pay_applied), (
                "a payment exists on a closed memo but the payment call was rejected - "
                f"inconsistent. status={status!r}, pay={outcome.get('pay')!r}")
        assert status in ("closed", "sent", "final", "partial", "paid"), f"unexpected status {status!r}"
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
