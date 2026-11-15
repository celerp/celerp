# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Bank statement routes: read a statement, preview it, import it, the group table of a
bank's open lines, suggestions, resolving lines, undo, bank-wide completion, removing a
statement, the opening balance, reclassifying Suspense, and client questions.

Every keyed write answers a replay of the same request with its first answer
(``_replayed`` / ``_recorded``); every record id in a request is resolved through
``statement_lines.scoped``.
"""

from __future__ import annotations

import uuid
from datetime import date as Date
from decimal import Decimal

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import catalog_refusal
from celerp.db import get_session
from celerp.events.engine import emit_event, keyed_event, request_fingerprint
from celerp.schemas.numbers import FiniteFloat, PositiveFloat
from celerp.services.auth import get_current_company_id, get_current_user
from celerp.services.money import round_money, to_decimal
from celerp.services.permissions import require_permission
from celerp_accounting import statement_lines as SL
from celerp_accounting.models import BankAccount, BankStatementLine, ReconciliationSession
from ui.i18n import refusal_text, t

router = APIRouter()

API = "/reconciliation"
_NOTE_MAX = 5000


def _agent(selection: str | None = None, *, credits: bool = False, any_permission: list[str] | None = None) -> dict:
    """The AI markers of a keyed bank write: idempotent under ``idempotency_key``, the
    request field that selects its records, a credit spend, and alternative permissions."""
    extra: dict = {"x-celerp-agent": True, "x-celerp-agent-idempotent": True, "x-celerp-agent-key": "idempotency_key"}
    if selection:
        extra["x-celerp-agent-selection"] = selection
    if credits:
        extra["x-celerp-agent-credits"] = True
    if any_permission:
        extra["x-celerp-agent-any-permission"] = any_permission
    return extra


# ── Replay store ────────────────────────────────────────────────────────────

def _request(payload: BaseModel, **path) -> dict:
    return {**payload.model_dump(mode="json", exclude={"idempotency_key"}), **{k: str(v) for k, v in path.items()}}


async def _replayed(db: AsyncSession, company_id: uuid.UUID, key: str, request: dict) -> dict | None:
    """The first answer given under ``key`` for this request; None when the key is unused.
    The same key with another request is refused."""
    row = await keyed_event(db, company_id, key, request)
    return None if row is None else (row.metadata_ or {}).get("result")


async def _recorded(db: AsyncSession, company_id: uuid.UUID, actor_id, key: str, request: dict, operation: str,
                    bank_id, result: dict, entity_id: str | None = None) -> dict:
    """Store ``result`` as the answer to ``key``; the caller commits."""
    entity_id = entity_id or f"bankop:{uuid.uuid5(uuid.NAMESPACE_URL, f'{company_id}:{key}')}"
    await emit_event(db, company_id=company_id, entity_id=entity_id,
                     entity_type="bank_operation", event_type="acc.bank_statement.done",
                     data={"operation": operation, "bank_account_id": None if bank_id is None else str(bank_id)},
                     actor_id=actor_id, location_id=None, source="reconciliation", idempotency_key=key,
                     metadata_={"request": request_fingerprint(request), "result": result})
    return result


async def _bank(db: AsyncSession, company_id: uuid.UUID, bank_id) -> BankAccount:
    return (await SL.scoped(db, company_id, "bank", [bank_id]))[0]


def _money_text(value, currency: str) -> str:
    return str(round_money(to_decimal(value), currency))


# ── Read ────────────────────────────────────────────────────────────────────

class ReadStatement(BaseModel):
    file_ids: list[str] = Field(min_length=1, max_length=50)
    bank_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)


@router.post(f"{API}/read-statement", summary="Read bank or card statement files with AI before import",
             openapi_extra=_agent(credits=True))
async def read_statement(payload: ReadStatement, company_id: uuid.UUID = Depends(get_current_company_id),
                         _: None = require_permission("manage_accounting"), user=Depends(get_current_user),
                         db: AsyncSession = Depends(get_session)) -> dict:
    from celerp.ai import batch, jobs

    request = _request(payload)
    if (done := await _replayed(db, company_id, payload.idempotency_key, request)) is not None:
        return done
    if payload.bank_id is not None:
        await _bank(db, company_id, payload.bank_id)
    if payload.job_id is not None:
        if await jobs.owned_job(db, payload.job_id, company_id=company_id, user_id=user.id, shape="documents") is None:
            raise HTTPException(status_code=404, detail=catalog_refusal("ai.err_job_not_found"))
        await jobs.retry_failed(payload.job_id, company_id, user.id)
        job_id = payload.job_id
    else:
        try:
            keys = batch.statement_step_keys(payload.file_ids, company_id, user.id)
        except (FileNotFoundError, PermissionError):
            raise HTTPException(status_code=404, detail=catalog_refusal("ai.err_file_not_found"))
        job_id = uuid.uuid5(uuid.NAMESPACE_URL, f"celerp:bank-read:{company_id}:{payload.idempotency_key}")
        await batch.create_batch_job(db, company_id, user.id, batch.STATEMENT_QUERY, payload.file_ids,
                                     job_id=job_id, step_keys=keys)
    result = await _recorded(db, company_id, user.id, payload.idempotency_key, request, "read", payload.bank_id,
                             {"job_id": str(job_id)}, entity_id=_read_entity(job_id))
    await db.commit()
    jobs.start_job(job_id)
    return result


# ── Preview ─────────────────────────────────────────────────────────────────

def _read_entity(job_id) -> str:
    """The record of a reading: the bank it reads for."""
    return f"bankread:{job_id}"


async def _read_bank(db: AsyncSession, company_id: uuid.UUID, job_id) -> BankAccount | None:
    from celerp.models.projections import Projection
    row = await db.get(Projection, {"company_id": company_id, "entity_id": _read_entity(job_id)})
    bank_id = (row.state or {}).get("bank_account_id") if row is not None else None
    return None if bank_id is None else await _bank(db, company_id, bank_id)

async def _bank_lines(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount) -> list[BankStatementLine]:
    return list((await db.execute(select(BankStatementLine).where(
        BankStatementLine.company_id == company_id, BankStatementLine.bank_account_id == bank.id))).scalars())


async def _known(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount | None) -> tuple[dict, dict]:
    """What a new statement is checked against: the fingerprints of the bank's lines
    (``{fingerprint: "Statement <date>"}``) and, per (date, amount), how many of the bank's
    posted entries no line holds yet (a line on them is probably already in the books)."""
    if bank is None:
        return {}, {}
    from celerp_accounting.routes import _je_entries_for_account, _opening_je_id
    lines = await _bank_lines(db, company_id, bank)
    dates = {s.id: s.statement_date for s in (await db.execute(select(ReconciliationSession).where(
        ReconciliationSession.company_id == company_id, ReconciliationSession.bank_account_id == bank.id))).scalars()}
    existing = {l.fingerprint: t("acct.recon_duplicate_of", date=dates.get(l.reconciliation_id, "")) for l in lines}
    held = {j for l in lines for j in (l.matched_je_ids or [])} | {_opening_je_id(bank.id)}
    counts: dict = {}
    for e in await _je_entries_for_account(db, company_id, bank):
        if e["je_id"] in held or e["amount"] is None:
            continue
        bucket = (str(e["ts"])[:10], _money_text(e["amount"], bank.currency))
        counts[bucket] = counts.get(bucket, 0) + 1
    return existing, counts


def _parse_structured(content: bytes, meta: dict, column_map: dict | None) -> dict:
    """A CSV, spreadsheet or OFX statement read without the model."""
    from celerp_accounting.csv_parser import parse_bank_csv, parse_ofx
    from celerp_accounting.routes import _statement_csv_bytes
    name = meta.get("filename") or ""
    if name.lower().endswith((".ofx", ".qfx")):
        parsed = parse_ofx(content)
        return {"header": {k: parsed.get(k) for k in ("period_start", "period_end", "closing_balance", "currency")},
                "lines": parsed["lines"], "needs_mapping": False, "headers": []}
    parsed = parse_bank_csv(_statement_csv_bytes(content, name, meta.get("content_type")), column_map)
    lines = parsed.get("lines") or []
    dates = sorted(l["line_date"] for l in lines if l.get("line_date"))
    return {"header": {"period_start": dates[0] if dates else None, "period_end": dates[-1] if dates else None,
                       "opening_balance": None, "closing_balance": None},
            "lines": lines, "needs_mapping": parsed.get("needs_mapping", False), "headers": parsed.get("headers") or []}


def _saved_map(bank: BankAccount | None, headers: list[str]) -> dict | None:
    saved = (bank.statement_column_map if bank is not None else None) or {}
    return saved.get("columns") if saved.get("headers") == headers else None


def _step_file(key: str) -> tuple[str, str | None]:
    file_id, _, pages = key.partition(":")
    return file_id, pages or None


async def _read_job(db: AsyncSession, company_id: uuid.UUID, user_id, job_id: uuid.UUID, bank: BankAccount | None,
                    column_map: dict | None = None) -> dict:
    """The statements, receipts and failed reads of a finished reading job, before review."""
    from celerp.ai import jobs
    from celerp.ai.files import load_file
    from celerp.models.ai import AIJobStep

    job = await jobs.owned_job(db, job_id, company_id=company_id, user_id=user_id, shape="documents")
    if job is None:
        raise HTTPException(status_code=404, detail=catalog_refusal("ai.err_job_not_found"))
    if jobs.liveness(job) == "running":
        raise HTTPException(status_code=409, detail=catalog_refusal("acct.err_reading_not_finished"))
    steps = list((await db.execute(select(AIJobStep).where(AIJobStep.job_id == job.id)
                                   .order_by(AIJobStep.seq))).scalars())
    out = {"statements": [], "receipts": [], "failed_steps": [], "needs_mapping": False, "layout_saved": False,
           "column_map": column_map, "headers": None}
    by_file: dict[str, dict] = {}
    for step in steps:
        file_id, pages = _step_file(step.key)
        result = step.result or {}
        filename = result.get("filename") or file_id
        if step.state != "done":
            reason = result.get("error") or ""
            out["failed_steps"].append({"filename": filename, "pages": pages, "reason": reason, "retry": True,
                                        "text": t("acct.recon_read_failed_pages", pages=pages, filename=filename)
                                        if pages else t("acct.recon_read_failed", filename=filename)})
            continue
        if result.get("structured"):
            content, meta = load_file(file_id, company_id, user_id)
            first = _parse_structured(content, meta, column_map)
            cmap = column_map
            if first["needs_mapping"] and cmap is None and (saved := _saved_map(bank, first["headers"])):
                first, cmap = _parse_structured(content, meta, saved), saved
                out["layout_saved"] = True
            out["needs_mapping"] = out["needs_mapping"] or first["needs_mapping"]
            out["column_map"], out["headers"] = cmap, first["headers"] or out["headers"]
            out["statements"].append({"file_id": file_id, "header": first["header"], "lines": first["lines"]})
            continue
        extraction = result.get("extraction") or {}
        if extraction.get("kind") == "receipt":
            out["receipts"].append({"file_id": file_id, "filename": filename,
                                    **{k: extraction.get(k) for k in ("date", "total", "tax", "tax_id",
                                                                      "is_tax_invoice", "party")}})
            continue
        st = by_file.get(file_id)
        if st is None:
            st = by_file[file_id] = {"file_id": file_id, "header": {}, "lines": []}
            out["statements"].append(st)
        header = {k: v for k, v in (extraction.get("header") or {}).items() if v not in (None, "")}
        st["header"] = {**header, **st["header"]}
        for raw in extraction.get("lines") or []:
            line = {k: v for k, v in raw.items() if k != "balance"}
            line["raw_balance"] = raw.get("balance")
            st["lines"].append(line)
    return out


def _review(st: dict, bank: BankAccount | None, base: str, existing: dict, held: dict) -> dict:
    from celerp_accounting.statement_reader import review_statement
    currency = bank.currency if bank is not None else (st["header"].get("currency") or base)
    return review_statement(st["header"], st["lines"], bank_currency=currency,
                            bank_id=bank.id if bank is not None else "new", existing=existing, held_counts=held)


@router.get(f"{API}/statement-preview/{{job_id}}", summary="The statements a reading job found, checked before import",
            openapi_extra={"x-celerp-agent": True})
async def statement_preview(job_id: uuid.UUID,
                            company_id: uuid.UUID = Depends(get_current_company_id),
                            _: None = require_permission("manage_accounting"), user=Depends(get_current_user),
                            db: AsyncSession = Depends(get_session)) -> dict:
    from celerp_accounting.routes import _base_currency
    bank = await _read_bank(db, company_id, job_id)
    read = await _read_job(db, company_id, user.id, job_id, bank)
    base = await _base_currency(db, company_id)
    existing, held = await _known(db, company_id, bank)
    statements = [{"file_id": st["file_id"], **_review(st, bank, base, existing, held)} for st in read["statements"]]
    out = {"job_id": str(job_id), "bank_id": None if bank is None else str(bank.id), "statements": statements,
           **{k: read[k] for k in ("receipts", "failed_steps", "needs_mapping", "layout_saved", "column_map")}}
    if bank is None:
        first = statements[0]["header"] if statements else {}
        out["bank"] = None
        out["new_bank"] = {"bank_name": first.get("bank_name") or "", "bank_type": "checking",
                           "currency": first.get("currency") or base,
                           "opening_balance": first.get("opening_balance"), "opening_date": first.get("period_start")}
    return out


# ── Import ──────────────────────────────────────────────────────────────────

class StatementIn(BaseModel):
    header: dict
    lines: list[dict] | None = None


class ImportStatement(BaseModel):
    job_id: uuid.UUID
    statements: list[StatementIn] = Field(min_length=1, max_length=50)
    bank_id: uuid.UUID | None = None
    new_bank: dict | None = None
    column_map: dict[str, str] | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)


async def _book_before(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount, start: str) -> tuple[Decimal, list[str]]:
    """The bank's book balance before ``start`` and the entries that make it, the opening
    entry counted in the balance but never listed (every statement counts it already)."""
    from celerp_accounting.routes import _je_entries_for_account, _opening_je_id
    total, ids = Decimal(0), []
    for e in await _je_entries_for_account(db, company_id, bank):
        if str(e["ts"])[:10] < start and e["amount"] is not None:
            total += to_decimal(e["amount"])
            if e["je_id"] != _opening_je_id(bank.id):
                ids.append(e["je_id"])
    return round_money(total, bank.currency), ids


async def _sessions(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount) -> list[ReconciliationSession]:
    return list((await db.execute(select(ReconciliationSession).where(
        ReconciliationSession.company_id == company_id, ReconciliationSession.bank_account_id == bank.id)
        .order_by(ReconciliationSession.statement_date, ReconciliationSession.id))).scalars())


async def _seed(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount, start: str, opening) -> list[str]:
    """What a new statement counts as cleared from the start: an earlier completed
    statement's list brought forward; for the bank's first statement, the entries before it
    when they add up to its opening balance, otherwise none (the group table says why)."""
    earlier = [s for s in await _sessions(db, company_id, bank) if s.statement_date < start]
    done = [s for s in earlier if s.status == "completed"]
    if done:
        return list(done[-1].reconciled_je_ids or [])
    if earlier:
        return []
    book, ids = await _book_before(db, company_id, bank, start)
    return ids if opening is not None and book == round_money(to_decimal(opening), bank.currency) else []


@router.post(f"{API}/import-statement", summary="Import the reviewed statements onto a bank account",
             openapi_extra=_agent("statements"))
async def import_statement(payload: ImportStatement, company_id: uuid.UUID = Depends(get_current_company_id),
                           _: None = require_permission("manage_accounting"), user=Depends(get_current_user),
                           db: AsyncSession = Depends(get_session)) -> dict:
    from datetime import datetime, timezone
    from celerp.services.company_lock import lock_company
    from celerp_accounting.routes import BankAccountCreate, _base_currency, add_bank

    request = _request(payload)
    if (done := await _replayed(db, company_id, payload.idempotency_key, request)) is not None:
        return done
    if payload.bank_id is None and payload.new_bank is None:
        raise HTTPException(status_code=422, detail=catalog_refusal("acct.err_import_needs_bank"))
    bank = await _bank(db, company_id, payload.bank_id) if payload.bank_id is not None else None
    read = await _read_job(db, company_id, user.id, payload.job_id, bank, column_map=payload.column_map)
    if len(read["statements"]) != len(payload.statements):
        raise HTTPException(status_code=422, detail=catalog_refusal("acct.err_import_statement_count"))
    if payload.column_map:
        for column in payload.column_map.values():
            if column not in (read["headers"] or []):
                raise HTTPException(status_code=422, detail=catalog_refusal("acct.err_column_missing", column=column))
    await lock_company(db, company_id)
    if bank is None:
        bank = await add_bank(db, company_id, user.id, BankAccountCreate(**payload.new_bank))
    base = await _base_currency(db, company_id)
    out = []
    for given, found in zip(payload.statements, read["statements"]):
        existing, held = await _known(db, company_id, bank)
        lines = given.lines if given.lines is not None else found["lines"]
        reviewed = _review({"header": {**found["header"], **given.header}, "lines": lines}, bank, base, existing, held)
        if reviewed["blocking"]:
            raise HTTPException(status_code=422, detail=" ".join(b["text"] for b in reviewed["blocking"]))
        header = reviewed["header"]
        start, end = header.get("period_start"), header.get("period_end")
        if any(s.statement_date == end for s in await _sessions(db, company_id, bank)):
            raise HTTPException(status_code=409, detail=catalog_refusal("acct.err_recon_open_exists"))
        recon = ReconciliationSession(
            id=uuid.uuid4(), company_id=company_id, bank_account_id=bank.id, statement_date=end,
            statement_balance=to_decimal(header["closing_balance"]), period_start=start,
            opening_balance=to_decimal(header.get("opening_balance") or 0), source_file_id=found["file_id"],
            status="open", reconciled_je_ids=await _seed(db, company_id, bank, start, header.get("opening_balance")),
            imported_at=datetime.now(timezone.utc))
        db.add(recon)
        await db.flush()
        imported = skipped = 0
        for i, line in enumerate(reviewed["lines"]):
            if line["duplicate"]:
                skipped += 1
                continue
            sent = lines[i] if i < len(lines) else {}
            if not sent.get("ticked", line["ticked"]):
                continue
            db.add(BankStatementLine(
                id=uuid.uuid4(), company_id=company_id, reconciliation_id=recon.id, bank_account_id=bank.id,
                fingerprint=line["fingerprint"], line_date=line["line_date"], description=line.get("description") or "",
                amount=to_decimal(line["amount"]), reference=line.get("reference"),
                raw_balance=None if line.get("raw_balance") is None else to_decimal(line["raw_balance"]),
                counterparty_key=line.get("counterparty_key"), counterparty_account=line.get("counterparty_account"),
                status="unmatched", matched_je_ids=[], raw_csv_row=dict(line.get("raw_csv_row") or {})))
            imported += 1
        recon.csv_row_count = imported
        out.append({"id": str(recon.id), "statement_date": end, "imported": imported, "skipped_duplicates": skipped})
    if payload.column_map and read["headers"]:
        bank.statement_column_map = {"headers": read["headers"], "columns": payload.column_map}
    result = await _recorded(db, company_id, user.id, payload.idempotency_key, request, "import", bank.id,
                             {"bank_id": str(bank.id), "sessions": out})
    await db.commit()
    return result


# ── The group table ─────────────────────────────────────────────────────────

def _line_dict(line: BankStatementLine, currency: str) -> dict:
    return {"id": str(line.id), "statement_id": str(line.reconciliation_id), "line_date": line.line_date,
            "description": line.description, "amount": _money_text(line.amount, currency), "status": line.status,
            "matched_je_ids": list(line.matched_je_ids or []), "suggestion": line.suggestion,
            "counterparty_key": line.counterparty_key, "client_query": line.client_query}


def _group_key(line: dict) -> str:
    return line["counterparty_key"] or line["description"]


def _starts_ticked(line: dict) -> bool:
    """Whether a line's suggestion is evidence enough to start ticked: a source that names
    its evidence, a key that is not a sparse one, and an account or entry to post to."""
    from celerp_accounting.suggest import TICKED_SOURCES
    s = line["suggestion"] or {}
    if s.get("source") not in TICKED_SOURCES or _group_key(line).startswith("SPARSE:"):
        return False
    return s.get("kind") != "post" or bool(s.get("account_code"))


def is_suspense(line: BankStatementLine) -> bool:
    """A posted line whose only entry is its Suspense posting (reclassify adds the answer)."""
    ids = list(line.matched_je_ids or [])
    return line.status == "posted" and len(ids) == 1 and ids[0].startswith("je:recon:suspense:")


async def _difference_notes(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount,
                            sessions: list[ReconciliationSession]) -> list[str]:
    if not sessions or sessions[0].period_start is None or sessions[0].opening_balance is None:
        return []
    first = sessions[0]
    book, _ids = await _book_before(db, company_id, bank, first.period_start)
    opening = round_money(to_decimal(first.opening_balance), bank.currency)
    if book == opening:
        return []
    return [t("acct.recon_book_before", date=first.period_start, book=str(book), opening=str(opening))]


@router.get(f"{API}/bank/{{bank_id}}/groups", summary="A bank's open statement lines, grouped by counterparty",
            openapi_extra={"x-celerp-agent": True})
async def bank_groups(bank_id: uuid.UUID, view: str = Query("open", pattern="^(open|all)$"),
                      company_id: uuid.UUID = Depends(get_current_company_id),
                      _: None = require_permission("manage_accounting"),
                      db: AsyncSession = Depends(get_session)) -> dict:
    bank = await _bank(db, company_id, bank_id)
    sessions = await _sessions(db, company_id, bank)
    open_ids = {s.id for s in sessions if s.status == "open"}
    rows = sorted(await _bank_lines(db, company_id, bank), key=lambda l: (l.line_date, str(l.id)))
    shown = rows if view == "all" else [l for l in rows if l.reconciliation_id in open_ids and l.status == "unmatched"]
    groups: dict = {}
    for row in shown:
        line = _line_dict(row, bank.currency)
        direction = "in" if row.amount > 0 else "out"
        g = groups.setdefault((_group_key(line), direction), {"key": _group_key(line), "direction": direction,
                                                              "count": 0, "lines": []})
        g["count"] += 1
        g["lines"].append(line)
    for g in groups.values():
        g["ticked"] = all(_starts_ticked(l) for l in g["lines"])
    return {"bank_id": str(bank.id), "groups": sorted(groups.values(), key=lambda g: (-g["count"], g["key"])),
            "difference": {"notes": await _difference_notes(db, company_id, bank, sessions)},
            "suspense_count": sum(1 for l in rows if is_suspense(l))}


# ── Resolve ─────────────────────────────────────────────────────────────────

_KINDS = Literal["post", "match", "no_entry", "transfer", "cash", "pay_doc", "suspense"]
_PAIR_DAYS = 3


class SplitIn(BaseModel):
    account_code: str = Field(min_length=1, max_length=32)
    amount: FiniteFloat
    contact: str | None = Field(None, max_length=128)
    fund_id: str | None = Field(None, max_length=128)


class DocIn(BaseModel):
    doc_id: str = Field(min_length=1, max_length=128)
    amount: FiniteFloat


class PartIn(BaseModel):
    account_code: str = Field(min_length=1, max_length=32)
    amount: FiniteFloat


class Decision(BaseModel):
    """One answer for one or several lines: post to an account (or split), match book
    entries, no entry needed, a transfer to another of the company's banks, a cash
    withdrawal, a document payment, or Suspense."""
    line_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    kind: _KINDS
    account_code: str | None = Field(None, max_length=32)
    splits: list[SplitIn] | None = Field(None, max_length=50)
    contact: str | None = Field(None, max_length=128)
    fund_id: str | None = Field(None, max_length=128)
    rate: PositiveFloat | None = None
    memo: str | None = Field(None, max_length=500)
    date: Date | None = None
    remember: bool = False
    je_ids: list[str] | None = Field(None, max_length=200)
    target_bank_id: uuid.UUID | None = None
    to_amount: PositiveFloat | None = None
    docs: list[DocIn] | None = Field(None, max_length=50)
    parts: list[PartIn] | None = Field(None, max_length=20)


class Accept(BaseModel):
    group_keys: list[str] = Field(min_length=1, max_length=500)
    line_ids: list[uuid.UUID] | None = Field(None, max_length=5000)


class Resolve(BaseModel):
    decisions: list[Decision] | None = Field(None, max_length=1000)
    accept: Accept | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)


def _reason(exc: Exception) -> str:
    return refusal_text(exc.detail) if isinstance(exc, HTTPException) else str(exc)


def _check_money(decisions: list[Decision], currency: str) -> None:
    """Every amount a decision carries is real money in the bank's currency, before anything posts."""
    for d in decisions:
        for amount in [s.amount for s in d.splits or ()] + [x.amount for x in d.docs or ()] + \
                [p.amount for p in d.parts or ()]:
            SL.money_value(amount, currency)


