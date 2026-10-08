# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Online invoice payment - Stripe checkout for a shared invoice, brokered by
Celerp Connect.

Public: /pay/{token} (start), /pay/{token}/return (back to the invoice). A payment
is recorded only when Celerp Cloud delivers it (``payments.receive_payment``), on
the books its payment page opened with. Authed: the merchant's payment connection
status + connect/disconnect. The instance stores no Stripe credentials; all money
records through the same path as a manual payment.
"""
from __future__ import annotations

import datetime
import logging
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import AccountRole
from celerp.db import get_session
from celerp.models.accounting import UserCompany
from celerp.models.company import Company
from celerp.models.projections import Projection
from celerp.services import payments as pay
from celerp.services.payments import ONLINE_DEPOSIT_ACCOUNT_KEY, WOOCOMMERCE_DEPOSIT_ACCOUNT_KEY
from celerp.services.account_roles import resolve
from celerp.services.auth import get_current_company_id, get_current_user, require_install_owner
from celerp.services.business_time import business_date_at, business_timezone
from celerp.services.doc_balance import outstanding_balance
from celerp.services.journal_accounts import require_settlement_account
from celerp.services.money import books_currency, checked_exchange_rate, require_doc_rate, round_money
from celerp.services.permissions import require_permission

log = logging.getLogger(__name__)

public_router = APIRouter()
router = APIRouter(dependencies=[Depends(get_current_user)])

# Only these can be paid online (a bill/PO is money you owe, not money owed to you).
_PAYABLE_TYPES = frozenset({"invoice", "proforma"})


async def _doc_for_token(session: AsyncSession, token: str):
    """Resolve a live share link to its document (a revoked or expired link must not
    take money)."""
    from celerp_docs.routes_share import _active_share_row
    share = await _active_share_row(session, token)
    if share is None:
        return None, None, None
    row = await session.get(Projection, (share.company_id, share.entity_id))
    if row is None:
        return None, None, None
    return share.company_id, share.entity_id, dict(row.state)


def _outstanding(state: dict) -> Decimal:
    """What the document still owes (``outstanding_balance``); 0, so not payable, when the
    recorded balance is not a number."""
    return outstanding_balance(state) or Decimal(0)


async def _company_owner_id(session: AsyncSession, company_id):
    return (await session.execute(
        select(UserCompany.user_id).where(
            UserCompany.company_id == company_id, UserCompany.role == "owner")
    )).scalars().first()


async def deposit_account(
    session: AsyncSession, company_id, *, override_key: str | None = None
) -> str:
    """GL account a received online payment clears to: the channel's own
    setting when one is chosen, else the company's online-payments default,
    else the default deposit account."""
    company = await session.get(Company, company_id)
    settings = (company.settings or {}) if company else {}
    return (
        (settings.get(override_key) if override_key else None)
        or settings.get(ONLINE_DEPOSIT_ACCOUNT_KEY)
        or await resolve(session, company_id, AccountRole.DEFAULT_DEPOSIT)
    )


async def require_online_deposit_account(session: AsyncSession, company_id, code: str) -> None:
    """422 unless online payments may be deposited to *code*: an active asset account of
    the company that is its default deposit account or behind one of its active bank
    accounts. The bank account and the chart account are read FOR SHARE, so neither can be
    archived or retyped while a payment posts to them."""
    from celerp_accounting.models import BankAccount
    default = await resolve(session, company_id, AccountRole.DEFAULT_DEPOSIT)
    try:
        if code != default and (await session.execute(select(BankAccount.id).where(
                BankAccount.company_id == company_id, BankAccount.chart_account_code == code,
                BankAccount.is_active.is_(True)).with_for_update(read=True))).first() is None:
            raise HTTPException(status_code=422, detail="No active bank account uses it.")
        await require_settlement_account(session, company_id, code)
    except HTTPException as refused:
        reason = refused.detail["message"] if isinstance(refused.detail, dict) else refused.detail
        raise HTTPException(status_code=422, detail=(
            f"Online payments can be deposited only to the default deposit account ({default}) or an active "
            f"bank account; '{code}' is neither. {reason}")) from None


_BOOKS = ("deposit_account", "timezone", "base_currency", "rate")


async def payment_books(session: AsyncSession, company_id, state: dict) -> dict:
    """The books an online payment on *state* is recorded on, fixed when its payment
    page opens: the deposit account, the business timezone that dates it, the company
    currency and the document's rate. ValueError when the document cannot be paid in
    the company's books (no usable timezone or rate)."""
    company = await session.get(Company, company_id)
    settings = (company.settings or {}) if company else {}
    base = books_currency(settings)
    return {"deposit_account": await deposit_account(session, company_id),
            "timezone": business_timezone(settings.get("timezone")).key,
            "base_currency": base, "rate": str(require_doc_rate(state, base))}


