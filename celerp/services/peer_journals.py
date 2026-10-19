# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Journal entries and the lock date between a client company and its accountant.

The accountant's company (the firm) holds the client's copy and a key it issued to the
client. On the client's own sync run the client fetches the firm's manual journal entries
and lock date, posts each journal once in its own books, and sends its own manual journal
entries, its reversals of the firm's journals among them, back to the firm. Each side is
told what the other did: the client by a notice, the firm by its firm view. A journal
that arrived from the other side is never sent back to it, and an entry a document posted
is never sent at all: the document carries it.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import PERIOD_LOCK_PATH
from celerp.events.engine import emit_event, write_period_lock
from celerp.importers.results import failure_reason
from celerp.models.company import Company
from celerp.models.company_peer import CompanyPeerDelivery, CompanyPeerIdentity
from celerp.models.projections import Projection
from celerp.schemas.numbers import FiniteFloat
from celerp.services import journal_reversals, peer_access, peer_accounts, peer_attention
from celerp.services.business_time import business_date_of
from celerp.services.company_lock import locked_company
from celerp.services.je_keys import je_void_data
from celerp.services.posting_readiness import account_names
from ui.i18n import t

FROM_ACCOUNTANT = "/settings/connectors/celerp#from-accountant"
# Period lock under an address of its own, so moving the lock date for older stock never
# withdraws these notices (notifications.service.mark_done).
LOCK_DATE = f"{PERIOD_LOCK_PATH}#period-lock-content"
_CATEGORY = "integrations"
_PAGE = 100
# The delivery marks of the firm's journals the client fetched.
INBOUND = "journals_in"
_SWAPPED = {"debit": "credit", "credit": "debit", "fx_debit": "fx_credit", "fx_credit": "fx_debit"}


# ---- shared ----------------------------------------------------------------------------

def local_id(firm_company: uuid.UUID, firm_journal: str) -> str:
    """The client's id for a journal of the firm's: the same on every run."""
    return "je:peer:" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"celerp-peer-journal:{firm_company}:{firm_journal}"))


def reversal_id(je_id: str) -> str:
    """The id of the one reversal a journal from the firm, or a reversal of one, may have."""
    return "je:reversal:" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"celerp-peer-reversal:{je_id}"))


