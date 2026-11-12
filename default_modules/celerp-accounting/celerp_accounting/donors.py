# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Donors: the gifts each contact gave that were received and kept, and their statements.

A gift is cash received for a line whose item sits in a gift category (each receipt's share
of the document, so a ticket or dues line on the same invoice never counts), net of what was
refunded or charged back of that receipt, plus money posted by hand or by a bank line to a gift
income account with the donor named on the line. A posting the books made for a document never
counts again, so an invoice settled by a bank line counts once. Credit applied to an invoice is
not cash and counts nothing. Amounts are in the books' currency at each receipt's own rate.

A statement lists one donor's gifts in a period under the company's wording (``WORDING``,
tax-neutral by default). Sending one records it on the contact (``STATEMENT_SENT``) under a key
naming the donor, the period and the wording version, so sending again answers with the first
result; a resend is asked for by name, and a change of wording makes a new statement.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from fasthtml.common import H2, Div, P, Table, Tbody, Td, Th, Thead, Tr
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import catalog_refusal
from celerp.db import get_session
from celerp.downloads import content_disposition
from celerp.events.engine import emit_event, find_event_by_idempotency
from celerp.models.company import Company
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services import email as email_service
from celerp.services.account_roles import current_settings, merge_survivors
from celerp.services.auth import get_current_company_id, get_current_user
from celerp.services.auto_je import _line_allocations
from celerp.services.business_time import company_today
from celerp.services.company_lock import lock_company, locked_company
from celerp.services.document_lines import line_item_id
from celerp.services.journal_accounts import income_account_of
from celerp.services.ledger_rows import journal_rows
from celerp.services.money import books_currency, round_money, to_base, to_decimal, to_stored_float
from celerp.services.permissions import require_permission
from celerp.services.recognition import gift_categories
from celerp_accounting.routes import _je_doc_refs, _line_amounts, _line_party, _require_contact, _require_date_range
from celerp_docs.doc_constants import CREDIT_APPLICATION_METHODS
from celerp_docs.doc_projections import UNISSUED_STATUSES
from ui.components.encode import to_markup
from ui.i18n import t

router = APIRouter()

WORDING = "donor_statement_wording"  # company setting {"text", "version"}
STATEMENT_SENT = "crm.contact.statement_sent"
_SIGN = {"invoice": 1, "credit_note": -1}


def _gift(day: str, amount: Decimal, base: str, **source) -> dict:
    return {"date": day, "amount": to_stored_float(round_money(amount, base)), **source}


def _in_period(day: str, date_from: str | None, date_to: str | None) -> bool:
    return bool(day) and (not date_from or day >= date_from) and (not date_to or day <= date_to)


async def _gift_items(session: AsyncSession, company_id, gifts: frozenset[str]) -> dict[str, dict]:
    """Every item in a gift category, by id."""
    if not gifts:
        return {}
    rows = (await session.execute(select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "item",
        Projection.state["category"].as_string().in_(sorted(gifts))))).all()
    return {str(eid): state or {} for eid, state in rows}


def _document_gifts(doc_id: str, doc: dict, items: dict[str, dict], base: str,
                    date_from: str | None, date_to: str | None) -> list[dict]:
    """The cash kept of each receipt on ``doc`` that its gift lines account for: the gift lines'
    share of the document's net over its total, so tax and other lines take their own share."""
    lines = doc.get("line_items") or []
    shares = _line_allocations(doc, base, 0)
    gift = sum((to_decimal(share["doc"]) for index, share in shares.items()
                if isinstance(lines[int(index)], dict) and str(line_item_id(lines[int(index)])) in items),
               Decimal(0))
    total = to_decimal(round_money(doc.get("total") or 0, doc.get("currency", "USD")))
    if not gift or total <= 0:
        return []
    out = []
    for payment in doc.get("payments") or []:
        day = str(payment.get("payment_date") or "")[:10]
        if (payment.get("status", "active") != "active" or payment.get("method") in CREDIT_APPLICATION_METHODS
                or not _in_period(day, date_from, date_to)):
            continue
        kept = to_decimal(payment.get("amount") or 0) - to_decimal(payment.get("refunded") or 0)
        rate = ((payment.get("books") or {}).get("settlement_rate") or payment.get("conversion_rate")
                or doc.get("conversion_rate") or 1)
        amount = to_decimal(to_base(kept * gift / total, rate, base)) * _SIGN[doc["doc_type"]]
        if amount:
            out.append(_gift(day, amount, base, doc_id=doc_id, ref=doc.get("ref_id") or ""))
    return out


