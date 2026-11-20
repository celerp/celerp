# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Donors: the gifts each contact gave as the books keep them, and their statements.
A donor's gifts are the posted lines stamped ``gift`` when they were posted (auto_je stamps the
part of a document's revenue its gift lines account for, a credit note takes back what its
invoice posted as gifts, and a manual line naming its donor on a gift income account is stamped
at posting), attributed to the party each line belongs to (_line_party, as on the ledger), at
the amount the books posted in their own currency. Nothing is classified again when read, so a
later change of an item's category, the gift setting or a category's account moves no gift.
Rate differences on receipts land where the books put them (exchange gain or loss), refunds
and voided receipts move no gift income.
A statement lists one donor's gifts in a period under the company's wording (``WORDING``,
tax-neutral by default). Sending one records it on the contact (``STATEMENT_SENT``) under a key
naming the donor, the period and the wording version, so sending again answers with the first
result; a resend is asked for by name with the number of the send it asks for, so one press sends
once, and a change of wording makes a new statement.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from fasthtml.common import H2, Div, P, Table, Tbody, Td, Th, Thead, Tr
from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import catalog_refusal
from celerp.db import get_session
from celerp.downloads import content_disposition
from celerp.events.engine import emit_event, find_event_by_idempotency
from celerp.models.company import Company
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.output.doc_print import fmt_money
from celerp.services import email as email_service
from celerp.services.account_roles import current_settings, merge_survivors
from celerp.services.auth import get_current_company_id, get_current_user
from celerp.services.business_time import company_today
from celerp.services.company_lock import lock_company, locked_company
from celerp.services.ledger_rows import journal_rows
from celerp.services.money import books_currency, round_money, to_decimal, to_stored_float
from celerp.services.permissions import require_permission
from celerp.services.visible_text import visible_text
from celerp_accounting.routes import (
    _je_doc_refs, _line_amounts, _line_party, _require_contact, _require_date_range, open_period,
)
from ui.components.encode import to_markup
from ui.i18n import t

router = APIRouter()

WORDING = "donor_statement_wording"  # company setting {"text", "version"}
STATEMENT_SENT = "crm.contact.statement_sent"


def _in_period(day: str, date_from: str | None, date_to: str | None) -> bool:
    return bool(day) and (not date_from or day >= date_from) and (not date_to or day <= date_to)


async def donor_history(session: AsyncSession, company_id, contact_id: str | None,
                        date_from: str | None, date_to: str | None) -> dict[str, dict]:
    """Each donor's gifts dated in the period, newest first, and their total, {contact: {"total", "gifts"}};
    ``contact_id`` narrows it to that donor. A gift is a posted line stamped ``gift`` (classified
    once, at posting), attributed to the party the line belongs to (_line_party), so a contact
    merged into another gives as the one that survives it. One gift per posting and party."""
    base = books_currency(await current_settings(session, company_id))
    survivors = await merge_survivors(session, company_id)
    posted = [(je_id, state, day) for je_id, state, day in await journal_rows(session, company_id)
              if _in_period(day, date_from, date_to) and any(e.get("gift") for e in state.get("entries") or [])]
    refs = await _je_doc_refs(session, company_id, [je_id for je_id, _, _ in posted])
    found: dict[str, list[dict]] = {}
    for je_id, state, day in posted:
        ref = refs.get(je_id) or {}
        given: dict[str, Decimal] = {}
        for entry in state.get("entries") or []:
            if entry.get("gift") and (amounts := _line_amounts(entry)):
                party = _line_party(refs, je_id, entry, survivors)
                if party and contact_id in (None, party):
                    given[party] = given.get(party, Decimal(0)) + amounts[1] - amounts[0]
        source = {"doc_id": ref["doc_id"], "ref": ref.get("doc_ref") or ""} if ref.get("doc_id") \
            else {"je_id": je_id, "ref": state.get("memo") or ""}
        for party, amount in given.items():
            if amount := round_money(amount, base):
                found.setdefault(party, []).append({"date": day, "amount": to_stored_float(amount), **source})

    return {party: {"total": to_stored_float(round_money(sum(to_decimal(g["amount"]) for g in rows), base)),
                    "gifts": sorted(rows, key=lambda g: (g["date"], g.get("doc_id") or g.get("je_id") or ""),
                                    reverse=True)}
            for party, rows in found.items() if rows}


