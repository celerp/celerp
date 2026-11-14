# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Automatic card collection through Celerp Cloud (collection protocol 1).

Cloud holds the payer's card and charges it only when the app asks: the app decides
every attempt (its number, its amount, when a retry is due) and Cloud reports what
became of it. Collection is offered only while Cloud's ``collection_protocol`` flag is
exactly this protocol; anything else fails closed.

What is stored here:
- the payer's card on file, on the payer contact (``BINDING_EVENT``), from Cloud's
  collection.binding deliveries and the operator's revoke;
- each invoice's collection, on the document (``ATTEMPT_EVENT``, written only by
  ``set_attempt`` under the document's row lock): the attempt number and its request
  key, the amount and currency asked for, Cloud's state for it, when a retry may be
  asked for, the bank's action link, a cancel waiting for Cloud's answer, whether a
  cancelled attempt is followed by one for what the invoice owes then (``replace``), the
  invoice of the same agreement it waits on (``held_by``), and why collection stopped.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from celerp.modules.boundary import needs_modules

log = logging.getLogger(__name__)

# An attempt delivery needs Documents on the company it names.
_on_invoices = needs_modules("celerp-docs", company=lambda payload: payload.get("company_id"))

PROTOCOL = 1
ROUTE = "/billing/connect/collection"
BINDING_EVENT = "crm.contact.collection_binding"
ATTEMPT_EVENT = "doc.collection"
BINDING_STATES = frozenset({"active", "revoked", "requires_action", "access_removed"})
# Cloud's states for an attempt (collection.attempt), and the app's own: "pending" (asked
# for, Cloud has not answered), "paid" (its charge is recorded on the invoice), "stopped".
# "succeeded": the card was charged and its payment is on the way, or was kept among the
# unmatched payments because the invoice could not take it ("kept": that payment's reference).
CLOUD_STATES = frozenset({"submitted", "processing", "requires_action", "succeeded", "failed", "canceled",
                          "unknown", "unverifiable"})
# The attempt is with Cloud and may still charge the card.
IN_FLIGHT = frozenset({"pending", "submitted", "processing", "requires_action", "unknown"})
# The app's own reasons an invoice's collection "stopped"; any other reason is Cloud's refusal key.
STOP_REASONS = frozenset({"invoice_settled", "mode_manual", "no_card_on_file", "authorizer_lapsed", "collection_off",
                          "nothing_to_charge", "no_books"})
CONSENT_TEXT_VERSION = "1"


def offered() -> bool:
    """Whether Cloud offers collection under this protocol (its flag is exactly the integer 1)."""
    from celerp.gateway.state import get_feature_flags
    value = get_feature_flags().get("collection_protocol")
    return type(value) is int and value == PROTOCOL


async def _ask(path: str, payload: dict) -> tuple[int, dict] | None:
    from celerp.services.payments import _cloud_answer
    return await _cloud_answer(path, payload)


async def ask(step: str, payload: dict) -> tuple[int, dict] | None:
    """Cloud's status and answer to one collection request, or None with no readable answer."""
    return await _ask(f"{ROUTE}/{step}", payload)


def refusal_of(answer: tuple[int, dict] | None) -> str | None:
    """The refusal key in Cloud's answer, or None."""
    detail = answer[1].get("detail") if answer is not None else None
    return detail if isinstance(detail, str) else None


# ── card on file ─────────────────────────────────────────────────────────────

def binding_of(contact_state: dict | None) -> dict | None:
    return (contact_state or {}).get("collection_binding")


def card_on_file(contact_state: dict | None) -> bool:
    return (binding_of(contact_state) or {}).get("state") == "active"


async def payer(session, company_id, contact_id) -> dict | None:
    """The payer contact's state, or None when ``contact_id`` names no contact of the company."""
    from celerp.models.projections import Projection
    if not isinstance(contact_id, str) or not contact_id:
        return None
    row = await session.get(Projection, (company_id, contact_id))
    return row.state if row is not None and row.entity_type == "contact" else None


