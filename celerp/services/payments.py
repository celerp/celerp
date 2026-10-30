# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Online invoice payment via Stripe, brokered by Celerp Connect.

The instance stores no Stripe credentials: it asks Celerp Connect to create a Checkout
Session on the merchant's connected account. Payment surfaces are gated on a
"payments enabled" flag; Celerp Cloud delivers each confirmed payment, which records
through the same path as a manual payment.

A delivery (payment, fees, refund, release) is recorded on invoices, so while the
company it is written to cannot use Documents (turned off, or not started yet) it is
refused before anything is written (``needs_modules``) and not acknowledged.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import delete, func, select

from celerp.models.payment_closure import PaymentClosure, PaymentRecovery, UnmatchedPayment, UnmatchedRefund
from celerp.modules.boundary import ModuleRefused, judge_writes, needs_modules, require_company_uses
from celerp.services.money import round_money, to_decimal
from ui.i18n import t

log = logging.getLogger(__name__)

# A delivery needs Documents on the company it names. A payment recorded on another
# company's invoice is judged by that company once it is known (_home_uses_documents).
_on_invoices = needs_modules("celerp-docs", company=lambda payload: payload.get("company_id"))


async def _home_uses_documents(session, home: tuple[uuid.UUID, str]) -> None:
    """A payment a person recorded on another company's invoice is written there, not
    to the company its deliveries name: refused the same way, before anything is
    written, while that company has Documents turned off, and judged again by every write
    made there (``judge_writes``)."""
    from celerp.models.company import Company
    company = await session.get(Company, home[0])
    require_company_uses(company.settings if company is not None else None, "celerp-docs")
    judge_writes(home[0], "celerp-docs")

# GL account online payments clear to, per channel and for online payments generally.
# With neither chosen, payments land on the company's default deposit account.
ONLINE_DEPOSIT_ACCOUNT_KEY = "stripe_deposit_account"
WOOCOMMERCE_DEPOSIT_ACCOUNT_KEY = "woocommerce_deposit_account"


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


# ── Stripe amount units ──────────────────────────────────────────────────────
# Stripe's own unit rules (docs.stripe.com/currencies), which are not the books'
# precision (money.currency_dp): MGA is kept in hundredths but charged in whole
# ariary, and ISK and UGX are counted in hundredths but charged in whole units.
# Every amount sent to or received from Stripe crosses here.

_STRIPE_ZERO_DECIMAL = frozenset(
    "BIF CLP DJF GNF JPY KMF KRW MGA PYG RWF VND VUV XAF XOF XPF".split())
_STRIPE_THREE_DECIMAL = frozenset("BHD JOD KWD OMR TND".split())  # last digit always 0
_STRIPE_WHOLE_UNITS = frozenset({"ISK", "UGX"})  # two decimals in the API, always 00


def _stripe_exponent(currency: str) -> int:
    code = currency.upper()
    if code in _STRIPE_ZERO_DECIMAL:
        return 0
    return 3 if code in _STRIPE_THREE_DECIMAL else 2


def stripe_amount(api_amount: int, currency: str) -> Decimal:
    """*api_amount*, an integer amount in Stripe's units, as an amount of *currency*."""
    return Decimal(api_amount).scaleb(-_stripe_exponent(currency))


def to_stripe_amount(amount, currency: str) -> int:
    """*amount* of *currency* as the integer Stripe charges. ValueError, never rounding,
    when the books and Stripe cannot both hold it exactly: not positive, finer than the
    books keep *currency* or than Stripe charges it, a fraction of an ISK or UGX, or a
    three-decimal amount not ending in 0."""
    value = to_decimal(amount)
    scaled = value.scaleb(_stripe_exponent(currency))
    code = currency.upper()
    if (value <= 0 or value != round_money(value, code) or scaled != scaled.to_integral_value()
            or (code in _STRIPE_WHOLE_UNITS and value != value.to_integral_value())
            or (code in _STRIPE_THREE_DECIMAL and scaled % 10)):
        raise ValueError(t("error.stripe_amount", value=value, code=code))
    return int(scaled)


def from_stripe_amount(api_amount: int, currency: str) -> Decimal:
    """*api_amount*, as Stripe reports a charge, as an amount of *currency*: the exact
    inverse of ``to_stripe_amount``, and ValueError for any integer it would not produce."""
    value = stripe_amount(api_amount, currency)
    try:
        to_stripe_amount(value, currency)
    except ValueError as exc:
        raise ValueError(t("error.stripe_amount_inexact", value=api_amount, code=currency.upper())) from exc
    return value


# ── Payment (customer-facing, via the hosted invoice view) ───────────────────

class CheckoutPaused(Exception):
    """New online payments wait for a System Recovery restore Celerp Cloud has not
    confirmed: until it has, a payment the restore lost may not be recorded again yet,
    so an invoice it paid could look unpaid."""


