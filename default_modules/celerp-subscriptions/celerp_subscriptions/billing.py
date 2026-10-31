# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Billing an agreement's periods: the one claim every route and the dispatcher use.

A period is billed once. The claim runs under the company and agreement locks and keys
its document event on the agreement and period (``sub:{agreement}:p{k}:{kind}:r{n}``), so
the ledger's unique key refuses a second document even if the lock were bypassed.
Unattended billing (``run_due``) bills only agreements whose authorizer still holds the
permission, at most ``budget`` documents per pass, oldest period first, and never a
backlog older than the agreement's catch-up window. Sending is requested after commit.
"""
from __future__ import annotations

import copy
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.events.engine import emit_event, find_event_by_idempotency
from celerp.models.accounting import UserCompany
from celerp.models.company import Company
from celerp.models.projections import Projection
from celerp.services import email
from celerp.services.business_time import business_today
from celerp.services.company_lock import lock_projections, locked_company
from celerp.services.permissions import role_has_permission
from celerp.services.record_quantities import template_label
from celerp.services.terms import resolve_document_terms
from celerp.services.units import require_moving_fits_units
from celerp_docs.doc_money import document_money
from celerp_docs.periodic import OperationReport
from celerp_subscriptions.periods import BILLED, Schedule
from celerp_subscriptions.search import SUBSCRIPTION_DOC_TYPES
from ui.i18n import t

log = logging.getLogger(__name__)

AUTO_MODES = frozenset({"auto_invoice", "auto_invoice_collect"})
# Each unattended mode and the permission its authorizer must keep holding.
MODE_PERMISSION = {"generation": "automate_billing", "sending": "automate_sending",
                   "collection": "automate_collection"}
CATCHUP_CAP = 12


def now_stamp() -> str:
    """Wall-clock time of a record (not the business day): what a summary acknowledgment compares."""
    return datetime.now(timezone.utc).isoformat()


def doc_kind(state: dict) -> str:
    return "invoice" if state.get("doc_type") == "subscription_invoice" else "purchase_order"


def periods_of(state: dict) -> dict[str, dict]:
    return copy.deepcopy(dict(state.get("periods") or {}))


def review_floor(state: dict) -> int | None:
    """While earlier documents wait for review, the periods before the anchor are not billed."""
    return 0 if state.get("history_review") else None


def compute_due_date(issue_date: date, payment_terms: str | None, company: Company) -> str | None:
    """ISO due date from the payment terms' days in the company settings."""
    if not payment_terms:
        return None
    for term in (company.settings or {}).get("payment_terms", []):
        if term.get("name") == payment_terms:
            days = int(term.get("days") or 0)
            return (issue_date + timedelta(days=days)).isoformat() if days else None
    return None


async def locked_agreement(session: AsyncSession, company_id, entity_id: str) -> Projection:
    """The agreement under the company lock and its own row lock, as the previous writer
    committed it: every schedule operation reads and writes it under this lock."""
    proj = (await lock_projections(session, company_id, [entity_id])).get(entity_id)
    if not proj or proj.state.get("doc_type") not in SUBSCRIPTION_DOC_TYPES:
        raise HTTPException(status_code=404, detail=t("subscriptions.err_not_found"))
    return proj


async def write_agreement(session: AsyncSession, company_id, entity_id: str, changes: dict, actor_id, *,
                          drop: tuple[str, ...] = (), key: str | None = None, result: dict | None = None) -> None:
    """Record a change to the agreement's own fields (``{field: new value}``); ``drop``
    removes fields from its state."""
    await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.updated",
        data={"fields_changed": {**{k: {"new": v} for k, v in changes.items()}, **{k: {"drop": True} for k in drop}}},
        actor_id=actor_id, location_id=None, source="subscription",
        idempotency_key=key or str(uuid.uuid4()), metadata_={"result": result} if result is not None else None,
    )


def refuse_status(state: dict) -> None:
    status = state.get("status")
    if status in ("draft", "paused", "cancelled"):
        key = "subscriptions.err_generate_cancelled" if status == "cancelled" else f"subscriptions.err_bill_{status}"
        raise HTTPException(status_code=409, detail=t(key))


async def doc_state(session, company_id, doc_id: str) -> dict | None:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": doc_id})
    return (row.state or {}) if row is not None else None


async def doc_ref(session, company_id, doc_id: str) -> str:
    return ((await doc_state(session, company_id, doc_id)) or {}).get("ref_id") or doc_id