async def record_binding(session, company_id, contact_id: str, binding: dict, *, actor_id=None,
                         source: str = "celerp_cloud", key: str | None = None) -> bool:
    """Store the payer's card on file as ``binding``; False (nothing written) when it holds it
    already or ``key`` (Cloud's delivery) was recorded before."""
    from celerp.events.engine import emit_event
    from celerp.models.projections import Projection
    row = await session.get(Projection, (company_id, contact_id))
    if row is None or binding_of(row.state) == binding:
        return False
    entry = await emit_event(session, company_id=company_id, entity_id=contact_id, entity_type="contact",
                             event_type=BINDING_EVENT, data=binding, actor_id=actor_id, location_id=None,
                             source=source, idempotency_key=key or f"{BINDING_EVENT}:{uuid.uuid4().hex}")
    return not getattr(entry, "was_deduped", False)


def _text(value) -> bool:
    return value is None or isinstance(value, str)


async def receive_binding(payload: dict) -> bool:
    """Record Cloud's collection.binding delivery on the payer contact. False for one without
    its fields; True once handled, including one naming no company or no contact of it (logged,
    nothing written). Stripe access removed tells the company."""
    from celerp.services.payments import _company, _own_session
    company, contact_id, state = payload.get("company_id"), payload.get("payer_contact_id"), payload.get("state")
    brand, last4, receipt = payload.get("card_brand"), payload.get("card_last4"), payload.get("receipt_sent")
    if not (isinstance(company, str) and isinstance(contact_id, str) and contact_id and state in BINDING_STATES
            and _text(brand) and _text(last4) and isinstance(receipt, bool)):
        return False
    cid = _company(company)
    async with _own_session() as session:
        if cid is None or await payer(session, cid, contact_id) is None:
            log.warning("Collection binding for an unknown payer %s of company %s", contact_id, company)
            return True
        before = binding_of(await payer(session, cid, contact_id)) or {}
        binding = {"state": state, "card_brand": brand, "card_last4": last4, "receipt_sent": receipt}
        delivery = payload.get("delivery_id")
        key = f"{BINDING_EVENT}:{delivery}" if isinstance(delivery, str) and delivery else None
        if await record_binding(session, cid, contact_id, binding, key=key) and state == "access_removed" != before.get(
                "state"):
            await notify_access_removed(session, cid)
        await session.commit()
    return True


async def notify_access_removed(session, company_id) -> None:
    from celerp.notifications.service import notify_once
    from ui.i18n import t
    title, body = "collection.access_removed_title", "collection.access_removed_body"
    await notify_once(session, company_id, "system", t(title, "en"), t(body, "en"), action_url="/subscriptions",
                      i18n={"title": title, "body": body, "params": {}})


# ── an invoice's attempts ────────────────────────────────────────────────────

def request_key(doc_id: str, n: int) -> str:
    return f"collect:{doc_id}:{n}"


def outstanding_minor(doc_state: dict) -> int | None:
    """What the invoice still owes, as the integer Cloud charges; None when it owes nothing or
    the amount cannot be charged exactly."""
    from celerp.services.doc_balance import outstanding_balance
    from celerp.services.payments import to_stripe_amount
    owed = outstanding_balance(doc_state)
    if owed is None or owed <= 0:
        return None
    try:
        return to_stripe_amount(owed, doc_state.get("currency") or "")
    except ValueError:
        return None


def is_open(doc_state: dict) -> bool:
    return doc_state.get("status") not in ("draft", "void") and outstanding_minor(doc_state) is not None


async def set_attempt(session, company_id, doc_row, change: dict, *, drop: tuple[str, ...] = ()) -> dict:
    """Write the invoice's collection with ``change`` (and without ``drop``): the one writer of a
    document's collection. The caller holds the document's row lock and commits."""
    from celerp.events.engine import emit_event
    before = doc_row.state.get("collection") or {}
    after = {**{k: v for k, v in before.items() if k not in drop}, **change}
    if after != before:
        await emit_event(session, company_id=company_id, entity_id=doc_row.entity_id, entity_type="doc",
                         event_type=ATTEMPT_EVENT, data=after, actor_id=None, location_id=None, source="collection",
                         idempotency_key=f"{ATTEMPT_EVENT}:{uuid.uuid4().hex}")
    return after


async def open_attempt(session, company_id, doc_row, n: int) -> dict:
    """Make attempt ``n`` on the invoice for what it owes now, to be asked for after commit; a
    document that owes nothing chargeable records why collection stopped instead."""
    amount = outstanding_minor(doc_row.state)
    if amount is None:
        return await set_attempt(session, company_id, doc_row, {"state": "stopped", "reason": "nothing_to_charge"})
    return await set_attempt(session, company_id, doc_row, {
        "state": "pending", "asked": False, "attempt_number": n, "request_key": request_key(doc_row.entity_id, n),
        "amount_minor": amount, "currency": (doc_row.state.get("currency") or "").upper()},
        drop=("request", "failure_code", "retry_eligible_at", "action_url", "cancel", "reason", "definitive", "replace",
              "held_by", "kept"))