# --- Wording ---

def wording(settings: dict) -> dict:
    """The statement wording and its version; the tax-neutral default until a company sets its own."""
    stored = settings.get(WORDING) or {}
    return {"text": stored.get("text") or t("donors.statement_wording_default"), "version": int(stored.get("version") or 0)}


def _worded(text: str, name: str) -> str:
    return text.replace("{name}", name)


class WordingIn(BaseModel):
    text: str = Field(max_length=2000)


def _wording_text(raw: str) -> str:
    """The wording as stored, trimmed; refused when nothing in it would show on a statement."""
    if not (text := visible_text(raw)):
        raise HTTPException(status_code=422, detail=catalog_refusal("donors.wording_required"))
    return text


# --- Donors and statements ---

async def _donor_rows(session: AsyncSession, company_id, date_from: str | None, date_to: str | None) -> list[dict]:
    """Every donor with gifts in the period: name, email, total and gifts, largest total first."""
    history = await donor_history(session, company_id, None, date_from, date_to)
    contacts = {str(eid): state or {} for eid, state in (await session.execute(select(
        Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "contact",
        Projection.entity_id.in_(sorted(history))))).all()} if history else {}
    rows = [{"contact_id": party, "name": contacts.get(party, {}).get("name") or party,
             "email": (contacts.get(party, {}).get("email") or "").strip() or None, **found}
            for party, found in history.items()]
    for row in rows:
        row["no_email"] = row["email"] is None
    return sorted(rows, key=lambda r: (-r["total"], r["name"].lower()))


def _period(date_from: str | None, date_to: str | None) -> tuple[str | None, str | None]:
    _require_date_range(date_from, date_to)
    return date_from, date_to


def _statement_html(company: str, row: dict, text: str, date_from: str, date_to: str, currency: str) -> str:
    """The statement email: the wording, then each gift with its date and amount, then the total.
    Headers are centered and the amount column right-aligned over its figures, inline because
    mail clients drop stylesheets."""
    def cells(day, source, amount):
        return Tr(Td(day), Td(source), Td(fmt_money(amount, currency), style="text-align: right"))

    return str(to_markup(Div(
        P(_worded(text, row["name"])),
        H2(t("donors.statement_heading", start=date_from, end=date_to)),
        Table(Thead(Tr(Th(t("th.date"), style="text-align: center"), Th(t("th.source"), style="text-align: center"),
                       Th(t("label.amount"), style="text-align: right"))),
              Tbody(*(cells(g["date"], g.get("ref") or "--", g["amount"]) for g in row["gifts"]),
                    cells("", t("th.total"), row["total"]))),
        P(company),
    )))


def _statement_key(party: str, date_from: str, date_to: str, version: int) -> str:
    """The idempotency key of a donor's statement for a period under one wording version."""
    return f"donor-statement:{party}:{date_from}:{date_to}:v{version}"


def _send_key(statement: str, n: int) -> str:
    """The key of send ``n`` of a statement: 0 its first send, n resend n."""
    return f"{statement}:resend:{n}" if n else statement


async def _taken(session: AsyncSession, company_id, statements: list[str]) -> dict[str, dict[int, str | None]]:
    """The sends recorded for each statement key, in order of their number (0 its first send, n
    resend n), each with its email's recorded state."""
    taken: dict[str, dict[int, str | None]] = {key: {} for key in statements}
    for key, meta in (await session.execute(select(LedgerEntry.idempotency_key, LedgerEntry.metadata_).where(
            LedgerEntry.company_id == company_id,
            or_(LedgerEntry.idempotency_key.in_(statements),
                *(LedgerEntry.idempotency_key.startswith(f"{k}:resend:", autoescape=True) for k in statements))))).all():
        statement, _, n = key.rpartition(":resend:") if key not in taken else (key, "", "0")
        if statement in taken and n.isdecimal():
            taken[statement][int(n)] = ((meta or {}).get("email") or {}).get("state")
    return {key: dict(sorted(sends.items())) for key, sends in taken.items()}


