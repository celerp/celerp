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

from sqlalchemy import select

from celerp.models.payment_closure import PaymentClosure

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
    """Celerp Cloud did not confirm that a company's online payments are frozen.

    ``reason`` is "disconnected", "payment_settling", "payment_unrecorded" or "unconfirmed".
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_CLOSURE = "/billing/connect/companies/retire"
_PREPARED = ("prepared", "retired")
_REFUSED = ("payment_settling", "payment_unrecorded")


def _own_session():
    import celerp.db
    from sqlalchemy.ext.asyncio import AsyncSession
    return AsyncSession(bind=celerp.db.engine, expire_on_commit=False)


async def _closure_step(step: str, operation_id: uuid.UUID, company_id: uuid.UUID) -> tuple[int, dict] | None:
    """Ask Celerp Cloud to take one step of a closing request: its status and answer,
    or None when there is no readable answer."""
    from celerp.services import cloud_entitlement
    try:
        response = await cloud_entitlement.authenticated_request(
            "POST", f"{_CLOSURE}/{step}", total_s=30.0,
            json={"company_id": str(company_id), "operation_id": str(operation_id)})
        data = response.json() if response is not None else None
    except Exception as exc:
        log.warning("Closing company payments (%s) failed: %s", step, type(exc).__name__)
        return None
    return (response.status_code, data) if isinstance(data, dict) else None


def _reached(answer: tuple[int, dict] | None, operation_id: uuid.UUID, company_id: uuid.UUID,
             states: tuple[str, ...]) -> bool:
    return answer is not None and answer[0] == 200 and answer[1] in [
        {"company_id": str(company_id), "operation_id": str(operation_id), "state": state} for state in states]


async def _settle(session, closure: PaymentClosure, company_exists: bool) -> bool:
    """Tell Celerp Cloud how *closure* ended: a company still here reopens its payments,
    a deleted one closes them for good. Forgets the closure once Cloud confirms."""
    from celerp.config import settings
    if settings.cloud_disconnected:
        return False
    step, state = ("cancel", "cancelled") if company_exists else ("finalize", "retired")
    if not _reached(await _closure_step(step, closure.operation_id, closure.target_company),
                    closure.operation_id, closure.target_company, (state,)):
        return False
    await session.delete(closure)
    await session.commit()
    return True


async def prepare_company_closure(company_id: uuid.UUID) -> uuid.UUID | None:
    """Freeze a company's online invoice payments at Celerp Cloud before it is deleted:
    its open payment pages are closed, no new one can start, and payments arriving
    meanwhile wait. The caller holds the company against deletion, and once its
    transaction has committed or rolled back passes the returned id to
    ``settle_company_closure``, which closes the payments for good or reopens them.

    An installation without a Celerp Cloud credential never took online payments and
    returns None at once. Otherwise returns only when Cloud confirms the freeze, and
    raises PaymentsNotClosed for anything else, after asking Cloud to drop the request.
    An earlier request for the same company that never settled is reopened first."""
    from celerp.config import settings
    from celerp.services import cloud_entitlement
    if not await cloud_entitlement.stored_api_key():
        return None
    if settings.cloud_disconnected:
        raise PaymentsNotClosed("disconnected")
    async with _own_session() as session:
        stale = (await session.scalars(select(PaymentClosure).where(
            PaymentClosure.target_company == company_id))).all()
        for closure in stale:
            if not await _settle(session, closure, company_exists=True):
                raise PaymentsNotClosed("unconfirmed")
        closure = PaymentClosure(operation_id=uuid.uuid4(), target_company=company_id)
        session.add(closure)
        await session.commit()
        answer = await _closure_step("prepare", closure.operation_id, company_id)
        if _reached(answer, closure.operation_id, company_id, _PREPARED):
            return closure.operation_id
        # Cloud may have frozen the payments without the answer arriving; until Cloud
        # confirms they are reopened the request is kept, and the payments stay frozen.
        await _settle(session, closure, company_exists=True)
    if answer is not None and answer[0] == 409 and answer[1].get("detail") in _REFUSED:
        raise PaymentsNotClosed(answer[1]["detail"])
    raise PaymentsNotClosed("unconfirmed")


async def settle_company_closure(operation_id: uuid.UUID | None) -> bool:
    """Settle one closing request once the transaction that asked for it has ended:
    the company gone closes its payments for good, the company still here reopens
    them. Waits for a deletion of the company still in flight. True once Cloud has
    confirmed (or there was nothing to settle); False leaves the request, and the
    payments frozen, for the next attempt."""
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


async def settle_company_closures() -> None:
    """Settle every closing request left over, e.g. by a restart part way through a
    reset. A request that cannot be settled now keeps its company's payments frozen."""
    try:
        async with _own_session() as session:
            pending = (await session.scalars(select(PaymentClosure.operation_id))).all()
    except Exception:
        log.warning("Reading unsettled company payment closures failed", exc_info=True)
        return
    for operation_id in pending:
        await settle_company_closure(operation_id)


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
    """Authoritative status for the settings page: {"enabled": bool}. Falls back to
    the cached feature flag if Cloud is unreachable."""
    return (await _cloud_get("/billing/connect/status")) or {"enabled": payments_enabled()}


async def disconnect() -> bool:
    """Disconnect the merchant's account via Cloud."""
    return bool(await _cloud_post("/billing/connect/disconnect", {}))


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