def _one(lines: list[BankStatementLine]) -> BankStatementLine:
    if len(lines) != 1:
        raise HTTPException(status_code=422, detail=catalog_refusal("acct.err_recon_one_line"))
    return lines[0]


async def _locked_lines(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount, line_ids: list,
                        state: tuple[str, ...] | None = ("unmatched",)) -> list[BankStatementLine]:
    """The lines under their locks, taken in the one order: each line's statement (in id
    order, refused when completed), then the lines in id order."""
    from celerp_accounting.routes import _lock_recon

    sessions = (await db.execute(select(BankStatementLine.reconciliation_id).where(
        BankStatementLine.id.in_(line_ids), BankStatementLine.company_id == company_id))).scalars()
    for sid in sorted(set(sessions), key=str):
        await _lock_recon(db, sid, company_id, editable=True)
    return await SL.scoped(db, company_id, "line", line_ids, bank=bank, state=state)


async def suspense_account(db: AsyncSession, company_id: uuid.UUID, actor_id) -> str:
    """The Suspense account, added and mapped the first time a line goes there. An account
    the chart already holds at that number is the company's own, and a chart from elsewhere
    predates the role, so neither is guessed: the line is refused until the role has its
    account in Posting accounts."""
    from celerp.accounting_roles import ADDED_WHEN_TURNED_ON, AccountRole, chart_from_elsewhere
    from celerp.models.company import Company
    from celerp.services.account_roles import role_map, set_role
    from celerp.services.journal_accounts import lock_accounts
    from celerp_accounting.import_service import add_posting_account

    role = AccountRole.SUSPENSE
    settings = (await db.execute(select(Company.settings).where(Company.id == company_id))).scalar_one_or_none() or {}
    if code := role_map(settings).get(role.value):
        return code
    code, name, parent = ADDED_WHEN_TURNED_ON[role]
    if chart_from_elsewhere(settings) or code in (await lock_accounts(db, company_id, {code}) or {}):
        raise HTTPException(status_code=422, detail=catalog_refusal("acct.err_suspense_unmapped", role=role.value))
    await add_posting_account(db, company_id, code=code, name=name, account_type="asset", parent_code=parent)
    await set_role(db, company_id, role.value, code, actor_id)
    return code


