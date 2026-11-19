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

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date as Date, timedelta
from decimal import Decimal

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import AfterValidator, BaseModel, BeforeValidator, Field, model_validator
from sqlalchemy import func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from celerp.accounting_roles import KeyedRefusal, catalog_refusal
from celerp.agent_markers import agent_excluded
from celerp.db import get_session
from celerp.events.engine import emit_event, keyed_event, refuse_other_request, request_fingerprint
from celerp.importers.results import keyed_reason
from celerp.schemas.numbers import FiniteFloat, PositiveFloat
from celerp.services.auth import get_current_company_id, get_current_role, get_current_user
from celerp.services.payments import lock_recorded_unmatched
from celerp.services.money import round_money, to_decimal
from celerp.services.permissions import require_permission
from celerp_accounting import statement_lines as SL
from celerp_accounting.models import (Account, BankAccount, BankStatementLine, ReconciliationRule,
                                      ReconciliationSession, stored_text)
from celerp_accounting.routes import BankAccountCreate
from celerp_accounting.statement_reader import block_refusal
from ui.i18n import refusal_text, t

router = APIRouter()

API = "/reconciliation"
_NOTE_MAX = 5000


def _agent(selection: str | None = None, *, credits: bool = False, any_permission: list[str] | None = None,
           result_permissions: list[str] | None = None) -> dict:
    """The AI markers of a keyed bank write: idempotent under ``idempotency_key``, the
    request field that selects its records, a credit spend, alternative permissions and
    the permissions its handler consults beyond its own."""
    extra: dict = {"x-celerp-agent": True, "x-celerp-agent-idempotent": True, "x-celerp-agent-key": "idempotency_key"}
    if selection:
        extra["x-celerp-agent-selection"] = selection
    if credits:
        extra["x-celerp-agent-credits"] = True
    if any_permission:
        extra["x-celerp-agent-any-permission"] = any_permission
    if result_permissions:
        extra["x-celerp-agent-result-permissions"] = result_permissions
    return extra


# ── Replay store ────────────────────────────────────────────────────────────

# A bank request's key, as the bank stores it: in a namespace of its own, so a key a client
# chooses is never one the books write under themselves (je:manual:..., <entry>:void).
RequestKey = Annotated[str, Field(min_length=1, max_length=200), AfterValidator(lambda key: f"bank:{key}")]


def _request(payload: BaseModel, **path) -> dict:
    return {**payload.model_dump(mode="json", exclude={"idempotency_key"}), **{k: str(v) for k, v in path.items()}}


def _bound(request: dict, operation: str) -> dict:
    """What a key is bound to: the request and the operation it was sent to, so a key used by
    one bank operation is refused by every other, never answered with another's result."""
    return {**request, "operation": operation}


async def _replayed(db: AsyncSession, company_id: uuid.UUID, key: str, request: dict, operation: str) -> dict | None:
    """The first answer given under ``key`` for this request to ``operation``; None when the
    key is unused. The same key with another request or operation is refused."""
    row = await keyed_event(db, company_id, key, _bound(request, operation))
    return None if row is None else (row.metadata_ or {}).get("result")


async def _claimed(db: AsyncSession, company_id: uuid.UUID, actor_id, key: str, request: dict, operation: str,
                   bank_id, result: dict, entity_id: str | None = None, data: dict | None = None) -> tuple[dict, bool]:
    """Store ``result`` as the answer to ``key`` in the caller's transaction; the caller commits.
    A run that stored under ``key`` first is waited for (the key's unique index): its answer
    comes back with False, and another request under the key is refused (request.key_reused)."""
    entity_id = entity_id or f"bankop:{uuid.uuid5(uuid.NAMESPACE_URL, f'{company_id}:{key}')}"
    entry = await emit_event(db, company_id=company_id, entity_id=entity_id,
                     entity_type="bank_operation", event_type="acc.bank_statement.done",
                     data={"operation": operation, "bank_account_id": None if bank_id is None else str(bank_id),
                           **(data or {})},
                     actor_id=actor_id, location_id=None, source="reconciliation", idempotency_key=key,
                     metadata_={"request": request_fingerprint(_bound(request, operation)), "result": result})
    if not getattr(entry, "was_deduped", False):
        return result, True
    refuse_other_request((entry.metadata_ or {}).get("request"), _bound(request, operation))
    return (entry.metadata_ or {}).get("result"), False


async def _recorded(db: AsyncSession, company_id: uuid.UUID, actor_id, key: str, request: dict, operation: str,
                    bank_id, result: dict, entity_id: str | None = None, data: dict | None = None) -> dict:
    """The answer to ``key``: ``result``, or the one a run that stored under ``key`` first gave."""
    return (await _claimed(db, company_id, actor_id, key, request, operation, bank_id, result,
                           entity_id, data))[0]


def _unit(key: str, request: dict, line_ids) -> tuple[str, dict]:
    """The key and request one decision of a resolve stores its outcome under: the request
    key and the lines it decides, so the same lines under the same key are the same unit."""
    ids = sorted(str(l) for l in line_ids)
    return f"{key}:u:{uuid.uuid5(uuid.NAMESPACE_URL, ','.join(ids))}", {"request": request, "lines": ids}


async def _unit_done(db: AsyncSession, company_id: uuid.UUID, key: str, request: dict, line_ids) -> list | None:
    """The stored outcome rows of one decision of ``key``; None while it has not run."""
    unit_key, unit_request = _unit(key, request, line_ids)
    done = await _replayed(db, company_id, unit_key, unit_request, "resolve_unit")
    return None if done is None else done["results"]


async def _unit_recorded(db: AsyncSession, company_id: uuid.UUID, actor_id, key: str, request: dict, bank_id,
                         line_ids, rows: list) -> list:
    """Store one decision's outcome rows in the transaction of its effect; the caller commits."""
    unit_key, unit_request = _unit(key, request, line_ids)
    return (await _recorded(db, company_id, actor_id, unit_key, unit_request, "resolve_unit", bank_id,
                            {"results": rows}))["results"]


async def _bank(db: AsyncSession, company_id: uuid.UUID, bank_id, *, lock: bool = False) -> BankAccount:
    """The company's active bank account ``bank_id``; with ``lock``, read again under its row
    lock by a statement that names the company, so the lock is taken under the company's. The
    lock is the one an UPDATE of the row takes (FOR NO KEY UPDATE): its key stays free, so rows
    referring to the account never wait on it."""
    bank = (await SL.scoped(db, company_id, "bank", [bank_id]))[0]
    if lock:
        await db.execute(select(BankAccount).where(BankAccount.id == bank.id, BankAccount.company_id == company_id)
                         .with_for_update(key_share=True).execution_options(populate_existing=True))
    return bank


def _money_text(value, currency: str) -> str:
    return str(round_money(to_decimal(value), currency))


# ── Read ────────────────────────────────────────────────────────────────────

class ReadStatement(BaseModel):
    file_ids: list[str] = Field(min_length=1, max_length=50)
    bank_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    idempotency_key: RequestKey


@router.post(f"{API}/read-statement", summary="Read bank or card statement files with AI before import.",
             openapi_extra=_agent(credits=True))
async def read_statement(payload: ReadStatement, company_id: uuid.UUID = Depends(get_current_company_id),
                         _: None = require_permission("manage_accounting"), user=Depends(get_current_user),
                         db: AsyncSession = Depends(get_session)) -> dict:
    from celerp.ai import batch, jobs

    request = _request(payload)
    if (done := await _replayed(db, company_id, payload.idempotency_key, request, "read")) is not None:
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

async def _statement_lines(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount) -> list[BankStatementLine]:
    return list((await db.execute(select(BankStatementLine).where(
        BankStatementLine.company_id == company_id, BankStatementLine.bank_account_id == bank.id))).scalars())


async def _known(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount | None) -> tuple[list, dict]:
    """What a new statement is checked against: the bank's lines (as ``review_statement``
    returns them, each with its statement's date) and, per (date, amount), how many of the
    bank's posted entries no line holds yet (a line on them is probably already in the books)."""
    if bank is None:
        return [], {}
    from celerp_accounting.routes import _je_entries_for_account, _opening_je_id
    lines = await _statement_lines(db, company_id, bank)
    dates = {s.id: s.statement_date for s in (await db.execute(select(ReconciliationSession).where(
        ReconciliationSession.company_id == company_id, ReconciliationSession.bank_account_id == bank.id))).scalars()}
    known = [{"fingerprint": l.fingerprint, "line_date": l.line_date, "description": l.description,
              "amount": l.amount, "raw_balance": l.raw_balance, "reference": l.reference, "fitid": l.fitid,
              "statement": dates.get(l.reconciliation_id, "")} for l in lines]
    held = {j for l in lines for j in (l.matched_je_ids or [])} | {_opening_je_id(bank.id)}
    counts: dict = {}
    for e in await _je_entries_for_account(db, company_id, bank):
        if e["je_id"] in held or e["amount"] is None:
            continue
        bucket = (str(e["ts"])[:10], _money_text(e["amount"], bank.currency))
        counts[bucket] = counts.get(bucket, 0) + 1
    return known, counts


def _statement_csv_bytes(content: bytes, filename: str, content_type: str | None) -> bytes:
    """A spreadsheet statement read through the shared table reader and handed on as CSV,
    so one parser serves both file types."""
    from celerp.ai.files import XLSX_CONTENT_TYPE
    from celerp.importers.tabular import TabularError, _rows_to_csv, read_table

    if content_type == XLSX_CONTENT_TYPE or filename.lower().endswith(".xlsx"):
        try:
            cols, rows = read_table(content, "statement.xlsx")
        except TabularError as e:
            raise HTTPException(status_code=422, detail=e.detail)
        return _rows_to_csv(rows, cols).encode("utf-8")
    return content


def _parse_structured(content: bytes, meta: dict, column_map: dict | None) -> dict:
    """A CSV, spreadsheet or OFX statement read without the model."""
    from celerp_accounting.csv_parser import parse_bank_csv, parse_ofx
    name = meta.get("filename") or ""
    empty = HTTPException(status_code=422, detail=catalog_refusal("acct.err_statement_no_lines", filename=name))
    if name.lower().endswith((".ofx", ".qfx")):
        parsed = parse_ofx(content)
        if not parsed["lines"]:
            raise empty
        return {"header": {k: parsed.get(k) for k in ("period_start", "period_end", "closing_balance", "currency")},
                "lines": parsed["lines"], "needs_mapping": False, "headers": []}
    parsed = parse_bank_csv(_statement_csv_bytes(content, name, meta.get("content_type")), column_map)
    lines = parsed.get("lines") or []
    if not lines and not (parsed.get("needs_mapping") and parsed.get("headers")):
        raise empty
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
    from celerp.importers.tabular import TabularError
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
        if not jobs.answered(step):
            reason = result.get("error") or ""
            out["failed_steps"].append({"filename": filename, "pages": pages, "reason": reason, "retry": True,
                                        "text": t("acct.recon_read_failed_pages", pages=pages, filename=filename)
                                        if pages else t("acct.recon_read_failed", filename=filename)})
            continue
        if result.get("structured"):
            content, meta = load_file(file_id, company_id, user_id)
            try:
                first = _parse_structured(content, meta, column_map)
            except (TabularError, HTTPException) as exc:  # a header that would lose values, an unreadable sheet
                reason = refusal_text(exc.detail)
                out["failed_steps"].append({"filename": filename, "pages": None, "reason": reason, "retry": False,
                                            "text": reason})
                continue
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


def _review(st: dict, bank: BankAccount | None, base: str, known: list, held: dict) -> dict:
    from celerp_accounting.statement_reader import review_statement
    currency = bank.currency if bank is not None else (st["header"].get("currency") or base)
    return review_statement(st["header"], st["lines"], bank_currency=currency,
                            known=known, held_counts=held)