async def _with_sends(session: AsyncSession, company_id, rows: list[dict], date_from: str, date_to: str,
                      version: int) -> list[dict]:
    """``rows`` each with ``sends``, the number the next Send again of the period's statement under
    this wording names: 0 while it was never sent, one past the last send otherwise, or the last
    send itself while its email failed, so sending again retries it."""
    keys = {_statement_key(row["contact_id"], date_from, date_to, version): row for row in rows}
    taken = await _taken(session, company_id, list(keys))

    def sends(numbers: dict[int, str | None]) -> int:
        last = max(numbers, default=-1)
        return last if last >= 0 and numbers[last] == "failed" else last + 1
    return [{**row, "sends": sends(taken[key])} for key, row in keys.items()]


class SendIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    date_from: date = Field(alias="from")
    date_to: date = Field(alias="to")
    confirm: bool
    resend: dict[str, NonNegativeInt] = {}


async def send_statements(session: AsyncSession, company_id, user_id, payload: SendIn) -> list[dict]:
    """Email each donor of the period their statement and answer per donor: sent, no_email (no
    address, skipped), failed (the transport refused it; sending again retries the same send) or
    unconfirmed (it may have gone out, so it is never sent again through a transport that could
    deliver it twice). A statement already sent answers with its stored result, marked ``repeat``;
    ``resend`` sends again to the donors it names, and only to them, each with the number of the
    send it asks for (``sends`` as the asker read it), so the same press arriving twice sends once
    and the second answers with that send as stored, marked ``repeat``. Each donor's send goes
    through the one durable send (email.send_durably): it holds the company from the check to its
    record, records the attempt on the send's key and commits before the email goes out, so a
    second send at once waits and answers with the first and no lock is held while it goes."""
    date_from, date_to = _period(payload.date_from.isoformat(), payload.date_to.isoformat())
    settings = await current_settings(session, company_id)
    words = wording(settings)
    currency = books_currency(settings)
    company = (await session.get(Company, company_id)).name
    results = []
    rows = await _donor_rows(session, company_id, date_from, date_to)
    for row in [r for r in rows if r["contact_id"] in payload.resend] if payload.resend else rows:
        answer = {"contact_id": row["contact_id"], "name": row["name"], "total": row["total"]}
        if row["no_email"]:
            results.append({**answer, "state": "no_email"})
            continue
        message = {"to": row["email"],
                   "subject": t("donors.statement_subject", company=company, start=date_from, end=date_to),
                   "body_html": _statement_html(company, row, words["text"], date_from, date_to, currency),
                   "body_text": _worded(words["text"], row["name"]), "from_name": company}
        results.append(await _send_statement(session, company_id, user_id, payload, row, answer, message,
                                             date_from, date_to, words["version"]))
    return results