async def _result(session, company_id, doc_id: str, k: int, schedule: Schedule, state: dict) -> dict:
    nxt = schedule.next_bill_date(floor=review_floor(state))
    return {"doc_id": doc_id, "ref_id": await doc_ref(session, company_id, doc_id), "period_index": k,
            "next_bill_date": nxt.isoformat() if nxt else None}


async def _bill(session, company, company_id, tid: str, state: dict, schedule: Schedule, k: int, actor,
                *, auto: bool, send: bool) -> str:
    """Create, finalize and claim period ``k``'s document. Caller holds the locks and commits."""
    from celerp_docs.routes import finalize_document
    from celerp_docs.sequences import next_draft_ref

    kind = doc_kind(state)
    entry = (state.get("periods") or {}).get(str(k)) or {}
    release = entry.get("release_count", 0)
    claim_key = f"sub:{tid}:p{k}:{kind}:r{release}"
    if (earlier := await find_event_by_idempotency(session, company_id, claim_key)) is not None:
        return earlier.entity_id
    today = business_today((company.settings or {}).get("timezone"))
    contact_id = state.get("contact_id")
    contact = (await session.get(Projection, {"company_id": company_id, "entity_id": contact_id})) if contact_id else None
    contact_state = (contact.state or {}) if contact is not None and contact.entity_type == "contact" else {}
    service = {"source_agreement": tid, "period_index": k,
               "service_start": schedule.start(k).isoformat(), "service_end": schedule.end(k).isoformat()}
    lines = [{**li, **service} for li in copy.deepcopy(list(state.get("line_items") or []))]
    template_name = template_label(state, tid)
    auto_note = f"Auto-created from subscription template {template_name}"
    notes = f"{state['notes']}\n{auto_note}".strip() if state.get("notes") else auto_note
    currency = state.get("currency") or (company.settings or {}).get("currency", "USD")
    money_inputs = {f: state[f] for f in ("discount", "discount_type", "shipping", "tax", "tax_rate", "doc_taxes")
                    if f in state}
    money = document_money(money_inputs, lines, currency, keep_unrated_tax=True)
    ref = next_draft_ref(company, kind)
    doc_id = f"doc:{ref}"
    doc = {
        "doc_type": kind, "ref_id": ref, "contact_id": contact_id,
        "contact_name": contact_state.get("name") or "", "contact_company_name": contact_state.get("company_name") or "",
        "line_items": lines, "payment_terms": state.get("payment_terms"), "currency": currency,
        **money_inputs, **money, "amount_outstanding": money["total"], "issue_date": today.isoformat(),
        "notes": notes, **service,
    }
    if due := compute_due_date(today, state.get("payment_terms"), company):
        doc["due_date"] = due
    # The template may predate a precision change; what it makes is held to today's units.
    await require_moving_fits_units(session, company_id, doc_id, "doc", doc, template=template_name)
    doc.update(resolve_document_terms(state, company.settings or {}, kind))
    if "customer_note" in state:
        doc["customer_note"] = state.get("customer_note") or ""
    await emit_event(session, company_id=company_id, entity_id=doc_id, entity_type="doc", event_type="doc.created",
                     data=doc, actor_id=actor.id, location_id=None, source="subscription", idempotency_key=claim_key)
    await session.flush()
    await finalize_document(doc_id, company_id, actor, session, commit=False)
    await session.flush()
    periods = periods_of(state)
    periods[str(k)] = {"state": BILLED, "doc_id": doc_id, "release_count": release, "at": now_stamp(),
                       "by": str(actor.id) if actor.id else None, "auto": auto,
                       **({"send": {"state": "pending", "n": 0}} if send else {})}
    await write_agreement(session, company_id, tid, {"periods": periods}, actor.id)
    return doc_id