async def _preview(db: AsyncSession, company_id: uuid.UUID, user_id, job_id: uuid.UUID,
                   given: list[dict] | None = None, column_map: dict | None = None) -> dict:
    """The reading job's statements reviewed for import; ``given`` (one entry per
    statement, as Import takes them) replaces header figures and lines first, and
    ``column_map`` reads a tabular file's columns as Import will."""
    from celerp_accounting.routes import _base_currency
    bank = await _read_bank(db, company_id, job_id)
    read = await _read_job(db, company_id, user_id, job_id, bank, column_map=column_map)
    if given is not None and len(given) != len(read["statements"]):
        raise HTTPException(status_code=422, detail=catalog_refusal("acct.err_import_statement_count"))
    base = await _base_currency(db, company_id)
    known, held = await _known(db, company_id, bank)
    statements = []
    for i, st in enumerate(read["statements"]):
        edit = (given or [{}] * len(read["statements"]))[i]
        found = {"header": {**st["header"], **(edit.get("header") or {})},
                 "lines": st["lines"] if edit.get("lines") is None else edit["lines"]}
        statements.append({"file_id": st["file_id"], **_review(found, bank, base, known, held)})
    out = {"job_id": str(job_id), "bank_id": None if bank is None else str(bank.id), "statements": statements,
           **{k: read[k] for k in ("receipts", "failed_steps", "needs_mapping", "layout_saved", "column_map", "headers")}}
    if bank is None:
        first = statements[0]["header"] if statements else {}
        out["bank"] = None
        out["new_bank"] = {"bank_name": first.get("bank_name") or "", "bank_type": "checking",
                           "currency": first.get("currency") or base,
                           "opening_balance": first.get("opening_balance"), "opening_date": first.get("period_start")}
    return out


@router.get(f"{API}/statement-preview/{{job_id}}", summary="The statements a reading job found, checked before import.",
            openapi_extra={"x-celerp-agent": True})
async def statement_preview(job_id: uuid.UUID,
                            company_id: uuid.UUID = Depends(get_current_company_id),
                            _: None = require_permission("manage_accounting"), user=Depends(get_current_user),
                            db: AsyncSession = Depends(get_session)) -> dict:
    return await _preview(db, company_id, user.id, job_id)


_Figure = str | int | float | None


class StatementHeaderIn(BaseModel):
    """The header figures a statement imports with; a figure left out keeps the one read."""
    period_start: str | None = None
    period_end: str | None = None
    opening_balance: _Figure = None
    closing_balance: _Figure = None
    currency: str | None = None


class StatementLineIn(BaseModel):
    """One statement line as the reader takes it; what the review works out from it
    (fingerprint, counterparty, duplicate) is worked out again, never taken."""
    line_date: str | None = None
    description: str | None = None
    amount: _Figure = None
    reference: str | None = None
    raw_balance: _Figure = None
    fitid: str | None = None
    source: str | None = None
    counterparty_account: str | None = None
    raw_csv_row: dict | None = None
    flags: list[str] | None = None
    ticked: bool | None = None


class StatementIn(BaseModel):
    header: StatementHeaderIn
    lines: list[StatementLineIn] | None = None

    def edit(self) -> dict:
        """The edit as the review reads it: only what was sent."""
        return {"header": self.header.model_dump(exclude_unset=True),
                "lines": None if self.lines is None else [l.model_dump(exclude_unset=True) for l in self.lines]}


class PreviewEdit(BaseModel):
    statements: list[StatementIn] = Field(min_length=1, max_length=50)
    column_map: dict[str, str] | None = None  # {field: column} for a file whose columns were not recognised


@router.post(f"{API}/statement-preview/{{job_id}}", summary="Check a reading job's statements again with edited "
             "header figures, lines or column map, before import.", openapi_extra={"x-celerp-agent": True,
                                                                       "x-celerp-agent-confirm": False})
async def statement_preview_edited(job_id: uuid.UUID, payload: PreviewEdit,
                                   company_id: uuid.UUID = Depends(get_current_company_id),
                                   _: None = require_permission("manage_accounting"), user=Depends(get_current_user),
                                   db: AsyncSession = Depends(get_session)) -> dict:
    """Nothing is written: the same review Import runs, on the figures given."""
    return await _preview(db, company_id, user.id, job_id, [s.edit() for s in payload.statements],
                          payload.column_map)


# ── Import ──────────────────────────────────────────────────────────────────

class ImportStatement(BaseModel):
    job_id: uuid.UUID
    statements: list[StatementIn] = Field(min_length=1, max_length=50)
    bank_id: uuid.UUID | None = None
    new_bank: BankAccountCreate | None = None
    column_map: dict[str, str] | None = None
    idempotency_key: RequestKey


async def _carried_in(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount, start: str, opening,
                     matched: set[str]) -> tuple[Decimal, list[str], bool]:
    """The first statement's carry-in as the books stand now: the book balance before ``start``
    less the entries the statement's own lines match (those clear in it), the other entries
    before ``start`` (the opening entry counted in the balance but never listed: every statement
    counts it already), and whether that balance is the statement's opening, when those
    entries cleared before it."""
    from celerp_accounting.routes import _je_entries_for_account, _opening_je_id
    total, ids = Decimal(0), []
    for e in await _je_entries_for_account(db, company_id, bank):
        if str(e["ts"])[:10] < start and e["amount"] is not None and e["je_id"] not in matched:
            total += to_decimal(e["amount"])
            if e["je_id"] != _opening_je_id(bank.id):
                ids.append(e["je_id"])
    book = round_money(total, bank.currency)
    return book, ids, opening is not None and book == round_money(to_decimal(opening), bank.currency)


async def _first_counted(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount,
                         recon: ReconciliationSession) -> list[str] | None:
    """What the bank's first statement counts while it is open, its carry-in worked out from the
    books as they stand (see _carried_in), so an entry keyed after the import counts as it would
    have before: None for any other statement or a completed one, whose list is final."""
    sessions = await _sessions(db, company_id, bank)
    if sessions[0].id != recon.id or SL.completed(recon) or recon.period_start is None:
        return None
    matched = {j for ids in (await db.execute(select(BankStatementLine.matched_je_ids).where(
        BankStatementLine.reconciliation_id == recon.id))).scalars() for j in ids or []}
    _book, before, tied = await _carried_in(db, company_id, bank, recon.period_start, recon.opening_balance, matched)
    return [j for j in recon.reconciled_je_ids or [] if j not in before] + (before if tied else [])


async def read_counted(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount, recon: ReconciliationSession) -> None:
    """For a read: ``recon`` shows what it counts as the books stand (see _first_counted), and
    nothing is written; completion stores it under the statement's lock."""
    if (counted := await _first_counted(db, company_id, bank, recon)) is not None:
        set_committed_value(recon, "reconciled_je_ids", counted)


async def _sessions(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount) -> list[ReconciliationSession]:
    return list((await db.execute(select(ReconciliationSession).where(
        ReconciliationSession.company_id == company_id, ReconciliationSession.bank_account_id == bank.id)
        .order_by(ReconciliationSession.statement_date, ReconciliationSession.id))).scalars())


async def _seed(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount, start: str, opening) -> list[str]:
    """What a new statement counts as cleared from the start: an earlier completed
    statement's list brought forward; for the bank's first statement, the entries before it
    when they add up to its opening balance, otherwise none (the group table says why)."""
    earlier = [s for s in await _sessions(db, company_id, bank) if s.statement_date < start]
    done = [s for s in earlier if SL.completed(s)]
    if done:
        return list(done[-1].reconciled_je_ids or [])
    if earlier:
        return []
    _book, ids, tied = await _carried_in(db, company_id, bank, start, opening, set())
    return ids if tied else []


@router.post(f"{API}/import-statement", summary="Import the reviewed statements onto a bank account.",
             openapi_extra=_agent("statements"))
async def import_statement(payload: ImportStatement, company_id: uuid.UUID = Depends(get_current_company_id),
                           _: None = require_permission("manage_accounting"), user=Depends(get_current_user),
                           db: AsyncSession = Depends(get_session)) -> dict:
    from datetime import datetime, timezone
    from celerp.services.company_lock import lock_company
    from celerp_accounting.routes import _base_currency, add_bank

    request = _request(payload)
    if (done := await _replayed(db, company_id, payload.idempotency_key, request, "import")) is not None:
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
        bank = await add_bank(db, company_id, user.id, payload.new_bank)
    base = await _base_currency(db, company_id)
    out = []
    for given, found in zip(payload.statements, read["statements"]):
        known, held = await _known(db, company_id, bank)
        edit = given.edit()
        lines = edit["lines"] if edit["lines"] is not None else found["lines"]
        reviewed = _review({"header": {**found["header"], **edit["header"]}, "lines": lines}, bank, base, known, held)
        if reviewed["blocking"]:
            refusals = []
            for block in reviewed["blocking"]:
                refusals.append(block_refusal(block["code"], **block["params"]))
            raise HTTPException(status_code=422, detail={**refusals[0], "also": refusals[1:]})
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
                amount=to_decimal(line["amount"]), reference=line.get("reference"), fitid=line["fitid"],
                raw_balance=None if line.get("raw_balance") is None else to_decimal(line["raw_balance"]),
                counterparty_key=line.get("counterparty_key"), counterparty_account=line.get("counterparty_account"),
                status="unmatched", matched_je_ids=[], raw_csv_row=dict(line.get("raw_csv_row") or {})))
            imported += 1
        recon.csv_row_count = imported
        out.append({"id": str(recon.id), "statement_date": end, "imported": imported, "skipped_duplicates": skipped})
    if payload.column_map and read["headers"]:
        bank.statement_column_map = {"headers": read["headers"], "columns": payload.column_map}
    result = await _recorded(db, company_id, user.id, payload.idempotency_key, request, "import", bank.id,
                             {"bank_id": str(bank.id), "sessions": out}, data={"receipts": read["receipts"]})
    await db.commit()
    return result


# ── The group table ─────────────────────────────────────────────────────────

def _line_dict(line: BankStatementLine, currency: str) -> dict:
    return {"id": str(line.id), "statement_id": str(line.reconciliation_id), "line_date": line.line_date,
            "description": line.description, "amount": _money_text(line.amount, currency), "status": line.status,
            "reference": line.reference, "fitid": line.fitid, "counterparty_key": line.counterparty_key,
            "counterparty_account": line.counterparty_account,
            "raw_balance": None if line.raw_balance is None else _money_text(line.raw_balance, currency),
            "matched_je_ids": list(line.matched_je_ids or []), "suggestion": line.suggestion,
            "suggestion_version": suggestion_version(line.suggestion), "client_query": line.client_query, "attachments": _attachments(line)}


async def _posted_to(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount,
                     lines: list[BankStatementLine]) -> dict[uuid.UUID, list[dict]]:
    """The accounts each answered line's entries moved its money to, besides the bank's own."""
    from celerp.services.ledger_rows import journal_rows
    from celerp.services.posting_readiness import account_names

    held = {l.id: list(l.matched_je_ids or []) for l in lines if l.status != "unmatched" and l.matched_je_ids}
    if not held:
        return {}
    accounts = {je_id: {e.get("account") for e in state.get("entries", [])} - {bank.chart_account_code, None}
                for je_id, state, _ in await journal_rows(db, company_id, entry_ids=sorted(
                    {j for ids in held.values() for j in ids}))}
    codes = {lid: sorted(set().union(*(accounts.get(j, set()) for j in ids))) for lid, ids in held.items()}
    names = await account_names(db, company_id, {c for cs in codes.values() for c in cs})
    return {lid: [{"code": c, "name": names[c]} for c in cs] for lid, cs in codes.items()}