async def _send_statement(session: AsyncSession, company_id, user_id, payload: SendIn, row: dict, answer: dict,
                          message: dict, date_from: str, date_to: str, version: int) -> dict:
    """One donor's statement through email.send_durably; the attempt lives on the send's own
    STATEMENT_SENT record (metadata ``email``), its answer in metadata ``result``. The donor's
    address is read again just before the email goes out (the batch read it before its first
    send): one that changed meanwhile is not sent to, and the statement answers failed, so the
    next Send goes to the address the donor has then."""
    party = row["contact_id"]
    statement = _statement_key(party, date_from, date_to, version)
    held: dict = {}

    async def begin(s) -> list:
        await lock_company(s, company_id)  # two sends at once email a donor once
        taken, n = (await _taken(s, company_id, [statement]))[statement], payload.resend.get(party, 0)
        n = n if n in taken or party in payload.resend or not taken else next(iter(taken))
        key = _send_key(statement, n)
        entry = await find_event_by_idempotency(s, company_id, key) if n in taken else None
        record = (entry.metadata_ or {}).get("email") if entry is not None else None
        step = "new" if entry is None else email_service.next_step(record)
        if step not in ("new", "replay"):
            state = email_service.attempt_state(record, datetime.now(timezone.utc))
            held["answer"] = {**entry.metadata_["result"], "repeat": True,  # still sending: it may have gone out
                              "state": "unconfirmed" if state in (None, "sending") else state}
            return []
        attempt = email_service.Attempt(
            record["operation_id"] if step == "replay" else
            str(uuid.uuid5(uuid.NAMESPACE_URL, f"celerp-donor-statement:{company_id}:{key}")),
            message)  # the address read now: a replay to one the donor no longer has fails its digest
        if step == "replay" and email_service.payload_digest(attempt.message) != record["digest"]:
            held["answer"] = {**entry.metadata_["result"], "state": "unconfirmed", "repeat": True}
            return []
        stored = {"result": {**answer, "state": "sending", "email": attempt.message["to"]},
                  "email": {**email_service.attempt_record(attempt, record), "to": attempt.message["to"]}}
        if entry is None:
            entry = await emit_event(s, company_id=company_id, entity_id=party, entity_type="contact",
                                     event_type=STATEMENT_SENT, actor_id=user_id, location_id=None, source="api",
                                     data={"date_from": date_from, "date_to": date_to, "version": version,
                                           "total": row["total"], "email": attempt.message["to"]},
                                     idempotency_key=key, metadata_=stored)
        else:
            entry.metadata_ = {**entry.metadata_, **stored}
        held["key"] = key
        return [attempt]

    async def current(s, attempt) -> bool:
        state = (await s.execute(select(Projection.state).where(
            Projection.company_id == company_id, Projection.entity_type == "contact",
            Projection.entity_id == party))).scalar_one_or_none() or {}
        return (state.get("email") or "").strip() == attempt.message["to"]

    async def settle(s, results: list) -> dict:
        if "key" not in held:
            await s.rollback()
            return held["answer"]
        await lock_company(s, company_id)
        entry = await find_event_by_idempotency(s, company_id, held["key"])
        record = email_service.settled_record(entry.metadata_["email"], results[0])
        result = {**entry.metadata_["result"], "state": record["state"],
                  **({"detail": record["detail"]} if record["state"] == "failed" else {})}
        entry.metadata_ = {**entry.metadata_, "email": record, "result": result}
        return result

    return await email_service.send_durably(session, begin, current, settle)


_READ = (require_permission("view_contacts"), require_permission("view_payments"))