async def claim_period(session: AsyncSession, company_id, template_id: str, *, actor, early: bool = False,
                       period_index: int | None = None, request_key: str | None = None) -> dict:
    """Bill the agreement's oldest open period: due (its bill date has come) or, with
    ``early``, the next one ahead of its date. With nothing due, the latest due period's
    document when it is billed; otherwise a refusal naming why. A ``request_key`` repeats
    the first answer to the same request. Caller commits."""
    company = await locked_company(session, company_id)
    req = f"subreq:{template_id}:{request_key}" if request_key else None
    if req and (earlier := await find_event_by_idempotency(session, company_id, req)) is not None:
        return earlier.metadata_["result"]
    proj = await locked_agreement(session, company_id, template_id)
    state = proj.state
    refuse_status(state)
    schedule = Schedule.from_state(state)
    today = business_today((company.settings or {}).get("timezone"))
    k = schedule.oldest_open(floor=review_floor(state))
    if early:
        if k is None:
            raise HTTPException(status_code=409, detail=t("subscriptions.err_no_period_left"))
        if period_index is not None and period_index != k:
            raise HTTPException(status_code=409, detail=t("subscriptions.err_early_not_next"))
    elif k is None or schedule.bill_date(k) > today:
        if schedule.ended(today):
            raise HTTPException(status_code=409, detail=t("subscriptions.err_bill_ended"))
        latest = schedule.latest_due(today)
        if latest is not None and schedule.state(latest) == BILLED:
            return await _result(session, company_id, schedule.periods[latest]["doc_id"], latest, schedule, state)
        nxt = schedule.next_bill_date(floor=review_floor(state))
        raise HTTPException(status_code=409, detail=(
            t("subscriptions.err_nothing_due", date=nxt.isoformat()) if nxt
            else t("subscriptions.err_no_period_left")))
    doc_id = await _bill(session, company, company_id, template_id, state, schedule, k, actor, auto=False, send=False)
    state = (await session.get(Projection, {"company_id": company_id, "entity_id": template_id})).state
    result = await _result(session, company_id, doc_id, k, Schedule.from_state(state), state)
    if req:
        await emit_event(session, company_id=company_id, entity_id=template_id, entity_type="doc",
                         event_type="doc.updated", data={"fields_changed": {}}, actor_id=actor.id, location_id=None,
                         source="subscription", idempotency_key=req, metadata_={"result": result})
    return result


async def agreements(session: AsyncSession, company_id) -> list[Projection]:
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "doc",
        Projection.state["doc_type"].as_string().in_(SUBSCRIPTION_DOC_TYPES),
    ).order_by(Projection.entity_id))).scalars().all()
    return list(rows)


def _period_row(schedule: Schedule, k: int) -> dict:
    return {"period_index": k, "service_start": schedule.start(k).isoformat(),
            "service_end": schedule.end(k).isoformat(), "bill_date": schedule.bill_date(k).isoformat()}


async def period_list(session: AsyncSession, company_id, state: dict, today: date, *, limit: int = 24) -> dict:
    """An agreement's periods from its lowest to the one after today (and any claimed later),
    newest first, at most ``limit``, with each claim's state and document; its next bill date;
    the earlier documents waiting for history review, each with its candidate periods; and the
    ones set aside as billing no period of their own."""
    if not state.get("anchor_date"):
        return {"next_bill_date": None, "rows": [], "review": [], "set_aside": []}
    schedule = Schedule.from_state(state)
    nxt = schedule.next_bill_date(floor=review_floor(state))
    first = schedule.first()
    rows = []
    if first is not None:
        top = max([schedule.current(today) + 1, *schedule.periods])
        k = top
        while k >= first and len(rows) < limit:
            if schedule.exists(k):
                entry = schedule.periods.get(k) or {}
                doc_id = entry.get("doc_id")
                rows.append({**_period_row(schedule, k), "state": entry.get("state"), "doc_id": doc_id,
                             "ref_id": await doc_ref(session, company_id, doc_id) if doc_id else None,
                             "reason": entry.get("reason"), "history": bool(entry.get("history"))})
            k -= 1
    review = [{"doc_id": r.get("doc_id"), "ref_id": await doc_ref(session, company_id, r.get("doc_id")),
               "candidates": [_period_row(schedule, c) for c in r.get("candidates") or []]}
              for r in state.get("history_review") or []]
    set_aside = [{"doc_id": r.get("doc_id"), "ref_id": await doc_ref(session, company_id, r.get("doc_id"))}
                 for r in state.get("history_set_aside") or []]
    return {"next_bill_date": nxt.isoformat() if nxt else None, "rows": rows, "review": review,
            "set_aside": set_aside}


def due_periods(state: dict, today: date) -> tuple[Schedule, list[int]] | None:
    """The schedule and its due open periods, for an active agreement with a schedule."""
    if state.get("status") != "active" or not state.get("anchor_date"):
        return None
    schedule = Schedule.from_state(state)
    return schedule, schedule.due(today, floor=review_floor(state))


