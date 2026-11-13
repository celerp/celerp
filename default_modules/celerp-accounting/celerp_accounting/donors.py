# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Donors: the gifts each contact gave as the books keep them, and their statements.

A donor's gifts are read from the posted lines on the gift income accounts (the accounts the
gift categories and items are credited to), attributed to the party each line belongs to
(_line_party, as on the ledger), at the amount the books posted in their own currency. Where a
posting records the document lines behind it (its line allocations), only the part its gift
lines account for counts (_gift_shares), so a ticket or dues line on the same account never
counts; sales revenue counts through such postings alone, since nothing else tells its gift
lines apart. Rate differences on receipts land where the books put them (exchange gain or
loss), refunds and voided receipts move no gift income, and a credit note takes back what its
posting takes off gift income, however that credit is settled.

A statement lists one donor's gifts in a period under the company's wording (``WORDING``,
tax-neutral by default). Sending one records it on the contact (``STATEMENT_SENT``) under a key
naming the donor, the period and the wording version, so sending again answers with the first
result; a resend is asked for by name with the number of sends the asker saw, so one press sends
once, and a change of wording makes a new statement.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from fasthtml.common import H2, Div, P, Table, Tbody, Td, Th, Thead, Tr
from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import R, catalog_refusal
from celerp.db import get_session
from celerp.downloads import content_disposition
from celerp.events.engine import emit_event, find_event_by_idempotency
from celerp.models.company import Company
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services import email as email_service
from celerp.services.account_roles import current_settings, merge_survivors, role_map
from celerp.services.auth import get_current_company_id, get_current_user
from celerp.services.auto_je import _posted_line_shares
from celerp.services.business_time import company_today
from celerp.services.company_lock import lock_company, locked_company
from celerp.services.document_lines import line_item_id
from celerp.services.journal_accounts import income_account_of
from celerp.services.ledger_rows import REVERSAL_SUFFIX, journal_rows
from celerp.services.money import books_currency, round_money, to_decimal, to_stored_float
from celerp.services.permissions import require_permission
from celerp.services.recognition import gift_categories
from celerp.services.visible_text import visible_text
from celerp_accounting.routes import _id_chunks, _je_doc_refs, _line_amounts, _line_party, _require_contact, _require_date_range
from ui.components.encode import to_markup
from ui.i18n import t

router = APIRouter()

WORDING = "donor_statement_wording"  # company setting {"text", "version"}
STATEMENT_SENT = "crm.contact.statement_sent"


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


async def _posting_allocations(session: AsyncSession, company_id, je_ids: list[str]) -> dict[str, dict]:
    """The line allocations each entry's creation recorded, by entry id; a reversal row reads
    those of the entry it reverses."""
    originals = {je_id: je_id.removesuffix(REVERSAL_SUFFIX) for je_id in je_ids}
    found: dict[str, dict] = {}
    for chunk in _id_chunks(sorted(set(originals.values()))):
        for entity_id, meta in (await session.execute(select(LedgerEntry.entity_id, LedgerEntry.metadata_).where(
                LedgerEntry.company_id == company_id, LedgerEntry.event_type == "acc.journal_entry.created",
                LedgerEntry.entity_id.in_(chunk)))).all():
            if (meta or {}).get("line_allocations"):
                found[entity_id] = meta["line_allocations"]
    return {je_id: found[original] for je_id, original in originals.items() if original in found}


def _is_gift(line, items: dict[str, dict]) -> bool:
    return isinstance(line, dict) and str(line_item_id(line)) in items


async def _gift_shares(session: AsyncSession, company_id, doc: dict, allocations: dict, items: dict[str, dict],
                       moved: dict[str, Decimal]) -> dict[str, Decimal]:
    """account -> the share of what a document's posting ``moved`` on it that gift lines account
    for, from the posting's line ``allocations``. A line the posting named an account for counts
    there, a credit note line linked to an invoice line as that invoice line; the rest of what a
    credit note moved on an account is what it spread over the revenue of the invoice it credits,
    and counts at the gift share of that invoice's revenue on the account, as its posting
    allocated it."""
    lines = doc.get("line_items") or []
    original = str(doc.get("original_doc_id") or "")
    invoice_lines: dict = {}
    spread: dict[str, list[Decimal]] = {}
    if original:
        row = await session.get(Projection, {"company_id": company_id, "entity_id": original})
        invoice_lines = {li.get("line_id"): li for li in ((row.state or {}) if row is not None else {}).get("line_items") or []
                         if isinstance(li, dict)}
        for line_id, share in (await _posted_line_shares(session, company_id, original)).items():
            _add(spread, share["account"], to_decimal(share["base"]), _is_gift(invoice_lines.get(line_id), items))
    named: dict[str, list[Decimal]] = {}
    for index, share in allocations.items():
        if share.get("account") and int(index) < len(lines):
            line = lines[int(index)]
            linked = invoice_lines.get(line.get("source_line_id")) if isinstance(line, dict) else None
            _add(named, share["account"], to_decimal(share.get("base") or 0), _is_gift(linked or line, items))
    shares = {}
    for account, amount in moved.items():
        gift, counted = named.get(account) or (Decimal(0), Decimal(0))
        spread_gift, spread_all = spread.get(account) or (Decimal(0), Decimal(0))
        if amount > counted and spread_all:
            gift += (amount - counted) * spread_gift / spread_all
        shares[account] = gift / amount if amount else Decimal(0)
    return shares


def _add(totals: dict[str, list[Decimal]], account: str, base: Decimal, gift: bool) -> None:
    """Add ``base`` to the [gift, all] totals of ``account``."""
    pair = totals.setdefault(account, [Decimal(0), Decimal(0)])
    pair[1] += base
    if gift:
        pair[0] += base


