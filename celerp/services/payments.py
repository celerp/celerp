# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Online invoice payment via Stripe, brokered by Celerp Connect.

The instance stores no Stripe credentials: it asks Celerp Connect to create a Checkout
Session on the merchant's connected account and to report its status. Payment
surfaces are gated on a "payments enabled" flag; confirmed payments record through
the same path as a manual payment.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select

from celerp.models.payment_closure import PaymentClosure, PaymentRecovery, UnmatchedPayment

log = logging.getLogger(__name__)


def payments_enabled() -> bool:
    """Whether online payment is available (cached flag from Celerp Connect).

    Cheap enough for the per-render pay-button gate.
    """
    from celerp.gateway.state import get_feature_flags
    return bool(get_feature_flags().get("payments_enabled"))


async def _cloud_request(method: str, path: str, payload: dict | None = None) -> dict | None:
    """Use durable instance authority; a live WebSocket is not billing authority."""
    from celerp.config import settings
    if settings.cloud_disconnected:
        return None
    from celerp.services.cloud_entitlement import authenticated_request
    try:
        response = await authenticated_request(method, path, total_s=20.0, json=payload)
        if response is None or response.status_code != 200:
            return None
        data = response.json()
        return data if isinstance(data, dict) else None
    except Exception as exc:
        log.warning("cloud %s %s failed: %s", method, path, exc)
        return None


async def _cloud_get(path: str) -> dict | None:
    return await _cloud_request("GET", path)


async def _cloud_post(path: str, payload: dict) -> dict | None:
    return await _cloud_request("POST", path, payload)


# ── Payment (customer-facing, via the hosted invoice view) ───────────────────

async def checkout_paused() -> bool:
    """Whether new online payments wait for a System Recovery restore Celerp Cloud has
    not confirmed: until it has, a payment the restore lost may not be recorded again
    yet, so an invoice it paid could look unpaid."""
    try:
        async with _own_session() as session:
            return await report_recoveries(session) is None
    except Exception:
        log.warning("Checking for unconfirmed System Recovery restores failed", exc_info=True)
        return True



async def create_checkout(*, amount_minor: int, currency: str, description: str,
                          company_id: str, entity_id: str,
                          share_token: str) -> dict | None:
    """Ask Cloud to open a Checkout Session on the merchant's connected account.

    Returns {"url": <stripe checkout url>} (redirect the customer there), or None on
    failure. `amount_minor` is the balance due in minor units.
    """
    return await _cloud_post("/billing/connect/checkout", {
        "amount_minor": amount_minor, "currency": currency,
        "description": description[:250],
        "company_id": company_id, "entity_id": entity_id,
        "share_token": share_token,
    })


