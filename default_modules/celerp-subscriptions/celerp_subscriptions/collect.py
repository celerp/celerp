# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Bill and collect automatically: the app's side of each invoice's card collection.

An agreement on ``COLLECT_MODE`` bills as automatic invoicing does, and each invoice it
issues, a period's or an amendment's, opens attempt 1 for what the invoice owes
(billing.issue_document). Nothing leaves inside
the billing transaction: every pass of the dispatcher, after billing commits, advances
each invoice's collection here, one invoice at a time, never holding a lock while it
waits on Celerp Cloud:

what each pass does is one cell of TABLE (the invoice's phase x what the pass finds). In short:
- an attempt asked for and not answered is asked for again with exactly the request first
  sent (stored on the invoice's collection as ``request``); Cloud refusing such a retry ends
  nothing while the first outcome is unknown: the attempt is cancelled at Cloud and ends on
  Cloud's answer or delivery;
- an attempt Cloud may hold on an invoice that may no longer be collected (``stop_reason``) is
  cancelled, one it never saw stops; a cancel is asked for every pass until Cloud answers;
- an attempt Cloud may hold for more than its invoice owes now (a payment by hand, an
  amendment's credit) is cancelled and, once cancelled, followed by the next attempt for what
  the invoice owes then; one never asked for is made again for that amount;
- no new charge is asked on an invoice while another invoice of its agreement has a charge
  unresolved that may take more than that invoice owes (with Cloud, succeeded and not on the
  books, or kept among the unmatched payments):
  the invoice shows which one it waits on, and is asked what it owes once that is resolved;
- a failed attempt whose retry time has passed is followed by the next attempt for what the
  invoice owes then, or stops with the reason it may not go on;
- Cloud no longer offering collection stops only new charge requests: cancels, retries of a
  request already sent and Cloud's deliveries go on until every outcome is known;
- an invoice whose collection ended because it was settled and that owes again (a payment by
  hand voided, a refund) is never charged again on its own: the company is told once and its
  collection stops with the reason.
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

# Collection states a pass may still act on, besides an attempt cancelled to be replaced
# (``replace``): one Cloud cancelled because its invoice came to owe less than it asked, and an
# ended collection (``ended``) whose invoice awaits payment again.
ACTIVE = frozenset({*collection.IN_FLIGHT, "failed"})
# Cloud refusals of an attempt that pass: the same attempt is asked for again next pass.
WAIT = frozenset({"attempt_in_progress", "checkout_open", "before_retry_time"})

# ── the transition table ─────────────────────────────────────────────────────
# Every pass reads the invoice's collection as one phase (phase_of) and what it finds as one
# event (_event, the first that holds), and does what the table names; every cell is defined.
#   new             attempt made, never asked for
#   asked           asked for, Cloud has not answered (it may have charged)
#   submitted .. unknown   with Cloud, which may still charge the card
#   cancel_queued   a cancel asked for, Cloud has not answered it
#   cancel_refused  Cloud said the attempt runs to its outcome
#   failed          the attempt failed; the next one is due at its retry time (next_when_due)
#   replace         Cloud cancelled an attempt because its invoice came to owe less
#   succeeded       Cloud charged the card and the payment is not on the invoice yet: its delivery
#                   records it on the invoice whole, so every event waits for it
#   ended           paid, or stopped or cancelled because the invoice was settled: the invoice
#                   may owe again, which is told, never charged on its own
#   settled         nothing left to do (canceled, unverifiable, stopped for another reason)
# Events: stop (stop_reason: the invoice is paid, credited or voided, the agreement left
# collection, the card or the consenting member is gone), reduced / raised (the invoice owes
# less / more than the attempt asks), off (Cloud no longer offers collection: only new charge
# requests wait; cancels, retries of a request already sent and deliveries go on), none.
PHASES = ("new", "asked", "submitted", "processing", "requires_action", "unknown", "cancel_queued",
          "cancel_refused", "failed", "replace", "succeeded", "ended", "settled")
EVENTS = ("stop", "reduced", "raised", "off", "none")
_WITH_CLOUD = {"stop": "cancel", "reduced": "cancel_replace", "raised": "wait", "off": "wait", "none": "wait"}
TABLE: dict[tuple[str, str], str] = {
    **{("new", e): a for e, a in {"stop": "stop", "reduced": "reopen", "raised": "reopen", "off": "stop",
                                   "none": "ask"}.items()},
    **{("asked", e): a for e, a in {"stop": "cancel", "reduced": "cancel_replace", "raised": "ask", "off": "ask",
                                     "none": "ask"}.items()},
    **{(p, e): a for p in ("submitted", "processing", "requires_action", "unknown") for e, a in _WITH_CLOUD.items()},
    **{("cancel_queued", e): "send_cancel" for e in EVENTS},
    **{("cancel_refused", e): "wait" for e in EVENTS},
    **{("failed", e): "wait" if e == "off" else "next_when_due" for e in EVENTS},
    **{("replace", e): "wait" if e == "off" else "next" for e in EVENTS},
    **{("succeeded", e): "wait" for e in EVENTS},
    **{("ended", e): "stop" if e == "stop" else "tell" for e in EVENTS},
    **{("settled", e): "wait" for e in EVENTS},
}


def phase_of(current: dict) -> str:
    """The invoice's collection as a row of TABLE."""
    state = current.get("state")
    if state in ACTIVE and current.get("cancel") == "queued":
        return "cancel_queued"
    if state in collection.IN_FLIGHT and current.get("cancel") == "refused":
        return "cancel_refused"
    if state == "pending":
        return "asked" if current.get("asked") else "new"
    if state in collection.IN_FLIGHT:
        return state
    if state in ("failed", "succeeded"):
        return state
    if state == "canceled" and current.get("replace") is True:
        return "replace"
    return "ended" if _ended(current) else "settled"


def _ended(current: dict) -> bool:
    """Whether the collection ended because its invoice was paid or settled."""
    return current.get("state") == "paid" or (
        current.get("state") in ("stopped", "canceled") and current.get("reason") == "invoice_settled")


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


def _in_progress(company_id, *where):
    """The ids of the company's documents whose collection a pass may still act on: ACTIVE, an
    attempt cancelled to be replaced, or an ended one (_ended) on an invoice awaiting payment."""
    from sqlalchemy import or_
    from celerp.services.doc_balance import AWAITING_PAYMENT_STATUSES
    c = Projection.state["collection"]
    state, reason = c["state"].as_string(), c["reason"].as_string()
    return select(Projection.entity_id).where(
        Projection.company_id == company_id, Projection.entity_type == "doc", *where,
        or_(state.in_(sorted(ACTIVE)),
            (state == "canceled") & (c["replace"].as_boolean().is_(True)),
            ((state == "paid") | (state.in_(("stopped", "canceled")) & (reason == "invoice_settled")))
            & Projection.state["status"].as_string().in_(sorted(AWAITING_PAYMENT_STATUSES["invoice"])))
    ).order_by(Projection.entity_id)


async def advance_all(factory, company_id) -> None:
    """Advance the collection of every invoice of the company that has one in progress."""
    async with factory() as session:
        ids = (await session.execute(_in_progress(company_id))).scalars().all()
    for doc_id in ids:
        try:
            await advance(factory, company_id, doc_id)
        except Exception:
            log.exception("Advancing the collection of %s failed", doc_id)


async def ask_after_bill(session, company_id, doc_ids) -> None:
    """Ask for the charge of each invoice a bill by hand just committed, as the dispatcher's
    pass does after an unattended bill; an invoice with no collection waiting asks nothing. The
    other invoices of its agreement with a collection in progress are advanced first, so a
    cancel an amendment's credit calls for leaves before the amendment's invoice is decided."""
    from celerp_subscriptions.billing import doc_state, same_session
    for doc_id in doc_ids:
        tid = ((await doc_state(session, company_id, doc_id)) or {}).get("source_agreement")
        before = (await session.execute(_in_progress(
            company_id, Projection.state["source_agreement"].as_string() == tid,
            Projection.entity_id != doc_id))).scalars().all() if tid else []
        for other in (*before, doc_id):
            await advance(lambda: same_session(session), company_id, other)


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
    retries an attempt already asked for), or None when there is none to make. The invoice's
    phase and the pass's event name the action (TABLE)."""
    row = await _locked(session, company_id, doc_id)
    if row is None:
        return None
    current = row.state.get("collection") or {}
    phase = phase_of(current)
    if phase == "settled":  # every event of the row waits: nothing is read
        return None
    event, why = await _event(session, company_id, row, current, phase)
    return await _ACTIONS[TABLE[phase, event]](session, company_id, row, current, event, why)


async def _event(session, company_id, row, current: dict, phase: str) -> tuple[str, str | None]:
    """The first of EVENTS that holds for the invoice, with what it names: the stop reason, or the
    reason a cancel for a smaller balance gives. Only an attempt that may still charge asks an
    amount (a failed or cancelled one asks nothing)."""
    if reason := await stop_reason(session, company_id, row.state):
        return "stop", reason
    owed, asks = collection.outstanding_minor(row.state), current.get("amount_minor")
    if phase not in ("failed", "replace", "ended") and type(asks) is int and owed is not None and owed != asks:
        return ("reduced", "amount_changed") if owed < asks else ("raised", None)
    if not collection.offered():
        return "off", "collection_off"
    return "none", None


def _cancel(company_id, row) -> tuple[str, dict, bool]:
    return "cancel", {"company_id": str(company_id), "entity_id": row.entity_id}, False


async def _wait(session, company_id, row, current, event, why):
    return None


async def _ask(session, company_id, row, current, event, why):
    return await _attempt_request(session, company_id, row, current)


async def _stop(session, company_id, row, current, event, why):
    await collection.set_attempt(session, company_id, row, {"state": "stopped", "reason": why},
                                 drop=("replace", "waits"))
    return None


async def _reopen(session, company_id, row, current, event, why):
    """The attempt was never asked for: it is made again, same number, for what the invoice owes
    now, and decided again."""
    await collection.open_attempt(session, company_id, row, current["attempt_number"])
    return await _decide(session, company_id, row.entity_id)


async def _tell(session, company_id, row, current, event, why):
    """The invoice owes again after its collection ended: the company is told once, and
    collection stops with the reason. The card is not charged again on its own, since what
    reopened the invoice (a refund, a dispute, a payment voided) may be the payer's own doing."""
    from celerp.notifications.service import notify_once
    from celerp.services.payments import dispute_amount
    params = {"document": row.state.get("ref_id") or row.entity_id,
              "amount": dispute_amount(collection.outstanding_minor(row.state), row.state.get("currency") or "")}
    title, body = "collection.owes_again_title", "collection.owes_again_body"
    await notify_once(session, company_id, "accounting", t(title, "en", **params), t(body, "en", **params),
                      action_url=f"/docs/{row.entity_id}", i18n={"title": title, "body": body, "params": params})
    await collection.set_attempt(session, company_id, row, {"state": "stopped", "reason": "owes_again"},
                                 drop=("replace",))
    return None


async def _queue_cancel(session, company_id, row, current, event, why):
    await collection.set_attempt(session, company_id, row, {"cancel": "queued", "reason": why})
    return _cancel(company_id, row)


async def _cancel_replace(session, company_id, row, current, event, why):
    """Cloud may still charge more than the invoice owes: the attempt is cancelled at Cloud and,
    once cancelled, the next one asks what the invoice owes then."""
    await collection.set_attempt(session, company_id, row, {"cancel": "queued", "reason": why, "replace": True})
    return _cancel(company_id, row)


async def _send_cancel(session, company_id, row, current, event, why):
    return _cancel(company_id, row)


async def _next(session, company_id, row, current, event, why):
    """The next attempt for what the invoice owes now, decided at once; on a stop event,
    collection stops with its reason instead."""
    if event == "stop":
        return await _stop(session, company_id, row, current, event, why)
    await collection.open_attempt(session, company_id, row, current["attempt_number"] + 1)
    return await _decide(session, company_id, row.entity_id)


async def _next_when_due(session, company_id, row, current, event, why):
    from celerp_subscriptions import billing
    eligible = collection.stamp(current.get("retry_eligible_at"))
    if eligible is None or billing.now() < eligible:
        return None
    return await _next(session, company_id, row, current, event, why)


_ACTIONS = {"wait": _wait, "ask": _ask, "stop": _stop, "reopen": _reopen, "cancel": _queue_cancel,
            "cancel_replace": _cancel_replace, "send_cancel": _send_cancel, "next": _next,
            "next_when_due": _next_when_due, "tell": _tell}


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
    if (other := await _charge_open_elsewhere(session, company_id, row)) is not None:
        await collection.set_attempt(session, company_id, row, {"waits": other})
        return None
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
    await collection.set_attempt(session, company_id, row, {"asked": True, "request": request}, drop=("waits",))
    return "attempt", request, False


async def _charge_open_elsewhere(session, company_id, row) -> str | None:
    """The number of another invoice of the same agreement whose card charge may take, or took,
    more than that invoice owes now, or None. Cloud keeps one charge at a time per invoice only, so
    an invoice is not asked while another of its agreement (an amendment's invoice waits on the one
    its credit lowered) has such a charge unresolved: asked for and with Cloud (a cancel refused
    included) or succeeded with its payment not arrived, for more than the invoice owes, or with
    its payment kept among the unmatched payments, not yet recorded on an invoice nor refunded.
    Whatever such a charge takes beyond its own invoice is money the agreement may already have,
    so asking more could take more than the agreement owes. A charge for no more than its invoice
    owes holds nothing: whatever it takes, that invoice keeps. An invoice that waits has nothing
    with Cloud, so no two invoices wait on each other's charges. Read from what is stored on every
    pass, under a lock per agreement, so two passes never both ask."""
    from celerp.modules.registry import _named_lock
    tid = row.state.get("source_agreement")
    if not tid:
        return None
    await _named_lock(session, f"collection:{company_id}:{tid}", "pg_advisory_xact_lock")
    c = Projection.state["collection"]
    others = (await session.execute(select(Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "doc",
        Projection.state["source_agreement"].as_string() == tid, Projection.entity_id != row.entity_id,
        c["state"].as_string().in_(sorted(collection.IN_FLIGHT | {"succeeded"}))
    ).order_by(Projection.entity_id))).scalars().all()
    for state in others:
        current = state["collection"]
        if current.get("kept"):
            held = await collection.kept_unresolved(session, current["kept"])
        else:
            held = (current["state"] != "pending" or bool(current.get("asked"))) \
                and current.get("amount_minor", 0) > (collection.outstanding_minor(state) or 0)
        if held:
            return state.get("ref_id") or state.get("doc_number") or ""
    return None


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