def _attachments(line: BankStatementLine) -> list[dict]:
    from celerp_accounting.routes import _line_attachment
    return [_line_attachment(line.company_id, u) for u in line.attachment_ids or []]


def _group_key(line: dict) -> str:
    return line["counterparty_key"] or line["description"]


def _starts_ticked(line: dict) -> bool:
    """Whether a line's suggestion is evidence enough to start ticked: a source of
    ``suggest.TICKED_EVIDENCE`` whose evidence carries the record that source names, a key that
    is not a sparse one, and an account or entry to post to. A match starts ticked only when
    ``suggest.identified`` proved the entry is the line's own; a reversal or transfer pair (two
    statement lines taken as one transaction) never does: the user ticks it."""
    from celerp_accounting.suggest import TICKED_EVIDENCE
    s = line["suggestion"] or {}
    named = TICKED_EVIDENCE.get(s.get("source"))
    if named is None or not (s.get("evidence") or {}).get(named) or _group_key(line).startswith("SPARSE:"):
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
    matched = {j for ids in (await db.execute(select(BankStatementLine.matched_je_ids).where(
        BankStatementLine.reconciliation_id == first.id))).scalars() for j in ids or []}
    book, _ids, tied = await _carried_in(db, company_id, bank, first.period_start, first.opening_balance, matched)
    if tied:
        return []
    opening = round_money(to_decimal(first.opening_balance), bank.currency)
    return [t("acct.recon_book_before", date=first.period_start, book=str(book), opening=str(opening))]


def _counted_entries(recon: ReconciliationSession | None, rows: list[BankStatementLine], entries: list[dict],
                     currency: str) -> list[dict]:
    """The book entries an open statement counts by their own match that no line holds,
    each for its Undo: an entry with no amount in the bank's currency keeps a statement
    from balancing until it is let go of."""
    if recon is None:
        return []
    held = {je for line in rows for je in line.matched_je_ids or []}
    counted = set(recon.reconciled_je_ids or []) - held
    return [{"je_id": e["je_id"], "ts": e.get("ts"), "memo": e.get("memo"),
             "amount": None if e["amount"] is None else _money_text(e["amount"], currency)}
            for e in entries if e["je_id"] in counted]


# The views of the group table: which of a bank's lines each shows.
VIEWS = ("open", "suggested", "questions", "posted", "all")


def _in_view(line: BankStatementLine, view: str, open_ids: set) -> bool:
    waiting = line.reconciliation_id in open_ids and line.status == "unmatched"
    return {"open": waiting, "suggested": waiting and bool(line.suggestion),
            "questions": waiting and SL.asked(line), "posted": line.status != "unmatched", "all": True}[view]


def _searched(line: BankStatementLine, q: str) -> bool:
    text = " ".join(filter(None, (line.description, line.counterparty_key, line.reference))).casefold()
    return q.casefold() in text


@router.get(f"{API}/bank/{{bank_id}}/groups", summary="A bank's statement lines, grouped by counterparty.",
            openapi_extra={"x-celerp-agent": True})
async def bank_groups(bank_id: uuid.UUID, view: str = Query("open", pattern=f"^({'|'.join(VIEWS)})$"),
                      q: str = Query("", max_length=200),
                      company_id: uuid.UUID = Depends(get_current_company_id),
                      _: None = require_permission("manage_accounting"),
                      db: AsyncSession = Depends(get_session)) -> dict:
    """Groups by counterparty and direction, the largest total first (it clears the
    difference fastest); the lines of a group newest first. The statements strip, the
    summary counts and the first open statement's difference come with them."""
    from celerp_accounting.routes import _je_entries_for_account, _needs_review, _recon_balance

    bank = await _bank(db, company_id, bank_id)
    sessions = await _sessions(db, company_id, bank)
    entries = await _je_entries_for_account(db, company_id, bank)
    open_ids = {s.id for s in sessions if s.status == "open"}
    rows = sorted(await _statement_lines(db, company_id, bank), key=lambda l: (l.line_date, str(l.id)), reverse=True)
    shown = [l for l in rows if _in_view(l, view, open_ids) and (not q or _searched(l, q))]
    posted_to = await _posted_to(db, company_id, bank, shown)
    groups: dict = {}
    for row in shown:
        line = {**_line_dict(row, bank.currency), "posted_to": posted_to.get(row.id, [])}
        direction = "in" if row.amount > 0 else "out"
        g = groups.setdefault((_group_key(line), direction), {"key": _group_key(line), "direction": direction,
                                                              "count": 0, "total": Decimal(0), "lines": []})
        g["count"] += 1
        g["total"] += to_decimal(row.amount)
        g["lines"].append(line)
    for g in groups.values():
        g["ticked"] = all(_starts_ticked(l) for l in g["lines"])
        g["suggestion"] = g["lines"][0]["suggestion"] if len({str(l["suggestion"]) for l in g["lines"]}) == 1 else None
    ordered = sorted(groups.values(), key=lambda g: (-abs(g["total"]), g["key"]))
    for g in ordered:
        g["total"] = _money_text(g["total"], bank.currency)
    first_open = next((s for s in sessions if s.status == "open"), None)
    if first_open is not None:
        await read_counted(db, company_id, bank, first_open)
    balance = None if first_open is None else _recon_balance(first_open, bank, entries)
    waiting = [l for l in rows if _in_view(l, "open", open_ids)]
    return {"bank_id": str(bank.id), "view": view, "q": q, "groups": ordered,
            "statements": [{"id": str(s.id), "period_start": s.period_start, "statement_date": s.statement_date,
                            "opening_balance": _money_text(s.opening_balance or 0, bank.currency),
                            "closing_balance": _money_text(s.statement_balance, bank.currency),
                            "status": s.status, "needs_review": _needs_review(s, bank, entries)}
                           for s in reversed(sessions)],
            "summary": {"lines": len(rows), "open": len(waiting),
                        "questions": sum(1 for l in waiting if SL.asked(l)),
                        "posted": sum(1 for l in rows if l.status != "unmatched")},
            "difference": {"amount": None if balance is None else _money_text(balance[1], bank.currency),
                           "statement_id": None if first_open is None else str(first_open.id),
                           "statement_date": None if first_open is None else first_open.statement_date,
                           "notes": await _difference_notes(db, company_id, bank, sessions),
                           "entries": _counted_entries(first_open, rows, entries, bank.currency)},
            "suspense_count": sum(1 for l in rows if is_suspense(l))}


# ── Resolve ─────────────────────────────────────────────────────────────────

_KINDS = Literal["post", "match", "no_entry", "transfer", "cash", "pay_doc", "suspense"]


# An account code as typed, pasted or proposed: surrounding spaces of any kind are not part of it.
AccountCode = Annotated[stored_text(Account.code, min_length=1),
                        BeforeValidator(lambda v: v.strip() if isinstance(v, str) else v)]


class SplitIn(BaseModel):
    account_code: AccountCode
    amount: FiniteFloat
    contact: stored_text(ReconciliationRule.contact_id) | None = None
    fund_id: str | None = Field(None, max_length=128)


class DocIn(BaseModel):
    doc_id: str = Field(min_length=1, max_length=128)
    amount: FiniteFloat


class PartIn(BaseModel):
    account_code: AccountCode
    amount: FiniteFloat


class Decision(BaseModel):
    """One answer for one or several lines: post to an account (or split), match book
    entries, no entry needed, a transfer to another of the company's banks, a cash
    withdrawal, a document payment, or Suspense."""
    line_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    kind: _KINDS
    account_code: AccountCode | None = None
    splits: list[SplitIn] | None = Field(None, max_length=50)
    contact: stored_text(ReconciliationRule.contact_id) | None = None
    fund_id: str | None = Field(None, max_length=128)
    rate: PositiveFloat | None = None
    memo: str | None = Field(None, max_length=500)
    date: Date | None = None
    remember: bool = False
    je_ids: list[str] | None = Field(None, max_length=200)
    target_bank_id: uuid.UUID | None = None
    paired_line_id: uuid.UUID | None = None
    to_amount: PositiveFloat | None = None
    docs: list[DocIn] | None = Field(None, max_length=50)
    parts: list[PartIn] | None = Field(None, max_length=20)
    separate_from: list[str] | None = Field(
        None, max_length=50, description="Book entries the user confirmed are not this line's money: the "
        "candidates a stop showed. The line posts even though they look like it; any other entry stops it again.")


def suggestion_version(suggestion: dict | None) -> str | None:
    """A short fingerprint of a line's suggestion: an accept names the version it saw, so a
    suggestion that changed before the accept ran is refused, never posted unseen."""
    if not suggestion:
        return None
    return hashlib.sha256(json.dumps(suggestion, sort_keys=True, default=str).encode()).hexdigest()[:16]


class Accept(BaseModel):
    """The ticked lines, each with the suggestion version it was shown, and the groups they
    were ticked in (the confirm card's labels)."""
    group_keys: list[str] = Field(min_length=1, max_length=500)
    line_ids: list[uuid.UUID] = Field(min_length=1, max_length=5000)
    versions: dict[uuid.UUID, Annotated[str, Field(max_length=64)]] = Field(
        max_length=5000, description="Each line's suggestion_version as the bank's groups showed it.",
        json_schema_extra={"x-celerp-unshown": True})

    @model_validator(mode="after")
    def _every_line_versioned(self) -> "Accept":
        if set(self.line_ids) - set(self.versions):
            raise ValueError("every line needs the suggestion version it was shown")
        return self


class Resolve(BaseModel):
    decisions: list[Decision] | None = Field(None, max_length=1000)
    accept: Accept | None = None
    idempotency_key: RequestKey




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