class PaymentsNotClosed(Exception):
    """Celerp Cloud did not confirm that a company's online payments are closing.

    ``reason`` is "disconnected", "payment_settling", "payment_unrecorded" or "unconfirmed".
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_CLOSURE = "/billing/connect/companies/retire"
_RECOVERY = "/billing/connect/recovery"
_PREPARED = ("prepared", "retired")
_REFUSED = ("payment_settling", "payment_unrecorded")
# Answers after which a step can never succeed: the request is forgotten, and Celerp
# Cloud keeps the company's payments closed.
_FINAL = {"finalize": ("generation_stale", "cancelled", "not_prepared"),
          "cancel": ("retired",)}
RECONCILE_INTERVAL_S = 300


def _own_session():
    import celerp.db
    from sqlalchemy.ext.asyncio import AsyncSession
    return AsyncSession(bind=celerp.db.engine, expire_on_commit=False)


async def _cloud_answer(path: str, payload: dict) -> tuple[int, dict] | None:
    """Celerp Cloud's status and answer to one payment lifecycle request, or None when
    there is no readable answer."""
    from celerp.services import cloud_entitlement
    try:
        response = await cloud_entitlement.authenticated_request("POST", path, total_s=30.0, json=payload)
        data = response.json() if response is not None else None
    except Exception as exc:
        log.warning("Celerp Cloud payment request %s failed: %s", path, type(exc).__name__)
        return None
    return (response.status_code, data) if isinstance(data, dict) else None


async def _closure_step(step: str, closure: PaymentClosure) -> tuple[int, dict] | None:
    return await _cloud_answer(f"{_CLOSURE}/{step}", {
        "company_id": str(closure.target_company), "operation_id": str(closure.operation_id),
        "generation": closure.generation})


def _reached(answer: tuple[int, dict] | None, closure: PaymentClosure, states: tuple[str, ...]) -> bool:
    return answer is not None and answer[0] == 200 and answer[1] in [
        {"company_id": str(closure.target_company), "operation_id": str(closure.operation_id), "state": state}
        for state in states]


async def receive_payment(payload: dict) -> bool:
    """Record an online payment Celerp Cloud delivered: on its invoice, or, when the
    company or the invoice no longer exists or the invoice refuses it, among the
    unmatched payments. True once recorded either way (Cloud is then told it
    arrived), False for a delivery that names no payment. Raises when nothing could
    be recorded, so Cloud delivers it again. Recording the same payment twice
    changes nothing."""
    from fastapi import HTTPException
    from celerp.models.projections import Projection
    from celerp.services.company_lock import hold_company
    from celerp_docs.routes_payments import record_stripe_payment
    company_id, entity_id, reference = (str(payload.get(k) or "") for k in ("company_id", "entity_id", "reference"))
    if not (company_id and entity_id and reference):
        return False
    amount_minor = int(payload.get("amount_minor") or 0)
    currency = str(payload.get("currency") or "USD").upper()
    try:
        cid = uuid.UUID(company_id)
    except ValueError:
        cid = None
    async with _own_session() as session:
        # A reset waits for this hold; once it has deleted the company, the payment is unmatched.
        row = await session.get(Projection, (cid, entity_id)) if cid and await hold_company(session, cid) else None
        if row is not None:
            try:
                await record_stripe_payment(session, cid, entity_id, dict(row.state), reference=reference,
                                            amount_minor=amount_minor, currency=currency)
                return True
            except HTTPException as exc:
                if exc.status_code >= 500:
                    raise
                await session.rollback()
                log.error("Invoice %s refused online payment %s: %s", entity_id, reference, exc.detail)
        from sqlalchemy.dialects.postgresql import insert
        await session.execute(insert(UnmatchedPayment).values(
            reference=reference, amount_minor=amount_minor, currency=currency,
            former_company=company_id, document=entity_id).on_conflict_do_nothing())
        await session.commit()
    return True


async def unmatched_payments(session) -> list[UnmatchedPayment]:
    """Every online payment that could not be recorded on its invoice, newest first."""
    return list((await session.scalars(
        select(UnmatchedPayment).order_by(UnmatchedPayment.received_at.desc()))).all())


async def report_recoveries(session) -> int | None:
    """Tell Celerp Cloud of every System Recovery restore it has not confirmed, oldest
    first, and return the installation's current payment generation. None while a
    restore is still unconfirmed: no company's payments can be closed until it is."""
    from celerp.config import settings
    pending = (await session.scalars(select(PaymentRecovery).where(
        PaymentRecovery.generation.is_(None)).order_by(PaymentRecovery.created_at))).all()
    for recovery in pending:
        if settings.cloud_disconnected:
            return None
        answer = await _cloud_answer(_RECOVERY, {
            "recovery_id": str(recovery.recovery_id), "company_ids": recovery.company_ids})
        generation = answer[1].get("generation") if answer is not None and answer[0] == 200 else None
        if (type(generation) is not int or generation < 1
                or answer[1].get("recovery_id") != str(recovery.recovery_id)):
            return None
        recovery.generation = generation
        await session.commit()
    return await session.scalar(select(func.max(PaymentRecovery.generation))) or 0


def record_recovery(session, company_ids: list) -> None:
    """Record, in the restore's own transaction, that a System Recovery restore brought
    back *company_ids*; ``report_recoveries`` tells Celerp Cloud."""
    session.add(PaymentRecovery(recovery_id=uuid.uuid4(), company_ids=sorted(str(c) for c in company_ids)))


async def _settle(session, closure: PaymentClosure, company_exists: bool) -> bool:
    """Tell Celerp Cloud how *closure* ended: a company still here reopens its payments,
    a deleted one closes them for good. Forgets the closure once Cloud confirms, or
    once Cloud answers that the step can never succeed; the company's payments then
    stay closed at Cloud."""
    from celerp.config import settings
    if settings.cloud_disconnected:
        return False
    step, state = ("cancel", "cancelled") if company_exists else ("finalize", "retired")
    answer = await _closure_step(step, closure)
    if not _reached(answer, closure, (state,)):
        if answer is None or answer[0] != 409 or answer[1].get("detail") not in _FINAL[step]:
            return False
        log.error("Celerp Cloud refused to %s closing the online payments of company %s (%s); "
                  "they stay closed", step, closure.target_company, answer[1]["detail"])
    await session.delete(closure)
    await session.commit()
    return True


