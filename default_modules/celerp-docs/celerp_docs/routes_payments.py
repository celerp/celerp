# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Online invoice payment - Stripe checkout for a shared invoice, brokered by
Celerp Cloud.

Public: /pay/{token} (start), /pay/{token}/return (reconcile on the customer's
return); a backup confirmation covers a closed tab. Authed: the merchant's payment
connection status + connect/disconnect. The instance stores no Stripe credentials;
all money records through the same path as a manual payment.
"""
from __future__ import annotations

import datetime
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.config import settings
from celerp.db import get_session
from celerp.models.accounting import UserCompany
from celerp.models.company import Company
from celerp.models.projections import Projection
from celerp.models.share import DocShareToken
from celerp.services import payments as pay
from celerp.services.auth import get_current_user, require_admin
from celerp.services.money import currency_dp, to_minor_units

log = logging.getLogger(__name__)

public_router = APIRouter()
router = APIRouter(dependencies=[Depends(get_current_user)])

# Only these can be paid online (a bill/PO is money you owe, not money owed to you).
_PAYABLE_TYPES = frozenset({"invoice", "proforma"})
# GL account online payments clear to; overridable per company. Cash is seeded on
# every chart of accounts, so it's a safe default until a company picks one.
_DEFAULT_DEPOSIT_ACCOUNT = "1110"


async def _doc_for_token(session: AsyncSession, token: str, *, require_active: bool = True):
    """Resolve a pay token to its document. Starting a payment requires a live
    share link (a revoked or expired link must not take money); the return leg
    resolves any token, because a charge completed at Stripe has to reconcile
    even if the link lapsed mid-checkout."""
    if require_active:
        from celerp_docs.routes_share import _active_share_row
        share = await _active_share_row(session, token)
    else:
        share = (await session.execute(
            select(DocShareToken).where(DocShareToken.token == token)
        )).scalar_one_or_none()
    if share is None:
        return None, None, None
    row = await session.get(Projection, (share.company_id, share.entity_id))
    if row is None:
        return None, None, None
    return share.company_id, share.entity_id, dict(row.state)


def _outstanding(state: dict) -> float:
    return float(state.get("amount_outstanding", state.get("total", 0)) or 0)


async def _company_owner_id(session: AsyncSession, company_id):
    return (await session.execute(
        select(UserCompany.user_id).where(
            UserCompany.company_id == company_id, UserCompany.role == "owner")
    )).scalars().first()


async def _deposit_account(session: AsyncSession, company_id) -> str:
    company = await session.get(Company, company_id)
    return ((company.settings or {}).get("stripe_deposit_account") if company else None) or _DEFAULT_DEPOSIT_ACCOUNT


async def record_stripe_payment(session, company_id, entity_id, doc_state, *,
                                reference: str, amount_minor: int, currency: str):
    """Record a confirmed online charge as a payment. Idempotent on the reference
    (Stripe payment_intent). Shared by the customer-return and the gateway-push paths.

    Passes the raw charged amount: apply_doc_payment re-reads the document
    inside the per-document lock and clamps against the FRESH outstanding, so
    a manual payment racing the checkout can't reject or double-book the
    charge. Replays (same reference) come back as a quiet None."""
    if not reference:
        return None
    if any(p.get("reference") == reference for p in doc_state.get("payments", [])):
        return None  # already recorded - replayed push / re-opened return page
    amount = amount_minor / (10 ** currency_dp(currency))
    from celerp_docs.routes import apply_doc_payment
    body = {"amount": amount, "payment_date": datetime.date.today().isoformat(),
            "currency": currency.upper(), "bank_account": await _deposit_account(session, company_id),
            "method": "stripe", "reference": reference}
    try:
        entry = await apply_doc_payment(
            session, company_id, entity_id, doc_state, body,
            source="stripe", actor_id=await _company_owner_id(session, company_id),
            idempotency_key=reference,
        )
    except HTTPException:
        return None  # replay or already-settled invoice: nothing left to record
    await session.commit()
    return entry


# ── Public: pay + reconcile-on-return ────────────────────────────────────────

@public_router.get("/pay/{token}")
async def start_payment(token: str, session: AsyncSession = Depends(get_session)):
    company_id, entity_id, state = await _doc_for_token(session, token)
    if state is None:
        raise HTTPException(status_code=404, detail="Payment link not found")
    if not pay.payments_enabled():
        raise HTTPException(status_code=503, detail="Online payment is not available")
    if state.get("doc_type") not in _PAYABLE_TYPES or _outstanding(state) <= 0:
        raise HTTPException(status_code=409, detail="This document is not payable")

    base = (settings.celerp_public_url or "").rstrip("/")
    currency = state.get("currency", "USD")
    ref = state.get("ref_id") or state.get("doc_number") or entity_id.split(":")[-1][:8]
    result = await pay.create_checkout(
        amount_minor=to_minor_units(_outstanding(state), currency), currency=currency,
        description=f"Invoice {ref}",
        success_url=f"{base}/pay/{token}/return?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base}/share/{token}",
        metadata={"entity_id": entity_id, "company_id": str(company_id), "token": token},
    )
    if not result or not result.get("url"):
        raise HTTPException(status_code=502, detail="Could not start payment")
    return RedirectResponse(result["url"], status_code=303)


@public_router.get("/pay/{token}/return")
async def return_from_payment(token: str, session_id: str = Query(""),
                              session: AsyncSession = Depends(get_session)):
    """Customer's return from Stripe - reconcile immediately, then show the doc."""
    company_id, entity_id, state = await _doc_for_token(session, token, require_active=False)
    if state is not None and session_id:
        try:
            cs = await pay.checkout_status(session_id)
            if cs and cs.get("paid"):
                await record_stripe_payment(
                    session, company_id, entity_id, state,
                    reference=cs.get("reference"),
                    amount_minor=int(cs.get("amount_minor") or 0),
                    currency=cs.get("currency", state.get("currency", "USD")),
                )
        except Exception:
            log.warning("Payment reconcile-on-return failed", exc_info=True)
    return RedirectResponse(f"/share/{token}", status_code=303)


# ── Authed: merchant Connect status + one-click connect/disconnect ───────────

@router.get("/payments/enabled")
async def payments_enabled_flag(user=Depends(get_current_user)) -> dict:
    """Cached feature flag, in-memory: cheap enough for per-page-render gates
    (the settings page uses /payments/status for the authoritative answer)."""
    return {"enabled": pay.payments_enabled()}


@router.get("/payments/status")
async def payments_status(user=Depends(get_current_user)) -> dict:
    return await pay.connect_status()


@router.post("/payments/connect")
async def payments_connect(_: None = Depends(require_admin)) -> dict:
    """Begin Connect OAuth via Cloud; returns {url} for the UI to redirect to."""
    result = await pay.connect_start()
    if not result or not result.get("url"):
        raise HTTPException(status_code=502, detail="Could not start Stripe connection")
    return {"url": result["url"]}


@router.post("/payments/disconnect")
async def payments_disconnect(_: None = Depends(require_admin)) -> dict:
    return {"disconnected": await pay.disconnect()}