async def due_preview(session: AsyncSession, company_id, today: date) -> dict:
    """Every active agreement with a period due, and the next bill date after them."""
    items, upcoming = [], []
    for row in await agreements(session, company_id):
        found = due_periods(row.state, today)
        if found is None:
            continue
        schedule, due = found
        if due:
            items.append({"entity_id": row.entity_id, "periods": [_period_row(schedule, k) for k in due]})
        elif (nxt := schedule.next_bill_date(floor=review_floor(row.state))) is not None:
            upcoming.append(nxt)
    return {"items": items, "total": len(items),
            "next_bill_date": min(upcoming).isoformat() if upcoming else None}


def outside_window(state: dict, schedule: Schedule, due: list[int], today: date) -> bool:
    """The oldest due period is further back than the agreement's catch-up window."""
    latest = schedule.latest_due(today)
    return bool(due) and latest is not None and latest - due[0] > int(state.get("catchup_periods") or 0)


def unattended_due(state: dict, today: date) -> tuple[Schedule, list[int]] | None:
    """The schedule and due periods of an agreement that bills itself, with no earlier
    document waiting for review; None when it is not one or nothing is due."""
    if state.get("generation_mode") not in AUTO_MODES or state.get("history_review"):
        return None
    found = due_periods(state, today)
    return found if found and found[1] else None


async def catch_up_preview(session: AsyncSession, company_id, today: date) -> list[dict]:
    """Unattended agreements behind by more than their catch-up window: billed only from here."""
    items = []
    for row in await agreements(session, company_id):
        found = unattended_due(row.state, today)
        if found and outside_window(row.state, *found, today):
            schedule, due = found
            items.append({"entity_id": row.entity_id, "periods": [_period_row(schedule, k) for k in due]})
    return items


async def _member_role(session, company_id, user_id) -> str | None:
    if not user_id:
        return None
    try:
        uid = uuid.UUID(str(user_id))
    except ValueError:
        return None
    return await session.scalar(select(UserCompany.role).where(
        UserCompany.company_id == company_id, UserCompany.user_id == uid, UserCompany.is_active.is_(True)))


async def authorizer(session, company_id, settings: dict, state: dict, mode: str):
    """The member whose consent runs ``mode`` unattended, while they still hold its
    permission; None when the consent is missing or has lapsed."""
    by = ((state.get("consents") or {}).get(mode) or {}).get("by")
    role = await _member_role(session, company_id, by)
    if role is None or not role_has_permission(settings, role, MODE_PERMISSION[mode]):
        return None
    return SimpleNamespace(id=uuid.UUID(str(by)))


def lapsed_modes(state: dict) -> list[str]:
    """The unattended modes an agreement is set to, by their consent key."""
    modes = []
    if state.get("generation_mode") in AUTO_MODES:
        modes.append("generation")
    if state.get("auto_send"):
        modes.append("sending")
    return modes


async def run_due(*, session: AsyncSession, company_id, today: date, budget: int) -> OperationReport:
    """The dispatcher's billing pass for one company (the ``periodic_operation`` slot)."""
    report = OperationReport()
    held: list[int] = []  # due periods of each agreement held for the catch-up preview
    for row in await agreements(session, company_id):
        state = row.state
        for k, entry in (state.get("periods") or {}).items():
            if ((entry.get("send") or {}).get("state")) == "pending":
                report.after_commit.append(_deliverer(company_id, row.entity_id, int(k)))
        found = unattended_due(state, today)
        if found is None:
            continue
        if outside_window(state, *found, today):
            held.append(len(found[1]))
            continue
        while report.done < budget:
            try:
                async with session.begin_nested():
                    # Decided again from the agreement as locked: the read above may be stale.
                    company = await locked_company(session, company_id)
                    proj = await locked_agreement(session, company_id, row.entity_id)
                    found = unattended_due(proj.state, today)
                    if found is None or outside_window(proj.state, *found, today):
                        break
                    settings = company.settings or {}
                    actor = await authorizer(session, company_id, settings, proj.state, "generation")
                    if actor is None:
                        break
                    send = (bool(proj.state.get("auto_send"))
                            and await authorizer(session, company_id, settings, proj.state, "sending") is not None)
                    k = found[1][0]
                    await _bill(session, company, company_id, row.entity_id, proj.state, found[0], k, actor,
                                auto=True, send=send)
            except Exception:
                log.exception("Unattended billing of %s failed", row.entity_id)
                break
            report.done += 1
            if send:
                report.after_commit.append(_deliverer(company_id, row.entity_id, k))
    if report.done:
        await _notify(session, company_id, "notice_billed", "/subscriptions/summary", day=today.isoformat())
    if held:
        await _notify(session, company_id, "notice_catchup", "/subscriptions/catch-up", n=len(held), m=sum(held))
    return report