async def _checked_books(session: AsyncSession, company_id, books) -> tuple[str, str, str, Decimal]:
    """(deposit account, timezone, base currency, rate) from the books a payment page
    opened with, or 422 when they are missing or unusable. Whether they still describe
    the company's ledger, and whether the deposit account may still take online
    payments, is judged when the payment is recorded, under the document's lock
    (apply_doc_payment)."""
    if not (isinstance(books, dict) and all(isinstance(books.get(k), str) and books[k] for k in _BOOKS)):
        raise HTTPException(status_code=422, detail="The payment carries no books to record it on")
    try:
        timezone, rate = business_timezone(books["timezone"]).key, checked_exchange_rate(books["rate"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return books["deposit_account"], timezone, books["base_currency"].upper(), rate


async def record_stripe_payment(session, company_id, entity_id, doc_state, *,
                                reference: str, amount_minor: int, currency: str,
                                paid_at: datetime.datetime | None, context, managed: bool,
                                idempotency_key: str | None = None):
    """Record a confirmed online charge as a payment on its invoice. Only
    ``payments.receive_payment`` calls it, and ``payments.record_unmatched_payment``
    for one a person records on an invoice from the unmatched payments, under an
    *idempotency_key* of its own (each recording is one; the charge is *reference*).

    A *managed* payment is Stripe's to manage (``stripe_receipt_references``): it is
    recorded on *context*, the books its payment page opened with
    (``payment_books``), and dated their business day at *paid_at*, when Stripe
    reported it paid, so a payment recorded again after a System Recovery posts
    exactly as it first did. Without usable books or *paid_at* it is refused (422),
    never recorded on today's settings. A payment taken on a page opened before
    pages carried their books is the company's to manage: it is recorded on the
    books the invoice is paid on now, dated when Stripe reported it paid, or now
    when that was not reported.

    The same charge (Stripe payment_intent) recorded again is a quiet None. A charge
    the invoice cannot take whole (it is already paid, or owes less than the charge)
    raises the invoice's 409: apply_doc_payment decides that under the document's
    row lock, against what it owes at that moment. The caller commits, so the refunds
    Stripe reported before the payment could be recorded apply with it."""
    if not reference:
        return None
    if any(p.get("reference") == reference and p.get("status") != "deleted"
           for p in doc_state.get("payments", [])):
        return None  # already recorded - a repeated delivery
    if not managed:
        try:
            context = await payment_books(session, company_id, doc_state)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        paid_at = paid_at or datetime.datetime.now(datetime.timezone.utc)
    account, timezone, base, rate = await _checked_books(session, company_id, context)
    if paid_at is None:
        raise HTTPException(status_code=422, detail="The payment carries no time it was paid")
    try:
        amount = pay.from_stripe_amount(amount_minor, currency)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    from celerp_docs.routes import apply_doc_payment
    body = {"amount": float(amount), "payment_date": business_date_at(paid_at, timezone),
            "currency": currency.upper(), "bank_account": account, "conversion_rate": float(rate),
            "method": "stripe", "reference": reference}
    if managed:
        body["stripe_managed"] = True
    try:
        entry, _amount = await apply_doc_payment(
            session, company_id, entity_id, body,
            source="stripe", actor_id=await _company_owner_id(session, company_id),
            idempotency_key=idempotency_key or reference, books=(base, rate), commit=False,
        )
    except HTTPException as exc:
        if exc.status_code == 409 and exc.detail == "Payment already recorded":
            return None
        raise
    if getattr(entry, "was_deduped", False):
        return None
    return entry


async def recorded_payment_books(session, company_id, row: Projection, reference: str) -> dict | None:
    """The books the online payment *reference* was recorded on, on the document
    *row*, as a payment page carries them (``payment_books``): the books a refund of
    it gives the money back on. None when the payment is not on the document."""
    payment = next((p for p in row.state.get("payments", [])
                    if p.get("reference") == reference and p.get("status") == "active"), None)
    if payment is None:
        return None
    company = await session.get(Company, company_id)
    settings = (company.settings or {}) if company else {}
    return {"deposit_account": payment.get("bank_account"),
            "timezone": business_timezone(settings.get("timezone")).key,
            "base_currency": (payment.get("books") or {}).get("base_currency") or books_currency(settings),
            "rate": str(payment.get("conversion_rate"))}


def unmatched_refusal(state: dict, amount: Decimal, currency: str) -> str | None:
    """Why the document *state* cannot take a whole payment of *amount* in *currency*
    (``pay.unmatched_refused_<reason>``), or None when it can: the checks
    apply_doc_payment makes, so the unmatched payments offer only invoices that take
    the payment and refuse the others with the reason."""
    from celerp_docs.routes import PAYABLE_STATUSES
    if state.get("doc_type") not in _PAYABLE_TYPES:
        return "not_invoice"
    if state.get("status") not in PAYABLE_STATUSES:
        return "status"
    if str(state.get("currency") or "USD").upper() != currency.upper():
        return "currency"
    outstanding = round_money(_outstanding(state), currency.upper())
    if outstanding <= 0:
        return "paid"
    return "too_small" if outstanding < amount else None


REFUND_TRANSITIONS = ("applied", "reversed")


NOT_LINKED_TO_STRIPE = "The payment is no longer linked to Stripe"


def stripe_refund_key(refund_id: str, cycle: int, transition: str) -> str:
    """The ledger name of one change to a Stripe refund, in the *cycle* Stripe put
    the refund through."""
    return f"stripe-refund:{refund_id}:{cycle}:{transition}"


async def record_stripe_refund(session, company_id, row: Projection, *, refund_id: str, cycle: int,
                               transition: str, reference: str, amount_minor: int, currency: str,
                               occurred_at: datetime.datetime | None, context):
    """Apply a change Stripe reported to a refund of the online payment *reference* on
    the locked document *row*. Only ``payments.receive_refund`` and the drain of
    parked refunds call it; the caller commits.

    "applied" gives the money back from the payment (``apply_payment_refund``) on
    *context*, the books the payment was recorded on, dated their business day at
    *occurred_at*. "reversed" undoes the refund applied in the same *cycle* with one
    entry on the same books; a refund Stripe puts through again is applied again in
    its next cycle. The same change recorded again is a quiet None. A change that
    cannot be applied raises a 4xx and is kept for later: the payment is not on the
    invoice or is no longer linked to Stripe, the refund was never applied in that
    cycle, it gives back more than is left of the payment (never cut down to fit),
    the company now keeps its books in another currency, or it carries no usable
    books, currency or time. A change Stripe made before the payment stopped being
    linked to it applies whenever it arrives; one made after is never applied
    here."""
    from celerp.events.engine import find_event_by_idempotency, stripe_receipt_references
    from celerp_docs.routes import PaymentBooks, apply_payment_refund, books_currency_still, reverse_payment_refund
    entity_id = row.entity_id
    key = stripe_refund_key(refund_id, cycle, transition)
    if await find_event_by_idempotency(session, company_id, key) is not None:
        return None
    account, timezone, base, rate = await _checked_books(session, company_id, context)
    if occurred_at is None:
        raise HTTPException(status_code=422, detail="The refund carries no time it happened")
    doc_currency = str(row.state.get("currency") or "USD").upper()
    if currency.upper() != doc_currency:
        raise HTTPException(status_code=422, detail=f"Refund currency {currency.upper()} does not match "
                                                    f"document currency {doc_currency}")
    payment = next((p for p in row.state.get("payments", [])
                    if p.get("reference") == reference and p.get("status") == "active"), None)
    if payment is None or payment.get("method") != "stripe":
        raise HTTPException(status_code=409, detail="The refunded payment is not on this document")
    released_at = payment.get("stripe_released_at")
    if (reference not in await stripe_receipt_references(session, company_id, entity_id, managed=True)
            or (released_at and occurred_at >= datetime.datetime.fromisoformat(released_at))):
        raise HTTPException(status_code=409, detail=NOT_LINKED_TO_STRIPE)
    if payment.get("bank_account") != account or Decimal(str(payment.get("conversion_rate"))) != rate:
        raise HTTPException(status_code=422, detail="The refund carries other books than its payment was recorded on")
    try:
        amount = pay.from_stripe_amount(amount_minor, currency)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await books_currency_still(session, company_id, base)
    day = business_date_at(occurred_at, timezone)
    actor = await _company_owner_id(session, company_id)
    books = PaymentBooks(bank_account=account, base_currency=base, doc_rate=float(rate), settlement_rate=float(rate))
    if transition == "applied":
        return await apply_payment_refund(
            session, company_id, entity_id, row, payment, amount=float(amount), refund_date=day, books=books,
            data={"method": "stripe", "reference": reference, "refund_id": refund_id},
            actor_id=actor, source="stripe", idempotency_key=key)
    applied = await find_event_by_idempotency(session, company_id, stripe_refund_key(refund_id, cycle, "applied"))
    if applied is None:
        raise HTTPException(status_code=409, detail="The refund being reversed was never applied")
    refund = dict(applied.data or {})
    if refund.get("reference") != reference or Decimal(str(refund.get("amount"))) != amount:
        raise HTTPException(status_code=422, detail="The reversal does not match the refund it reverses")
    return await reverse_payment_refund(session, company_id, entity_id, row, payment, refund,
                                        reversal_date=day, books=books, actor_id=actor, source="stripe",
                                        idempotency_key=key)


async def record_stripe_release(session, company_id, row: Projection, *, reference: str,
                                released_at: datetime.datetime) -> bool:
    """Record on the document *row* that the online payment *reference* is no
    longer linked to Stripe: Stripe was disconnected, so it is refunded or voided
    here from now on, for good. Only ``payments.receive_release`` and the
    payment intake call it; the caller commits. The owner is told, once. False when
    the payment is not on the document; recording it again is a quiet True."""
    from celerp.events.engine import emit_event
    from celerp.notifications import service as notif_service
    payment = next((p for p in row.state.get("payments", [])
                    if p.get("reference") == reference and p.get("method") == "stripe"
                    and p.get("status") != "deleted"), None)
    if payment is None:
        return False
    if payment.get("stripe_released_at"):
        return True
    entry = await emit_event(
        session, company_id=company_id, entity_id=row.entity_id, entity_type="doc",
        event_type="doc.payment.stripe_released",
        data={"payment_index": payment["index"], "reference": reference, "released_at": released_at.isoformat()},
        actor_id=await _company_owner_id(session, company_id), location_id=None, source="stripe",
        idempotency_key=f"stripe-release:{reference}")
    if not getattr(entry, "was_deduped", False):
        ref = _doc_ref(row.state) or row.entity_id
        await notif_service.create(
            session, company_id, "connector", "A payment is no longer linked to Stripe",
            f"Stripe is disconnected, so the online payment on {ref} is no longer linked to it. "
            "Record any refund of it here in Celerp.",
            action_url=f"/docs/{row.entity_id}", priority="high")
    return True


# ── Public: pay, and the customer's return ───────────────────────────────────

_NOT_SET_UP = "Online payment is not set up for this invoice. Please contact the business that sent it."


@public_router.get("/pay/{token}")
async def start_payment(token: str, session: AsyncSession = Depends(get_session)):
    company_id, entity_id, state = await _doc_for_token(session, token)
    if state is None:
        raise HTTPException(status_code=404, detail="Payment link not found")
    if not pay.payments_enabled():
        raise HTTPException(status_code=503, detail="Online payment is not available")
    if state.get("doc_type") not in _PAYABLE_TYPES or _outstanding(state) <= 0:
        raise HTTPException(status_code=409, detail="This document is not payable")
    currency = state.get("currency", "USD")
    ref = _doc_ref(state) or entity_id.split(":")[-1][:8]
    try:
        books = await payment_books(session, company_id, state)
    except ValueError:
        raise HTTPException(status_code=409, detail="This document is not payable")
    try:
        amount = pay.to_stripe_amount(_outstanding(state), currency)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        await require_online_deposit_account(session, company_id, books["deposit_account"])
    except HTTPException as exc:
        log.warning("Online payment refused for %s: %s", entity_id, exc.detail)
        raise HTTPException(status_code=409, detail=_NOT_SET_UP) from exc
    try:
        result = await pay.create_checkout(
            amount_minor=amount, currency=currency,
            description=f"Invoice {ref}",
            company_id=str(company_id), entity_id=entity_id, share_token=token,
            generation=await pay.checkout_generation(), context=books,
        )
    except pay.CheckoutPaused:
        raise HTTPException(status_code=409, detail=pay.PAUSED)
    if not _stripe_url(result):
        raise HTTPException(status_code=502, detail="Could not start payment")
    return RedirectResponse(result["url"], status_code=303)


@public_router.get("/pay/{token}/return")
async def return_from_payment(token: str):
    """Customer's return from Stripe: back to the invoice. Nothing is recorded here;
    Celerp Cloud delivers the payment once Stripe reports it paid."""
    return RedirectResponse(f"/share/{token}", status_code=303)


# ── Authed: merchant Connect status + one-click connect/disconnect ───────────

@router.get("/payments/enabled")
async def payments_enabled_flag(user=Depends(get_current_user)) -> dict:
    """Cached feature flag, in-memory: cheap enough for per-page-render gates
    (the settings page uses /payments/status for the authoritative answer)."""
    return {"enabled": pay.payments_enabled()}


def _stripe_url(result) -> bool:
    """Cloud answered with an https address to send the user to."""
    url = result.get("url") if isinstance(result, dict) else None
    return isinstance(url, str) and url.startswith("https://")


@router.get("/payments/status")
async def payments_status(user=Depends(get_current_user)) -> dict:
    return await pay.connect_status()


@router.post("/payments/connect", dependencies=[Depends(require_install_owner)])
async def payments_connect() -> dict:
    """Begin Connect OAuth via Cloud; returns {url} for the UI to redirect to."""
    result = await pay.connect_start()
    if not _stripe_url(result):
        raise HTTPException(status_code=502, detail="Could not start Stripe connection")
    return {"url": result["url"]}


@router.post("/payments/disconnect", dependencies=[Depends(require_install_owner)])
async def payments_disconnect() -> dict:
    """What Cloud did: disconnected, or disconnecting while existing payments finish."""
    result = await pay.disconnect()
    if result is None:
        raise HTTPException(status_code=502, detail="Could not disconnect Stripe")
    return result


@router.get("/payments/unmatched", dependencies=[Depends(require_install_owner)])
async def payments_unmatched(company_id=Depends(get_current_company_id),
                             session: AsyncSession = Depends(get_session)) -> dict:
    """Online payments received for a company or invoice that no longer exists, or
    that the invoice refused, and the refunds of online payments kept until their
    payment is on its invoice, each newest first. Each date is a business day in the
    timezone of the company the row books in: its own company while that exists,
    else this one, which recording it from here books it on; none when that
    timezone is not usable."""
    payments, refunds = await pay.unmatched_payments(session), await pay.unmatched_refunds(session)
    names, books_company = await _names_still_here(session, [*payments, *refunds])
    here = await session.get(Company, company_id)

    def day(row, instant: datetime.datetime | None) -> str | None:
        company = books_company(row) or here
        try:
            timezone = business_timezone(((company.settings or {}) if company else {}).get("timezone")).key
        except ValueError:
            return None
        return business_date_at(instant, timezone) if instant else None

    return {"items": [{
        "reference": p.reference, "amount": float(pay.stripe_amount(p.amount_minor, p.currency)),
        "currency": p.currency, **names(p), "received_on": day(p, p.received_at), "paid_on": day(p, p.paid_at),
    } for p in payments], "refunds": [{
        "refund_id": r.refund_id, "cycle": r.cycle, "transition": r.transition, "reference": r.reference,
        "amount": float(pay.stripe_amount(r.amount_minor, r.currency)), "currency": r.currency,
        **names(r), "received_on": day(r, r.received_at), "refunded_on": day(r, r.occurred_at),
    } for r in refunds]}


# Why an unmatched payment was not recorded on the chosen invoice
# (``pay.UnmatchedRefused``); the web app shows them in the user's language.
UNMATCHED_REFUSALS = {
    "gone": "This payment is no longer with the unmatched payments.",
    "other_company": "This payment was made to another company. Switch to that company to record it there.",
    "missing": "This invoice no longer exists.",
    "not_invoice": "Only an invoice can take this payment.",
    "status": "This invoice cannot take a payment in its current status.",
    "currency": "This invoice is in another currency than the payment.",
    "paid": "This invoice is already paid.",
    "too_small": "This invoice owes less than the payment.",
    "refused": "This invoice cannot take this payment.",
}


class UnmatchedRecordBody(BaseModel):
    reference: str
    entity_id: str


_UNMATCHED_RECORDERS = [Depends(require_install_owner), require_permission("record_payments")]


def _unmatched_refused(reason: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"message": UNMATCHED_REFUSALS[reason], "reason": reason})


@router.get("/payments/unmatched/invoices", dependencies=_UNMATCHED_RECORDERS)
async def payments_unmatched_invoices(reference: str, company_id=Depends(get_current_company_id),
                                      session: AsyncSession = Depends(get_session)) -> dict:
    """The invoices of the current company the unmatched payment *reference* can be
    recorded on: open, in its currency, owing at least the whole payment
    (``unmatched_refusal``), by reference."""
    kept = await pay.unmatched_payment(session, reference)
    if kept is None:
        raise _unmatched_refused("gone")
    try:
        await pay.refuse_other_company(session, kept, company_id)
    except pay.UnmatchedRefused as refused:
        raise _unmatched_refused(refused.reason) from None
    amount = pay.unmatched_amount(kept)
    rows = (await session.scalars(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "doc",
        Projection.state["doc_type"].as_string().in_(_PAYABLE_TYPES)))).all()
    items = [{"id": r.entity_id, "ref": _doc_ref(r.state) or r.entity_id,
              "contact_name": r.state.get("contact_name"), "outstanding": float(_outstanding(r.state)),
              "currency": kept.currency}
             for r in rows if unmatched_refusal(r.state, amount, kept.currency) is None]
    return {"items": sorted(items, key=lambda i: i["ref"])}