async def _locked_lines(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount | None, line_ids: list,
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
    the amount on a sparse key), one per (field, pattern, amount). A line imported before
    lines carried a counterparty is keyed now, as an import keys it."""
    from celerp_accounting.statement_reader import counterparty_key

    key, account = line.counterparty_key, line.counterparty_account
    if not key:
        key, account = counterparty_key(line.description, line.reference, Decimal(str(line.amount)), account)
    field, pattern = ("counterparty_account", account) if account else ("counterparty_key", key)
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


async def _pairs(db: AsyncSession, company_id: uuid.UUID, line: BankStatementLine, target: BankAccount,
                 to_amount: float | None) -> list[dict]:
    """The target bank's open lines the transfer ``line`` could be by amount and date, nearest
    first: ``{id, line_date, amount, reference}``. Which one it is, if any, only the user says
    (``Decision.paired_line_id``)."""
    from celerp_accounting.suggest import transfer_pairs
    want = -Decimal(str(line.amount)) if to_amount is None else Decimal(str(to_amount)) * (1 if line.amount < 0 else -1)
    rows = (await db.execute(select(BankStatementLine.id, BankStatementLine.line_date, BankStatementLine.amount,
                                    BankStatementLine.reference).join(
        ReconciliationSession, ReconciliationSession.id == BankStatementLine.reconciliation_id).where(
        BankStatementLine.company_id == company_id, BankStatementLine.bank_account_id == target.id,
        BankStatementLine.status == "unmatched", BankStatementLine.amount == want,
        ReconciliationSession.status == "open"))).all()
    return transfer_pairs(want, line.line_date,
                          [{"id": i, "line_date": d, "amount": a, "reference": r} for i, d, a, r in rows])


async def _cash_bank(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount) -> BankAccount:
    row = (await db.execute(select(BankAccount.id).where(
        BankAccount.company_id == company_id, BankAccount.is_active.is_(True), BankAccount.bank_type == "cash",
        BankAccount.id != bank.id).order_by(BankAccount.created_at, BankAccount.id))).scalars().first()
    if row is None:
        raise HTTPException(status_code=422, detail=catalog_refusal("acct.err_recon_no_cash_account"))
    return (await SL.scoped(db, company_id, "bank", [row]))[0]


async def _transfer(db: AsyncSession, company_id: uuid.UUID, actor_id, bank: BankAccount, d: Decision,
                    key: str, instead: dict, book: "_Book") -> dict:
    if len(d.line_ids) != 1:
        raise HTTPException(status_code=422, detail=catalog_refusal("acct.err_recon_one_line"))
    if d.kind == "cash":
        target = await _cash_bank(db, company_id, bank)
    else:
        if d.target_bank_id is None:
            raise HTTPException(status_code=422, detail=catalog_refusal("acct.err_recon_transfer_needs_bank"))
        target = (await SL.scoped(db, company_id, "bank", [d.target_bank_id]))[0]
    line_id = d.line_ids[0]
    probe = await db.get(BankStatementLine, line_id)
    pairs = await _pairs(db, company_id, probe, target, d.to_amount) if probe and probe.status == "unmatched" else []
    pair_id = d.paired_line_id
    # Without one the user named, a lone candidate stays open with this transfer as its suggestion.
    hint_id = pairs[0]["id"] if pair_id is None and len(pairs) == 1 else None
    other_id = pair_id or hint_id
    lines = await _locked_lines(db, company_id, None, [line_id] + ([other_id] if other_id else []), state=None)
    by_id = {l.id: l for l in lines}
    line = by_id[line_id]
    if line.bank_account_id != bank.id:
        raise SL._not_found("line")
    held = list(line.matched_je_ids or [])
    if line.status == "matched" and held and all(j.startswith("je:recon:transfer:") for j in held):
        return {str(line.id): held}
    if line.status != "unmatched":
        raise HTTPException(status_code=409, detail=catalog_refusal("acct.err_recon_line_state", status=line.status))
    want = {"reference": line.reference, "accounts": {target.chart_account_code}}
    if (je_ids := await _keyed_instead(db, company_id, bank, line, instead, book, want, d.separate_from)) is not None:
        return {str(line.id): je_ids}
    # Under the locks the named pair must still be the transfer's counterpart.
    still = [c["id"] for c in await _pairs(db, company_id, line, target, d.to_amount)] if other_id else []
    if pair_id is not None and pair_id not in still:
        raise _refused(409, "reconciliation.line_not_open")
    je_ids = await SL.post_transfer(db, company_id=company_id, actor_id=actor_id, bank=bank, line=line, target=target,
                                    paired=by_id.get(pair_id) if pair_id else None, rate=d.rate,
                                    to_amount=d.to_amount, key=key)
    if hint_id is not None and hint_id in still:
        SL.write_suggestion(by_id[hint_id], {"kind": "match", "source": "transfer", "je_ids": je_ids, "confidence": "low",
                                             "reason": str(t("acct.recon_reason_transfer", date=line.line_date[:10]))})
    return {str(line.id): je_ids}


def _refused(status: int, key: str, /, **params) -> HTTPException:
    return HTTPException(status_code=status, detail=catalog_refusal(key, **params))


class _Twins(HTTPException):
    """A line whose money book entries may already be, none of them proven its own (see
    _keyed_instead): it posts nothing, the first becomes its match suggestion and ``candidates``
    are shown, for the user to match one or post anyway (``Decision.separate_from``)."""

    def __init__(self, line_id: uuid.UUID, suggestion: dict, candidates: list[str], detail: dict):
        super().__init__(status_code=409, detail=detail)
        self.line_id, self.suggestion, self.candidates = line_id, suggestion, candidates


async def _keyed_instead(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount, line: BankStatementLine,
                         instead: dict, book: "_Book", want: dict, separate_from: list[str] | None) -> list[str] | None:
    """Under the line's locks, before it posts: the entry already keyed for its money. The
    candidates are ``suggest.twins`` (equal money near the line's date) over the entries no
    statement or line of this bank counts, less those the user said are another transaction
    (``separate_from``) and those another open line of this bank is identified with (its entry,
    not this one's). The one ``suggest.identified`` proves is this line's: the line is matched to
    it instead of posting a second copy, said in ``instead``. Otherwise, with candidates left, the
    line is refused with them shown, ranked by ``want``'s contacts and accounts (``suggest.rank``),
    and the first as its match suggestion: the user matches one or posts anyway. No candidate: the
    line posts. ``book`` is the request's read of the bank's entries near its lines, caught up
    under the company lock (see _Book); whether one of them is counted already, the identifier each
    carries (_entry_identifiers) and the statement lines carrying it are read here."""
    from celerp_accounting import suggest

    near = [e for e in suggest.twins(line.amount, line.line_date, book.entries)
            if e["je_id"] not in set(separate_from or ())]
    held = await _held(db, company_id, bank, near) if near else set()
    found = [e for e in near if e["je_id"] not in held]
    if not found:
        return None
    carried = await _entry_identifiers(db, company_id, {e["je_id"] for e in found})
    found = [{**e, "identifier": carried.get(e["je_id"])} for e in found]
    idents = [e["identifier"] for e in found]
    references = await _references(db, company_id, set(idents) | {suggest.normalised(line.reference)})

    def pick(reference: str | None, line_id: str) -> int | None:
        return suggest.own_twin(line_id, reference, found, references)

    if (mine := pick(line.reference, str(line.id))) is not None:
        je_ids = [found[mine]["je_id"]]
        await SL.match_lines(db, company_id=company_id, bank=bank, lines=[line], je_ids=je_ids)
        instead[str(line.id)] = catalog_refusal("acct.recon_reason_match", date=str(found[mine]["ts"])[:10])
        return je_ids
    claims = {l["id"]: i for l in references.carrying(idents)
              if l["bank_account_id"] == bank.id and l["id"] != line.id
              and (i := pick(l["reference"], str(l["id"]))) is not None}
    if claims:
        still_open = (await db.execute(select(BankStatementLine.id).where(
            BankStatementLine.company_id == company_id, BankStatementLine.status == "unmatched",
            BankStatementLine.id.in_(list(claims))))).scalars().all()
        theirs = {claims[i] for i in still_open}
        found = [e for i, e in enumerate(found) if i not in theirs]
    if not found:
        return None
    shown = suggest.rank(want, found)
    date = str(shown[0]["ts"])[:10]
    detail = catalog_refusal("acct.recon_reason_looks_like", count=len(shown), date=date)
    reason = t(detail["message_key"], count=len(shown), date=date)
    candidates = [e["je_id"] for e in shown]
    raise _Twins(line.id, {"kind": "match", "source": "twins", "je_ids": candidates[:1], "candidates": candidates,
                           "confidence": "low", "reason": str(reason)}, candidates, detail)


async def _pay_doc(db: AsyncSession, company_id: uuid.UUID, actor_id, role: str, bank: BankAccount, d: Decision,
                   key: str, instead: dict, book: "_Book") -> dict:
    """Pay open documents with one line: an invoice from a deposit, a bill from a
    withdrawal, each at most its open balance (the payment service refuses more, never
    clamps), and the parts that are not the documents (a fee taken out, an overpayment
    kept) as one entry, all together equal to the line."""
    from celerp.models.projections import Projection
    from celerp.services.permissions import admit_permission
    from celerp_accounting.routes import _money_text as money_text
    from celerp_docs.routes import apply_doc_payment

    await admit_permission(db, company_id, role, "record_payments")
    line = _one(await _locked_lines(db, company_id, bank, d.line_ids))
    rows = {x.doc_id: await db.get(Projection, {"company_id": company_id, "entity_id": x.doc_id}) for x in d.docs or ()}
    want = {"reference": line.reference, "accounts": {p.account_code for p in d.parts or ()},
            "contacts": {(r.state or {}).get("contact_id") for r in rows.values() if r is not None} - {None}}
    if (keyed := await _keyed_instead(db, company_id, bank, line, instead, book, want, d.separate_from)) is not None:
        return {str(line.id): keyed}
    if not d.docs:
        raise _refused(422, "acct.err_recon_pay_needs_doc")
    amount = abs(Decimal(str(line.amount)))
    total = sum((Decimal(str(x.amount)) for x in d.docs), Decimal(0)) + \
        sum((Decimal(str(p.amount)) for p in d.parts or ()), Decimal(0))
    if total != amount:
        raise _refused(422, "acct.err_split_total", total=money_text(total, bank.currency),
                       amount=money_text(amount, bank.currency))
    want = "invoice" if line.amount > 0 else "bill"
    je_ids: list[str] = []
    for x in d.docs:
        row = rows[x.doc_id]
        if row is not None and row.entity_type == "doc" and (row.state or {}).get("doc_type") != want:
            raise _refused(422, "acct.err_recon_doc_direction")
        event, _applied = await apply_doc_payment(
            db, str(company_id), x.doc_id,
            {"amount": x.amount, "payment_date": line.line_date, "bank_account": bank.chart_account_code,
             **({"conversion_rate": d.rate} if d.rate else {})},
            source="reconciliation", actor_id=actor_id, idempotency_key=f"{key}:{line.id}:pay:{x.doc_id}",
            commit=False)
        je_ids.append(SL.payment_entry_id(x.doc_id, event.data["index"]))
    if d.parts:
        je_ids.append(await SL.post_parts(
            db, company_id=company_id, actor_id=actor_id, bank=bank, line=line,
            parts=[({"account": p.account_code}, Decimal(str(p.amount))) for p in d.parts], rate=d.rate, key=key))
    SL.settle_payment(await SL._session(db, line), line, je_ids)
    return {str(line.id): je_ids}


def _offsets(d: Decision, lines: list[BankStatementLine]) -> list[tuple[dict, Decimal] | None]:
    """Per line, what it posts against: the decision's splits (one line only) or its account."""
    def fields(account, contact, fund_id) -> dict:
        return {"account": account, **({"contact": contact} if contact else {}), **({"fund_id": fund_id} if fund_id else {})}
    if d.splits:
        _one(lines)
        return [[(fields(s.account_code, s.contact, s.fund_id), Decimal(str(s.amount))) for s in d.splits]]
    if not d.account_code:
        raise _refused(422, "acct.err_recon_needs_account")
    return [[(fields(d.account_code, d.contact, d.fund_id), abs(Decimal(str(l.amount))))] for l in lines]


async def _decide(db: AsyncSession, company_id: uuid.UUID, actor_id, role: str, bank: BankAccount, d: Decision,
                  key: str, instead: dict, book: "_Book") -> dict:
    """Carry out one decision; ``{line_id: je_ids}`` for each line it settled. A line that would
    post (an entry, a transfer, a payment) whose money is already keyed in the books is matched
    to that entry instead, noted in ``instead`` (see _keyed_instead)."""
    if d.kind in ("transfer", "cash"):
        return await _transfer(db, company_id, actor_id, bank, d, key, instead, book)
    if d.kind == "pay_doc":
        return await _pay_doc(db, company_id, actor_id, role, bank, d, key, instead, book)
    lines = await _locked_lines(db, company_id, bank, d.line_ids)
    if d.kind == "match":
        await SL.match_lines(db, company_id=company_id, bank=bank, lines=lines, je_ids=d.je_ids or [])
        return {str(l.id): list(d.je_ids or []) for l in lines}
    if d.kind == "no_entry":
        for line in lines:
            await SL.skip_line(db, line)
        return {str(l.id): [] for l in lines}
    if d.kind == "suspense":
        account = await suspense_account(db, company_id, actor_id)
        offsets = [[({"account": account}, abs(Decimal(str(l.amount))))] for l in lines]
    else:
        offsets = _offsets(d, lines)
    out = {}
    for line, own in zip(lines, offsets):
        want = {"reference": line.reference, "accounts": {o["account"] for o, _ in own},
                "contacts": {o["contact"] for o, _ in own if o.get("contact")}}
        if (keyed := await _keyed_instead(db, company_id, bank, line, instead, book, want, d.separate_from)) is not None:
            out[str(line.id)] = keyed
            continue
        out[str(line.id)] = await SL.post_line(
            db, company_id=company_id, actor_id=actor_id, bank=bank, line=line, offsets=own, rate=d.rate,
            memo=d.memo, entry_date=d.date.isoformat() if d.date else None, key=key,
            je_type="recon_suspense" if d.kind == "suspense" else None)
        if d.remember:
            await _remember(db, company_id, line, d)
    return out


async def _suggest_twin(db: AsyncSession, company_id: uuid.UUID, exc: _Twins) -> None:
    """The match suggestion a refused line with several keyed entries is left with, while it is open."""
    line = (await SL.scoped(db, company_id, "line", [exc.line_id]))[0]
    if line.status == "unmatched":
        SL.write_suggestion(line, exc.suggestion)


async def _accepted(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount, accept: Accept, key: str,
                    request: dict) -> tuple[list[Decision], dict]:
    """The accept's line list, fixed when it was confirmed, one unit per line: a line already
    done under ``key`` answers from its recorded outcome (returned by line id); an open line
    whose suggestion still has the version it was shown becomes the decision that suggestion
    makes; any other line refuses the request. The group keys only label the confirm card and
    are never read here. Each line's state is read before its recorded outcome, so a line
    another run under ``key`` finished in between is answered as done, never refused."""
    from celerp.accounting_roles import AccountRole
    from celerp.services import account_roles

    open_ids = {s.id for s in await _sessions(db, company_id, bank) if s.status == "open"}
    named = await SL.scoped(db, company_id, "line", accept.line_ids, bank=bank)
    earlier: dict = {}
    chosen = []
    for line in named:
        if (rows := await _unit_done(db, company_id, key, request, [line.id])) is not None:
            earlier[str(line.id)] = rows
        elif line.reconciliation_id in open_ids and line.status == "unmatched" and line.suggestion \
                and suggestion_version(line.suggestion) == accept.versions[line.id]:
            chosen.append(line)
        else:
            raise _refused(409, "reconciliation.line_not_open")
    tax_account = None
    out = []
    for line in sorted(chosen, key=lambda l: (l.line_date, str(l.id))):
        s = line.suggestion or {}
        base = {"line_ids": [line.id], "kind": s.get("kind")}
        if s.get("kind") == "post" and s.get("vat"):
            tax_account = tax_account or await account_roles.resolve(db, company_id, AccountRole.TAX_INPUT)
            base["splits"] = [{"account_code": s.get("account_code"), "amount": s["vat"]["net"],
                               "contact": s.get("contact_id")},
                              {"account_code": tax_account, "amount": s["vat"]["tax"]}]
        elif s.get("kind") == "post":
            base.update(account_code=s.get("account_code"), contact=s.get("contact_id"))
        elif s.get("kind") == "match":
            base["je_ids"] = s.get("je_ids")
        elif s.get("kind") == "transfer":
            base.update(target_bank_id=s.get("target_bank_id"), paired_line_id=s.get("paired_line_id"))
        elif s.get("kind") == "pay_doc":
            base["docs"] = s.get("docs")
        if s.get("kind") not in _KINDS.__args__:
            raise _refused(409, "reconciliation.line_not_open")
        out.append(Decision(**base))
    return out, earlier


async def _bump_rules(db: AsyncSession, company_id: uuid.UUID, done: dict, by_line: dict) -> None:
    """Count each rule whose suggestion a posted line followed."""
    from celerp.services.company_lock import lock_company
    await lock_company(db, company_id)

    counts: dict = {}
    for lid in done:
        if rule_id := by_line.get(lid):
            counts[rule_id] = counts.get(rule_id, 0) + 1
    for rule_id, n in counts.items():
        rule = (await db.execute(select(ReconciliationRule).where(
            ReconciliationRule.id == uuid.UUID(rule_id), ReconciliationRule.company_id == company_id)
            .with_for_update())).scalars().first()
        if rule is not None:
            rule.times_applied = (rule.times_applied or 0) + n


async def _resolve_unit(db: AsyncSession, company_id: uuid.UUID, actor_id, role: str, bank_id, key: str,
                        request: dict, d: Decision, book: _Book) -> list:
    """One decision of a resolve in a transaction of its own, under the company lock taken
    before any statement line: the outcome rows recorded when a run under ``key`` carried it out
    already, otherwise its effect (on the books as they stand now, see _caught_up) and its
    outcome, committed together."""
    from celerp.services.company_lock import lock_company

    await lock_company(db, company_id)
    if (rows := await _unit_done(db, company_id, key, request, d.line_ids)) is not None:
        await db.rollback()
        return rows
    try:
        bank = await _bank(db, company_id, bank_id)
        await _caught_up(db, company_id, bank, book)
        instead: dict = {}
        settled = await _decide(db, company_id, actor_id, role, bank, d, key, instead, book)
        rows = [{"line_id": lid, "ok": True, "je_ids": ids, "reason": instead.get(lid),
                 "matched_instead": lid in instead, "candidates": []} for lid, ids in settled.items()]
        if d.kind in ("transfer", "cash"):
            for row in rows:
                pair = [str(i) for j in row["je_ids"] for i in await SL.paired_lines(db, company_id, bank.id, j)]
                row["paired_line_id"] = pair[0] if pair else None
    except (HTTPException, KeyedRefusal) as exc:
        await db.rollback()
        rows = [{"line_id": str(lid), "ok": False, "je_ids": [], "reason": keyed_reason(exc), "matched_instead": False,
                 "candidates": exc.candidates if isinstance(exc, _Twins) else []} for lid in d.line_ids]
        if isinstance(exc, _Twins):
            await lock_company(db, company_id)
            await _suggest_twin(db, company_id, exc)
    rows = await _unit_recorded(db, company_id, actor_id, key, request, bank_id, d.line_ids, rows)
    await db.commit()
    return rows


@router.post(f"{API}/bank/{{bank_id}}/resolve", summary="Resolve a bank's statement lines: post, match, no entry, "
             "transfer, cash, pay documents or Suspense.", openapi_extra={
                 **_agent("decisions[].line_ids accept.line_ids", result_permissions=["record_payments"]),
                 "x-celerp-agent-card": "/accounting/reconcile/bank/{bank_id}"})
async def resolve(bank_id: uuid.UUID, payload: Resolve, company_id: uuid.UUID = Depends(get_current_company_id),
                  _: None = require_permission("manage_accounting"), role: str = Depends(get_current_role),
                  user=Depends(get_current_user), db: AsyncSession = Depends(get_session)) -> dict:
    """Each decision commits on its own: a refused one (a line another user settled, a
    locked period, a document over its open balance) is reported with its reason while
    the rest go through. Each decision stores its own outcome under the request key in the
    same commit as its effect, so a repeat of the key answers the decisions already
    carried out from those outcomes and runs only the rest. Each of its transactions takes the
    company lock before any statement line, as every other writer does."""
    from celerp.services.company_lock import lock_company

    key, actor_id = payload.idempotency_key, user.id
    request = _request(payload, bank_id=bank_id)
    if (first := await _replayed(db, company_id, key, request, "resolve")) is not None:
        return first
    await lock_company(db, company_id)
    bank = await _bank(db, company_id, bank_id)
    decisions = list(payload.decisions or [])
    by_line: dict = {}
    earlier: dict = {}
    if payload.accept is not None:
        accepted, earlier = await _accepted(db, company_id, bank, payload.accept, key, request)
        ids = [x for d in accepted for x in d.line_ids] + [uuid.UUID(x) for x in earlier]
        by_line = {str(l.id): (l.suggestion or {}).get("rule_id")
                   for l in (await SL.scoped(db, company_id, "line", ids, bank=bank) if ids else [])}
        decisions += accepted
    _check_money(decisions, bank.currency)
    book = await _near_entries(db, company_id, bank, [lid for d in decisions for lid in d.line_ids])
    await db.rollback()
    results: list[dict] = [r for rows in earlier.values() for r in rows]
    for d in decisions:
        results += await _resolve_unit(db, company_id, actor_id, role, bank_id, key, request, d, book)
    done = {r["line_id"]: r["je_ids"] for r in results if r["ok"]}
    if by_line:
        await _bump_rules(db, company_id, done, by_line)
    result = await _recorded(db, company_id, actor_id, key, request, "resolve", bank_id, {"results": results, "refused": sum(1 for r in results if not r["ok"])})
    await db.commit()
    return result


# ── Suggest ─────────────────────────────────────────────────────────────────

class Keyed(BaseModel):
    idempotency_key: RequestKey


async def _states(db: AsyncSession, company_id: uuid.UUID, entity_ids) -> dict:
    from celerp.models.projections import Projection

    ids = sorted(set(entity_ids))
    if not ids:
        return {}
    rows = await db.execute(select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_id.in_(ids)))
    return {i: s or {} for i, s in rows.all()}


def _contact(state: dict, bank_code: str | None) -> str | None:
    return next((e["contact"] for e in state.get("entries", []) if e.get("account") != bank_code and e.get("contact")),
                None)


async def _held(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount, entries: list[dict]) -> set[str]:
    """Those of ``entries`` (book entries of this bank) a statement or a line of this bank already
    counts, and the opening entry. Another bank's statement counting an entry leaves it free
    here, as for a manual match: a transfer is reconciled on both accounts. The first open
    statement's carry-in is worked out (see _first_counted) only when an entry is dated before it."""
    from celerp_accounting.routes import _opening_je_id

    ids = {e["je_id"] for e in entries}
    held = {_opening_je_id(bank.id)}
    for i, recon in enumerate(await _sessions(db, company_id, bank)):
        early = i == 0 and recon.period_start is not None and \
            any(str(e["ts"])[:10] < str(recon.period_start)[:10] for e in entries)
        counted = await _first_counted(db, company_id, bank, recon) if early else None
        held.update((recon.reconciled_je_ids or []) if counted is None else counted)
    return (held & ids) | await SL.lines_holding(db, company_id, bank.id, ids)


async def _free_entries(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount) -> list[dict]:
    """The bank's book entries no statement or line of the bank counts yet: what a line can match."""
    from celerp_accounting.routes import _je_entries_for_account

    entries = [e for e in await _je_entries_for_account(db, company_id, bank) if e["amount"] is not None]
    held = await _held(db, company_id, bank, entries)
    return [e for e in entries if e["je_id"] not in held]


@dataclass
class _Book:
    """A request's read of the bank's book entries a line of it could already be (see
    _keyed_instead): dated within ``days`` (the exact-amount window of the lines' dates), of an
    amount in ``amounts`` (those the lines carry), as the books stood at ledger row ``mark``.
    Statement lines are not kept here: a decision reads what it checks of them under its own
    lock, since another request can import, match or release one between two decisions."""
    days: tuple[str, str] | None
    amounts: set
    entries: list[dict]
    mark: int


async def _ledger_mark(db: AsyncSession, company_id: uuid.UUID) -> int:
    from celerp.models.ledger import LedgerEntry

    return (await db.execute(select(func.coalesce(func.max(LedgerEntry.id), 0)).where(
        LedgerEntry.company_id == company_id))).scalar_one()


async def _book_entries(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount, book: _Book,
                        entry_ids: list[str] | None = None) -> list[dict]:
    from celerp_accounting import suggest
    from celerp_accounting.routes import _je_entries_for_account

    entries = [e for e in await _je_entries_for_account(db, company_id, bank, days=book.days, entry_ids=entry_ids)
               if e["amount"] is not None and suggest._amt(e["amount"]) in book.amounts]
    states = await _states(db, company_id, [e["je_id"] for e in entries])
    for e in entries:
        others = [x for x in (states.get(e["je_id"]) or {}).get("entries", []) if x.get("account") != bank.chart_account_code]
        e["evidence"] = {"accounts": {x.get("account") for x in others} - {None},
                         "contacts": {x.get("contact") for x in others} - {None, ""}}
    return entries


async def _near_entries(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount, line_ids: list) -> _Book:
    """One read per request, under the company lock, of the bank's book entries a line of
    ``line_ids`` could already be."""
    from celerp_accounting import matcher, suggest

    lines = (await db.execute(select(BankStatementLine.line_date, BankStatementLine.amount).where(
        BankStatementLine.company_id == company_id, BankStatementLine.bank_account_id == bank.id,
        BankStatementLine.id.in_(line_ids)))).all() if line_ids else []
    book = _Book(None, set(), [], await _ledger_mark(db, company_id))
    if lines:
        window, dated = timedelta(days=matcher.EXACT_DAYS), [Date.fromisoformat(d[:10]) for d, _ in lines]
        book.days = (str(min(dated) - window), str(max(dated) + window))
        book.amounts = {suggest._amt(a) for _, a in lines}
        book.entries = await _book_entries(db, company_id, bank, book)
    return book


async def _references(db: AsyncSession, company_id: uuid.UUID, idents: set) -> "object":
    """The company's statement lines (every bank, every state, any date) that carry a reference, as
    a ``suggest.References``: those whose normalised reference is one of ``idents``, the only
    ones ``References.elsewhere`` is asked about. A document number names a payment only while no other line carries it."""
    from celerp_accounting import suggest

    q = select(BankStatementLine.id, BankStatementLine.bank_account_id, BankStatementLine.line_date,
               BankStatementLine.reference, BankStatementLine.matched_je_ids).where(
        BankStatementLine.company_id == company_id, BankStatementLine.reference.is_not(None),
        BankStatementLine.reference != "")
    idents = set(idents) - {None}
    if not idents:
        return suggest.References([])
    q = q.where(_narrowed(BankStatementLine.reference, idents))
    rows = [{"id": i, "bank_account_id": b, "line_date": d, "reference": r, "matched_je_ids": m}
            for i, b, d, r, m in (await db.execute(q)).all()]
    return suggest.References([r for r in rows if suggest.normalised(r["reference"]) in idents])


_DIGITS = re.compile(r"[0-9]+")


def _narrowed(column, idents: set):
    """A SQL filter keeping every row whose ``column`` may normalise (``suggest.normalised``) to one
    of ``idents``; the caller compares exactly in Python, so the database never casefolds. ASCII
    digits are the same under any casefolding, so a number is narrowed by its longest digit run;
    one with no digits is not narrowed."""
    runs = [max(_DIGITS.findall(i), key=len, default=None) for i in idents]
    if None in runs:
        return true()
    return or_(*(column.contains(r, autoescape=True) for r in set(runs)))


async def _entry_identifiers(db: AsyncSession, company_id: uuid.UUID, je_ids) -> dict[str, str]:
    """The identifier each of ``je_ids`` carries, as the books stand: the number of the document a
    payment entry pays, while that payment is the document's only one (none refunded) and no other
    document of the company has that number. A memo, a contact or an account is never one, and an
    entry keyed by hand carries none: it is a candidate the user confirms."""
    from celerp_accounting import suggest
    from celerp_accounting.routes import _je_doc_refs

    out = {}
    for je_id, ref in (await _je_doc_refs(db, company_id, sorted(je_ids))).items():
        payments = ref["doc"].get("payments") or []
        only = payments[0] if len(payments) == 1 else {}
        ident = suggest.normalised(ref["numbers"].get(ref["doc_id"]))
        if ref["trigger"] == "doc.payment.received" and ident and only.get("status", "active") == "active" \
                and not only.get("refunded") and not only.get("refund_count"):
            out[je_id] = ident
    once = await _carried_once(db, company_id, set(out.values()))
    return {je_id: ident for je_id, ident in out.items() if ident in once}


async def _carried_once(db: AsyncSession, company_id: uuid.UUID, idents: set) -> set:
    """Of ``idents`` (normalised document numbers), those exactly one record of the company
    carries as its number: what makes a number an identifier."""
    from celerp.models.projections import Projection
    from celerp_accounting import suggest

    if not idents:
        return set()
    numbers = [Projection.state[k].as_string() for k in ("ref_id", "doc_number")]
    holders: dict[str, set] = {}
    for entity_id, *found in (await db.execute(select(Projection.entity_id, *numbers).where(
            Projection.company_id == company_id, or_(*(_narrowed(n, idents) for n in numbers))))).all():
        for number in {suggest.normalised(f) for f in found} & idents:
            holders.setdefault(number, set()).add(entity_id)
    return {ident for ident in idents if len(holders.get(ident, ())) == 1}


async def _caught_up(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount, book: _Book) -> None:
    """Under the company lock, bring ``book`` up to the books as they stand: the entries written
    since its mark are read again (an entry keyed by someone else, or changed or voided). Every
    ledger row is written under that lock, so none is in flight and none is missed."""
    from celerp.models.ledger import LedgerEntry

    if book.days is None:
        return
    rows = (await db.execute(select(LedgerEntry.id, LedgerEntry.entity_type, LedgerEntry.entity_id).where(
        LedgerEntry.company_id == company_id, LedgerEntry.id > book.mark))).all()
    if not rows:
        return
    book.mark = max(r.id for r in rows)
    changed = sorted({r.entity_id for r in rows if r.entity_type == "journal_entry"})
    if changed:
        book.entries = [e for e in book.entries if e["je_id"] not in changed] + \
            await _book_entries(db, company_id, bank, book, changed)


async def _open_docs(db: AsyncSession, company_id: uuid.UUID, currency: str, base: str) -> list[dict]:
    """Invoices and bills with a balance left, in the bank's currency, each with its
    ``identifier``: its normalised number while no other record of the company carries it,
    else None (see ``suggest.identified``)."""
    from celerp.models.projections import Projection
    from celerp_accounting import suggest

    rows = (await db.execute(select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "doc"))).all()
    out = []
    for doc_id, state in rows:
        state = state or {}
        if state.get("doc_type") not in ("invoice", "bill") or state.get("status") in ("draft", "void"):
            continue
        if (state.get("currency") or base) != currency or not state.get("amount_outstanding"):
            continue
        out.append({"doc_id": doc_id, "doc_type": state["doc_type"], "contact_id": state.get("contact_id"),
                    "open_balance": _money_text(state["amount_outstanding"], currency), "ref": state.get("ref_id"),
                    "identifier": suggest.normalised(state.get("ref_id") or state.get("doc_number"))})
    once = await _carried_once(db, company_id, {d["identifier"] for d in out if d["identifier"]})
    return [{**d, "identifier": d["identifier"] if d["identifier"] in once else None} for d in out]