async def donor_history(session: AsyncSession, company_id, contact_id: str | None,
                        date_from: str | None, date_to: str | None) -> dict[str, dict]:
    """Each donor's gifts dated in the period, newest first, and their total, {contact: {"total", "gifts"}};
    ``contact_id`` narrows it to that donor. A contact merged into another gives as the one that
    survives it. One gift per posting and party."""
    settings = await current_settings(session, company_id)
    base = books_currency(settings)
    gifts = gift_categories(settings)
    items = await _gift_items(session, company_id, gifts)
    survivors = await merge_survivors(session, company_id)
    mapped = {code for code in ((settings.get("category_income_accounts") or {}).get(key) for key in gifts) if code}
    mapped |= {code for item in items.values() if (code := income_account_of(settings, item))}
    accounts = mapped | {code for code in [role_map(settings).get(R.SALES_REVENUE)] if code and items}
    posted = [(je_id, state, day) for je_id, state, day in await journal_rows(session, company_id)
              if _in_period(day, date_from, date_to)
              and any(entry.get("account") in accounts for entry in state.get("entries") or [])]
    je_ids = [je_id for je_id, _, _ in posted]
    refs = await _je_doc_refs(session, company_id, je_ids)
    allocations = await _posting_allocations(session, company_id, je_ids)
    found: dict[str, list[dict]] = {}
    for je_id, state, day in posted:
        ref = refs.get(je_id) or {}
        lines = [(entry, amounts[1] - amounts[0]) for entry in state.get("entries") or []
                 if entry.get("account") in accounts and (amounts := _line_amounts(entry))]
        if ref.get("doc_id") and je_id in allocations:
            moved: dict[str, Decimal] = {}
            for entry, net in lines:
                moved[entry["account"]] = moved.get(entry["account"], Decimal(0)) + net
            shares = await _gift_shares(session, company_id, ref.get("doc") or {}, allocations[je_id], items,
                                        {code: abs(net) for code, net in moved.items()})
        else:
            shares = dict.fromkeys(mapped, Decimal(1))
        given: dict[str, Decimal] = {}
        for entry, net in lines:
            party = _line_party(refs, je_id, entry, survivors)
            if party and contact_id in (None, party):
                given[party] = given.get(party, Decimal(0)) + net * shares.get(entry["account"], Decimal(0))
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
        return Tr(Td(day), Td(source), Td(f"{amount:,.2f} {currency}", style="text-align: right"))

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
    """The key of send ``n`` of a statement: its first send, then each resend after it."""
    return f"{statement}:resend:{n}" if n else statement


async def _sends(session: AsyncSession, company_id, statements: list[str]) -> dict[str, int]:
    """How many times each statement key was sent: its first send and every resend."""
    if not statements:
        return {}
    keys = set((await session.execute(select(LedgerEntry.idempotency_key).where(
        LedgerEntry.company_id == company_id,
        or_(LedgerEntry.idempotency_key.in_(statements),
            *(LedgerEntry.idempotency_key.startswith(f"{k}:resend:", autoescape=True) for k in statements))))).scalars())
    counts = {}
    for key in statements:
        n = 0
        while _send_key(key, n) in keys:
            n += 1
        counts[key] = n
    return counts


async def _with_sends(session: AsyncSession, company_id, rows: list[dict], date_from: str, date_to: str,
                      version: int) -> list[dict]:
    """``rows`` each with how many times the period's statement under this wording was sent."""
    keys = {_statement_key(row["contact_id"], date_from, date_to, version): row for row in rows}
    sends = await _sends(session, company_id, list(keys))
    return [{**row, "sends": sends[key]} for key, row in keys.items()]


class SendIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    date_from: date = Field(alias="from")
    date_to: date = Field(alias="to")
    confirm: bool
    resend: dict[str, NonNegativeInt] = {}


async def send_statements(session: AsyncSession, company_id, user_id, payload: SendIn) -> list[dict]:
    """Email each donor of the period their statement and answer per donor: sent, no_email (no
    address, skipped) or failed (nothing recorded, so sending again retries). A statement already
    sent answers with its first result, marked ``repeat``; ``resend`` sends again to the donors it names, and only
    to them, each with the number of sends the asker saw, so the same press arriving twice sends once and
    the second answers with that send as stored, marked ``repeat``. Each donor holds the
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
        statement = _statement_key(party, date_from, date_to, words["version"])
        await lock_company(session, company_id)  # two sends at once email a donor once
        sends, seen = (await _sends(session, company_id, [statement]))[statement], payload.resend.get(party, 0)
        if seen < sends:
            earlier = await find_event_by_idempotency(session, company_id, _send_key(statement, seen))
            results.append({**((earlier.metadata_ or {}).get("result") or answer), "repeat": True})
            await session.rollback()
            continue
        key = _send_key(statement, sends)
        sent = await email_service.send_email(
            to=row["email"], subject=t("donors.statement_subject", company=company, start=date_from, end=date_to),
            body_html=_statement_html(company, row, words["text"], date_from, date_to, currency),
            body_text=_worded(words["text"], row["name"]), from_name=company,
            operation_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"celerp-donor-statement:{company_id}:{key}")),
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
    """Who a send for the period reaches, each with their total and gifts and how many times their
    statement under the current wording was sent; a donor with no email address is marked and
    would be skipped."""
    words = wording(await current_settings(session, company_id))
    rows = await _donor_rows(session, company_id, *_period(date_from, date_to))
    return {"donors": await _with_sends(session, company_id, rows, date_from, date_to, words["version"]),
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