async def checkout_generation() -> int:
    """The installation's current payment generation, which every new payment carries.
    Raises CheckoutPaused while a System Recovery restore is unconfirmed."""
    try:
        async with _own_session() as session:
            generation = await report_recoveries(session)
    except PaymentsNotClosed:
        generation = None
    except Exception:
        log.warning("Checking for unconfirmed System Recovery restores failed", exc_info=True)
        generation = None
    if generation is None:
        raise CheckoutPaused
    return generation


async def create_checkout(*, amount_minor: int, currency: str, description: str,
                          company_id: str, entity_id: str,
                          share_token: str, generation: int, context: dict) -> dict | None:
    """Ask Cloud to open a Checkout Session on the merchant's connected account, for
    the installation's payment *generation*. *context* is the books the payment will
    be recorded on; Cloud keeps it and returns it with every delivery of the payment.

    Returns {"url": <stripe checkout url>} (redirect the customer there), or None on
    failure. Raises CheckoutPaused when Cloud holds new payments for a restore.
    `amount_minor` is the balance due in Stripe's units (``to_stripe_amount``).
    """
    from celerp.config import settings
    if settings.cloud_disconnected:
        return None
    answer = await _cloud_answer("/billing/connect/checkout", {
        "amount_minor": amount_minor, "currency": currency,
        "description": description[:250],
        "company_id": company_id, "entity_id": entity_id,
        "share_token": share_token, "generation": generation, "context": context,
    })
    if answer is not None and answer[0] == 409 and answer[1].get("detail") in ("generation_stale", "recovery_pending"):
        raise CheckoutPaused
    return answer[1] if answer is not None and answer[0] == 200 else None