async def _slips(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount) -> list[dict]:
    """The receipts read alongside the bank's imported statements."""
    from celerp.models.projections import Projection

    rows = (await db.execute(select(Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "bank_operation"))).scalars()
    return [{**r, "party_contact_id": None} for s in rows
            if (s or {}).get("operation") == "import" and s.get("bank_account_id") == str(bank.id)
            for r in s.get("receipts") or []]


async def _history(db: AsyncSession, company_id: uuid.UUID) -> dict:
    """What earlier posted lines went to, by counterparty key and by counterparty account."""
    banks = {b.id: b.chart_account_code for b in (await db.execute(select(BankAccount).where(
        BankAccount.company_id == company_id))).scalars()}
    lines = list((await db.execute(select(BankStatementLine).where(
        BankStatementLine.company_id == company_id, BankStatementLine.status == "posted"))).scalars())
    states = await _states(db, company_id, [j for l in lines for j in l.matched_je_ids or []])
    out: dict = {}
    for line in lines:
        code = banks.get(line.bank_account_id)
        for je_id in line.matched_je_ids or []:
            state = states.get(je_id) or {}
            if state.get("status") == "void":
                continue
            for e in state.get("entries", []):
                if e.get("account") == code:
                    continue
                row = {"account_code": e.get("account"), "contact_id": e.get("contact"), "amount": str(line.amount),
                       "line_date": line.line_date}
                for k in {line.counterparty_key, line.counterparty_account} - {None, ""}:
                    out.setdefault(k, []).append(row)
    return out