def _instant(value) -> str | None | bool:
    """An ISO instant with its zone, in UTC; None for none; False when unreadable."""
    if value is None:
        return None
    try:
        at = datetime.fromisoformat(str(value))
    except ValueError:
        return False
    return False if at.utcoffset() is None else at.astimezone(timezone.utc).isoformat()


def outcome(answer: dict) -> dict | None:
    """The collection fields Cloud's attempt answer or delivery sets, or None when unreadable."""
    state, eligible = answer.get("state"), _instant(answer.get("retry_eligible_at"))
    if state not in CLOUD_STATES or eligible is False or not (
            _text(answer.get("failure_code")) and _text(answer.get("action_url"))):
        return None
    return {"state": state, "failure_code": answer.get("failure_code"), "retry_eligible_at": eligible,
            "action_url": answer.get("action_url"), "definitive": answer.get("definitive") is True
            or state in ("succeeded", "failed", "canceled", "unverifiable")}


def stamp(value) -> datetime | None:
    """A stored retry time as an instant, or None."""
    return datetime.fromisoformat(value) if isinstance(value, str) and _instant(value) else None


async def apply_outcome(session, company_id, doc_row, found: dict) -> None:
    """Record Cloud's ``found`` outcome of the invoice's current attempt, from its answer or its
    delivery, whichever comes first. A definitive outcome is not replaced by a later one that is
    not, and an attempt the app already settled (paid, stopped) stays so. A definitive outcome
    ends a cancel waiting for Cloud's answer: there is nothing left to cancel."""
    current = doc_row.state.get("collection") or {}
    if (current.get("definitive") and not found["definitive"]) or current.get("state") in ("paid", "stopped"):
        return
    await set_attempt(session, company_id, doc_row, {**found, "asked": True},
                      drop=("cancel",) if found["definitive"] else ())


@_on_invoices
async def receive_attempt(payload: dict) -> bool:
    """Record Cloud's collection.attempt delivery on its invoice (apply_outcome). False for one
    without its fields; True once handled, including one naming no company, no document of it or
    an attempt the document is not on (logged, nothing written)."""
    from fastapi import HTTPException
    from celerp.services.payments import _company, _own_session
    from celerp_docs.routes import _get_doc
    company, doc_id, n = payload.get("company_id"), payload.get("entity_id"), payload.get("attempt_number")
    found = outcome(payload)
    if not (isinstance(company, str) and isinstance(doc_id, str) and doc_id and type(n) is int and n > 0
            and found is not None):
        return False
    cid = _company(company)
    async with _own_session() as session:
        try:
            row = await _get_doc(session, cid, doc_id, for_update=True) if cid else None
        except HTTPException:
            row = None
        if row is None or (row.state.get("collection") or {}).get("attempt_number") != n:
            log.warning("Collection attempt %s for %s of company %s is not the document's", n, doc_id, company)
            return True
        await apply_outcome(session, cid, row, found)
        await session.commit()
    return True


async def charge_kept(session, company_id, doc_id: str, reference: str) -> None:
    """An online charge for the invoice was kept among the unmatched payments: a succeeded
    attempt's payment has arrived, so it can take no more money. Locks the document's row, after
    the company and the unmatched payment as every writer of a payment does; the caller commits."""
    from celerp.models.projections import Projection
    row = await session.get(Projection, (company_id, doc_id), with_for_update=True, populate_existing=True)
    if row is None:
        return
    current = row.state.get("collection") or {}
    if current.get("state") == "succeeded" and not current.get("kept"):
        await set_attempt(session, company_id, row, {"kept": reference})


async def charge_recorded(session, company_id, doc_row) -> None:
    """An online charge was recorded on the invoice: an attempt with Cloud that it settles is paid."""
    current = doc_row.state.get("collection") or {}
    if current.get("state") in IN_FLIGHT | {"succeeded"} and not is_open(doc_row.state):
        await set_attempt(session, company_id, doc_row, {"state": "paid"}, drop=("cancel",))
