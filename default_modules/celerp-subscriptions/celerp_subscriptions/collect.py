# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Bill and collect automatically: the app's side of each invoice's card collection.

An agreement on ``COLLECT_MODE`` bills as automatic invoicing does, and each invoice it
issues, a period's or an amendment's, opens attempt 1 for what the invoice owes
(billing.issue_document). Nothing leaves inside
the billing transaction: every pass of the dispatcher, after billing commits, advances
each invoice's collection here, one invoice at a time, never holding a lock while it
waits on Celerp Cloud:

- an attempt asked for and not answered is asked for again with exactly the request first
  sent (stored on the invoice's collection as ``request``), unless collection may no longer go
  on (``stop_reason``), when one Cloud may hold is cancelled and one it never saw stops; Cloud
  refusing such a retry ends nothing while the first outcome is unknown: the attempt is
  cancelled at Cloud and ends on Cloud's answer or delivery;
- an attempt with Cloud on an invoice paid another way, or whose agreement stopped
  collecting, is cancelled; a cancel is asked for every pass until Cloud answers;
- a failed attempt whose retry time has passed is followed by the next attempt for what the
  invoice owes then, or stops with the reason it may not go on.
Cloud never retries on its own.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import select

from celerp.models.company import Company
from celerp.models.projections import Projection
from celerp.services import collection
from celerp_subscriptions.periods import COLLECT_MODE
from ui.i18n import t

log = logging.getLogger(__name__)

# Collection states a pass may still act on.
ACTIVE = frozenset({*collection.IN_FLIGHT, "failed"})
# Cloud refusals of an attempt that pass: the same attempt is asked for again next pass.
WAIT = frozenset({"attempt_in_progress", "checkout_open", "before_retry_time"})


def require_sales(terms: dict) -> None:
    """409 for a purchasing agreement: it bills what the company owes its supplier, so it
    never collects from a card."""
    from celerp_subscriptions.billing import doc_kind
    if doc_kind(terms) != "invoice":
        raise HTTPException(status_code=409, detail=t("subscriptions.err_collection_sales_only"))


async def require_collectable(session, company_id, terms: dict) -> None:
    """409 unless the agreement is a sales one, Cloud offers collection and its payer has a
    card on file."""
    require_sales(terms)
    if not collection.offered():
        raise HTTPException(status_code=409, detail=t("subscriptions.err_collection_off"))
    if not collection.card_on_file(await collection.payer(session, company_id, terms.get("contact_id"))):
        raise HTTPException(status_code=409, detail=t("subscriptions.err_no_card_on_file"))


async def stop_reason(session, company_id, doc_state: dict) -> str | None:
    """Why collection on the invoice may not go on, or None: it must be open, its agreement
    still on ``COLLECT_MODE``, the payer's card on file and the consenting member still
    holding the collection permissions."""
    from celerp_subscriptions import billing
    if not collection.is_open(doc_state):
        return "invoice_settled"
    agreement = await billing.doc_state(session, company_id, doc_state.get("source_agreement"))
    if not agreement or agreement.get("generation_mode") != COLLECT_MODE:
        return "mode_manual"
    if not collection.card_on_file(await collection.payer(session, company_id, agreement.get("contact_id"))):
        return "no_card_on_file"
    company = await session.get(Company, company_id)
    settings = (company.settings if company else None) or {}
    if await billing.authorizer(session, company_id, settings, agreement, "collection") is None:
        return "authorizer_lapsed"
    return None


def pass_for(company_id):
    async def _run(factory) -> None:
        await advance_all(factory, company_id)
    return _run


async def advance_all(factory, company_id) -> None:
    """Advance the collection of every invoice of the company that has one in progress."""
    async with factory() as session:
        ids = (await session.execute(select(Projection.entity_id).where(
            Projection.company_id == company_id, Projection.entity_type == "doc",
            Projection.state["collection"]["state"].as_string().in_(sorted(ACTIVE))))).scalars().all()
    for doc_id in sorted(ids):
        try:
            await advance(factory, company_id, doc_id)
        except Exception:
            log.exception("Advancing the collection of %s failed", doc_id)


async def ask_after_bill(session, company_id, doc_ids) -> None:
    """Ask for the charge of each invoice a bill by hand just committed, as the dispatcher's
    pass does after an unattended bill; an invoice with no collection waiting asks nothing."""
    from celerp_subscriptions.billing import same_session
    for doc_id in doc_ids:
        await advance(lambda: same_session(session), company_id, doc_id)


async def advance(factory, company_id, doc_id: str) -> None:
    """Decide the invoice's next step under its row lock and commit it, ask Cloud with no lock
    held, then record the answer under the lock again."""
    async with factory() as session:
        step = await _decide(session, company_id, doc_id)
        await session.commit()
    if step is None:
        return
    kind, payload, retry = step
    answer = await collection.ask(kind, payload)
    async with factory() as session:
        row = await _locked(session, company_id, doc_id)
        if row is None:
            return
        if kind == "attempt":
            reconcile = await _attempted(session, company_id, row, payload, answer, retry=retry)
        else:
            reconcile = False
            await _cancelled(session, company_id, row, payload, answer)
        await session.commit()
    if reconcile:
        await advance(factory, company_id, doc_id)  # the cancel it queued leaves in this same pass


async def _locked(session, company_id, doc_id: str):
    from celerp_docs.routes import _get_doc
    try:
        return await _get_doc(session, company_id, doc_id, for_update=True)
    except HTTPException:
        return None


async def _decide(session, company_id, doc_id: str) -> tuple[str, dict, bool] | None:
    """The next request to Cloud for the invoice's collection: (endpoint, request, whether it
    retries an attempt already asked for), or None when there is none to make."""
    from celerp_subscriptions import billing
    row = await _locked(session, company_id, doc_id)
    current = (row.state.get("collection") or {}) if row is not None else {}
    state = current.get("state")
    if state not in ACTIVE:
        return None
    if not collection.offered():
        if state == "pending" and not current.get("asked"):
            await collection.set_attempt(session, company_id, row, {"state": "stopped", "reason": "collection_off"})
        return None
    cancel = ("cancel", {"company_id": str(company_id), "entity_id": doc_id}, False)
    if current.get("cancel") == "queued":
        return cancel
    if state == "failed":
        eligible = collection.stamp(current.get("retry_eligible_at"))
        if eligible is None or billing.now() < eligible:
            return None
        if reason := await stop_reason(session, company_id, row.state):
            await collection.set_attempt(session, company_id, row, {"state": "stopped", "reason": reason})
            return None
        current = await collection.open_attempt(session, company_id, row, current["attempt_number"] + 1)
        if current["state"] != "pending":
            return None
        state = "pending"
    reason = await stop_reason(session, company_id, row.state)
    owed = collection.outstanding_minor(row.state)
    if state == "pending" and reason is None and owed is not None and owed < current["amount_minor"]:
        reason = "amount_changed"
    if reason is not None:
        if state == "pending" and not current.get("asked"):
            await collection.set_attempt(session, company_id, row, {"state": "stopped", "reason": reason})
            return None
        if current.get("cancel") == "refused":
            return None
        await collection.set_attempt(session, company_id, row, {"cancel": "queued", "reason": reason})
        return cancel
    if state != "pending":
        return None
    return await _attempt_request(session, company_id, row, current)


async def _attempt_request(session, company_id, row, current: dict) -> tuple[str, dict, bool] | None:
    """The attempt request for the pending attempt: the one first sent when it was asked for
    before, else a new one stored on the collection before it leaves; None while payments are
    paused, or with the reason collection stopped when the invoice has no books."""
    from celerp.services.payments import CheckoutPaused, checkout_generation
    from celerp_docs.routes_payments import payment_books
    from celerp_subscriptions import billing
    try:
        generation = await checkout_generation()
    except CheckoutPaused:
        return None
    if current.get("request") is not None:
        return "attempt", current["request"], True
    try:
        books = await payment_books(session, company_id, row.state)
    except ValueError:
        await collection.set_attempt(session, company_id, row, {"state": "stopped", "reason": "no_books"})
        return None
    agreement = await billing.doc_state(session, company_id, row.state.get("source_agreement")) or {}
    request = {
        "protocol": collection.PROTOCOL, "company_id": str(company_id), "entity_id": row.entity_id,
        "attempt_number": current["attempt_number"], "request_key": current["request_key"],
        "amount_minor": current["amount_minor"], "currency": current["currency"],
        "payer_contact_id": agreement.get("contact_id"), "generation": generation, "context": books}
    await collection.set_attempt(session, company_id, row, {"asked": True, "request": request})
    return "attempt", request, False


async def _attempted(session, company_id, row, payload: dict, answer, *, retry: bool) -> bool:
    """Record Cloud's answer to an attempt request; True when it queued a cancel to reconcile:
    a refused first request stops collection, a refused retry of one whose first answer was lost
    never does (that first request may have charged), it is cancelled at Cloud instead."""
    current = row.state.get("collection") or {}
    if current.get("attempt_number") != payload["attempt_number"] or current.get("state") != "pending":
        return False  # a delivery recorded the attempt's outcome first
    if answer is None:
        return False
    status, body = answer
    if status == 200:
        if (found := collection.outcome(body)) is not None:
            await collection.apply_outcome(session, company_id, row, found)
        return False
    refusal = collection.refusal_of(answer)
    if status != 409 or not refusal or refusal in WAIT:
        return False
    if retry:
        await collection.set_attempt(session, company_id, row, {"cancel": "queued", "reason": refusal})
        return True
    await collection.set_attempt(session, company_id, row, {"state": "stopped", "reason": refusal})
    return False


async def _cancelled(session, company_id, row, payload: dict, answer) -> None:
    current = row.state.get("collection") or {}
    if current.get("cancel") != "queued" or answer is None:
        return
    status, body = answer
    if status == 200 or collection.refusal_of(answer) == "no_attempt":
        await collection.set_attempt(session, company_id, row, {"state": "canceled"}, drop=("cancel",))
    elif status == 409:
        await collection.set_attempt(session, company_id, row, {"cancel": "refused"})


# ── the payer's card on file ─────────────────────────────────────────────────

async def _agreement(session, company_id, entity_id: str) -> dict:
    from celerp_subscriptions import billing
    from celerp_subscriptions.search import SUBSCRIPTION_DOC_TYPES
    state = await billing.doc_state(session, company_id, entity_id) if "\x00" not in entity_id else None
    if not state or state.get("doc_type") not in SUBSCRIPTION_DOC_TYPES:
        raise HTTPException(status_code=404, detail=t("subscriptions.err_not_found"))
    require_sales(state)
    return state


async def view(session, company_id, entity_id: str) -> dict:
    """Whether Cloud offers collection, and the agreement payer's card on file (None without one)."""
    agreement = await _agreement(session, company_id, entity_id)
    return {"offered": collection.offered(),
            "binding": collection.binding_of(await collection.payer(session, company_id, agreement.get("contact_id")))}


async def _payer_of(session, company_id, entity_id: str) -> tuple[str, dict]:
    agreement = await _agreement(session, company_id, entity_id)
    contact = await collection.payer(session, company_id, agreement.get("contact_id"))
    if contact is None:
        raise HTTPException(status_code=409, detail=t("subscriptions.err_payer_not_contact"))
    return agreement["contact_id"], contact


def _refused(answer) -> HTTPException:
    """Cloud's refusal of a card on file request as the app answers it: no readable answer is
    Cloud unreachable; a daily limit or collection being off is named; any other refusal says
    Cloud refused, naming its reason."""
    if answer is None:
        return HTTPException(status_code=502, detail=t("subscriptions.err_cloud_unreachable"))
    reason = collection.refusal_of(answer)
    if answer[0] == 429 and reason in ("setup_link_daily_limit", "setup_email_daily_limit"):
        return HTTPException(status_code=429, detail=t(f"subscriptions.err_{reason}"))
    if reason == "collection_off":
        return HTTPException(status_code=409, detail=t("subscriptions.err_collection_off"))
    return HTTPException(status_code=409, detail=t("subscriptions.err_collection_refused", reason=reason or "-"))


async def setup_link(session, company_id, entity_id: str, delivery: str, resend_receipt: bool) -> dict:
    """Ask Cloud for the link where the agreement's payer puts a card on file (emailed to the
    payer, or only returned to copy), or with ``resend_receipt`` to email the payer the receipt
    of the card they put on file again. A link Cloud could not email is still returned."""
    import uuid
    if not collection.offered():
        raise HTTPException(status_code=409, detail=t("subscriptions.err_collection_off"))
    payer_id, contact = await _payer_of(session, company_id, entity_id)
    email = (contact.get("email") or "").strip()
    if not email:
        raise HTTPException(status_code=409, detail=t("subscriptions.err_payer_no_email"))
    answer = await collection.ask("setup-link", {
        "protocol": collection.PROTOCOL, "company_id": str(company_id), "payer_contact_id": payer_id,
        "payer_email": email, "consent_text_version": collection.CONSENT_TEXT_VERSION,
        "request_key": uuid.uuid4().hex, "delivery": delivery, "resend_receipt": resend_receipt})
    if answer is None or answer[0] != 200:
        raise _refused(answer)
    body = answer[1]
    if resend_receipt:
        return {"receipt_sent": body.get("receipt_sent") is True}
    if not isinstance(body.get("link"), str) or not body["link"].startswith("https://"):
        raise _refused(None)
    return {"link": body["link"], "emailed": body.get("emailed") is True}


async def revoke(session, company_id, entity_id: str, actor_id) -> dict:
    """Withdraw the payer's card on file at Cloud, then record it revoked; nothing changes
    unless Cloud confirms."""
    payer_id, contact = await _payer_of(session, company_id, entity_id)
    binding = collection.binding_of(contact) or {}
    if binding.get("state") != "active":
        raise HTTPException(status_code=409, detail=t("subscriptions.err_no_active_binding"))
    answer = await collection.ask("revoke", {"company_id": str(company_id), "payer_contact_id": payer_id})
    if answer is None or answer[0] != 200:
        raise _refused(answer)
    await collection.record_binding(session, company_id, payer_id, {**binding, "state": "revoked"},
                                    actor_id=actor_id, source="api")
    await session.commit()
    return {"binding": {**binding, "state": "revoked"}}