async def _postable(db: AsyncSession, company_id: uuid.UUID) -> list[str]:
    """The active accounts a line can post to: none that is another account's parent."""
    from celerp_accounting.models import Account

    rows = (await db.execute(select(Account.code, Account.parent_code, Account.is_active).where(
        Account.company_id == company_id))).all()
    parents = {p for _c, p, _a in rows if p}
    return sorted(c for c, _p, active in rows if active and c not in parents)


async def _ladder_context(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount, own_lines: list) -> "object":
    from celerp.accounting_roles import AccountRole
    from celerp.services.account_roles import resolve as resolve_role
    from celerp_accounting import suggest
    from celerp_accounting.routes import _base_currency, _rule_to_dict

    own_ids = {line.id for line in own_lines}
    book = await _free_entries(db, company_id, bank)
    states = await _states(db, company_id, [e["je_id"] for e in book])
    carried = await _entry_identifiers(db, company_id, [e["je_id"] for e in book])
    entries = [{"je_id": e["je_id"], "amount": str(e["amount"]), "date": str(e["ts"])[:10], "memo": e["memo"],
                "identifier": carried.get(e["je_id"]),
                "contact": _contact(states.get(e["je_id"]) or {}, bank.chart_account_code)} for e in book]
    rules = [_rule_to_dict(r) for r in (await db.execute(select(ReconciliationRule).where(
        ReconciliationRule.company_id == company_id, ReconciliationRule.is_active.is_(True)))).scalars()]
    open_sessions = {s.id for s in (await db.execute(select(ReconciliationSession).where(
        ReconciliationSession.company_id == company_id, ReconciliationSession.status == "open"))).scalars()}
    pair_lines = [{"id": str(l.id), "bank_id": str(l.bank_account_id), "amount": str(l.amount), "line_date": l.line_date,
                   "reference": l.reference}
                  for l in (await db.execute(select(BankStatementLine).where(
                      BankStatementLine.company_id == company_id, BankStatementLine.status == "unmatched",
                      BankStatementLine.bank_account_id != bank.id))).scalars()
                  if l.reconciliation_id in open_sessions and l.id not in own_ids]
    base = await _base_currency(db, company_id)
    return suggest.LadderContext(
        bank_id=str(bank.id), entries=entries, open_docs=await _open_docs(db, company_id, bank.currency, base),
        rules=rules, pair_lines=pair_lines,
        references=await _references(db, company_id, {suggest.normalised(l.reference) for l in own_lines}),
        slips=await _slips(db, company_id, bank),
        history=await _history(db, company_id), accounts=await _postable(db, company_id),
        general_expense=await resolve_role(db, company_id, AccountRole.GENERAL_EXPENSE))


async def _model_replies(residue: list[dict], accounts: list[str], call) -> dict | None:
    """The model's replies for the residue groups; None when the model cannot run."""
    import inspect

    from celerp.ai.llm import RelayError
    from celerp_accounting import suggest

    token = suggest.current_call.set(call)
    replies: dict = {}
    try:
        rung = suggest.model_rung
        if rung is None:
            return None
        for batch in suggest.model_batches(residue):
            reply = rung(batch, accounts)
            replies.update((await reply if inspect.isawaitable(reply) else reply) or {})
    except suggest.ModelStepRunning:  # the first run of the request still asks: no write, no failure
        raise HTTPException(status_code=409, detail=catalog_refusal("acct.err_suggest_running")) from None
    except RelayError as exc:
        if exc.code == "operation_mismatch":  # the service broke the step's identity: no write, no failure
            raise HTTPException(status_code=409, detail=catalog_refusal("acct.err_suggest_mismatch")) from None
        return None
    except Exception:  # the model's failure leaves the lines to questions, said in the summary
        return None
    finally:
        suggest.current_call.reset(token)
    return replies