async def donor_history(session: AsyncSession, company_id, contact_id: str | None,
                        date_from: str | None, date_to: str | None) -> dict[str, dict]:
    """Each donor's gifts dated in the period and their total, {contact: {"total", "gifts"}};
    ``contact_id`` narrows it to that donor. A contact merged into another gives as the one that
    survives it."""
    settings = await current_settings(session, company_id)
    base = books_currency(settings)
    gifts = gift_categories(settings)
    items = await _gift_items(session, company_id, gifts)
    survivors = await merge_survivors(session, company_id)
    found: dict[str, list[dict]] = {}

    docs = (await session.execute(select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "doc",
        Projection.state["doc_type"].as_string().in_(sorted(_SIGN))))).all() if items else []
    for doc_id, doc in docs:
        party = (doc or {}).get("contact_id")
        party = survivors.get(party, party)
        if not party or doc.get("status", "draft") in UNISSUED_STATUSES or contact_id not in (None, party):
            continue
        found.setdefault(party, []).extend(_document_gifts(doc_id, doc, items, base, date_from, date_to))

    accounts = {code for code in ((settings.get("category_income_accounts") or {}).get(key) for key in gifts) if code}
    accounts |= {code for item in items.values() if (code := income_account_of(settings, item))}
    posted = [(je_id, state, day) for je_id, state, day in await journal_rows(session, company_id)
              if _in_period(day, date_from, date_to)
              and any(entry.get("account") in accounts for entry in state.get("entries") or [])]
    refs = await _je_doc_refs(session, company_id, [je_id for je_id, _, _ in posted])
    for je_id, state, day in posted:
        if (refs.get(je_id) or {}).get("doc_id"):
            continue  # the books' posting for a document: its receipts count above
        for entry in state.get("entries") or []:
            amounts = _line_amounts(entry) if entry.get("account") in accounts else None
            party = _line_party(refs, je_id, entry, survivors) if amounts else ""
            if party and contact_id in (None, party) and (net := amounts[1] - amounts[0]):
                found.setdefault(party, []).append(_gift(day, net, base, je_id=je_id, ref=state.get("memo") or ""))

    return {party: {"total": to_stored_float(round_money(sum(to_decimal(g["amount"]) for g in rows), base)),
                    "gifts": sorted(rows, key=lambda g: (g["date"], g.get("doc_id") or g.get("je_id") or ""))}
            for party, rows in found.items() if rows}


# --- Wording ---

def wording(settings: dict) -> dict:
    """The statement wording and its version; the tax-neutral default until a company sets its own."""
    stored = settings.get(WORDING) or {}
    return {"text": stored.get("text") or t("donors.statement_wording_default"), "version": int(stored.get("version") or 0)}


def _worded(text: str, name: str) -> str:
    return text.replace("{name}", name)


class WordingIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)

    @field_validator("text")
    @classmethod
    def _not_blank(cls, text: str) -> str:
        if not text.strip():
            raise ValueError("blank")
        return text.strip()


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
    """The statement email: the wording, then each gift with its date and amount, then the total."""
    def cells(*values):
        return Tr(*(Td(v) for v in values))

    return str(to_markup(Div(
        P(_worded(text, row["name"])),
        H2(t("donors.statement_heading", start=date_from, end=date_to)),
        Table(Thead(Tr(Th(t("th.date")), Th(t("th.source")), Th(t("label.amount")))),
              Tbody(*(cells(g["date"], g.get("ref") or "--", f"{g['amount']:,.2f} {currency}") for g in row["gifts"]),
                    cells("", t("th.total"), f"{row['total']:,.2f} {currency}"))),
        P(company),
    )))


def _statement_key(party: str, date_from: str, date_to: str, version: int) -> str:
    """The idempotency key of a donor's statement for a period under one wording version."""
    return f"donor-statement:{party}:{date_from}:{date_to}:v{version}"


async def _with_sent(session: AsyncSession, company_id, rows: list[dict], date_from: str, date_to: str,
                     version: int) -> list[dict]:
    """``rows`` each marked with whether the period's statement under this wording was sent."""
    keys = {_statement_key(row["contact_id"], date_from, date_to, version): row for row in rows}
    sent = set((await session.execute(select(LedgerEntry.idempotency_key).where(
        LedgerEntry.company_id == company_id, LedgerEntry.idempotency_key.in_(sorted(keys))))).scalars()) if keys else set()
    return [{**row, "statement_sent": key in sent} for key, row in keys.items()]


async def _free_key(session: AsyncSession, company_id, key: str) -> str:
    """The key of the next resend of the statement ``key``."""
    n = 1
    while await find_event_by_idempotency(session, company_id, f"{key}:resend:{n}") is not None:
        n += 1
    return f"{key}:resend:{n}"


class SendIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    date_from: date = Field(alias="from")
    date_to: date = Field(alias="to")
    confirm: bool
    resend: list[str] = []