async def _remember(db: AsyncSession, company_id: uuid.UUID, line: BankStatementLine, d: Decision) -> None:
    """The company-wide answer rule for the line's counterparty account, else its key (with
    the amount on a sparse key), one per (field, pattern, amount)."""
    from celerp_accounting.models import ReconciliationRule

    field, pattern = (("counterparty_account", line.counterparty_account) if line.counterparty_account
                      else ("counterparty_key", line.counterparty_key or line.description))
    amount = Decimal(str(line.amount)) if pattern.startswith("SPARSE:") else None
    q = select(ReconciliationRule).where(
        ReconciliationRule.company_id == company_id, ReconciliationRule.bank_account_id.is_(None),
        ReconciliationRule.match_field == field, ReconciliationRule.match_pattern == pattern,
        ReconciliationRule.match_amount.is_(None) if amount is None else ReconciliationRule.match_amount == amount)
    rule = (await db.execute(q.with_for_update())).scalars().first()
    if rule is None:
        rule = ReconciliationRule(id=uuid.uuid4(), company_id=company_id, bank_account_id=None, match_field=field,
                                  match_pattern=pattern, match_type="exact", match_amount=amount, origin="answer",
                                  is_active=True, times_applied=0)
        db.add(rule)
    rule.target_account_code = d.account_code if d.kind == "post" else rule.target_account_code
    rule.target_bank_id = d.target_bank_id if d.kind == "transfer" else None
    rule.contact_id = d.contact
    if rule.target_account_code is None:
        raise HTTPException(status_code=422, detail=catalog_refusal("acct.err_recon_remember_kind"))