async def _suggest_input(db: AsyncSession, company_id: uuid.UUID, bank_id) -> tuple:
    """(open unmatched lines, what the ladder reads of each, its context, its answers before
    the model, the residue groups for the model) of a bank, as the transaction reads them."""
    from celerp_accounting import suggest

    bank = await _bank(db, company_id, bank_id)
    open_ids = {s.id for s in await _sessions(db, company_id, bank) if s.status == "open"}
    rows = [l for l in await _statement_lines(db, company_id, bank)
            if l.reconciliation_id in open_ids and l.status == "unmatched"]
    lines = [{"id": str(l.id), "line_date": l.line_date, "description": l.description,
              "amount": _money_text(l.amount, bank.currency), "counterparty_key": l.counterparty_key,
              "counterparty_account": l.counterparty_account, "reference": l.reference,
              "client": l.client_query if SL.asked(l) and l.client_query.get("answered") else None} for l in rows]
    ctx = await _ladder_context(db, company_id, bank, rows)
    return (rows, lines, ctx, *suggest.ladder_rungs(lines, ctx))


@router.post(f"{API}/bank/{{bank_id}}/suggest", summary="Suggest an answer for each open statement line of a bank.",
             openapi_extra=_agent(credits=True))
async def suggest_lines(bank_id: uuid.UUID, payload: Keyed, company_id: uuid.UUID = Depends(get_current_company_id),
                        _: None = require_permission("manage_accounting"), user=Depends(get_current_user),
                        db: AsyncSession = Depends(get_session)) -> dict:
    """Suggestions only: no line is posted or matched. A replay of the same key answers
    the first result and the model is not asked again.

    The lines are read and the model asked with no lock held; the suggestions are written
    under the company lock only when no run under the key has answered meanwhile (its answer
    is returned instead) and the lines, and what the ladder makes of them, are as they were
    read. Otherwise the request is refused and nothing is written. A run whose model step is
    still being answered for another run of the same request waits for that answer, and past
    the synchronous wait is refused with nothing written, as is a run the AI service says
    was sent with other content under its step's identity. A run whose model could not
    answer records no outcome, so the same key asks again."""
    from celerp.services.company_lock import lock_company
    from celerp_accounting import suggest

    key, actor_id = payload.idempotency_key, user.id
    request = _request(payload, bank_id=bank_id)
    if (first := await _replayed(db, company_id, key, request, "suggest")) is not None:
        return first
    _rows, lines, ctx, out, residue = await _suggest_input(db, company_id, bank_id)
    await db.rollback()  # a read: nothing it took is held while the model runs
    replies = await _model_replies(residue, ctx.accounts, suggest.ModelCall(company_id, actor_id, key)) \
        if residue else {}
    await lock_company(db, company_id)
    if (first := await _replayed(db, company_id, key, request, "suggest")) is not None:
        await db.rollback()
        return first
    rows, now_lines, now_ctx, now_out, now_residue = await _suggest_input(db, company_id, bank_id)
    if (now_lines, now_out, now_residue, now_ctx.accounts) != (lines, out, residue, ctx.accounts):
        await db.rollback()
        raise HTTPException(status_code=409, detail=catalog_refusal("acct.err_suggest_stale"))
    suggest.apply_model(now_out, now_residue, replies or {}, now_ctx.accounts)
    found = suggest.finish(now_out)
    for line in rows:
        SL.write_suggestion(line, found.get(str(line.id)))
    result = {"model_ran": bool(residue) and replies is not None, "suggested": len(found),
              "summary": str(t("acct.recon_suggested", count=len(found)) if replies is not None else
                             t("acct.recon_model_not_run", count=sum(g["count"] for g in residue)))}
    if replies is not None:
        result = await _recorded(db, company_id, actor_id, key, request, "suggest", bank_id, result)
    await db.commit()
    return result


# ── Unmatch, complete, remove a statement ───────────────────────────────────

class Unmatch(BaseModel):
    line_ids: list[uuid.UUID] = Field(min_length=1, max_length=5000)
    idempotency_key: RequestKey


@router.post(f"{API}/bank/{{bank_id}}/unmatch", summary="Undo settled statement lines: void what they posted and "
             "release what they matched.", openapi_extra=_agent("line_ids", result_permissions=["record_payments"]))
async def unmatch_lines(bank_id: uuid.UUID, payload: Unmatch, company_id: uuid.UUID = Depends(get_current_company_id),
                        _: None = require_permission("manage_accounting"), role: str = Depends(get_current_role),
                        user=Depends(get_current_user), db: AsyncSession = Depends(get_session)) -> dict:
    """All the lines or none: the entries the lines posted are voided, the document payments
    they recorded deleted, and the lines are open again."""
    key, actor_id = payload.idempotency_key, user.id
    request = _request(payload, bank_id=bank_id)
    if (first := await _replayed(db, company_id, key, request, "unmatch")) is not None:
        return first
    # A document payment a line recorded is deleted the way any payment is, which locks the
    # payments recorded from unmatched ones first (after the company, before the lines).
    await lock_recorded_unmatched(db, company_id)
    bank = await _bank(db, company_id, bank_id)
    lines = await _locked_lines(db, company_id, bank, payload.line_ids, state=("matched", "posted", "skipped"))
    for line in lines:
        await SL.unmatch_line(db, company_id=company_id, actor_id=actor_id, role=role, line=line)
    result = await _recorded(db, company_id, actor_id, key, request, "unmatch", bank.id,
                             {"unmatched": [str(l.id) for l in lines]})
    await db.commit()
    return result


@router.post(f"{API}/bank/{{bank_id}}/complete", summary="Complete a bank's open statements in date order, up to "
             "the first that does not balance.", openapi_extra=_agent())
async def complete_bank(bank_id: uuid.UUID, payload: Keyed, company_id: uuid.UUID = Depends(get_current_company_id),
                        _: None = require_permission("manage_accounting"), user=Depends(get_current_user),
                        db: AsyncSession = Depends(get_session)) -> dict:
    """Each balanced statement is completed and hands the entries it counts to the next
    one, so the next statement's balance starts where this one ended."""
    from celerp.services.journal_accounts import hold_fund_ledger
    from celerp_accounting.routes import _je_entries_for_account, _lock_recon, _money_text as money_text, \
        _settled_difference

    key = payload.idempotency_key
    request = _request(payload, bank_id=bank_id)
    if (first := await _replayed(db, company_id, key, request, "complete")) is not None:
        return first
    # The lock every posting takes first, before the entries that decide the balance are read:
    # a posting in flight is waited for and then counted, never judged on the books before it.
    await hold_fund_ledger(db, company_id)
    bank = await _bank(db, company_id, bank_id)
    entries = await _je_entries_for_account(db, company_id, bank)
    pending = [s.id for s in await _sessions(db, company_id, bank) if s.status == "open"]
    completed: list[str] = []
    stopped_at = None
    for i, sid in enumerate(pending):
        recon, bank = await _lock_recon(db, sid, company_id, editable=True)
        if (counted := await _first_counted(db, company_id, bank, recon)) is not None:
            SL.carry_in(recon, counted)
        try:
            difference = await _settled_difference(db, recon, bank, entries)
            if difference != 0:
                raise HTTPException(status_code=422, detail=catalog_refusal(
                    "acct.err_recon_unbalanced", difference=money_text(difference, bank.currency)))
        except HTTPException as exc:
            stopped_at = {"statement_date": recon.statement_date, "reason": keyed_reason(exc)}
            break
        SL.complete(recon)
        completed.append(recon.statement_date)
        if i + 1 < len(pending):
            nxt, _b = await _lock_recon(db, pending[i + 1], company_id, editable=True)
            SL.hold(nxt, list(recon.reconciled_je_ids or []))
    result = await _recorded(db, company_id, user.id, key, request, "complete", bank.id,
                             {"completed": completed, "stopped_at": stopped_at})
    await db.commit()
    return result


@router.delete(f"{API}/{{session_id}}", summary="Remove an imported statement with none of its lines settled.",
               openapi_extra=_agent())
async def delete_statement(session_id: uuid.UUID, payload: Keyed,
                           company_id: uuid.UUID = Depends(get_current_company_id),
                           _: None = require_permission("manage_accounting"), user=Depends(get_current_user),
                           db: AsyncSession = Depends(get_session)) -> dict:
    """A statement imported by mistake goes with its lines. One with a settled line is
    refused until those lines are undone, so nothing it posted is left without its line."""
    from sqlalchemy import delete

    from celerp_accounting.routes import _lock_recon

    key = payload.idempotency_key
    request = _request(payload, session_id=session_id)
    if (first := await _replayed(db, company_id, key, request, "delete")) is not None:
        return first
    recon, bank = await _lock_recon(db, session_id, company_id, editable=True)
    settled = (await db.execute(select(BankStatementLine.id).where(
        BankStatementLine.company_id == company_id, BankStatementLine.reconciliation_id == recon.id,
        BankStatementLine.status != "unmatched").limit(1))).scalar_one_or_none()
    if settled is not None:
        raise HTTPException(status_code=409, detail=catalog_refusal("acct.err_recon_delete_settled"))
    await db.execute(delete(BankStatementLine).where(BankStatementLine.company_id == company_id,
                                                     BankStatementLine.reconciliation_id == recon.id))
    await db.delete(recon)
    result = await _recorded(db, company_id, user.id, key, request, "delete", bank.id,
                             {"deleted": str(session_id), "statement_date": recon.statement_date})
    await db.commit()
    return result


# ── Opening balance and Suspense ────────────────────────────────────────────

class Opening(BaseModel):
    amount: FiniteFloat
    date: Date
    rate: PositiveFloat | None = None
    idempotency_key: RequestKey


@router.put("/bank-accounts/{bank_id}/opening", summary="Set a bank's opening balance; a later change posts "
            "an adjustment.", openapi_extra=_agent())
async def set_opening(bank_id: uuid.UUID, payload: Opening, company_id: uuid.UUID = Depends(get_current_company_id),
                      _: None = require_permission("manage_accounting"), user=Depends(get_current_user),
                      db: AsyncSession = Depends(get_session)) -> dict:
    """The first figure posts the opening entry; a later one posts the difference as an
    adjustment dated ``date``, so the entries already posted stay as they are."""
    from celerp.services.journal_accounts import hold_fund_ledger
    from celerp_accounting.routes import _bank_rate, _base_currency, _opening_je_id, post_opening

    key = payload.idempotency_key
    request = _request(payload, bank_id=bank_id)
    if (first := await _replayed(db, company_id, key, request, "opening")) is not None:
        return first
    await hold_fund_ledger(db, company_id)
    bank = await _bank(db, company_id, bank_id, lock=True)
    amount = SL.money_value(payload.amount, bank.currency, zero_ok=True)
    current = round_money(to_decimal(bank.opening_balance or 0), bank.currency)
    states = await _states(db, company_id, [_opening_je_id(bank.id)])
    posted = bool(states)
    change = amount - current if posted else amount
    je_id = None
    if change != 0:
        je_id = f"je:opening-adj:{bank.id}:{uuid.uuid5(uuid.NAMESPACE_URL, f'{company_id}:{key}')}" if posted \
            else _opening_je_id(bank.id)
        fx = _bank_rate(bank, await _base_currency(db, company_id), payload.rate)
        memo = t("acct.recon_opening_adjusted" if posted else "acct.recon_opening_memo", bank=bank.bank_name)
        await post_opening(db, company_id, user.id, bank, je_id, change, payload.date.isoformat(), fx, str(memo))
    bank.opening_balance = float(amount)
    result = await _recorded(db, company_id, user.id, key, request, "opening", bank.id,
                             {"bank_id": str(bank.id), "opening_balance": str(amount), "je_id": je_id})
    await db.commit()
    return result