def _day(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _amount(line: dict) -> tuple[str, Decimal]:
    debit = Decimal(str(line.get("debit") or 0))
    return ("Dr", debit) if debit else ("Cr", Decimal(str(line.get("credit") or 0)))


async def _names(session: AsyncSession, company_id: uuid.UUID, entries: list[dict]) -> dict[str, str]:
    return await account_names(session, company_id, {line.get("account") for line in entries})


def _lines_text(entries: list[dict], names: dict[str, str]) -> str:
    """Each line as its account, with its name from ``names``, then side and amount:
    "6500 Audit fees Dr 125.00; 6200 Accruals Cr 125.00"."""
    out = []
    for line in entries:
        code, (side, amount) = line.get("account"), _amount(line)
        account = code if names[code] == code else f"{code} {names[code]}"
        out.append(f"{account} {side} {amount.quantize(Decimal('0.01'))}")
    return "; ".join(out)


async def _notify(session: AsyncSession, company_id: uuid.UUID, key: str, *,
                  action_url: str = FROM_ACCOUNTANT, **params) -> None:
    """One unmissable notice to the company, told once; the caller commits."""
    from celerp.notifications import service as notification_service
    await notification_service.notify_once(
        session, company_id, _CATEGORY, t(f"{key}.title", "en", **params),
        t(f"{key}.body", "en", **params), action_url=action_url,
        i18n={"title": f"{key}.title", "body": f"{key}.body", "params": params})


async def delivery_marks(session: AsyncSession, company_id: uuid.UUID, grant_id: uuid.UUID,
                         entity: str, entity_ids: list[str]) -> dict[str, CompanyPeerDelivery]:
    if not entity_ids:
        return {}
    rows = (await session.scalars(select(CompanyPeerDelivery).where(
        CompanyPeerDelivery.company_id == company_id, CompanyPeerDelivery.grant_id == grant_id,
        CompanyPeerDelivery.entity == entity, CompanyPeerDelivery.entity_id.in_(entity_ids)))).all()
    return {row.entity_id: row for row in rows}


async def mark(session: AsyncSession, *, company_id: uuid.UUID, grant_id: uuid.UUID, entity: str,
               entity_id: str, revision: int, refused: str | None) -> None:
    """Note the revision of a record that was sent or fetched, and why it was refused or
    declined, if it was; the caller commits."""
    now = datetime.now(timezone.utc)
    await session.execute(pg_insert(CompanyPeerDelivery).values(
        id=uuid.uuid4(), company_id=company_id, grant_id=grant_id, entity=entity,
        entity_id=entity_id, revision=revision, refused=refused, at=now,
    ).on_conflict_do_update(constraint="uq_company_peer_delivery_record",
                            set_={"revision": revision, "refused": refused, "at": now}))


async def sent_marks(company_id: uuid.UUID, grant_id: uuid.UUID, entity: str,
                     entity_ids: list[str]) -> tuple[dict[str, tuple[int, str | None]], str | None]:
    """What became of each record's last delivery to the firm, (revision, refusal), and
    the lock date this key's firm last sent."""
    from celerp.db import get_session_ctx
    async with get_session_ctx() as session:
        marks = await delivery_marks(session, company_id, grant_id, entity, entity_ids)
        company = await session.get(Company, company_id)
        held = _firm_locks(company.settings or {} if company is not None else {}).get(str(grant_id), {})
    lock = held.get("date") if isinstance(held.get("date"), str) else None
    return {eid: (int(m.revision), m.refused) for eid, m in marks.items()}, lock


def refused_by_lock(refused: str | None, firm_lock: str | None) -> bool:
    """Whether a refusal was the firm's lock date, unchanged since: the same revision would
    be refused again, so it is not sent until it changes or the date does."""
    return bool(refused and firm_lock and firm_lock in refused)


async def note_delivery(company_id: uuid.UUID, grant_id: uuid.UUID, entity: str, entity_id: str,
                        revision: int, refused: str | None, record: str) -> None:
    """Note a record the firm took, or refused; a refusal is told to the client once."""
    from celerp.db import get_session_ctx
    async with get_session_ctx() as session:
        await mark(session, company_id=company_id, grant_id=grant_id, entity=entity,
                   entity_id=entity_id, revision=revision, refused=refused)
        if refused is not None:
            await _notify(session, company_id, "notice.peer_record_refused",
                          record=record, reason=refused)
        await session.commit()


# ---- the firm's side: what the client fetches -----------------------------------------

async def _caller_grant(session: AsyncSession, ctx):
    if ctx.peer is None:
        raise HTTPException(403, peer_access.UNAVAILABLE)
    return await peer_access.authenticate(session, ctx.peer.credential,
                                          source_company_id=ctx.peer.source_company_id,
                                          scope=ctx.peer.scope)


async def journal_feed(session: AsyncSession, ctx, after: str = "") -> dict:
    """The firm's own manual journal entries after ``after``, posted or voided, for the
    client its key belongs to; never one that came from that client. A line naming a
    contact the client does not hold is held back with the reason. Each entry carries
    the firm's accounts its lines name, with their parents, for the client to place."""
    grant = await _caller_grant(session, ctx)
    if not isinstance(after, str) or len(after) > 512:
        raise HTTPException(422, "Invalid journal cursor")
    mapped = select(CompanyPeerIdentity.target_entity_id).where(
        CompanyPeerIdentity.company_id == grant.company_id,
        CompanyPeerIdentity.source_company_id == grant.source_company_id,
        CompanyPeerIdentity.entity_type == "journal_entry")
    rows = (await session.execute(select(Projection.entity_id, Projection.version, Projection.state).where(
        Projection.company_id == grant.company_id, Projection.entity_type == "journal_entry",
        Projection.entity_id > after,
        Projection.state["je_type"].as_string() == "manual",
        Projection.state["status"].as_string().in_(("posted", "void")),
        Projection.state["peer_source"].as_string().is_(None),
        Projection.entity_id.not_in(mapped),
    ).order_by(Projection.entity_id).limit(_PAGE))).all()
    contacts = dict((await session.execute(select(
        CompanyPeerIdentity.target_entity_id, CompanyPeerIdentity.source_entity_id).where(
        CompanyPeerIdentity.company_id == grant.company_id,
        CompanyPeerIdentity.source_company_id == grant.source_company_id,
        CompanyPeerIdentity.entity_type == "contact"))).all())
    items = []
    for entity_id, version, state in rows:
        state = state or {}
        held = None
        entries = []
        for line in state.get("entries") or []:
            line = dict(line)
            if line.get("contact"):
                if line["contact"] not in contacts:
                    held = "A line names a contact your books do not hold."
                else:
                    line["contact"] = contacts[line["contact"]]
            entries.append(line)
        accounts = await peer_accounts.outgoing(session, grant.company_id, grant.source_company_id,
                                                [line.get("account") for line in entries])
        items.append({"id": entity_id, "revision": int(version), "memo": state.get("memo") or "",
                      "ts": str(state.get("ts") or "")[:10], "status": state.get("status"),
                      "entries": entries, "held": held, "accounts": accounts})
    peer_access.mark_used(grant)
    await session.commit()
    return {"firm": grant.firm, "items": items,
            "next": rows[-1][0] if len(rows) == _PAGE else None}


async def lock_feed(session: AsyncSession, ctx) -> dict:
    """The firm's lock date for the client its key belongs to, and until when that key
    works, which is as long as the date binds the client."""
    grant = await _caller_grant(session, ctx)
    company = await session.get(Company, grant.company_id)
    lock = (company.settings or {}).get("lock_date")
    expires = grant.expires_at if grant.expires_at.tzinfo else grant.expires_at.replace(tzinfo=timezone.utc)
    await session.commit()
    return {"lock_date": lock if isinstance(lock, str) else None, "firm": grant.firm,
            "expires_at": expires.isoformat()}


# ---- the firm's side: what the client sends ---------------------------------------------

class _Line(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account: str = Field(min_length=1, max_length=64)
    debit: FiniteFloat = Field(ge=0)
    credit: FiniteFloat = Field(ge=0)
    contact: str | None = Field(default=None, max_length=512)
    fx_debit: FiniteFloat | None = None
    fx_credit: FiniteFloat | None = None
    fx_currency: str | None = Field(default=None, max_length=3)
    fx_rate: FiniteFloat | None = None


class _Reverses(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: str | None = Field(default=None, min_length=1, max_length=512)
    journal: str | None = Field(default=None, min_length=1, max_length=512)


class _Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    journal: str = Field(min_length=1, max_length=512)
    declined: str | None = Field(default=None, min_length=1, max_length=2000)


class _Journal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memo: str = Field(default="", max_length=2000)
    ts: str = Field(min_length=10, max_length=32)
    status: str = Field(pattern="^(posted|void)$")
    entries: list[_Line] = Field(min_length=2, max_length=500)
    reverses: _Reverses | None = None
    accounts: dict | None = None  # peer_accounts.parse


class _Answered(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: _Answer


async def _firm_journal(session: AsyncSession, company_id: uuid.UUID, je_id: str) -> dict | None:
    row = await session.get(Projection, (company_id, je_id))
    return dict(row.state or {}) if row is not None and row.entity_type == "journal_entry" else None


async def _answer(session: AsyncSession, grant, answer: _Answer) -> str:
    """The client posted a journal of the firm's, or declined it and said why."""
    journal = await _firm_journal(session, grant.company_id, answer.journal)
    if journal is None or journal.get("peer_source"):
        raise HTTPException(409, "The journal answered is not in these books.")
    where = dict(company_id=grant.company_id, source_company_id=grant.source_company_id,
                 kind=peer_attention.DECLINED, entity_type="journal_entry",
                 source_entity_id=answer.journal)
    if answer.declined is None:
        await peer_attention.clear(session, **where)
    else:
        await peer_attention.record(session, **{k: v for k, v in where.items() if k != "kind"},
                                    kind=peer_attention.DECLINED,
                                    label=journal.get("memo") or answer.journal,
                                    reason=f"{grant.label} did not post it: {answer.declined}")
    return "updated"


async def _client_lines(session: AsyncSession, grant, journal: _Journal) -> list[dict]:
    """The lines of a client's journal as the firm posts them: its contacts and accounts
    the firm's own, balanced."""
    from celerp.services.journal_accounts import require_destinations
    from celerp.services.peer_identity import peer_identity

    company_id, source = grant.company_id, grant.source_company_id
    entries = []
    debits = credits = Decimal(0)
    for line in journal.entries:
        stored = line.model_dump(exclude_none=True)
        if line.contact:
            mapped = await peer_identity(session, target_company=company_id, source_company=source,
                                         entity_type="contact", source_entity_id=line.contact)
            if mapped is None:
                raise HTTPException(409, "A line names a contact that has not been sent yet.")
            stored["contact"] = mapped
        debits += Decimal(str(line.debit))
        credits += Decimal(str(line.credit))
        entries.append(stored)
    if debits != credits or debits == 0:
        raise HTTPException(422, "The journal entry does not balance.")
    company = await session.get(Company, company_id)
    placed = await peer_accounts.place(
        session, company_id=company_id, source_company_id=source,
        accounts=peer_accounts.parse(journal.accounts), codes=[e["account"] for e in entries],
        sender=grant.label, receiver=company.name, record=f"the journal {journal.memo or 'entry'}",
        firm=True)
    for stored in entries:
        stored["account"] = placed[stored["account"]]
    await require_destinations(session, company_id, {e["account"] for e in entries})
    return entries


async def receive_journal(session: AsyncSession, *, grant, source_entity_id: str,
                          source_revision: int, data: dict) -> str:
    """One manual journal entry of the client's, posted once in the firm's copy as the
    client posted it, its contacts the firm's own; a later revision that voids it voids
    the firm's. A reversal of a journal of the firm's is listed in the firm view with Post
    again, and reversing that reversal (Undo) removes it from the list. Also takes the
    client's answer to a journal of the firm's (``{"answer": ...}``)."""
    from celerp.services.peer_identity import claim_peer_revision, commit_peer_revision, peer_identity

    try:
        if isinstance(data, dict) and "answer" in data:
            return await _answer(session, grant, _Answered.model_validate(data).answer)
        journal = _Journal.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(422, "Invalid journal entry") from exc
    if _day(journal.ts) is None:
        raise HTTPException(422, "Invalid journal entry")
    company_id, source = grant.company_id, grant.source_company_id
    claim = await claim_peer_revision(
        session, grant=grant, entity_type="journal_entry",
        source_entity_id=source_entity_id, source_revision=source_revision,
        data=journal.model_dump(mode="json"))
    if claim.replay:
        return "skipped"
    if claim.mapping is not None:
        state = await _firm_journal(session, company_id, claim.target_id) or {}
        if journal.status == "void" and state.get("status") == "posted":
            await emit_event(
                session, company_id=company_id, entity_id=claim.target_id,
                entity_type="journal_entry", event_type="acc.journal_entry.voided",
                data=je_void_data(f"Voided by {grant.label}", state), actor_id=None,
                location_id=None, source="connector",
                idempotency_key=f"peer:je:{source}:{claim.source_key}:void", metadata_={})
        await commit_peer_revision(session, grant=grant, claim=claim, entity_type="journal_entry",
                                   source_entity_id=source_entity_id, source_revision=source_revision)
        return "updated"
    if journal.status == "void":
        return "skipped"  # voided before it was ever sent: nothing to post

    reverses = origin = None
    if journal.reverses is not None:
        if journal.reverses.origin is not None:
            origin = await _firm_journal(session, company_id, journal.reverses.origin)
            if origin is None or origin.get("peer_source"):
                raise HTTPException(409, "The journal it reverses is not in these books.")
            reverses = journal.reverses.origin
        elif journal.reverses.journal is not None:
            reverses = await peer_identity(session, target_company=company_id, source_company=source,
                                           entity_type="journal_entry",
                                           source_entity_id=journal.reverses.journal)
            if reverses is None:
                raise HTTPException(409, "The journal it reverses has not been sent yet.")
    if reverses is not None:
        # A reversal takes the lines it reverses back off the accounts they posted to here,
        # whatever the client's accounts and roles are now.
        reversed_state = origin or await _firm_journal(session, company_id, reverses) or {}
        entries = [{_SWAPPED.get(k, k): v for k, v in line.items()}
                   for line in reversed_state.get("entries") or []]
        if sorted(_amount(e) for e in entries) != sorted(
                _amount(line.model_dump()) for line in journal.entries):
            raise HTTPException(409, "The reversal does not match the journal it reverses.")
    else:
        entries = await _client_lines(session, grant, journal)
    created = {"memo": journal.memo, "ts": journal.ts, "entries": entries, "je_type": "manual",
               "status": "posted",
               "peer_source": {"journal": source_entity_id, "company": str(source)},
               **({"reverses": reverses} if reverses else {})}
    key = f"peer:je:{source}:{claim.source_key}"
    await emit_event(session, company_id=company_id, entity_id=claim.target_id,
                     entity_type="journal_entry", event_type="acc.journal_entry.created",
                     data=created, actor_id=None, location_id=None, source="connector",
                     idempotency_key=f"{key}:c", metadata_={})
    await emit_event(session, company_id=company_id, entity_id=claim.target_id,
                     entity_type="journal_entry", event_type="acc.journal_entry.posted",
                     data={"ts": journal.ts}, actor_id=None, location_id=None, source="connector",
                     idempotency_key=f"{key}:p", metadata_={})
    await commit_peer_revision(session, grant=grant, claim=claim, entity_type="journal_entry",
                               source_entity_id=source_entity_id, source_revision=source_revision)
    where = dict(company_id=company_id, source_company_id=source, kind=peer_attention.REVERSAL,
                 entity_type="journal_entry")
    if origin is not None:
        memo = origin.get("memo") or reverses
        await peer_attention.record(
            session, **where, source_entity_id=reverses, label=memo,
            reason=f"{grant.label} reversed {memo} dated {str(origin.get('ts'))[:10]}; posted {journal.ts[:10]}")
    elif reverses is not None:
        undone = await _firm_journal(session, company_id, reverses) or {}
        firm_own = undone.get("reverses")
        if firm_own and not ((await _firm_journal(session, company_id, firm_own)) or {}).get("peer_source"):
            await peer_attention.clear(session, **where, source_entity_id=firm_own)
    return "created"


# ---- the client's side ------------------------------------------------------------------

@dataclass
class Fetched:
    """What became of one journal of the firm's on the client: the run's outcome, the
    answer to send the firm (None when it already has it), the reason it was declined, and
    whether that reason is the lock date, which the client moves in Period lock."""
    status: str
    answer: dict | None = None
    declined: str | None = None
    locked: bool = False


def _lock_of(settings: dict) -> date | None:
    return _day(settings.get("lock_date"))


def _firm_locks(settings: dict) -> dict[str, dict]:
    """Each accountant's lock date by the key it came through (apply_firm_lock)."""
    held = settings.get("firm_locks")
    return held if isinstance(held, dict) else {}


def _instant(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def firm_lock(settings: dict, now: datetime | None = None) -> dict | None:
    """The accountant's lock date that binds these books, {"date", "firm"}: the latest of
    those sent through keys still in force, that is not expired, revoked or no longer
    opening journals. None when no accountant holds a period closed."""
    when = now or datetime.now(timezone.utc)
    binding = None
    for held in _firm_locks(settings).values():
        day, expires = _day(held.get("date")), _instant(held.get("expires_at"))
        if day is None or held.get("ended") or expires is None or expires <= when:
            continue
        if binding is None or day > _day(binding["date"]):
            binding = {"date": held["date"], "firm": held.get("firm")}
    return binding


def _firm_lock_of(settings: dict) -> date | None:
    binding = firm_lock(settings)
    return _day(binding["date"]) if binding else None


@dataclass
class _Placed:
    """A journal of the firm's as the client's books would post it, or why they cannot:
    ``fix`` says how to fix a hold on one of its accounts (peer_accounts.AccountHold);
    ``locked`` that the lock date holds it."""
    entries: list[dict]
    reason: str | None = None
    fix: dict | None = None
    locked: bool = False


async def _place_firm_journal(session: AsyncSession, company: Company, firm: str,
                              firm_company: uuid.UUID, item: dict) -> _Placed:
    from celerp.services.journal_accounts import require_destinations

    entries = [dict(line) for line in item.get("entries") or []]
    if item.get("held"):
        return _Placed(entries, str(item["held"]))
    lock = _lock_of(company.settings or {})
    day = _day(item.get("ts"))
    if day is None:
        return _Placed(entries, "Its date could not be read.")
    if lock is not None and day <= lock:
        return _Placed(entries, f"The books are locked through {lock.isoformat()}.", locked=True)
    for line in entries:
        contact = line.get("contact")
        if contact and await session.get(Projection, (company.id, contact)) is None:
            return _Placed(entries, "A line names a contact these books do not hold.")
    try:
        placed = await peer_accounts.place(
            session, company_id=company.id, source_company_id=firm_company,
            accounts=peer_accounts.parse(item.get("accounts")),
            codes=[line.get("account") for line in entries], sender=firm, receiver=None,
            record=f"the journal {item.get('memo') or 'entry'}", firm=False)
        for line in entries:
            line["account"] = placed[line.get("account")]
        await require_destinations(session, company.id, {line.get("account") for line in entries})
    except HTTPException as exc:
        return _Placed(entries, failure_reason(exc), getattr(exc, "fix", None))
    return _Placed(entries)


def _answer_record(item: dict, declined: str | None) -> dict:
    return {"answer": {"journal": item["id"], "declined": declined and declined[:2000]}}


async def apply_firm_journal(company_id: uuid.UUID, grant_id: uuid.UUID, firm: str,
                             firm_company: uuid.UUID, item: dict) -> Fetched:
    """Post one journal of the firm's in the client's books once, with a notice naming its
    date, accounts, amounts and the firm; void it when the firm voids it; or decline it
    with the reason, told once. The firm hears of a decline, and of a posting that
    follows one, through the answer the caller sends (answered)."""
    from celerp.db import get_session_ctx

    firm_je, revision = str(item.get("id") or ""), item.get("revision")
    if not firm_je or type(revision) is not int or revision < 1:
        raise ValueError("The accountant's journal could not be read")
    je_id = local_id(firm_company, firm_je)
    memo = str(item.get("memo") or "") or "Journal entry"
    async with get_session_ctx() as session:
        held = (await delivery_marks(session, company_id, grant_id, INBOUND, [firm_je])).get(firm_je)
        existing = await session.get(Projection, (company_id, je_id))
        state = dict(existing.state or {}) if existing is not None else None
        if item.get("status") == "void":
            if state is not None and state.get("status") == "posted":
                # A reversal posted here before the firm's void arrived already cancels the
                # entry; it is voided with it, so the entry is cancelled once.
                reversed_here = await journal_reversals.standing_reversals(session, company_id, je_id)
                for rid, reversal in reversed_here:
                    await emit_event(
                        session, company_id=company_id, entity_id=rid, entity_type="journal_entry",
                        event_type="acc.journal_entry.voided",
                        data=je_void_data(f"Voided by {firm}", reversal), actor_id=None,
                        location_id=None, source="connector", idempotency_key=f"{rid}:void", metadata_={})
                await emit_event(
                    session, company_id=company_id, entity_id=je_id, entity_type="journal_entry",
                    event_type="acc.journal_entry.voided",
                    data=je_void_data(f"Voided by {firm}", state), actor_id=None,
                    location_id=None, source="connector", idempotency_key=f"{je_id}:void", metadata_={})
                await _notify(session, company_id, "notice.peer_journal_voided_reversal" if reversed_here
                              else "notice.peer_journal_voided",
                              memo=memo, date=str(state.get("ts"))[:10], firm=firm)
                status = "updated"
            else:
                status = "skipped"
            await mark(session, company_id=company_id, grant_id=grant_id, entity=INBOUND,
                       entity_id=firm_je, revision=revision, refused=None)
            await session.commit()
            return Fetched(status)
        if state is not None:
            # Posted already; the firm still lists it as declined until it hears otherwise.
            answer = _answer_record(item, None) if held is not None and held.refused else None
            if held is None:
                await mark(session, company_id=company_id, grant_id=grant_id, entity=INBOUND,
                           entity_id=firm_je, revision=revision, refused=None)
                await session.commit()
            return Fetched("skipped", answer)
        company = await locked_company(session, company_id)
        placed = await _place_firm_journal(session, company, firm, firm_company, item)
        reason = placed.reason
        hold = dict(company_id=company_id, source_company_id=firm_company, entity_type="journals",
                    source_entity_id=firm_je)
        if reason is not None:
            await session.rollback()
            await _notify(session, company_id, "notice.peer_journal_declined", memo=memo,
                          date=str(item.get("ts"))[:10], firm=firm, reason=reason,
                          action_url=LOCK_DATE if placed.locked else FROM_ACCOUNTANT)
            if placed.fix is not None:
                await peer_attention.record(session, **hold, label=memo, reason=reason, fix=placed.fix)
            else:
                await peer_attention.clear(session, **hold)
            await session.commit()
            told = held is not None and held.refused == reason
            return Fetched("skipped", None if told else _answer_record(item, reason), reason, placed.locked)
        await peer_attention.clear(session, **hold)
        entries = placed.entries
        data = {"memo": item.get("memo") or "", "ts": item["ts"], "entries": entries,
                "je_type": "manual", "status": "posted",
                "peer_origin": {"journal": firm_je, "firm": firm, "firm_company": str(firm_company),
                                "revision": revision}}
        await emit_event(session, company_id=company_id, entity_id=je_id, entity_type="journal_entry",
                         event_type="acc.journal_entry.created", data=data, actor_id=None,
                         location_id=None, source="connector", idempotency_key=f"{je_id}:c", metadata_={})
        await emit_event(session, company_id=company_id, entity_id=je_id, entity_type="journal_entry",
                         event_type="acc.journal_entry.posted", data={"ts": item["ts"]}, actor_id=None,
                         location_id=None, source="connector", idempotency_key=f"{je_id}:p", metadata_={})
        lines = _lines_text(entries, await _names(session, company_id, entries))
        await _notify(session, company_id, "notice.peer_journal_posted", memo=memo,
                      date=str(item["ts"])[:10], lines=lines, firm=firm)
        if held is None or not held.refused:
            await mark(session, company_id=company_id, grant_id=grant_id, entity=INBOUND,
                       entity_id=firm_je, revision=revision, refused=None)
        await session.commit()
        return Fetched("created", _answer_record(item, None) if held is not None and held.refused else None)


async def answered(company_id: uuid.UUID, grant_id: uuid.UUID, item: dict, declined: str | None) -> None:
    """The firm has the client's answer to its journal."""
    from celerp.db import get_session_ctx
    async with get_session_ctx() as session:
        await mark(session, company_id=company_id, grant_id=grant_id, entity=INBOUND,
                   entity_id=str(item["id"]), revision=item["revision"], refused=declined)
        await session.commit()


async def apply_firm_lock(company_id: uuid.UUID, grant_id: uuid.UUID, body: dict | None) -> bool:
    """Keep the lock date the firm sent through this key beside the client's own, apart
    from any other key's, and move the client's forward to it once, with a notice: never
    back, and the same date from this key never again, so the client may move its own
    back. ``body`` None means the key no longer works or no longer opens journals: its
    date stops binding until the key answers again (firm_lock). A record the firm refused
    is sent once more when its date changes. Returns whether anything changed."""
    from celerp.db import get_session_ctx
    from celerp.services.lot_origin import place_after_lock_change

    lock = firm = None
    if body is not None:
        lock, firm = body.get("lock_date"), body.get("firm")
        if (lock is not None and _day(lock) is None or not isinstance(firm, str) or not firm.strip()
                or _instant(body.get("expires_at")) is None):
            raise ValueError("The accountant's lock date could not be read")
    async with get_session_ctx() as session:
        company = await locked_company(session, company_id)
        original = dict(company.settings or {})
        settings = dict(original)
        locks = dict(_firm_locks(settings))
        held = dict(locks.get(str(grant_id)) or {})
        before = _day(held.get("date"))
        if body is None:
            if held:
                locks[str(grant_id)] = {**held, "ended": True}
        else:
            held = {"firm": firm, "date": lock, "expires_at": body["expires_at"],
                    "applied": held.get("applied")}
            locks[str(grant_id)] = held
        settings["firm_locks"] = locks
        moved = False
        if lock is not None and held.get("applied") != lock:
            own = _lock_of(settings)
            if own is None or _day(lock) > own:
                company.settings = settings
                write_period_lock(company, lock, None)
                settings = dict(company.settings)
                moved = True
            held["applied"] = lock
        if not locks:
            settings.pop("firm_locks")
        company.settings = settings
        if body is not None and before != (_day(lock) if lock else None):
            await session.execute(delete(CompanyPeerDelivery).where(
                CompanyPeerDelivery.company_id == company_id,
                CompanyPeerDelivery.grant_id == grant_id,
                CompanyPeerDelivery.entity != INBOUND,
                CompanyPeerDelivery.refused.is_not(None)))
        if moved:
            await _notify(session, company_id, "notice.peer_lock_applied", firm=firm, date=lock,
                          action_url=LOCK_DATE)
            await place_after_lock_change(session, company_id)
        await session.commit()
        return settings != original


async def from_accountant(session: AsyncSession, company_id: uuid.UUID) -> list[dict]:
    """The journals the firm posted in these books, newest first, each with the date its
    reversal would post on (None when no open date is left, with the lock date that
    closes today) and its reversal, if any."""
    company = await session.get(Company, company_id)
    settings = company.settings or {}
    rows = (await session.execute(select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "journal_entry",
        Projection.state["peer_origin"].as_string().is_not(None)))).all()
    ids = [reversal_id(str(eid)) for eid, _ in rows]
    reversals = {str(eid): dict(state or {}) for eid, state in (await session.execute(
        select(Projection.entity_id, Projection.state).where(
            Projection.company_id == company_id, Projection.entity_id.in_(ids)))).all()} if ids else {}
    undone = {str(eid) for eid, in (await session.execute(select(Projection.entity_id).where(
        Projection.company_id == company_id,
        Projection.entity_id.in_([reversal_id(r) for r in reversals])))).all()} if reversals else set()
    names = await _names(session, company_id, [line for _, state in rows for line in (state or {}).get("entries") or []])
    items = []
    for eid, state in rows:
        state = dict(state or {})
        rid = reversal_id(str(eid))
        reversal = reversals.get(rid)
        live = reversal is not None and reversal.get("status") == "posted" and reversal_id(rid) not in undone
        day, lock = reverse_day(settings, state.get("ts"))
        items.append({
            "id": str(eid), "memo": state.get("memo") or "", "ts": str(state.get("ts") or "")[:10],
            "status": state.get("status"), "firm": (state.get("peer_origin") or {}).get("firm"),
            "entries": state.get("entries") or [],
            "lines": _lines_text(state.get("entries") or [], names),
            "total": float(sum(Decimal(str(l.get("debit") or 0)) for l in state.get("entries") or [])),
            "reverse_on": day, "locked_through": lock,
            "reversal": ({"id": rid, "ts": str(reversal.get("ts"))[:10],
                          "undo_on": reverse_day(settings, reversal.get("ts"))[0]} if live else None),
        })
    items.sort(key=lambda i: (i["ts"], i["id"]), reverse=True)
    return items


def reverse_day(settings: dict, original) -> tuple[str | None, str | None]:
    """The date a reversal of an entry dated ``original`` posts on: that date while it is
    open in both the company's books and its accountant's, else today while today is.
    Returns (date, None), or (None, the lock date that closes today)."""
    locks = [d for d in (_lock_of(settings), _firm_lock_of(settings)) if d is not None]
    latest = max(locks) if locks else None
    day = _day(original)
    if day is not None and (latest is None or day > latest):
        return day.isoformat(), None
    today = date.fromisoformat(business_date_of(None, settings.get("timezone")))
    if latest is None or today > latest:
        return today.isoformat(), None
    return None, latest.isoformat()


async def reverse(session: AsyncSession, company_id: uuid.UUID, actor_id, je_id: str) -> dict:
    """Reverse a journal the firm posted, or undo such a reversal by reversing it: one
    reversal each, posted on reverse_day, with a notice when it posts today instead of the
    entry's own date. The reversal reaches the firm on the next sync. Commits."""
    row = await session.get(Projection, (company_id, je_id))
    state = dict(row.state or {}) if row is not None and row.entity_type == "journal_entry" else None
    if state is None:
        raise HTTPException(404, t("acct.err_je_not_found"))
    if not (state.get("peer_origin") or (state.get("reverses") and je_id == reversal_id(state["reverses"]))):
        raise HTTPException(422, t("acct.err_reverse_not_from_accountant"))
    rid = reversal_id(je_id)
    existing = await session.get(Projection, (company_id, rid))
    if existing is not None:
        return {"je_id": rid, "ts": str((existing.state or {}).get("ts"))[:10], "already": True}
    if state.get("status") != "posted":
        raise HTTPException(422, t("acct.err_reverse_not_posted"))
    company = await locked_company(session, company_id)
    day, lock = reverse_day(company.settings or {}, state.get("ts"))
    if day is None:
        raise HTTPException(422, t("error.period_locked", date=lock))
    memo = state.get("memo") or ""
    await emit_event(session, company_id=company_id, entity_id=rid, entity_type="journal_entry",
                     event_type="acc.journal_entry.created", data={
                         "memo": f"Reversal of {memo}" if memo else "Reversal", "ts": day,
                         "entries": [{_SWAPPED.get(k, k): v for k, v in line.items()}
                                     for line in state.get("entries") or []],
                         "je_type": "manual", "status": "posted", "reverses": je_id},
                     actor_id=actor_id, location_id=None, source="manual",
                     idempotency_key=f"{rid}:c", metadata_={})
    await emit_event(session, company_id=company_id, entity_id=rid, entity_type="journal_entry",
                     event_type="acc.journal_entry.posted", data={"ts": day}, actor_id=actor_id,
                     location_id=None, source="manual", idempotency_key=f"{rid}:p", metadata_={})
    original = str(state.get("ts"))[:10]
    if day != original:
        await _notify(session, company_id, "notice.peer_reversal_today", memo=memo or "Journal entry",
                      original=original, date=day)
    await session.commit()
    return {"je_id": rid, "ts": day, "already": False}


async def prefill(session: AsyncSession, company_id: uuid.UUID, je_id: str) -> dict:
    """A new journal prefilled from one of these books' entries, dated on the first open
    day from today, with the entry's own date as ``source_ts``; nothing is posted."""
    row = await session.get(Projection, (company_id, je_id))
    if row is None or row.entity_type != "journal_entry":
        raise HTTPException(404, t("acct.err_je_not_found"))
    state = dict(row.state or {})
    company = await session.get(Company, company_id)
    settings = company.settings or {}
    day = date.fromisoformat(business_date_of(None, settings.get("timezone")))
    lock = _lock_of(settings)
    if lock is not None and day <= lock:
        day = lock + timedelta(days=1)
    return {"memo": state.get("memo") or "", "ts": day.isoformat(),
            "source_ts": str(state.get("ts") or "")[:10],
            "entries": [{"account": l.get("account"), "debit": l.get("debit") or 0,
                         "credit": l.get("credit") or 0, "contact": l.get("contact")}
                        for l in state.get("entries") or []]}


async def synced_source(session: AsyncSession, company_id: uuid.UUID, entity_type: str,
                        entity_id: str) -> str | None:
    """The connected company a record came from, through sync or in the copy this company
    was opened from, while it still holds a live key to this company, by its key's label;
    None when the record is this company's own."""
    from celerp.models.company_peer import CompanyPeerGrant

    sources = select(CompanyPeerIdentity.source_company_id).where(
        CompanyPeerIdentity.company_id == company_id,
        CompanyPeerIdentity.entity_type == entity_type,
        CompanyPeerIdentity.target_entity_id == entity_id)
    return await session.scalar(select(CompanyPeerGrant.label).where(
        CompanyPeerGrant.company_id == company_id,
        CompanyPeerGrant.source_company_id.in_(sources),
        CompanyPeerGrant.revoked_at.is_(None),
        CompanyPeerGrant.expires_at > datetime.now(timezone.utc)).limit(1))
