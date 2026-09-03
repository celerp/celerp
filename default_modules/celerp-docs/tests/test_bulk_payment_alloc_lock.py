# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Bulk payment allocation correctness and skip-lock safety.

bulk_payment orders docs oldest-first from an UNLOCKED pre-read, then pays each
under apply_doc_payment, which re-reads that doc under SELECT ... FOR UPDATE and
validates against the fresh, committed outstanding. Two facts must hold:

  * the amount bulk REPORTS and DECREMENTS for a doc is the amount actually
    applied under that doc's row lock, not the stale pre-read allocation; and
  * a doc that cannot be paid (concurrently shrunk, closed, or locked past the
    lock_timeout) is SKIPPED with its lock released, so a second concurrent bulk
    run over the same set in the opposite order never deadlocks.

Row locking and lock_timeout are only observable across separately committed
transactions, so these run on real Postgres via the session-scoped _db_engine
(DATABASE_URL) with independent sessions, never sqlite."""

from __future__ import annotations

import asyncio
import types
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, text
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
        s.add(Company(id=company_id, name="Bulk Co", slug=f"bulk-{company_id.hex[:8]}"))
        s.add(User(id=user_id, email=f"bulk-{user_id.hex[:8]}@bulk.test", name="Bulk User",
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


async def _seed_invoice(factory, company_id, user, *, ref_id, total, contact_id) -> str:
    """A committed, issued (status 'sent') invoice with a real payable total."""
    entity_id = f"doc:{ref_id}-{uuid.uuid4().hex[:8]}"
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id=entity_id, entity_type="doc",
            event_type="doc.created",
            data={"doc_type": "invoice", "status": "draft", "ref_id": ref_id,
                  "contact_id": contact_id,
                  "line_items": [{"name": "X", "quantity": 1, "unit_price": total,
                                  "line_total": total}],
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


async def _state(factory, company_id, entity_id) -> dict:
    async with factory() as s:
        row = await s.get(Projection, {"company_id": company_id, "entity_id": entity_id},
                          populate_existing=True)
        return dict(row.state)


async def _outstanding(factory, company_id, entity_id) -> float:
    st = await _state(factory, company_id, entity_id)
    return float(st.get("amount_outstanding", st.get("total", 0)) or 0)


@pytest.mark.asyncio
async def test_bulk_payment_reports_applied_not_stale(_db_engine):
    """When a doc's outstanding shrinks AFTER bulk's unlocked pre-read but BEFORE the
    per-doc row lock, bulk must report and decrement the amount actually applied under
    the lock, not the stale pre-read allocation.

    A concurrent partial payment is injected into the doc between the pre-read and the
    locked apply by wrapping apply_doc_payment: the first call commits a partial payment
    on a separate session (shrinking outstanding), then delegates to the real helper,
    which re-reads the shrunk outstanding under the lock and clamps.

    At merge-base bulk reports `alloc` (the full stale pre-read) and discards the helper's
    return, so `allocations[0].amount` and `total_allocated` over-report the amount that
    the doc could actually absorb. Post-fix bulk uses the helper's returned applied amount,
    so the report equals what was applied and total_allocated == sum(applied)."""
    import celerp_docs.routes as _routes
    from celerp_docs.routes import bulk_payment, apply_doc_payment, BulkPaymentBody, DocPaymentBody, record_payment

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    _orig_apply = _routes.apply_doc_payment
    shrunk = {"done": False}
    try:
        # One payable invoice with outstanding 100; a concurrent partial payment of 60
        # lands between bulk's pre-read and its locked apply, leaving 40 payable.
        inv = await _seed_invoice(factory, company_id, user, ref_id="BULK-STALE",
                                  total=100.0, contact_id="contact:stale")

        async def _wrapped_apply(session, company_id_, entity_id_, body, **kwargs):
            # Shrink the doc once, on an independent committed session, right before the
            # first locked apply re-reads it. This models a partial payment racing bulk.
            if not shrunk["done"] and entity_id_ == inv:
                shrunk["done"] = True
                async with factory() as s2:
                    await record_payment(
                        inv, DocPaymentBody(amount=60.0, payment_date="2026-02-01",
                                            method="cash", bank_account="1111"),
                        company_id=company_id, _=None, user=user, session=s2)
            return await _orig_apply(session, company_id_, entity_id_, body, **kwargs)

        _routes.apply_doc_payment = _wrapped_apply

        async with factory() as s:
            result = await bulk_payment(
                BulkPaymentBody(doc_ids=[inv], amount=100.0, payment_date="2026-02-02",
                                method="cash", bank_account="1111"),
                company_id=company_id, _=None, user=user, session=s)

        # The doc could only absorb 40 under the lock (100 - the racing 60). Bulk must
        # report exactly 40 for it, and total_allocated must equal the sum of applied.
        applied = {a["doc_id"]: a["amount"] for a in result["allocations"]}
        assert inv in applied, f"the invoice must be paid; result={result!r}"
        assert abs(applied[inv] - 40.0) < 0.01, (
            f"bulk must report the applied amount (40), not the stale pre-read (100); "
            f"got {applied[inv]}")
        assert abs(result["total_allocated"] - sum(applied.values())) < 0.01, (
            f"total_allocated must equal the sum of applied amounts; result={result!r}")
        # The doc must be fully paid now (60 + 40), with no over-application.
        assert await _outstanding(factory, company_id, inv) < 0.01, (
            "the doc must be fully settled with no over-application")
    finally:
        _routes.apply_doc_payment = _orig_apply
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_bulk_clamps_shrunk_doc(_db_engine):
    """A bulk payment whose per-doc allocation overshoots the fresh outstanding under the
    row lock CLAMPS to the fresh outstanding and applies it, even when the bulk carries a
    `reference`. The clamp is the explicit bulk waterfall behavior, not gated on reference.

    A racing partial payment shrinks the doc between bulk's unlocked pre-read and its locked
    apply, so the helper sees the tendered amount overshoot the fresh outstanding. Bulk
    passes clamp_overshoot=True, so the helper clamps to the fresh outstanding, applies it,
    and reports the applied amount; no charged_amount is written (this is not a Stripe
    overpay). At merge-base the clamp branch keys on `not reference`, so a bulk WITH a
    reference falls through to the 409 exceeds-outstanding branch and the doc is skipped
    instead of clamped, leaving it under-paid."""
    import celerp_docs.routes as _routes
    from celerp_docs.routes import bulk_payment, BulkPaymentBody, DocPaymentBody, record_payment

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    _orig_apply = _routes.apply_doc_payment
    shrunk = {"done": False}
    try:
        # Outstanding 100; a racing 60 lands before the locked apply, leaving 40. The bulk
        # tenders 100 against this one doc WITH a reference, so under the lock the helper
        # sees a referenced 100 overshoot the fresh 40.
        inv = await _seed_invoice(factory, company_id, user, ref_id="BULK-REF",
                                  total=100.0, contact_id="contact:ref")

        async def _wrapped_apply(session, company_id_, entity_id_, body, **kwargs):
            if not shrunk["done"] and entity_id_ == inv:
                shrunk["done"] = True
                async with factory() as s2:
                    await record_payment(
                        inv, DocPaymentBody(amount=60.0, payment_date="2026-02-01",
                                            method="cash", bank_account="1111"),
                        company_id=company_id, _=None, user=user, session=s2)
            return await _orig_apply(session, company_id_, entity_id_, body, **kwargs)

        _routes.apply_doc_payment = _wrapped_apply

        async with factory() as s:
            result = await bulk_payment(
                BulkPaymentBody(doc_ids=[inv], amount=100.0, payment_date="2026-02-03",
                                method="cash", bank_account="1111", reference="CHK-9001"),
                company_id=company_id, _=None, user=user, session=s)

        # The referenced overshoot must clamp to the fresh outstanding (40) and be applied,
        # never skipped.
        skipped_ids = {s_["doc_id"] for s_ in result["skipped"]}
        applied = {a["doc_id"]: a["amount"] for a in result["allocations"]}
        assert inv not in skipped_ids, (
            f"a bulk overshoot must clamp-and-apply, not skip; result={result!r}")
        assert inv in applied, f"the invoice must be paid at its clamped outstanding; result={result!r}"
        assert abs(applied[inv] - 40.0) < 0.01, (
            f"bulk must clamp to the fresh outstanding (40) and report it; got {applied[inv]}")

        st = await _state(factory, company_id, inv)
        # The clamped 40 is recorded under the reference; no phantom charged_amount (this is
        # not a Stripe overpay), and the doc settles fully (60 racing + 40 clamped).
        refd = [p for p in (st.get("payments") or [])
                if p.get("status") != "deleted" and p.get("reference") == "CHK-9001"]
        assert refd, f"the clamped referenced payment must be recorded; state={st!r}"
        assert st.get("charged_amount") in (None, 0, 0.0), (
            f"no phantom charged_amount must be written for a non-Stripe clamp; got {st.get('charged_amount')!r}")
        assert await _outstanding(factory, company_id, inv) < 0.01, (
            "the racing 60 plus the clamped 40 must settle the doc in full")
    finally:
        _routes.apply_doc_payment = _orig_apply
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_stripe_overpay_still_clamps_and_records_charged(_db_engine):
    """Regression guard (GREEN on both trees): a genuine source=='stripe' overshoot still
    clamps to the fresh outstanding and records the original charge as charged_amount.

    This pins that re-gating the clamp off `reference` and onto `source` does NOT narrow
    the Stripe overpay behavior. record_stripe_payment is the sole source=='stripe' caller;
    driving it exercises the clamp branch via its real contract, independent of the helper's
    return shape, so this guard is green on both trees."""
    from celerp_docs import routes_payments

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        inv = await _seed_invoice(factory, company_id, user, ref_id="STRIPE-OVR",
                                  total=50.0, contact_id="contact:stripe")
        # A confirmed online charge of 200 (minor units) against a 50-outstanding invoice.
        async with factory() as s:
            doc_state = (await s.get(
                Projection, {"company_id": company_id, "entity_id": inv},
                populate_existing=True)).state
            await routes_payments.record_stripe_payment(
                s, company_id, inv, dict(doc_state),
                reference="pi_test_123", amount_minor=20000, currency="usd")

        st = await _state(factory, company_id, inv)
        # The clamp settled only what the invoice could absorb; the raw charge is on record.
        assert await _outstanding(factory, company_id, inv) < 0.01, (
            "the invoice must be settled to its outstanding, not overpaid")
        payments = [p for p in (st.get("payments") or []) if p.get("status") != "deleted"]
        assert payments, f"the stripe payment must be recorded; state={st!r}"
        assert any(abs(float(p.get("charged_amount") or 0) - 200.0) < 0.01 for p in payments), (
            f"the raw stripe charge (200) must be recorded as charged_amount; payments={payments!r}")
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_bulk_lock_timeout_skips_not_500(_db_engine):
    """A bulk doc held under a row lock past lock_timeout by a concurrent session is
    SKIPPED with a neutral reason and the bulk completes, never 500ing the whole request.

    One session takes the doc's row lock and holds it; a second bulk_payment runs with a
    short lock_timeout so its locked re-read of that doc times out (Postgres 55P03,
    surfaced as SQLAlchemy OperationalError). At merge-base bulk's except catches
    HTTPException only, so the OperationalError propagates and 500s the request. Post-fix
    the 55P03 timeout is caught, rolled back, and recorded as a skip; the bulk finishes."""
    from celerp_docs.routes import bulk_payment, BulkPaymentBody

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        held = await _seed_invoice(factory, company_id, user, ref_id="BULK-LOCK-H",
                                   total=100.0, contact_id="contact:lock")
        other = await _seed_invoice(factory, company_id, user, ref_id="BULK-LOCK-O",
                                    total=100.0, contact_id="contact:lock")

        lock_taken = asyncio.Event()
        release = asyncio.Event()

        async def _hold_lock(session):
            # Take the held doc's row lock and sit on it until released.
            await session.execute(
                text("SELECT 1 FROM projections WHERE company_id = :c AND entity_id = :e FOR UPDATE"),
                {"c": str(company_id), "e": held})
            lock_taken.set()
            await release.wait()
            await session.rollback()

        async def _bulk(session):
            # A short lock_timeout so contending on the held row times out fast (55P03).
            await session.execute(text("SET lock_timeout = '800ms'"))
            return await bulk_payment(
                BulkPaymentBody(doc_ids=[held, other], amount=250.0, payment_date="2026-02-05",
                                method="cash", bank_account="1111"),
                company_id=company_id, _=None, user=user, session=session)

        s_hold, s_bulk = factory(), factory()
        result: dict = {}
        try:
            async def _run_bulk():
                await lock_taken.wait()
                try:
                    result["out"] = await _bulk(s_bulk)
                finally:
                    release.set()

            await asyncio.wait_for(
                asyncio.gather(_hold_lock(s_hold), _run_bulk()), timeout=30)
        finally:
            await s_hold.close()
            await s_bulk.close()

        out = result.get("out")
        assert isinstance(out, dict), f"bulk must complete (no 500); got {out!r}"
        skipped_ids = {s_["doc_id"] for s_ in out["skipped"]}
        paid_ids = {a["doc_id"] for a in out["allocations"]}
        # The locked doc is skipped with a neutral reason; the other doc still gets paid.
        assert held in skipped_ids, (
            f"the locked-out doc must be skipped, not 500 the batch; result={out!r}")
        assert other in paid_ids, f"the unlocked doc must still be paid; result={out!r}"
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_bulk_reversed_order_skip_no_deadlock(_db_engine):
    """Two concurrent bulk runs over a shared multi-doc set in REVERSED doc order, with a
    forced skip on one doc, complete within the timeout: no cross-request deadlock.

    Run A processes [d0, d1] and run B [d1, d0]. Each run's FIRST doc is forced to skip and
    then parks (holding that doc's row lock) until BOTH have skipped-and-parked; only then
    do they proceed to lock their SECOND doc - which is the doc the other run is holding.

    At merge-base the skip retains the first doc's lock (the except has no rollback), so A
    holds d0 and waits on d1 while B holds d1 and waits on d0: an ABBA cycle Postgres must
    break by a lock_timeout (55P03). At merge-base that OperationalError is NOT caught (the
    except is HTTPException-only), so it propagates out of bulk_payment as an unhandled error
    (FAIL). Post-fix the skip rolls back and releases the first doc's lock BEFORE the run
    reaches its second doc, so no cycle ever forms and both runs finish cleanly; and even a
    genuine timeout is caught as a skip. The doc_id tie-breaker additionally makes both runs
    acquire in one canonical order."""
    import celerp_docs.routes as _routes
    from celerp_docs.routes import bulk_payment, apply_doc_payment, BulkPaymentBody

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    _orig_apply = _routes.apply_doc_payment
    try:
        # Two payable docs sharing all sort keys (no due/issue date), so at merge-base input
        # order alone decides acquisition. Same contact so bulk accepts the set.
        d0 = await _seed_invoice(factory, company_id, user, ref_id="BULK-DL-0", total=30.0,
                                 contact_id="contact:dl")
        d1 = await _seed_invoice(factory, company_id, user, ref_id="BULK-DL-1", total=30.0,
                                 contact_id="contact:dl")
        first_of = {"a": d0, "b": d1}  # each run's first-processed doc (opposite orders)
        parked = asyncio.Barrier(2)
        run_ctx: dict = {}

        async def _wrapped_apply(session, company_id_, entity_id_, body, **kwargs):
            key = run_ctx.get(id(session))
            # Force each run to skip on its OWN first doc while holding that doc's lock, then
            # park until both are parked, so the second-doc lock contends the other's hold.
            if key is not None and entity_id_ == first_of[key]:
                # Take the row lock first (mirrors the helper's own FOR UPDATE), so the skip
                # genuinely holds a lock at merge-base where no rollback follows.
                await session.execute(
                    text("SELECT 1 FROM projections WHERE company_id = :c AND entity_id = :e FOR UPDATE"),
                    {"c": str(company_id), "e": entity_id_})
                await parked.wait()
                raise HTTPException(status_code=409, detail="forced skip for deadlock probe")
            return await _orig_apply(session, company_id_, entity_id_, body, **kwargs)

        _routes.apply_doc_payment = _wrapped_apply

        s_a, s_b = factory(), factory()
        run_ctx[id(s_a)] = "a"
        run_ctx[id(s_b)] = "b"
        outcome: dict = {}

        async def _run(session, order, key):
            # A short lock_timeout so an ABBA cycle trips quickly (55P03) rather than sitting
            # to the fixture's 10s bound.
            await session.execute(text("SET lock_timeout = '1500ms'"))
            try:
                outcome[key] = await bulk_payment(
                    BulkPaymentBody(doc_ids=order, amount=60.0, payment_date="2026-02-06",
                                    method="cash", bank_account="1111"),
                    company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001
                outcome[key] = exc
                await session.rollback()

        try:
            # Opposite input orders over the same shared set; a deadlock at merge-base
            # surfaces as an unhandled OperationalError, caught here as the run's outcome.
            await asyncio.wait_for(
                asyncio.gather(_run(s_a, [d0, d1], "a"),
                               _run(s_b, [d1, d0], "b")),
                timeout=25)
        finally:
            await s_a.close()
            await s_b.close()

        # Neither run may deadlock/hang; both return a normal result dict.
        for key in ("a", "b"):
            assert isinstance(outcome.get(key), dict), (
                f"run {key} must complete without deadlock; got {outcome.get(key)!r}")
    finally:
        _routes.apply_doc_payment = _orig_apply
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_bulk_lock_timeout_prod_config_skips(_db_engine):
    """The per-doc lock wait is bounded by APPLICATION behavior, not connection config.

    The bulk session's own lock_timeout is disabled ('0') to model the production engine
    (celerp/db.py sets no lock_timeout), so nothing outside the bulk loop bounds the wait.
    A concurrent session holds one doc's row lock. Post-fix the bulk loop issues
    SET LOCAL lock_timeout before each per-doc lock, so the held doc times out (55P03),
    is skipped, the other doc is paid, and the bulk completes well inside the test bound.
    At merge-base no such statement runs, so with lock_timeout disabled the held doc
    blocks past the bound and the test fails (a bounded timeout, never an unbounded hang).
    A non-55P03 DB error would still propagate, unchanged by this fix."""
    from celerp_docs.routes import bulk_payment, BulkPaymentBody

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        held = await _seed_invoice(factory, company_id, user, ref_id="BULK-PROD-H",
                                   total=100.0, contact_id="contact:prod")
        other = await _seed_invoice(factory, company_id, user, ref_id="BULK-PROD-O",
                                    total=100.0, contact_id="contact:prod")

        lock_taken = asyncio.Event()
        release = asyncio.Event()

        async def _hold_lock(session):
            await session.execute(
                text("SELECT 1 FROM projections WHERE company_id = :c AND entity_id = :e FOR UPDATE"),
                {"c": str(company_id), "e": held})
            lock_taken.set()
            # Release either when told or on a safety timer, so a merge-base hang still ends.
            try:
                await asyncio.wait_for(release.wait(), timeout=12)
            except asyncio.TimeoutError:
                pass
            await session.rollback()

        async def _bulk(session):
            # Disable the connection's own lock_timeout: the ONLY bound is the loop's
            # SET LOCAL (F3). Without it (merge-base) the held doc blocks indefinitely.
            await session.execute(text("SET lock_timeout = '0'"))
            return await bulk_payment(
                BulkPaymentBody(doc_ids=[held, other], amount=250.0, payment_date="2026-02-08",
                                method="cash", bank_account="1111"),
                company_id=company_id, _=None, user=user, session=session)

        s_hold, s_bulk = factory(), factory()
        result: dict = {}
        try:
            async def _run_bulk():
                await lock_taken.wait()
                try:
                    result["out"] = await _bulk(s_bulk)
                finally:
                    release.set()

            # 8s bound: the F3 SET LOCAL is 3s, so a post-fix run finishes comfortably
            # inside it; a merge-base run (no bound) blocks and trips this as a red FAIL.
            await asyncio.wait_for(
                asyncio.gather(_hold_lock(s_hold), _run_bulk()), timeout=8)
        finally:
            await s_hold.close()
            await s_bulk.close()

        out = result.get("out")
        assert isinstance(out, dict), f"bulk must complete under the app-level bound; got {out!r}"
        skipped_ids = {s_["doc_id"] for s_ in out["skipped"]}
        paid_ids = {a["doc_id"] for a in out["allocations"]}
        assert held in skipped_ids, (
            f"the held doc must skip within the app-level lock bound; result={out!r}")
        assert other in paid_ids, f"the unlocked doc must still be paid; result={out!r}"
    finally:
        await _cleanup(factory, company_id, user_id)