class Reclassify(BaseModel):
    account_code: AccountCode
    contact: str | None = Field(None, max_length=200)
    idempotency_key: RequestKey


@router.post(f"{API}/lines/{{line_id}}/reclassify", summary="Move a line out of Suspense to the account it belongs to.",
             openapi_extra=_agent())
async def reclassify(line_id: uuid.UUID, payload: Reclassify, company_id: uuid.UUID = Depends(get_current_company_id),
                     _: None = require_permission("manage_accounting"), user=Depends(get_current_user),
                     db: AsyncSession = Depends(get_session)) -> dict:
    """One entry moves the Suspense posting to ``account_code``, in the same amounts and
    funds; the Suspense entry stays, so an undo of the line voids both."""
    key = payload.idempotency_key
    request = _request(payload, line_id=line_id)
    if (first := await _replayed(db, company_id, key, request, "reclassify")) is not None:
        return first
    line = (await _locked_lines(db, company_id, None, [line_id], state=("posted",)))[0]
    if not is_suspense(line):
        raise HTTPException(status_code=409, detail=catalog_refusal("acct.err_recon_not_suspense"))
    je_id = await SL.reclassify(db, company_id=company_id, actor_id=user.id, line=line,
                                account=payload.account_code, contact=payload.contact,
                                suspense=await suspense_account(db, company_id, user.id), key=key)
    result = await _recorded(db, company_id, user.id, key, request, "reclassify", line.bank_account_id,
                             {"line_id": str(line.id), "je_id": je_id})
    await db.commit()
    return result


# ── Questions for the client ────────────────────────────────────────────────

_QUESTION_READERS = ["answer_bank_questions", "manage_accounting"]


async def _question_reader(role: str = Depends(get_current_role),
                           company_id: uuid.UUID = Depends(get_current_company_id),
                           db: AsyncSession = Depends(get_session)) -> None:
    """403 unless the caller's role answers bank questions or keeps the books."""
    from celerp.models.company import Company
    from celerp.services.permissions import admit_permission, missing_permission, role_has_permission

    company = await db.get(Company, company_id)
    settings = company.settings if company else {}
    # Each key named where it is checked, so the tools declare what their answers depend on.
    held = ("answer_bank_questions" if role_has_permission(settings, role, "answer_bank_questions")
            else "manage_accounting" if role_has_permission(settings, role, "manage_accounting") else None)
    if held is None:
        raise missing_permission(_QUESTION_READERS[0])
    await admit_permission(db, company_id, role, held)


@router.post(f"{API}/files", status_code=201, summary="Keep statement and receipt files for reading or an answer.",
             openapi_extra=agent_excluded("internal"))
async def bank_files(files: list[UploadFile] = File(...), company_id: uuid.UUID = Depends(get_current_company_id),
                     _: None = Depends(_question_reader), user=Depends(get_current_user),
                     db: AsyncSession = Depends(get_session)) -> dict:
    """The bank page's file drop and the client's receipts: kept without AI access, since a
    CSV, Excel or OFX statement is read here and a receipt is stored with its answer. Not a
    tool: the assistant's files are already kept when they are attached."""
    from celerp.ai.files import keep_uploads
    return {"file_ids": await keep_uploads(db, company_id, user.id, files)}


class Ask(BaseModel):
    line_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    note: str = Field("", max_length=_NOTE_MAX)
    idempotency_key: RequestKey


@router.post(f"{API}/bank/{{bank_id}}/ask", summary="Ask the client about open statement lines, or remind them.",
             openapi_extra=_agent("line_ids", result_permissions=["answer_bank_questions"]))
async def ask(bank_id: uuid.UUID, payload: Ask, company_id: uuid.UUID = Depends(get_current_company_id),
              _: None = require_permission("manage_accounting"), user=Depends(get_current_user),
              db: AsyncSession = Depends(get_session)) -> dict:
    """The key is claimed with the Ask's plan before the questions and notices are written, and
    they are kept before any email goes out; a failed email is said in the outcome and the
    question stays."""
    key, actor_id = payload.idempotency_key, user.id
    request = _request(payload, bank_id=bank_id)
    if await _replayed(db, company_id, key, request, "ask") is None:
        bank = await _bank(db, company_id, bank_id)
        lines = await _locked_lines(db, company_id, bank, payload.line_ids)
        plan = await SL.ask_plan(db, company_id=company_id, lines=lines, note=payload.note)
        plan, first = await _claimed(db, company_id, actor_id, key, request, "ask", bank.id, plan)
        if first:
            await SL.write_ask(db, company_id=company_id, lines=lines, plan=plan)
            await db.commit()
        else:
            await db.rollback()
    return await SL.email_ask(db, company_id=company_id, key=key)


class LineIds(BaseModel):
    line_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    idempotency_key: RequestKey


@router.post(f"{API}/bank/{{bank_id}}/withdraw-question", summary="Take back questions asked about statement lines.",
             openapi_extra=_agent("line_ids"))
async def withdraw_question(bank_id: uuid.UUID, payload: LineIds,
                            company_id: uuid.UUID = Depends(get_current_company_id),
                            _: None = require_permission("manage_accounting"), user=Depends(get_current_user),
                            db: AsyncSession = Depends(get_session)) -> dict:
    from celerp.services.company_lock import lock_company

    key = payload.idempotency_key
    request = _request(payload, bank_id=bank_id)
    if (first := await _replayed(db, company_id, key, request, "withdraw_question")) is not None:
        return first
    bank = await _bank(db, company_id, bank_id)
    await lock_company(db, company_id)  # the order answer_question takes: the company, then the line
    lines = await SL.scoped(db, company_id, "line", payload.line_ids, bank=bank)
    SL.withdraw_lines(lines)
    result = await _recorded(db, company_id, user.id, key, request, "withdraw_question", bank.id,
                             {"withdrawn": [str(l.id) for l in lines]})
    await db.commit()
    return result


def _question(line: BankStatementLine, bank: BankAccount) -> dict:
    q = line.client_query or {}
    return {"line_id": str(line.id), "bank_name": bank.bank_name, "line_date": line.line_date,
            "description": line.description, "amount": _money_text(line.amount, bank.currency),
            "currency": bank.currency, "status": line.status, "note": q.get("note"), "asked": q.get("asked"),
            "asks": q.get("asks"), "answer": q.get("answer"), "contact_text": q.get("contact_text"),
            "answered": q.get("answered"), "attachments": _attachments(line)}


@router.get("/bank-questions", summary="The questions asked about bank lines, newest first.",
            openapi_extra={"x-celerp-agent": True, "x-celerp-agent-any-permission": _QUESTION_READERS,
                           "x-celerp-agent-result-permissions": _QUESTION_READERS})
async def bank_questions(status: Literal["open", "answered", "all"] = "open",
                         company_id: uuid.UUID = Depends(get_current_company_id),
                         _: None = Depends(_question_reader), db: AsyncSession = Depends(get_session)) -> dict:
    banks = {b.id: b for b in (await db.execute(select(BankAccount).where(
        BankAccount.company_id == company_id))).scalars()}
    rows = [l for l in (await db.execute(select(BankStatementLine).where(
        BankStatementLine.company_id == company_id, BankStatementLine.client_query.is_not(None)))).scalars()
            if SL.asked(l)]
    answered = {l.id for l in rows if (l.client_query or {}).get("answered")}
    shown = [l for l in rows if status == "all" or (status == "answered") == (l.id in answered)]
    shown.sort(key=lambda l: (l.client_query["asked"], str(l.id)), reverse=True)
    return {"items": [_question(l, banks[l.bank_account_id]) for l in shown],
            "open": len(rows) - len(answered), "answered": len(answered)}


class Answer(BaseModel):
    answer: str = Field(min_length=1, max_length=_NOTE_MAX)
    contact_text: str | None = Field(None, max_length=200)
    file_ids: list[str] = Field(default_factory=list, max_length=20)
    idempotency_key: RequestKey


async def _named_contact(db: AsyncSession, company_id: uuid.UUID, text: str | None) -> dict | None:
    """The suggestion an answer naming a contact gives: that contact with the account its
    last posted line went to; None when no contact has the name or none was posted."""
    from celerp.models.projections import Projection

    name = (text or "").strip().casefold()
    if not name:
        return None
    contacts = [cid for cid, state in (await db.execute(select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "contact"))).all()
        if str((state or {}).get("name") or "").strip().casefold() == name]
    if len(contacts) != 1:
        return None
    rows = [r for rs in (await _history(db, company_id)).values() for r in rs if r["contact_id"] == contacts[0]]
    if not rows:
        return None
    return {"account_code": max(rows, key=lambda r: r["line_date"])["account_code"], "contact_id": contacts[0]}


@router.post("/bank-questions/{line_id}/answer", summary="Answer a question about a bank line, with receipts.",
             openapi_extra=_agent(any_permission=_QUESTION_READERS, result_permissions=_QUESTION_READERS))
async def answer_question(line_id: uuid.UUID, payload: Answer, company_id: uuid.UUID = Depends(get_current_company_id),
                          _: None = Depends(_question_reader), user=Depends(get_current_user),
                          db: AsyncSession = Depends(get_session)) -> dict:
    """The answer and its receipts are kept together or not at all. An answer naming a
    contact becomes the line's suggestion; a free answer waits for the next suggestion run."""
    from datetime import datetime, timezone

    from celerp.ai.files import load_file
    from celerp.services.attachments import storing
    from celerp.services.company_lock import lock_company
    from celerp_accounting import suggest

    key = payload.idempotency_key
    request = _request(payload, line_id=line_id)
    if (first := await _replayed(db, company_id, key, request, "answer")) is not None:
        return first
    try:
        files = [load_file(fid, company_id, user.id) for fid in payload.file_ids]
    except (FileNotFoundError, PermissionError):
        raise HTTPException(status_code=404, detail=catalog_refusal("error.file_not_found"))
    named = await _named_contact(db, company_id, payload.contact_text)
    try:
        async with storing(db, company_id) as store:
            await lock_company(db, company_id)  # the one order: the company, then the line
            line = (await SL.scoped(db, company_id, "line", [line_id]))[0]
            if not SL.asked(line):
                raise HTTPException(status_code=404, detail=catalog_refusal("acct.err_statement_line_not_found"))
            if line.status != "unmatched":
                raise HTTPException(status_code=409, detail=catalog_refusal("reconciliation.question_settled"))
            for content, meta in files:
                await SL.attach_file(store, company_id, line, content, meta.get("filename"),
                                     meta.get("content_type") or "application/octet-stream")
            line.client_query = {**line.client_query, "answer": payload.answer, "contact_text": payload.contact_text,
                                 "answered": datetime.now(timezone.utc).isoformat(),
                                 "contact_id": None, "account_code": None, **(named or {})}
            if named:
                SL.write_suggestion(line, suggest.answer_suggestion(line.client_query))
            result = await _recorded(db, company_id, user.id, key, request, "answer", line.bank_account_id,
                                     {"line_id": str(line.id), "suggested": bool(named),
                                      "attachments": _attachments(line)})
    except OSError:
        raise HTTPException(status_code=503, detail=catalog_refusal("reconciliation.receipt_not_saved"))
    return result