async def _pair(db: AsyncSession, company_id: uuid.UUID, line: BankStatementLine, target: BankAccount,
                to_amount: float | None) -> uuid.UUID | None:
    """The target bank's open line for the same money within three days, if one is there."""
    from datetime import date as _date
    want = -Decimal(str(line.amount)) if to_amount is None else Decimal(str(to_amount)) * (1 if line.amount < 0 else -1)
    rows = (await db.execute(select(BankStatementLine.id, BankStatementLine.line_date).join(
        ReconciliationSession, ReconciliationSession.id == BankStatementLine.reconciliation_id).where(
        BankStatementLine.company_id == company_id, BankStatementLine.bank_account_id == target.id,
        BankStatementLine.status == "unmatched", BankStatementLine.amount == want,
        ReconciliationSession.status == "open"))).all()
    day = _date.fromisoformat(str(line.line_date)[:10])
    near = sorted((abs((_date.fromisoformat(str(d)[:10]) - day).days), str(i), i) for i, d in rows)
    return near[0][2] if near and near[0][0] <= _PAIR_DAYS else None


async def _cash_bank(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount) -> BankAccount:
    row = (await db.execute(select(BankAccount.id).where(
        BankAccount.company_id == company_id, BankAccount.is_active.is_(True), BankAccount.bank_type == "cash",
        BankAccount.id != bank.id).order_by(BankAccount.created_at, BankAccount.id))).scalars().first()
    if row is None:
        raise HTTPException(status_code=422, detail=catalog_refusal("acct.err_recon_no_cash_account"))
    return (await SL.scoped(db, company_id, "bank", [row], bank=bank))[0]