async def _notify(session, company_id, notice: str, url: str, **params) -> None:
    """Tell the company once about ``notice`` with these figures; new figures tell it again."""
    from celerp.notifications.service import notify_once

    title, body = f"subscriptions.{notice}_title", f"subscriptions.{notice}_body"
    await notify_once(session, company_id, "system", t(title, "en"), t(body, "en", **params), action_url=url,
                      i18n={"title": title, "body": body, "params": params})


def _deliverer(company_id, template_id: str, k: int):
    async def _run(factory) -> None:
        await deliver(factory, company_id, template_id, k)
    return _run


async def deliver(factory, company_id, template_id: str, k: int, *, retry_by=None) -> dict:
    """Email period ``k``'s document to the agreement's contact, once. The attempt is
    recorded and committed before the email goes, so two passes never both send it; the
    outcome is recorded after, and a sent email marks the document sent through its own
    send transition. Only a pending send goes, or, retried by the member ``retry_by``, a
    failed one or one cut off before its outcome was recorded."""
    from celerp_docs.routes import DocSendBody, doc_email, mark_doc_sent

    async with factory() as session:
        proj = await locked_agreement(session, company_id, template_id)
        periods = periods_of(proj.state)
        entry = periods.get(str(k)) or {}
        send = dict(entry.get("send") or {})
        if entry.get("state") != BILLED:
            raise HTTPException(status_code=409, detail=t("subscriptions.err_send_not_billed"))
        if send.get("state") not in (("pending", "failed", "sending") if retry_by else ("pending",)):
            return send
        doc = await session.get(Projection, {"company_id": company_id, "entity_id": entry["doc_id"]})
        contact_id = proj.state.get("contact_id")
        contact = (await session.get(Projection, {"company_id": company_id, "entity_id": contact_id})) if contact_id else None
        to = ((contact.state or {}).get("email") if contact is not None else None) or ""
        by = retry_by or ((proj.state.get("consents") or {}).get("sending") or {}).get("by") or entry.get("by")
        actor = SimpleNamespace(id=uuid.UUID(str(by)) if by else None)
        n = int(send.get("n") or 0) + 1
        if doc is None or (doc.state or {}).get("status") == "void" or not to:
            send = {"state": "failed", "n": n, "at": now_stamp(),
                    "error": t("subscriptions.err_send_no_email") if doc is not None and not to
                    else t("subscriptions.err_send_void")}
            periods[str(k)]["send"] = send
            await write_agreement(session, company_id, template_id, {"periods": periods}, actor.id)
            await session.commit()
            return send
        message = await doc_email(session, company_id, entry["doc_id"], doc.state, to, actor)
        periods[str(k)]["send"] = {"state": "sending", "n": n, "at": now_stamp()}
        await write_agreement(session, company_id, template_id, {"periods": periods}, actor.id)
        await session.commit()
    try:
        ok, detail = await email.send_email(**message)
    except Exception as exc:
        log.exception("Sending period %s of %s failed", k, template_id)
        ok, detail = False, str(exc)
    async with factory() as session:
        proj = await locked_agreement(session, company_id, template_id)
        periods = periods_of(proj.state)
        send = {"state": "sent" if ok else "failed", "n": n, "at": now_stamp(), **({} if ok else {"error": detail})}
        periods[str(k)]["send"] = send
        await write_agreement(session, company_id, template_id, {"periods": periods}, actor.id)
        if ok:
            try:
                async with session.begin_nested():
                    await mark_doc_sent(session, company_id, entry["doc_id"], actor, DocSendBody(
                        sent_via="email", sent_to=to, idempotency_key=f"send:{entry['doc_id']}:{n}"),
                        source="subscription")
            except HTTPException as exc:
                # Voided or closed while the email was on its way: the email went, the document keeps its state.
                log.warning("Period %s of %s was emailed but not marked sent: %s", k, template_id, exc.detail)
        await session.commit()
    return send


@asynccontextmanager
async def same_session(session: AsyncSession):
    """A session factory that hands out the caller's own session."""
    yield session