async def send_statements(session: AsyncSession, company_id, user_id, payload: SendIn) -> list[dict]:
    """Email each donor of the period their statement and answer per donor: sent, no_email (no
    address, skipped) or failed (nothing recorded, so sending again retries). A statement already
    sent answers with its first result, marked ``repeat``; ``resend`` sends again to the donors it names, and only
    to them. Each donor holds the
    company from the check to its record and commits on its own, so one failure never undoes
    another's record and a second send at once waits and answers with the first."""
    date_from, date_to = _period(payload.date_from.isoformat(), payload.date_to.isoformat())
    settings = await current_settings(session, company_id)
    words = wording(settings)
    currency = books_currency(settings)
    company = (await session.get(Company, company_id)).name
    results = []
    rows = await _donor_rows(session, company_id, date_from, date_to)
    for row in [r for r in rows if r["contact_id"] in payload.resend] if payload.resend else rows:
        party = row["contact_id"]
        answer = {"contact_id": party, "name": row["name"], "total": row["total"]}
        if row["no_email"]:
            results.append({**answer, "state": "no_email"})
            continue
        key = _statement_key(party, date_from, date_to, words["version"])
        await lock_company(session, company_id)  # two sends at once email a donor once
        earlier = await find_event_by_idempotency(session, company_id, key)
        if earlier is not None and party not in payload.resend:
            results.append({**((earlier.metadata_ or {}).get("result") or answer), "repeat": True})
            await session.rollback()
            continue
        if earlier is not None:
            key = await _free_key(session, company_id, key)
        sent = await email_service.send_email(
            to=row["email"], subject=t("donors.statement_subject", company=company, start=date_from, end=date_to),
            body_html=_statement_html(company, row, words["text"], date_from, date_to, currency),
            body_text=_worded(words["text"], row["name"]), from_name=company, operation_id=key,
            fail_closed_on_uncertain=True)
        if not sent.ok:
            await session.rollback()
            results.append({**answer, "state": "failed", "detail": sent.detail})
            continue
        result = {**answer, "state": "sent", "email": row["email"]}
        await emit_event(session, company_id=company_id, entity_id=party, entity_type="contact",
                         event_type=STATEMENT_SENT, actor_id=user_id, location_id=None, source="api",
                         data={"date_from": date_from, "date_to": date_to, "version": words["version"],
                               "total": row["total"], "email": row["email"]},
                         idempotency_key=key, metadata_={"result": result})
        await session.commit()
        results.append(result)
    return results


_READ = (require_permission("view_contacts"), require_permission("view_payments"))


@router.get("")
async def list_donors(
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    company_id: uuid.UUID = Depends(get_current_company_id),
    _c: None = _READ[0], _p: None = _READ[1],
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Every donor with gifts in the period (all time without one), largest total first."""
    return {"items": await _donor_rows(session, company_id, *_period(date_from, date_to))}


@router.get("/statements/preview")
async def preview_statements(
    date_from: str = Query(alias="from"),
    date_to: str = Query(alias="to"),
    company_id: uuid.UUID = Depends(get_current_company_id),
    _c: None = _READ[0], _p: None = _READ[1],
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Who a send for the period reaches, each with their total and gifts and whether their
    statement under the current wording was sent; a donor with no email address is marked and
    would be skipped."""
    words = wording(await current_settings(session, company_id))
    rows = await _donor_rows(session, company_id, *_period(date_from, date_to))
    return {"donors": await _with_sent(session, company_id, rows, date_from, date_to, words["version"]),
            "wording": words}


@router.post("/statements/send")
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


@router.get("/statements/wording")
async def get_wording(
    company_id: uuid.UUID = Depends(get_current_company_id),
    _c: None = _READ[0], _p: None = _READ[1],
    session: AsyncSession = Depends(get_session),
) -> dict:
    return wording(await current_settings(session, company_id))


@router.put("/statements/wording")
async def put_wording(
    payload: WordingIn,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = require_permission("send_statements"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Set the statement wording ("{name}" is the donor's name); each change is a new version,
    so statements already sent under the old wording can be sent again corrected."""
    company = await locked_company(session, company_id)
    settings = dict(company.settings or {})
    current = wording(settings)
    if payload.text != current["text"]:
        settings[WORDING] = {"text": payload.text, "version": current["version"] + 1}
        company.settings = settings
        session.add(company)
    await session.commit()
    return wording(settings)


@router.get("/{contact_id}/history")
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


@router.get("/{contact_id}/statement.pdf")
async def statement_pdf(
    contact_id: str,
    date_from: str = Query(alias="from"),
    date_to: str = Query(alias="to"),
    company_id: uuid.UUID = Depends(get_current_company_id),
    _c: None = _READ[0], _p: None = _READ[1],
    session: AsyncSession = Depends(get_session),
) -> Response:
    """One donor's statement for the period as a PDF, through the document PDF renderer."""
    from celerp.output.pdf import generate_document_pdf

    contact = (await _require_contact(session, company_id, contact_id)).state or {}
    found = (await donor_history(session, company_id, contact_id, *_period(date_from, date_to))).get(contact_id) \
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