@router.post("/payments/unmatched/record", dependencies=_UNMATCHED_RECORDERS)
async def payments_unmatched_record(body: UnmatchedRecordBody, company_id=Depends(get_current_company_id),
                                    session: AsyncSession = Depends(get_session)) -> dict:
    """Record the unmatched payment on an invoice of the current company
    (``payments.record_unmatched_payment``): 409 with the reason when it cannot be,
    and nothing is recorded. Recording it on the same invoice again changes nothing."""
    try:
        await pay.record_unmatched_payment(session, company_id, body.reference, body.entity_id)
    except pay.UnmatchedRefused as refused:
        raise _unmatched_refused(refused.reason) from None
    await session.commit()
    return {"reference": body.reference, "entity_id": body.entity_id}


def _doc_ref(state: dict) -> str | None:
    """The reference a person knows a document by, when it has one."""
    return state.get("ref_id") or state.get("doc_number")


async def _names_still_here(session: AsyncSession, rows: list):
    """For unmatched *rows*: a function giving each row's company and document ids
    with the company's name and the document's reference, each None once it no
    longer exists here (the rows outlive both), and one giving the row's Company
    while it exists."""
    ids = {}
    for row in rows:
        try:
            ids[row.former_company] = uuid.UUID(row.former_company)
        except ValueError:
            pass
    companies, docs = {}, {}
    if ids:
        companies = {c.id: c for c in (await session.scalars(
            select(Company).where(Company.id.in_(ids.values())))).all()}
        docs = {(d.company_id, d.entity_id): _doc_ref(d.state) or d.entity_id for d in (await session.scalars(
            select(Projection).where(Projection.company_id.in_(ids.values()),
                                     Projection.entity_id.in_({row.document for row in rows})))).all()}

    def names(row) -> dict:
        cid = ids.get(row.former_company)
        company = companies.get(cid)
        return {"company_id": row.former_company, "company_name": company.name if company else None,
                "document_id": row.document, "document_ref": docs.get((cid, row.document))}
    return names, lambda row: companies.get(ids.get(row.former_company))