async def prepare_company_closure(company_id: uuid.UUID) -> uuid.UUID | None:
    """Close a company's online invoice payments at Celerp Cloud before it is deleted:
    its open payment pages are closed and no new one can start. A payment already
    made still reaches the installation, and the closing becomes final only once it
    is recorded (``receive_payment``).
    The caller holds the company against deletion, and once its transaction has
    committed or rolled back passes the returned id to ``settle_company_closure``,
    which closes the payments for good or reopens them.

    An installation without a Celerp Cloud credential never took online payments and
    returns None at once. Otherwise returns only when Cloud confirms, and raises
    PaymentsNotClosed for anything else, after asking Cloud to drop the request. A
    System Recovery restore Cloud has not confirmed, or an earlier request for the
    same company that cannot be reopened, refuses it."""
    from celerp.config import settings
    from celerp.services import cloud_entitlement
    if not await cloud_entitlement.stored_api_key():
        return None
    if settings.cloud_disconnected:
        raise PaymentsNotClosed("disconnected")
    async with _own_session() as session:
        generation = await report_recoveries(session)
        if generation is None:
            raise PaymentsNotClosed("unconfirmed")
        stale = (await session.scalars(select(PaymentClosure).where(
            PaymentClosure.target_company == company_id))).all()
        for closure in stale:
            if not await _settle(session, closure, company_exists=True):
                raise PaymentsNotClosed("unconfirmed")
        closure = PaymentClosure(operation_id=uuid.uuid4(), target_company=company_id, generation=generation)
        session.add(closure)
        await session.commit()
        answer = await _closure_step("prepare", closure)
        if _reached(answer, closure, _PREPARED):
            return closure.operation_id
        # Cloud may have prepared without the answer arriving; until Cloud confirms
        # the payments are reopened the request is kept, and they stay closed.
        await _settle(session, closure, company_exists=True)
    if answer is not None and answer[0] == 409 and answer[1].get("detail") in _REFUSED:
        raise PaymentsNotClosed(answer[1]["detail"])
    raise PaymentsNotClosed("unconfirmed")


async def settle_company_closure(operation_id: uuid.UUID | None) -> bool:
    """Settle one closing request once the transaction that asked for it has ended:
    the company gone closes its payments for good, the company still here reopens
    them. Waits for a deletion of the company still in flight. True once settled (or
    there was nothing to settle); False leaves the request, and the payments closed,
    for the next attempt."""
    if operation_id is None:
        return True
    from celerp.models.company import Company
    try:
        async with _own_session() as session:
            closure = await session.get(PaymentClosure, operation_id)
            if closure is None:
                return True
            exists = await session.scalar(select(Company.id).where(
                Company.id == closure.target_company).with_for_update(read=True, key_share=True))
            closure = await session.get(PaymentClosure, operation_id, populate_existing=True)
            if closure is None:
                return True
            return await _settle(session, closure, company_exists=exists is not None)
    except Exception:
        log.warning("Settling a company payment closure failed", exc_info=True)
        return False


async def reconcile_payments() -> None:
    """Tell Celerp Cloud of any System Recovery restore it has not confirmed, then
    settle every closing request left over, e.g. by a restart part way through a
    reset. Whatever cannot be done now is tried again by the next call; meanwhile
    the companies concerned keep their payments closed."""
    try:
        async with _own_session() as session:
            await report_recoveries(session)
            pending = (await session.scalars(select(PaymentClosure.operation_id))).all()
    except Exception:
        log.warning("Reconciling online payments with Celerp Cloud failed", exc_info=True)
        return
    for operation_id in pending:
        await settle_company_closure(operation_id)


async def reconcile_payments_loop() -> None:
    """``reconcile_payments`` at start and every RECONCILE_INTERVAL_S after."""
    import asyncio
    while True:
        await reconcile_payments()
        await asyncio.sleep(RECONCILE_INTERVAL_S)


async def checkout_status(session_id: str) -> dict | None:
    """Session status for reconcile-on-return.

    Returns {"paid": bool, "reference": <intent id|None>, "amount_minor": int,
    "currency": str} or None.
    """
    return await _cloud_get(f"/billing/connect/checkout/{session_id}")


# ── Connect onboarding (merchant-facing, from Settings → Payments) ───────────

async def connect_start() -> dict | None:
    """Begin Connect OAuth via Cloud. Returns {"url": <stripe oauth url>} to
    redirect the merchant to, or None."""
    return await _cloud_post("/billing/connect/authorize", {})


async def connect_status() -> dict:
    """Authoritative status for the settings page: {"enabled": bool, "state":
    "connected" | "disconnecting" | "disconnected"}. Falls back to the cached feature
    flag, with no state, if Cloud is unreachable."""
    return (await _cloud_get("/billing/connect/status")) or {"enabled": payments_enabled()}


async def disconnect() -> dict | None:
    """Disconnect the merchant's account via Cloud. New payments stop at once; Cloud
    finishes the disconnect once every payment already started has been recorded.
    Returns Cloud's answer, {"disconnected": bool, "state": "disconnecting" |
    "disconnected"}, or None when Cloud gave none."""
    answer = await _cloud_post("/billing/connect/disconnect", {})
    if (answer is None or type(answer.get("disconnected")) is not bool
            or answer.get("state") not in ("disconnecting", "disconnected")):
        return None
    return {"disconnected": answer["disconnected"], "state": answer["state"]}


# ── Subscription management (merchant-facing, from Web Access settings) ──────

async def billing_portal_url() -> str | None:
    """Stripe Billing Portal URL for the merchant's Celerp subscription (cancel,
    change card, invoices). None if the relay is unreachable or no billing
    account is linked to this instance."""
    from celerp.services.cloud_entitlement import authenticated_request
    try:
        response = await authenticated_request("POST", "/billing/portal", json={})
    except Exception as exc:
        log.warning("billing portal request failed: %s", exc)
        return None
    if response is None or response.status_code != 200:
        return None
    data = response.json()
    return data.get("portal_url") if isinstance(data, dict) else None