class PaymentsNotClosed(Exception):
    """Celerp Cloud did not confirm that a company's online payments are closing.

    ``reason`` is "disconnected", "payment_settling", "payment_unrecorded",
    "reconnect_required" (a payment can be checked only once the merchant reconnects
    the Stripe account they withdrew), "update_required" (a refund waits for a
    version of Celerp that can record it), "fees_settling" (a payment's or refund's
    provider fees are not reported or recorded yet) or "unconfirmed".
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_CLOSURE = "/billing/connect/companies/retire"
_RECOVERY = "/billing/connect/recovery"
_PREPARED = ("prepared", "retired")
_REFUSED = ("payment_settling", "payment_unrecorded", "reconnect_required", "update_required",
            "fees_settling")
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


@_on_invoices
async def receive_payment(payload: dict) -> bool:
    """Record an online payment delivered by Celerp Cloud: on its invoice, on the
    books its payment page opened with and the day it was paid (``managed``: Stripe
    manages it; one paid on a page opened before pages carried their books is the
    company's, see ``record_stripe_payment``), when the invoice can
    take the whole charge, otherwise among the unmatched payments, whole (the company
    or the invoice no longer exists, the invoice is already paid, it owes less than
    the charge, or it refuses it, as it does a delivery without usable books). True once recorded
    either way (Cloud is then told it arrived), False for a delivery that names no
    payment, or no whole positive amount in a named currency (``_counted``). Raises when nothing could be recorded, so Cloud delivers it again.
    Recording the same payment twice changes nothing.

    Every delivery tries the invoice again, even for a payment already among the
    unmatched: one the invoice now takes, or already holds, leaves the unmatched
    payments in the same transaction that records it, and the refunds of it kept
    until then apply in that transaction too, in the order Stripe reported them. A
    payment kept after it stopped being linked to Stripe stops being linked to
    Stripe with it (``receive_release``). One a person recorded on another invoice
    (``record_unmatched_payment``) stays there."""
    from fastapi import HTTPException
    from celerp.models.projections import Projection
    from celerp_docs.routes_payments import record_stripe_payment
    company_id, entity_id, reference = (str(payload.get(k) or "") for k in ("company_id", "entity_id", "reference"))
    amount_minor, currency = payload.get("amount_minor"), payload.get("currency")
    if not (company_id and entity_id and reference and _counted(amount_minor)
            and isinstance(currency, str) and currency.strip()):
        return False
    managed = payload.get("managed") is True
    currency = currency.strip().upper()
    try:
        paid_at = datetime.fromisoformat(str(payload.get("paid_at")))
    except ValueError:
        paid_at = None
    if paid_at is not None and paid_at.utcoffset() is None:
        paid_at = None  # no zone, so no business day it can be placed on
    cid = _company(company_id)
    async with _own_session() as session:
        home = await _recorded_on(session, reference)
        if home is not None:
            await _home_uses_documents(session, home)
            await _deliver_to_recorded(session, home, reference, origin=(company_id, entity_id))
            await session.commit()
            return True
        # A reset waits for this lock; once it has deleted the company, the payment is unmatched.
        row = await session.get(Projection, (cid, entity_id)) if cid and await _lock_company_of(session, cid) else None
        if row is not None:
            kept = (await session.execute(delete(UnmatchedPayment).where(
                UnmatchedPayment.reference == reference).returning(
                    UnmatchedPayment.released_at, UnmatchedPayment.recorded_company))).first()
            if kept is not None and kept.recorded_company is not None:
                raise _Moved(reference)
            released_at = kept.released_at if kept is not None else None
            try:
                await record_stripe_payment(session, cid, entity_id, dict(row.state), reference=reference,
                                            amount_minor=amount_minor, currency=currency, paid_at=paid_at,
                                            context=payload.get("context"), managed=managed)
                if released_at is None:
                    await _apply_parked_refunds(session, cid, entity_id, reference)
                else:
                    await _apply_release(session, cid, entity_id, reference, released_at)
                await session.commit()  # already recorded: only the unmatched row goes
                return True
            except HTTPException as exc:
                if exc.status_code >= 500 or isinstance(exc, ModuleRefused):
                    raise
                await session.rollback()
                log.error("Invoice %s refused online payment %s: %s", entity_id, reference, exc.detail)
        from sqlalchemy.dialects.postgresql import insert
        await session.execute(insert(UnmatchedPayment).values(
            reference=reference, amount_minor=amount_minor, currency=currency,
            former_company=company_id, document=entity_id, paid_at=paid_at).on_conflict_do_nothing())
        await session.commit()
    return True


@_on_invoices
async def receive_fees(payload: dict) -> bool:
    """Record actual online-fee transactions on the books holding their gross receipt.

    A fee delivered before its payment is left pending, not silently lost. A fee that can
    never be recorded is acknowledged: with a notice to record it by hand when its
    document or payment was deleted, its payment is among the unmatched payments, or the
    payment's recorded books cannot take it, and silently when its company no longer
    exists (nobody is left to tell).
    """
    from datetime import timezone
    from celerp.events.engine import find_event_by_idempotency
    from celerp.models.projections import Projection
    from celerp.services import auto_je

    if not isinstance(payload, dict):
        return False
    reference = payload.get("reference")
    doc_id = payload.get("entity_id")
    origin = payload.get("company_id")
    fees = payload.get("fees")
    if (not isinstance(reference, str) or not reference or len(reference) > 255
            or not isinstance(doc_id, str) or not doc_id or len(doc_id) > 512
            or not isinstance(origin, str) or not _company(origin)
            or not isinstance(fees, dict) or fees.get("version") != 1):
        return False
    tx = fees.get("transaction_id")
    currency = fees.get("currency")
    names = ("processor_minor", "platform_minor", "other_minor", "total_minor")
    if (not isinstance(tx, str) or not 1 <= len(tx) <= 128
            or not tx.isascii() or not all(c.isalnum() or c in "_-" for c in tx)
            or not isinstance(currency, str) or len(currency) != 3
            or not currency.isascii() or not currency.isalpha()
            or any(type(fees.get(key)) is not int or abs(fees[key]) > 10**14 for key in names)
            or sum(fees[key] for key in names[:3]) != fees["total_minor"]):
        return False
    try:
        when = datetime.fromisoformat(str(payload.get("occurred_at")))
    except (ValueError, TypeError):
        return False
    if when.utcoffset() is None:
        return False
    home = None
    async with _own_session() as session:
        home = await _recorded_on(session, reference)
        if home is not None:
            await _home_uses_documents(session, home)
        cid, target_doc = home if home is not None else (_company(origin), doc_id)
        if not await _lock_company_of(session, cid):
            return True
        row = (await session.execute(select(Projection).where(
            Projection.company_id == cid,
            Projection.entity_id == target_doc,
            Projection.entity_type == "doc",
        ).with_for_update())).scalar_one_or_none()
        state = (row.state or {}) if row is not None else {}
        entries = [p for p in state.get("payments", [])
                   if p.get("reference") == reference and p.get("method") == "stripe"]
        payment = next((p for p in entries if p.get("status") != "deleted"), None)
        if row is not None and not entries:
            parked = await session.scalar(select(UnmatchedPayment.reference).where(
                UnmatchedPayment.reference == reference))
            if parked is None:
                return False
        books = (payment or {}).get("books") or {}
        settled_currency = str((payment or {}).get("currency") or state.get("currency") or "").lower()
        base_currency = str(books.get("base_currency") or "").lower()
        document = state.get("ref_id") or state.get("doc_number") or target_doc
        if not entries:
            problem = "unmatched" if row is not None else "deleted"
        elif payment is None:
            problem = "deleted"
        elif currency.lower() not in (settled_currency, base_currency):
            problem = "currency"
        elif not (isinstance(books, dict) and books.get("base_currency")
                  and payment.get("bank_account") and payment.get("conversion_rate")):
            problem = "books"
        else:
            from celerp.services.account_roles import PostingRoleError
            from celerp_docs.routes_payments import _company_owner_id
            actor = await _company_owner_id(session, cid)
            try:
                account = await auto_je.create_for_online_fees(
                    session, company_id=cid, user_id=actor, doc_id=target_doc,
                    reference=reference, payment=payment, fees=fees,
                    occurred_at=when.astimezone(timezone.utc),
                )
            except PostingRoleError as exc:
                await session.rollback()
                roles = [p["params"]["role"] for p in exc.detail["params"]["problems"]]
                await _fee_not_recorded(cid, target_doc, document, fees, "account", roles)
                return False
            except ValueError:
                await session.rollback()
                await _fee_not_recorded(cid, target_doc, document, fees, "conflict")
                raise
            if account is not None:
                await _fee_recorded(session, cid, target_doc, document, fees, account)
            await session.commit()
            return True
    told = await _fee_not_recorded(cid, target_doc if row is not None else None, document, fees, problem,
                                   reference=reference)
    return problem in _FEE_UNRECORDABLE and told


# Causes no later fix can reach: the document or payment is gone, or the payment's own
# recorded books (currency, bank account, rate) cannot take the fee.
_FEE_UNRECORDABLE = ("deleted", "unmatched", "currency", "books")


def _fee_amount(fees: dict) -> str:
    amount = abs(stripe_amount(fees["total_minor"], fees["currency"]))
    return f"{amount:.{_stripe_exponent(fees['currency'])}f} {fees['currency'].upper()}"


async def _fee_recorded(session, cid, doc_id: str, document: str, fees: dict, account: str) -> None:
    """Tell the company a fee was booked on its behalf, once per provider transaction
    (the posting itself is idempotent, so this runs only when the fee was just posted)."""
    from celerp.notifications import service as notification_service
    from celerp.services.posting_readiness import account_names

    name = (await account_names(session, cid, [account])).get(account)
    key = "notice.online_fee_recorded" if fees["total_minor"] > 0 else "notice.online_fee_returned"
    await notification_service.create_keyed(
        session, cid, "accounting", key,
        {"amount": _fee_amount(fees), "document": document,
         "account": f"{account} {name}" if name else account},
        action_url=f"/docs/{doc_id}")


async def _fee_not_recorded(cid, doc_id: str | None, document: str, fees: dict, problem: str,
                            roles=(), reference: str = "") -> bool:
    """Tell the company, unmissably and once per fee and cause, that a fee Celerp Cloud
    delivered could not be recorded and why; whether the notice was recorded. A fee whose
    cause is in _FEE_UNRECORDABLE is acknowledged once told (retrying cannot help, and
    *doc_id* is None when the document is gone); any other cause is not, so the fee is
    delivered again and recorded once the cause is fixed. A refused posting account also
    raises the standing posting-accounts notice (posting_readiness.notify_unmapped)."""
    from celerp.notifications import service as notification_service
    from celerp.services.posting_readiness import notify_unmapped

    reason = f"notice.online_fee_failed.{problem}"
    reason_params = {"reference": reference} if problem == "unmatched" else {}
    params = {"amount": _fee_amount(fees), "document": document, "transaction": fees["transaction_id"],
              "reason": t(reason, "en", **reason_params)}
    keyed = {**params, "reason": {"message": params["reason"], "message_key": reason, "params": reason_params}}
    try:
        async with _own_session() as session:
            await notification_service.notify_once(
                session, cid, "accounting",
                t("notice.online_fee_failed.title", "en", **params),
                t("notice.online_fee_failed.body", "en", **params),
                action_url=f"/docs/{doc_id}" if doc_id else None,
                i18n={"title": "notice.online_fee_failed.title", "body": "notice.online_fee_failed.body",
                      "params": keyed})
            if roles:
                await notify_unmapped(session, cid, roles)
            await session.commit()
    except Exception:
        log.exception("Could not record the notice for an unrecorded online fee")
        return False
    return True


class _Moved(Exception):
    """A person recorded the unmatched payment on an invoice while it was being
    delivered; delivered again, it goes there."""


def _company(company_id: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(company_id)
    except ValueError:
        return None


async def _recorded_on(session, reference: str) -> tuple[uuid.UUID, str] | None:
    """The company and invoice a person recorded the unmatched payment *reference* on
    (``record_unmatched_payment``), or None."""
    found = (await session.execute(select(UnmatchedPayment.recorded_company, UnmatchedPayment.recorded_document)
                                   .where(UnmatchedPayment.reference == reference,
                                          UnmatchedPayment.recorded_company.is_not(None)))).first()
    return (uuid.UUID(found.recorded_company), found.recorded_document) if found else None


async def _lock_company_of(session, company_id) -> bool:
    """Lock the company a payment is delivered to or recorded on, before its unmatched
    payment and its documents: the order every writer of a payment takes (the company,
    then unmatched_payments, then Projection rows), so a payment deleted from an invoice
    under the company lock never waits on a delivery that waits on it. A reset waits for
    the lock; False once a reset has deleted the company."""
    from celerp.services.company_lock import locked_company
    return await locked_company(session, company_id) is not None


async def _lock_recorded(session, reference: str, home: tuple[uuid.UUID, str]) -> UnmatchedPayment | None:
    """Lock the company of *home*, then the unmatched payment *reference*, still
    recorded there (_lock_company_of). None when the company is gone; raises _Moved
    when the payment was moved meanwhile."""
    if not await _lock_company_of(session, home[0]):
        return None
    kept = await session.scalar(select(UnmatchedPayment).where(
        UnmatchedPayment.reference == reference).with_for_update(key_share=True))
    if kept is None or (kept.recorded_company, kept.recorded_document) != (str(home[0]), home[1]):
        raise _Moved(reference)
    return kept


async def _deliver_to_recorded(session, home: tuple[uuid.UUID, str], reference: str, *,
                               origin: tuple[str, str]) -> None:
    """A delivery of a payment a person recorded on the invoice *home*: it is already
    there, so only the refunds kept for it under the *origin* company and document it
    was delivered for apply, and its release when it has one."""
    kept = await _lock_recorded(session, reference, home)
    if kept is None:
        return
    if kept.released_at is None:
        await _apply_parked_refunds(session, home[0], home[1], reference, origin=origin)
    else:
        await _apply_release(session, home[0], home[1], reference, kept.released_at, origin=origin)


def _instant(value) -> datetime | None:
    """A reported time with a zone, or None (no business day it can be placed on)."""
    try:
        instant = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return instant if instant.utcoffset() is not None else None


async def _apply_refund(session, cid, entity_id: str, refund: dict, *, recorded: bool = False) -> bool:
    """Apply one change to a refund on its document, under the document's lock and in a
    savepoint: True once applied (or applied before), and then no longer kept. False,
    with nothing applied, when the document is gone or refuses it (logged). A 5xx
    raises. A refund of a payment a person *recorded* on the document from the
    unmatched payments gives the money back on the books that payment was recorded on,
    not the ones its payment page opened with."""
    from fastapi import HTTPException
    from celerp.services.company_lock import lock_projections
    from celerp_docs.routes_payments import record_stripe_refund, recorded_payment_books
    row = (await lock_projections(session, cid, [entity_id])).get(entity_id)
    if row is None or row.entity_type != "doc":
        return False
    if recorded:
        refund = {**refund, "context": await recorded_payment_books(session, cid, row, refund["reference"])}
    try:
        async with session.begin_nested():
            await record_stripe_refund(
                session, cid, row, refund_id=refund["refund_id"], cycle=refund["cycle"],
                transition=refund["transition"], reference=refund["reference"], amount_minor=refund["amount_minor"],
                currency=refund["currency"], occurred_at=refund["occurred_at"], context=refund["context"])
    except HTTPException as exc:
        if exc.status_code >= 500 or isinstance(exc, ModuleRefused):
            raise
        log.error("Invoice %s refused refund %s (%s %s): %s", entity_id, refund["refund_id"],
                  refund["transition"], refund["cycle"], exc.detail)
        return False
    await session.execute(delete(UnmatchedRefund).where(
        UnmatchedRefund.refund_id == refund["refund_id"], UnmatchedRefund.cycle == refund["cycle"],
        UnmatchedRefund.transition == refund["transition"]))
    return True


async def _apply_parked_refunds(session, cid, entity_id: str, reference: str, *,
                                origin: tuple[str, str] | None = None) -> None:
    """Apply the kept refunds of the payment *reference*, now on its document, in the
    order Stripe reported them: those delivered for this company and this document
    only, or for the *origin* company and document the payment was delivered for
    when a person recorded it on another invoice. One the document still refuses
    stays kept: one Stripe made after the payment stopped being linked to it, for good."""
    former_company, document = origin or (str(cid), entity_id)
    recorded = origin is not None
    query = select(UnmatchedRefund).where(UnmatchedRefund.reference == reference,
                                          UnmatchedRefund.former_company == former_company,
                                          UnmatchedRefund.document == document)
    parked = (await session.scalars(query.order_by(
        UnmatchedRefund.occurred_at.asc().nulls_last(), UnmatchedRefund.refund_id, UnmatchedRefund.cycle,
        UnmatchedRefund.transition))).all()
    for p in parked:
        await _apply_refund(session, cid, entity_id, {
            "refund_id": p.refund_id, "cycle": p.cycle, "transition": p.transition, "reference": p.reference,
            "amount_minor": p.amount_minor, "currency": p.currency, "occurred_at": p.occurred_at,
            "context": p.context}, recorded=recorded)


@_on_invoices
async def receive_refund(payload: dict) -> bool:
    """Apply a change to a refund of an online payment delivered by Celerp Cloud
    (``record_stripe_refund``), or keep it with the unmatched payments, whole, when it
    cannot be applied yet: the company, invoice or payment is not there, or the
    invoice refuses it. A kept refund applies when its payment is recorded on its
    invoice (``receive_payment``). True once applied or kept (Cloud is then told it
    arrived), False for a delivery that names no refund. Raises when nothing could be
    recorded, so Cloud delivers it again. Recording the same change twice changes
    nothing."""
    from celerp_docs.routes_payments import REFUND_TRANSITIONS
    company_id, entity_id, reference, refund_id, transition = (
        str(payload.get(k) or "") for k in ("company_id", "entity_id", "reference", "refund_id", "transition"))
    amount_minor, cycle = payload.get("amount_minor"), payload.get("cycle")
    if not (company_id and entity_id and reference and refund_id and transition in REFUND_TRANSITIONS
            and _counted(amount_minor) and _counted(cycle)):
        return False
    refund = {"refund_id": refund_id, "cycle": cycle, "transition": transition, "reference": reference,
              "amount_minor": amount_minor, "currency": str(payload.get("currency") or "USD").upper(),
              "occurred_at": _instant(payload.get("occurred_at")), "context": payload.get("context")}
    cid = _company(company_id)
    async with _own_session() as session:
        home = await _recorded_on(session, reference)
        if home is not None:
            # A person recorded the payment on another invoice: the refund goes there.
            await _home_uses_documents(session, home)
            if (await _lock_recorded(session, reference, home) is not None
                    and await _apply_refund(session, home[0], home[1], refund, recorded=True)):
                await _apply_parked_refunds(session, home[0], home[1], reference, origin=(company_id, entity_id))
                await session.commit()
                return True
        # A reset waits for this lock; once it has deleted the company, the refund is kept.
        elif cid and await _lock_company_of(session, cid) and await _apply_refund(session, cid, entity_id, refund):
            # A reversal that arrived before its refund was applied follows it now.
            await _apply_parked_refunds(session, cid, entity_id, reference)
            await session.commit()
            return True
        elif (await session.scalar(select(UnmatchedPayment.recorded_company).where(
                UnmatchedPayment.reference == reference).with_for_update(key_share=True))) is not None:
            raise _Moved(reference)  # recorded on an invoice meanwhile
        # Kept under the same locks, so a payment recorded meanwhile cannot miss it.
        from sqlalchemy.dialects.postgresql import insert
        await session.execute(insert(UnmatchedRefund).values(
            **refund, former_company=company_id, document=entity_id).on_conflict_do_nothing())
        await session.commit()
    return True


def _counted(value) -> bool:
    """A whole number of at least one, as delivered (not a string or a boolean)."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


async def _apply_release(session, cid, entity_id: str, reference: str, released_at: datetime, *,
                         origin: tuple[str, str] | None = None) -> bool:
    """Record that the payment *reference* on the document is no longer linked to
    Stripe, then apply its kept refunds Stripe made before that, whenever they
    arrived (``_apply_parked_refunds``). False when the document or the payment is
    not there."""
    from celerp.services.company_lock import lock_projections
    from celerp_docs.routes_payments import record_stripe_release
    row = (await lock_projections(session, cid, [entity_id])).get(entity_id)
    if row is None or row.entity_type != "doc":
        return False
    if not await record_stripe_release(session, cid, row, reference=reference, released_at=released_at):
        return False
    await _apply_parked_refunds(session, cid, entity_id, reference, origin=origin)
    return True


@_on_invoices
async def receive_release(payload: dict) -> bool:
    """Record that an online payment delivered by Celerp Cloud is no longer linked to
    Stripe (Stripe was disconnected): on its invoice (``record_stripe_release``), or on
    the payment among the unmatched payments, applied when it is recorded on its
    invoice (``receive_payment``). True once recorded either way (Cloud is then told
    it arrived). False for a delivery that names no release or a payment not received
    yet, so Cloud delivers it again after the payment. Recording it twice changes
    nothing."""
    from sqlalchemy import update
    company_id, entity_id, reference = (str(payload.get(k) or "") for k in ("company_id", "entity_id", "reference"))
    released_at = _instant(payload.get("released_at"))
    if not (company_id and entity_id and reference and released_at):
        return False
    cid = _company(company_id)
    async with _own_session() as session:
        home = await _recorded_on(session, reference)
        if home is not None:
            await _home_uses_documents(session, home)
        held = bool(await _lock_company_of(session, home[0]) if home else cid and await _lock_company_of(session, cid))
        # The unmatched payment before the documents, in the same order as the payment intake.
        kept = (await session.execute(update(UnmatchedPayment).where(UnmatchedPayment.reference == reference)
                                      .values(released_at=func.coalesce(UnmatchedPayment.released_at, released_at))
                                      .returning(UnmatchedPayment.recorded_company,
                                                 UnmatchedPayment.recorded_document))).first()
        if (kept is not None and kept.recorded_company is not None) != (home is not None) or (
                home is not None and (kept.recorded_company, kept.recorded_document) != (str(home[0]), home[1])):
            raise _Moved(reference)
        if home is not None:
            # Recorded by a person on an invoice: released there too.
            if held:
                await _apply_release(session, home[0], home[1], reference, released_at,
                                     origin=(company_id, entity_id))
            recorded = True
        else:
            recorded = kept is not None or (held and await _apply_release(session, cid, entity_id, reference,
                                                                          released_at))
        await session.commit()
    return recorded


async def unmatched_refunds(session) -> list[UnmatchedRefund]:
    """Every refund of an online payment kept until its payment is on its invoice,
    newest first."""
    return list((await session.scalars(
        select(UnmatchedRefund).order_by(UnmatchedRefund.received_at.desc()))).all())


async def unmatched_payments(session) -> list[UnmatchedPayment]:
    """Every online payment that could not be recorded on its invoice and that no
    person has recorded on another (``record_unmatched_payment``), newest first."""
    return list((await session.scalars(
        select(UnmatchedPayment).where(UnmatchedPayment.recorded_company.is_(None))
        .order_by(UnmatchedPayment.received_at.desc()))).all())


class UnmatchedRefused(Exception):
    """Why an unmatched payment cannot be recorded on the chosen invoice: the *reason*
    names the message ``pay.unmatched_refused_<reason>``."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def unmatched_payment(session, reference: str) -> UnmatchedPayment | None:
    """The unmatched payment *reference* while it is on the unmatched list, or None."""
    return await session.scalar(select(UnmatchedPayment).where(
        UnmatchedPayment.reference == reference, UnmatchedPayment.recorded_company.is_(None)))


def unmatched_amount(payment: UnmatchedPayment) -> Decimal:
    return from_stripe_amount(payment.amount_minor, payment.currency)


async def refuse_other_company(session, payment: UnmatchedPayment, company_id: uuid.UUID) -> None:
    """UnmatchedRefused when *payment* was made to a company other than *company_id*
    that still exists: it is recorded there, by someone working in that company."""
    from celerp.models.company import Company
    owner = _company(payment.former_company)
    if owner is not None and owner != company_id and await session.get(Company, owner) is not None:
        raise UnmatchedRefused("other_company")


async def record_unmatched_payment(session, company_id: uuid.UUID, reference: str, entity_id: str) -> None:
    """Record the unmatched payment *reference* on the invoice *entity_id* of the
    company *company_id*, the way a delivery records it on its own invoice
    (``record_stripe_payment``): on the invoice's books, dated the day it was paid. It
    leaves the unmatched payments, and its kept refunds and its release apply with it,
    in the caller's transaction; every later delivery of it, its refunds and its
    release goes to this invoice. Raises UnmatchedRefused, with nothing recorded,
    when the payment is no longer on the list, belongs to another company that still
    exists, or the invoice cannot take it whole. Recording it on the same invoice
    twice changes nothing. Deleting the payment from the invoice puts it back
    (``return_unmatched``)."""
    from fastapi import HTTPException
    from celerp.services.company_lock import lock_projections
    from celerp_docs.routes_payments import payment_books, record_stripe_payment, unmatched_refusal
    if not await _lock_company_of(session, company_id):
        raise UnmatchedRefused("gone")
    kept = await session.scalar(select(UnmatchedPayment).where(
        UnmatchedPayment.reference == reference).with_for_update())
    if kept is None:
        raise UnmatchedRefused("gone")
    if kept.recorded_company is not None:
        if (kept.recorded_company, kept.recorded_document) == (str(company_id), entity_id):
            return
        raise UnmatchedRefused("gone")
    await refuse_other_company(session, kept, company_id)
    row = (await lock_projections(session, company_id, [entity_id])).get(entity_id)
    if row is None or row.entity_type != "doc":
        raise UnmatchedRefused("missing")
    state = dict(row.state)
    if reason := unmatched_refusal(state, unmatched_amount(kept), kept.currency):
        raise UnmatchedRefused(reason)
    try:
        books = await payment_books(session, company_id, state)
        await record_stripe_payment(session, company_id, entity_id, state, reference=reference,
                                    amount_minor=kept.amount_minor, currency=kept.currency,
                                    paid_at=kept.paid_at or kept.received_at, context=books, managed=True,
                                    idempotency_key=f"{reference}:{uuid.uuid4().hex}")
    except (ValueError, HTTPException) as exc:
        if isinstance(exc, HTTPException) and (exc.status_code >= 500 or isinstance(exc, ModuleRefused)):
            raise
        log.error("Invoice %s refused unmatched payment %s: %s", entity_id, reference,
                  getattr(exc, "detail", exc))
        raise UnmatchedRefused("refused") from None
    kept.recorded_company, kept.recorded_document = str(company_id), entity_id
    await session.flush()
    origin = (kept.former_company, kept.document)
    if kept.released_at is None:
        await _apply_parked_refunds(session, company_id, entity_id, reference, origin=origin)
    else:
        await _apply_release(session, company_id, entity_id, reference, kept.released_at, origin=origin)


async def recorded_unmatched(session, company_id, entity_id: str) -> set[str]:
    """References of the payments a person recorded on this document from the
    unmatched payments (``record_unmatched_payment``)."""
    return set((await session.scalars(select(UnmatchedPayment.reference).where(
        UnmatchedPayment.recorded_company == str(company_id),
        UnmatchedPayment.recorded_document == entity_id))).all())


async def return_unmatched(session, company_id, entity_id: str, reference: str | None) -> None:
    """Put the payment *reference*, recorded on this document from the unmatched
    payments, back on the unmatched list: its payment was deleted from the document.
    In the caller's transaction; nothing for any other payment."""
    from sqlalchemy import update
    if reference:
        await session.execute(update(UnmatchedPayment).where(
            UnmatchedPayment.reference == reference, UnmatchedPayment.recorded_company == str(company_id),
            UnmatchedPayment.recorded_document == entity_id).values(recorded_company=None, recorded_document=None))


async def unrecord_company(session, company_id) -> None:
    """Put back on the unmatched list every payment a person recorded on an invoice of
    the company being reset, in the reset's transaction: the invoice is gone."""
    from sqlalchemy import update
    await session.execute(update(UnmatchedPayment).where(UnmatchedPayment.recorded_company == str(company_id))
                          .values(recorded_company=None, recorded_document=None))


async def report_recoveries(session) -> int | None:
    """Tell Celerp Cloud of every System Recovery restore it has not confirmed, oldest
    first, and return the installation's current payment generation. None while a
    restore is still unconfirmed: no company's payments can be closed until it is.
    Raises PaymentsNotClosed when Cloud holds a restore back for a payment in flight."""
    from celerp.config import settings
    pending = (await session.scalars(select(PaymentRecovery).where(
        PaymentRecovery.generation.is_(None)).order_by(PaymentRecovery.created_at))).all()
    for recovery in pending:
        if settings.cloud_disconnected:
            return None
        answer = await _cloud_answer(_RECOVERY, {
            "recovery_id": str(recovery.recovery_id), "company_ids": recovery.company_ids})
        if answer is not None and answer[0] == 409 and answer[1].get("detail") in _REFUSED:
            raise PaymentsNotClosed(answer[1]["detail"])
        generation = answer[1].get("generation") if answer is not None and answer[0] == 200 else None
        if (type(generation) is not int or generation < 1
                or answer[1].get("recovery_id") != str(recovery.recovery_id)):
            return None
        recovery.generation = generation
        await session.commit()
    return await session.scalar(select(func.max(PaymentRecovery.generation))) or 0


def record_recovery(session, company_ids: list) -> None:
    """Record, in the restore's own transaction, that a System Recovery restore brought
    back *company_ids*; ``report_recoveries`` tells Celerp Cloud, which delivers again
    every payment the installation ever recorded."""
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
            try:
                await report_recoveries(session)
            except PaymentsNotClosed:
                pass  # reported again next time; leftover closings settle meanwhile
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


# ── Connect onboarding (merchant-facing, from Settings → Payments) ───────────

async def connect_start() -> dict | None:
    """Begin Connect OAuth via Cloud. Returns {"url": <stripe oauth url>} to
    redirect the merchant to, or None."""
    return await _cloud_post("/billing/connect/authorize", {})


async def connect_status() -> dict:
    """The connection status for the settings page: {"enabled": bool, "state":
    "connected" | "disconnecting" | "revoked" | "disconnected"}. Falls back to the cached feature
    flag, with no state, if Cloud is unreachable."""
    return (await _cloud_get("/billing/connect/status")) or {"enabled": payments_enabled()}


async def disconnect() -> dict | None:
    """Disconnect the merchant's account via Cloud. New payments stop at once; Cloud
    finishes the disconnect once every payment already started has been recorded.
    Returns Cloud's answer, {"disconnected": bool, "state": "disconnecting" |
    "revoked" | "disconnected"}, or None when Cloud gave none."""
    answer = await _cloud_post("/billing/connect/disconnect", {})
    if (answer is None or type(answer.get("disconnected")) is not bool
            or answer.get("state") not in ("disconnecting", "revoked", "disconnected")):
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