async def _transfer(db: AsyncSession, company_id: uuid.UUID, actor_id, bank: BankAccount, d: Decision,
                    key: str) -> dict:
    if len(d.line_ids) != 1:
        raise HTTPException(status_code=422, detail=catalog_refusal("acct.err_recon_one_line"))
    if d.kind == "cash":
        target = await _cash_bank(db, company_id, bank)
    else:
        if d.target_bank_id is None:
            raise HTTPException(status_code=422, detail=catalog_refusal("acct.err_recon_transfer_needs_bank"))
        target = (await SL.scoped(db, company_id, "bank", [d.target_bank_id], bank=bank))[0]
    line_id = d.line_ids[0]
    probe = await db.get(BankStatementLine, line_id)
    pair_id = None if probe is None or probe.status != "unmatched" else await _pair(db, company_id, probe, target,
                                                                                   d.to_amount)
    lines = await _locked_lines(db, company_id, bank, [line_id] + ([pair_id] if pair_id else []), state=None)
    by_id = {l.id: l for l in lines}
    line = by_id[line_id]
    held = list(line.matched_je_ids or [])
    if line.status == "matched" and held and all(j.startswith("je:recon:transfer:") for j in held):
        return {str(line.id): held}
    if line.status != "unmatched":
        raise HTTPException(status_code=409, detail=catalog_refusal("acct.err_recon_line_state", status=line.status))
    paired = by_id.get(pair_id) if pair_id else None
    if paired is not None and paired.status != "unmatched":
        paired = None
    je_ids = await SL.post_transfer(db, company_id=company_id, actor_id=actor_id, bank=bank, line=line, target=target,
                                    paired=paired, rate=d.rate, to_amount=d.to_amount, key=key)
    return {str(line.id): je_ids}