@router.get("", openapi_extra={"x-celerp-agent": True}, summary="List every donor with gifts in a period, largest total first.")
async def list_donors(
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    company_id: uuid.UUID = Depends(get_current_company_id),
    _c: None = _READ[0], _p: None = _READ[1],
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Every donor with gifts in the period (all time without one), largest total first."""
    return {"items": await _donor_rows(session, company_id, *_period(date_from, date_to))}


@router.get("/statements/preview", openapi_extra={"x-celerp-agent": True}, summary="Show who a donor statement send for a period would reach, with each donor's total.")
async def preview_statements(
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    company_id: uuid.UUID = Depends(get_current_company_id),
    _c: None = _READ[0], _p: None = _READ[1],
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Who a send for the period reaches, each with their total and gifts and the number the next
    Send again of their statement under the current wording names (0 while it was never sent); a donor with no email address is marked and
    would be skipped. An open end runs from the first entry or to today, and ``period`` names
    the dates the send and each statement then cover."""
    words = wording(await current_settings(session, company_id))
    date_from, date_to = await open_period(session, company_id, date_from, date_to)
    rows = await _donor_rows(session, company_id, date_from, date_to)
    return {"donors": await _with_sends(session, company_id, rows, date_from, date_to, words["version"]),
            "wording": words, "period": {"from": date_from, "to": date_to}}


@router.post("/statements/send", openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/donors", "x-celerp-agent-outside": True}, summary="Email donor statements for a period to every donor with gifts in it.")
async def send_statements_route(
    payload: SendIn,
    company_id: uuid.UUID = Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("send_statements"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Email the period's statements; refused without explicit confirmation. A sent email cannot
    be recalled from the donor's mailbox, so the send is recorded and a corrected statement goes
    out under new wording or by an explicit resend."""
    if not payload.confirm:
        raise HTTPException(status_code=422, detail=catalog_refusal("donors.err_confirm_send"))
    return {"results": await send_statements(session, company_id, user.id, payload)}


@router.get("/statements/wording", openapi_extra={"x-celerp-agent": True}, summary="Read the wording printed on donor statements.")
async def get_wording(
    company_id: uuid.UUID = Depends(get_current_company_id),
    _c: None = _READ[0], _p: None = _READ[1],
    session: AsyncSession = Depends(get_session),
) -> dict:
    return wording(await current_settings(session, company_id))


@router.put("/statements/wording", openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/donors"}, summary="Change the wording printed on donor statements.")
async def put_wording(
    payload: WordingIn,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = require_permission("send_statements"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Set the statement wording ("{name}" is the donor's name); each change is a new version,
    so statements already sent under the old wording can be sent again corrected."""
    text = _wording_text(payload.text)
    company = await locked_company(session, company_id)
    settings = dict(company.settings or {})
    current = wording(settings)
    if text != current["text"]:
        settings[WORDING] = {"text": text, "version": current["version"] + 1}
        company.settings = settings
        session.add(company)
    await session.commit()
    return wording(settings)


@router.get("/{contact_id}/history", openapi_extra={"x-celerp-agent": True}, summary="Show one donor's gifts in a period and their total.")
async def donor_history_route(
    contact_id: str,
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    company_id: uuid.UUID = Depends(get_current_company_id),
    _c: None = _READ[0], _p: None = _READ[1],
    session: AsyncSession = Depends(get_session),
) -> dict:
    """One donor's gifts in the period and their total."""
    await _require_contact(session, company_id, contact_id)
    found = await donor_history(session, company_id, contact_id, *_period(date_from, date_to))
    return found.get(contact_id) or {"total": 0.0, "gifts": []}


@router.get("/{contact_id}/statement.pdf", openapi_extra={"x-celerp-agent": True, "x-celerp-agent-download": "/donors/{contact_id}/statement.pdf"}, summary="Download one donor's statement for a period as a PDF file.")
async def statement_pdf(
    contact_id: str,
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    company_id: uuid.UUID = Depends(get_current_company_id),
    _c: None = _READ[0], _p: None = _READ[1],
    session: AsyncSession = Depends(get_session),
) -> Response:
    """One donor's statement for the period as a PDF, through the document PDF renderer; an open
    end runs from the first entry or to today, as on the preview."""
    from celerp.output.pdf import generate_document_pdf

    contact = (await _require_contact(session, company_id, contact_id)).state or {}
    date_from, date_to = await open_period(session, company_id, date_from, date_to)
    found = (await donor_history(session, company_id, contact_id, date_from, date_to)).get(contact_id) \
        or {"total": 0.0, "gifts": []}
    company_row = await session.get(Company, company_id)
    settings = company_row.settings or {}
    name = contact.get("name") or contact_id
    doc = {"doc_type": "donor_statement", "ref_id": f"{date_from} to {date_to}", "currency": books_currency(settings),
           "issue_date": (await company_today(session, company_id)).isoformat(),
           "contact_id": contact_id, "contact_name": name, "contact_email": contact.get("email") or "",
           "line_items": [{"description": f"{g['date']} {g.get('ref') or ''}".strip(), "quantity": 1,
                           "unit_price": g["amount"], "line_total": g["amount"]} for g in found["gifts"]],
           "subtotal": found["total"], "total": found["total"], "tax": 0,
           "customer_note": _worded(wording(settings)["text"], name)}
    pdf = await asyncio.to_thread(generate_document_pdf, doc, {"name": company_row.name} | settings)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": content_disposition(
                        "inline", f"statement-{date_from}-{date_to}.pdf")})
