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

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import KeyedRefusal, catalog_refusal
from celerp.db import get_session
from celerp.events.engine import emit_event, keyed_event, request_fingerprint
from celerp.schemas.numbers import FiniteFloat, PositiveFloat
from celerp.services.auth import get_current_company_id, get_current_role, get_current_user
from celerp.services.money import round_money, to_decimal
from celerp.services.permissions import require_permission
from celerp_accounting import statement_lines as SL
from celerp_accounting.models import BankAccount, BankStatementLine, ReconciliationSession
from celerp_accounting.routes import BankAccountCreate
from celerp_accounting.statement_reader import block_refusal
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
                    bank_id, result: dict, entity_id: str | None = None, data: dict | None = None) -> dict:
    """Store ``result`` as the answer to ``key``; the caller commits."""
    entity_id = entity_id or f"bankop:{uuid.uuid5(uuid.NAMESPACE_URL, f'{company_id}:{key}')}"
    await emit_event(db, company_id=company_id, entity_id=entity_id,
                     entity_type="bank_operation", event_type="acc.bank_statement.done",
                     data={"operation": operation, "bank_account_id": None if bank_id is None else str(bank_id),
                           **(data or {})},
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
        if step.state != "done":
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


def _review(st: dict, bank: BankAccount | None, base: str, existing: dict, held: dict) -> dict:
    from celerp_accounting.statement_reader import review_statement
    currency = bank.currency if bank is not None else (st["header"].get("currency") or base)
    return review_statement(st["header"], st["lines"], bank_currency=currency,
                            bank_id=bank.id if bank is not None else "new", existing=existing, held_counts=held)


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
    existing, held = await _known(db, company_id, bank)
    statements = []
    for i, st in enumerate(read["statements"]):
        edit = (given or [{}] * len(read["statements"]))[i]
        found = {"header": {**st["header"], **(edit.get("header") or {})},
                 "lines": st["lines"] if edit.get("lines") is None else edit["lines"]}
        statements.append({"file_id": st["file_id"], **_review(found, bank, base, existing, held)})
    out = {"job_id": str(job_id), "bank_id": None if bank is None else str(bank.id), "statements": statements,
           **{k: read[k] for k in ("receipts", "failed_steps", "needs_mapping", "layout_saved", "column_map", "headers")}}
    if bank is None:
        first = statements[0]["header"] if statements else {}
        out["bank"] = None
        out["new_bank"] = {"bank_name": first.get("bank_name") or "", "bank_type": "checking",
                           "currency": first.get("currency") or base,
                           "opening_balance": first.get("opening_balance"), "opening_date": first.get("period_start")}
    return out


@router.get(f"{API}/statement-preview/{{job_id}}", summary="The statements a reading job found, checked before import",
            openapi_extra={"x-celerp-agent": True})
async def statement_preview(job_id: uuid.UUID,
                            company_id: uuid.UUID = Depends(get_current_company_id),
                            _: None = require_permission("manage_accounting"), user=Depends(get_current_user),
                            db: AsyncSession = Depends(get_session)) -> dict:
    return await _preview(db, company_id, user.id, job_id)


class StatementIn(BaseModel):
    header: dict
    lines: list[dict] | None = None


class PreviewEdit(BaseModel):
    statements: list[StatementIn] = Field(min_length=1, max_length=50)
    column_map: dict[str, str] | None = None  # {field: column} for a file whose columns were not recognised


@router.post(f"{API}/statement-preview/{{job_id}}", summary="Check a reading job's statements again with edited "
             "header figures, lines or column map, before import", openapi_extra={"x-celerp-agent": True,
                                                                       "x-celerp-agent-confirm": False})
async def statement_preview_edited(job_id: uuid.UUID, payload: PreviewEdit,
                                   company_id: uuid.UUID = Depends(get_current_company_id),
                                   _: None = require_permission("manage_accounting"), user=Depends(get_current_user),
                                   db: AsyncSession = Depends(get_session)) -> dict:
    """Nothing is written: the same review Import runs, on the figures given."""
    return await _preview(db, company_id, user.id, job_id, [s.model_dump() for s in payload.statements],
                          payload.column_map)


# ── Import ──────────────────────────────────────────────────────────────────

class ImportStatement(BaseModel):
    job_id: uuid.UUID
    statements: list[StatementIn] = Field(min_length=1, max_length=50)
    bank_id: uuid.UUID | None = None
    new_bank: BankAccountCreate | None = None
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
    from celerp_accounting.routes import _base_currency, add_bank

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
        bank = await add_bank(db, company_id, user.id, payload.new_bank)
    base = await _base_currency(db, company_id)
    out = []
    for given, found in zip(payload.statements, read["statements"]):
        existing, held = await _known(db, company_id, bank)
        lines = given.lines if given.lines is not None else found["lines"]
        reviewed = _review({"header": {**found["header"], **given.header}, "lines": lines}, bank, base, existing, held)
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
                             {"bank_id": str(bank.id), "sessions": out}, data={"receipts": read["receipts"]})
    await db.commit()
    return result


# ── The group table ─────────────────────────────────────────────────────────

def _line_dict(line: BankStatementLine, currency: str) -> dict:
    return {"id": str(line.id), "statement_id": str(line.reconciliation_id), "line_date": line.line_date,
            "description": line.description, "amount": _money_text(line.amount, currency), "status": line.status,
            "reference": line.reference, "counterparty_key": line.counterparty_key,
            "counterparty_account": line.counterparty_account,
            "raw_balance": None if line.raw_balance is None else _money_text(line.raw_balance, currency),
            "matched_je_ids": list(line.matched_je_ids or []), "suggestion": line.suggestion,
            "client_query": line.client_query, "attachments": _attachments(line)}


def _attachments(line: BankStatementLine) -> list[dict]:
    from celerp_accounting.routes import _line_attachment
    return [_line_attachment(line.company_id, u) for u in line.attachment_ids or []]


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


@router.get(f"{API}/bank/{{bank_id}}/groups", summary="A bank's statement lines, grouped by counterparty",
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
    rows = sorted(await _bank_lines(db, company_id, bank), key=lambda l: (l.line_date, str(l.id)), reverse=True)
    shown = [l for l in rows if _in_view(l, view, open_ids) and (not q or _searched(l, q))]
    groups: dict = {}
    for row in shown:
        line = _line_dict(row, bank.currency)
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
    return refusal_text(exc.detail) if isinstance(exc, (HTTPException, KeyedRefusal)) else str(exc)


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
    lines = await _locked_lines(db, company_id, None, [line_id] + ([pair_id] if pair_id else []), state=None)
    by_id = {l.id: l for l in lines}
    line = by_id[line_id]
    if line.bank_account_id != bank.id:
        raise SL._not_found("line")
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


def _refused(status: int, key: str, **params) -> HTTPException:
    return HTTPException(status_code=status, detail=catalog_refusal(key, **params))


async def _pay_doc(db: AsyncSession, company_id: uuid.UUID, actor_id, role: str, bank: BankAccount, d: Decision,
                   key: str) -> dict:
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
        row = await db.get(Projection, {"company_id": company_id, "entity_id": x.doc_id})
        if row is not None and row.entity_type == "doc" and (row.state or {}).get("doc_type") != want:
            raise _refused(422, "acct.err_recon_doc_direction")
        event, _applied = await apply_doc_payment(
            db, str(company_id), x.doc_id,
            {"amount": x.amount, "payment_date": line.line_date, "bank_account": bank.chart_account_code,
             **({"conversion_rate": d.rate} if d.rate else {})},
            source="reconciliation", actor_id=actor_id, idempotency_key=f"{key}:{line.id}:pay:{x.doc_id}",
            commit=False)
        je_ids.append(f"je:auto:{x.doc_id}:pay:{event.data['index']}")
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
                  key: str) -> dict:
    """Carry out one decision; ``{line_id: je_ids}`` for each line it settled."""
    if d.kind in ("transfer", "cash"):
        return await _transfer(db, company_id, actor_id, bank, d, key)
    if d.kind == "pay_doc":
        return await _pay_doc(db, company_id, actor_id, role, bank, d, key)
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
        out[str(line.id)] = await SL.post_line(
            db, company_id=company_id, actor_id=actor_id, bank=bank, line=line, offsets=own, rate=d.rate,
            memo=d.memo, entry_date=d.date.isoformat() if d.date else None, key=key,
            je_type="recon_suspense" if d.kind == "suspense" else None)
        if d.remember:
            await _remember(db, company_id, line, d)
    return out


async def _accepted(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount, accept: Accept) -> list[Decision]:
    """The decisions the selected groups' suggestions make: each open line of a named group
    (narrowed to ``line_ids`` when given) that carries a suggestion. A key with no open
    line, or a named line that is not open with a suggestion, is refused."""
    from celerp.accounting_roles import AccountRole
    from celerp.services import account_roles

    open_ids = {s.id for s in await _sessions(db, company_id, bank) if s.status == "open"}
    rows = [l for l in await _bank_lines(db, company_id, bank)
            if l.reconciliation_id in open_ids and l.status == "unmatched"]
    keys = set(accept.group_keys)
    in_groups = {l.id: l for l in rows if _group_key(_line_dict(l, bank.currency)) in keys}
    found = {_group_key(_line_dict(l, bank.currency)) for l in in_groups.values()}
    if keys - found:
        raise _refused(409, "reconciliation.group_closed")
    if accept.line_ids is not None:
        named = await SL.scoped(db, company_id, "line", accept.line_ids, bank=bank)
        if any(l.id not in in_groups or not l.suggestion for l in named):
            raise _refused(409, "reconciliation.line_not_open")
        chosen = [in_groups[l.id] for l in named]
    else:
        chosen = [l for l in in_groups.values() if l.suggestion and l.suggestion.get("kind")]
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
            base["target_bank_id"] = s.get("target_bank_id")
        elif s.get("kind") == "pay_doc":
            base["docs"] = s.get("docs")
        if s.get("kind") not in _KINDS.__args__:
            raise _refused(409, "reconciliation.line_not_open")
        out.append(Decision(**base))
    return out


async def _bump_rules(db: AsyncSession, company_id: uuid.UUID, decisions: list[Decision], done: dict,
                      by_line: dict) -> None:
    """Count each rule whose suggestion a posted line followed."""
    from celerp.services.company_lock import lock_company
    from celerp_accounting.models import ReconciliationRule

    await lock_company(db, company_id)

    counts: dict = {}
    for d in decisions:
        for lid in d.line_ids:
            rule_id = by_line.get(lid)
            if rule_id and done.get(str(lid)):
                counts[rule_id] = counts.get(rule_id, 0) + 1
    for rule_id, n in counts.items():
        rule = (await db.execute(select(ReconciliationRule).where(
            ReconciliationRule.id == uuid.UUID(rule_id), ReconciliationRule.company_id == company_id)
            .with_for_update())).scalars().first()
        if rule is not None:
            rule.times_applied = (rule.times_applied or 0) + n


@router.post(f"{API}/bank/{{bank_id}}/resolve", summary="Resolve a bank's statement lines: post, match, no entry, "
             "transfer, cash, pay documents or Suspense", openapi_extra=_agent("decisions"))
async def resolve(bank_id: uuid.UUID, payload: Resolve, company_id: uuid.UUID = Depends(get_current_company_id),
                  _: None = require_permission("manage_accounting"), role: str = Depends(get_current_role),
                  user=Depends(get_current_user), db: AsyncSession = Depends(get_session)) -> dict:
    """Each decision commits on its own: a refused one (a line another user settled, a
    locked period, a document over its open balance) is reported with its reason while
    the rest go through. A replay of the same key answers the first result."""
    key, actor_id = payload.idempotency_key, user.id
    request = _request(payload, bank_id=bank_id)
    if (first := await _replayed(db, company_id, key, request)) is not None:
        return first
    bank = await _bank(db, company_id, bank_id)
    decisions = list(payload.decisions or [])
    by_line: dict = {}
    if payload.accept is not None:
        accepted = await _accepted(db, company_id, bank, payload.accept)
        lines = {l.id: l for l in await SL.scoped(db, company_id, "line", [x for d in accepted for x in d.line_ids],
                                                  bank=bank)} if accepted else {}
        by_line = {lid: (l.suggestion or {}).get("rule_id") for lid, l in lines.items()}
        decisions += accepted
    _check_money(decisions, bank.currency)
    await db.rollback()
    results: list[dict] = []
    done: dict = {}
    for d in decisions:
        try:
            bank = await _bank(db, company_id, bank_id)
            settled = await _decide(db, company_id, actor_id, role, bank, d, key)
            await db.commit()
        except (HTTPException, KeyedRefusal) as exc:
            await db.rollback()
            results += [{"line_id": str(lid), "ok": False, "je_ids": [], "reason": _reason(exc)} for lid in d.line_ids]
            continue
        done.update(settled)
        results += [{"line_id": lid, "ok": True, "je_ids": ids, "reason": None} for lid, ids in settled.items()]
    if by_line:
        await _bump_rules(db, company_id, decisions, done, by_line)
    result = await _recorded(db, company_id, actor_id, key, request, "resolve", bank_id, {"results": results, "refused": sum(1 for r in results if not r["ok"])})
    await db.commit()
    return result


# ── Suggest ─────────────────────────────────────────────────────────────────

class Keyed(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=200)


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


async def _held(db: AsyncSession, company_id: uuid.UUID) -> set[str]:
    """Every entry a statement or a line of the company already counts."""
    held: set[str] = set()
    for ids in (await db.execute(select(ReconciliationSession.reconciled_je_ids).where(
            ReconciliationSession.company_id == company_id))).scalars():
        held.update(ids or [])
    for ids in (await db.execute(select(BankStatementLine.matched_je_ids).where(
            BankStatementLine.company_id == company_id))).scalars():
        held.update(ids or [])
    return held


async def _open_docs(db: AsyncSession, company_id: uuid.UUID, currency: str, base: str) -> list[dict]:
    """Invoices and bills with a balance left, in the bank's currency."""
    from celerp.models.projections import Projection

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
                    "open_balance": _money_text(state["amount_outstanding"], currency), "ref": state.get("ref_id")})
    return out


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


async def _ladder_context(db: AsyncSession, company_id: uuid.UUID, bank: BankAccount, own_ids: set) -> "object":
    from celerp.accounting_roles import AccountRole
    from celerp.services.account_roles import resolve as resolve_role
    from celerp_accounting import suggest
    from celerp_accounting.models import ReconciliationRule
    from celerp_accounting.routes import _base_currency, _je_entries_for_account, _opening_je_id, _rule_to_dict

    held = await _held(db, company_id) | {_opening_je_id(bank.id)}
    book = [e for e in await _je_entries_for_account(db, company_id, bank)
            if e["je_id"] not in held and e["amount"] is not None]
    states = await _states(db, company_id, [e["je_id"] for e in book])
    entries = [{"je_id": e["je_id"], "amount": str(e["amount"]), "date": str(e["ts"])[:10], "memo": e["memo"],
                "contact": _contact(states.get(e["je_id"]) or {}, bank.chart_account_code)} for e in book]
    rules = [_rule_to_dict(r) for r in (await db.execute(select(ReconciliationRule).where(
        ReconciliationRule.company_id == company_id, ReconciliationRule.is_active.is_(True)))).scalars()]
    open_sessions = {s.id for s in (await db.execute(select(ReconciliationSession).where(
        ReconciliationSession.company_id == company_id, ReconciliationSession.status == "open"))).scalars()}
    pair_lines = [{"id": str(l.id), "bank_id": str(l.bank_account_id), "amount": str(l.amount), "line_date": l.line_date}
                  for l in (await db.execute(select(BankStatementLine).where(
                      BankStatementLine.company_id == company_id, BankStatementLine.status == "unmatched",
                      BankStatementLine.bank_account_id != bank.id))).scalars()
                  if l.reconciliation_id in open_sessions and l.id not in own_ids]
    base = await _base_currency(db, company_id)
    return suggest.LadderContext(
        bank_id=str(bank.id), entries=entries, open_docs=await _open_docs(db, company_id, bank.currency, base),
        rules=rules, pair_lines=pair_lines, slips=await _slips(db, company_id, bank),
        history=await _history(db, company_id), accounts=await _postable(db, company_id),
        general_expense=await resolve_role(db, company_id, AccountRole.GENERAL_EXPENSE))


async def _model_replies(residue: list[dict], accounts: list[str], call) -> dict | None:
    """The model's replies for the residue groups; None when the model cannot run."""
    import inspect

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
    except Exception:  # the model's failure leaves the lines to questions, said in the summary
        return None
    finally:
        suggest.current_call.reset(token)
    return replies


@router.post(f"{API}/bank/{{bank_id}}/suggest", summary="Suggest an answer for each open statement line of a bank",
             openapi_extra=_agent(credits=True))
async def suggest_lines(bank_id: uuid.UUID, payload: Keyed, company_id: uuid.UUID = Depends(get_current_company_id),
                        _: None = require_permission("manage_accounting"), user=Depends(get_current_user),
                        db: AsyncSession = Depends(get_session)) -> dict:
    """Suggestions only: no line is posted or matched. A replay of the same key answers
    the first result and the model is not asked again."""
    from celerp_accounting import suggest

    key, actor_id = payload.idempotency_key, user.id
    request = _request(payload, bank_id=bank_id)
    if (first := await _replayed(db, company_id, key, request)) is not None:
        return first
    bank = await _bank(db, company_id, bank_id)
    open_ids = {s.id for s in await _sessions(db, company_id, bank) if s.status == "open"}
    rows = [l for l in await _bank_lines(db, company_id, bank)
            if l.reconciliation_id in open_ids and l.status == "unmatched"]
    lines = [{"id": str(l.id), "line_date": l.line_date, "description": l.description,
              "amount": _money_text(l.amount, bank.currency), "counterparty_key": l.counterparty_key,
              "counterparty_account": l.counterparty_account, "reference": l.reference,
              "client": l.client_query if SL.asked(l) and l.client_query.get("answered") else None} for l in rows]
    ctx = await _ladder_context(db, company_id, bank, {l.id for l in rows})
    out, residue = suggest.ladder_rungs(lines, ctx)
    replies = await _model_replies(residue, ctx.accounts, suggest.ModelCall(db, company_id, actor_id, key)) \
        if residue else {}
    suggest.apply_model(out, residue, replies or {}, ctx.accounts)
    found = suggest.finish(out)
    for line in rows:
        SL.write_suggestion(line, found.get(str(line.id)))
    model_ran = bool(residue) and replies is not None
    summary = t("acct.recon_suggested", count=len(found)) if replies is not None else \
        t("acct.recon_model_not_run", count=sum(g["count"] for g in residue))
    result = await _recorded(db, company_id, actor_id, key, request, "suggest", bank.id,
                             {"model_ran": model_ran, "summary": str(summary), "suggested": len(found)})
    await db.commit()
    return result


# ── Unmatch, complete, remove a statement ───────────────────────────────────

class Unmatch(BaseModel):
    line_ids: list[uuid.UUID] = Field(min_length=1, max_length=5000)
    idempotency_key: str = Field(min_length=1, max_length=200)


@router.post(f"{API}/bank/{{bank_id}}/unmatch", summary="Undo settled statement lines: void what they posted and "
             "release what they matched", openapi_extra=_agent("line_ids"))
async def unmatch_lines(bank_id: uuid.UUID, payload: Unmatch, company_id: uuid.UUID = Depends(get_current_company_id),
                        _: None = require_permission("manage_accounting"), user=Depends(get_current_user),
                        db: AsyncSession = Depends(get_session)) -> dict:
    """All the lines or none: the entries the lines posted are voided and the lines are open again."""
    key, actor_id = payload.idempotency_key, user.id
    request = _request(payload, bank_id=bank_id)
    if (first := await _replayed(db, company_id, key, request)) is not None:
        return first
    bank = await _bank(db, company_id, bank_id)
    lines = await _locked_lines(db, company_id, bank, payload.line_ids, state=("matched", "posted", "skipped"))
    for line in lines:
        await SL.unmatch_line(db, company_id=company_id, actor_id=actor_id, line=line)
    result = await _recorded(db, company_id, actor_id, key, request, "unmatch", bank.id,
                             {"unmatched": [str(l.id) for l in lines]})
    await db.commit()
    return result


@router.post(f"{API}/bank/{{bank_id}}/complete", summary="Complete a bank's open statements in date order, up to "
             "the first that does not balance", openapi_extra=_agent())
async def complete_bank(bank_id: uuid.UUID, payload: Keyed, company_id: uuid.UUID = Depends(get_current_company_id),
                        _: None = require_permission("manage_accounting"), user=Depends(get_current_user),
                        db: AsyncSession = Depends(get_session)) -> dict:
    """Each balanced statement is completed and hands the entries it counts to the next
    one, so the next statement's balance starts where this one ended."""
    from datetime import datetime, timezone

    from celerp_accounting.routes import _je_entries_for_account, _lock_recon, _money_text as money_text, \
        _settled_difference

    key = payload.idempotency_key
    request = _request(payload, bank_id=bank_id)
    if (first := await _replayed(db, company_id, key, request)) is not None:
        return first
    bank = await _bank(db, company_id, bank_id)
    entries = await _je_entries_for_account(db, company_id, bank)
    pending = [s.id for s in await _sessions(db, company_id, bank) if s.status == "open"]
    completed: list[str] = []
    stopped_at = None
    for i, sid in enumerate(pending):
        recon, bank = await _lock_recon(db, sid, company_id, editable=True)
        try:
            difference = await _settled_difference(db, recon, bank, entries)
            if difference != 0:
                raise HTTPException(status_code=422, detail=catalog_refusal(
                    "acct.err_recon_unbalanced", difference=money_text(difference, bank.currency)))
        except HTTPException as exc:
            stopped_at = {"statement_date": recon.statement_date, "reason": _reason(exc)}
            break
        recon.status = "completed"
        recon.completed_at = datetime.now(timezone.utc)
        completed.append(recon.statement_date)
        if i + 1 < len(pending):
            nxt, _b = await _lock_recon(db, pending[i + 1], company_id, editable=True)
            SL.hold(nxt, list(recon.reconciled_je_ids or []))
    result = await _recorded(db, company_id, user.id, key, request, "complete", bank.id,
                             {"completed": completed, "stopped_at": stopped_at})
    await db.commit()
    return result


@router.delete(f"{API}/{{session_id}}", summary="Remove an imported statement with none of its lines settled",
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
    if (first := await _replayed(db, company_id, key, request)) is not None:
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
    idempotency_key: str = Field(min_length=1, max_length=200)


@router.put("/bank-accounts/{bank_id}/opening", summary="Set a bank's opening balance; a later change posts "
            "an adjustment", openapi_extra=_agent())
async def set_opening(bank_id: uuid.UUID, payload: Opening, company_id: uuid.UUID = Depends(get_current_company_id),
                      _: None = require_permission("manage_accounting"), user=Depends(get_current_user),
                      db: AsyncSession = Depends(get_session)) -> dict:
    """The first figure posts the opening entry; a later one posts the difference as an
    adjustment dated ``date``, so the entries already posted stay as they are."""
    from celerp.services.journal_accounts import hold_fund_ledger
    from celerp_accounting.routes import _bank_rate, _base_currency, _opening_je_id, post_opening

    key = payload.idempotency_key
    request = _request(payload, bank_id=bank_id)
    if (first := await _replayed(db, company_id, key, request)) is not None:
        return first
    await hold_fund_ledger(db, company_id)
    bank = await _bank(db, company_id, bank_id)
    await db.refresh(bank, with_for_update=True)
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
    account_code: str = Field(min_length=1, max_length=32)
    contact: str | None = Field(None, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)


@router.post(f"{API}/lines/{{line_id}}/reclassify", summary="Move a line out of Suspense to the account it belongs to",
             openapi_extra=_agent())
async def reclassify(line_id: uuid.UUID, payload: Reclassify, company_id: uuid.UUID = Depends(get_current_company_id),
                     _: None = require_permission("manage_accounting"), user=Depends(get_current_user),
                     db: AsyncSession = Depends(get_session)) -> dict:
    """One entry moves the Suspense posting to ``account_code``, in the same amounts and
    funds; the Suspense entry stays, so an undo of the line voids both."""
    key = payload.idempotency_key
    request = _request(payload, line_id=line_id)
    if (first := await _replayed(db, company_id, key, request)) is not None:
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
    held = next((k for k in _QUESTION_READERS if role_has_permission(company.settings if company else {}, role, k)),
                None)
    if held is None:
        raise missing_permission(_QUESTION_READERS[0])
    await admit_permission(db, company_id, role, held)


@router.post(f"{API}/files", status_code=201, summary="Keep statement and receipt files for reading or an answer")
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
    idempotency_key: str = Field(min_length=1, max_length=200)


@router.post(f"{API}/bank/{{bank_id}}/ask", summary="Ask the client about open statement lines, or remind them",
             openapi_extra=_agent("line_ids"))
async def ask(bank_id: uuid.UUID, payload: Ask, company_id: uuid.UUID = Depends(get_current_company_id),
              _: None = require_permission("manage_accounting"), user=Depends(get_current_user),
              db: AsyncSession = Depends(get_session)) -> dict:
    """The question is kept, and the client told in Celerp, before the email goes out; a
    failed email is said in the outcome and the question stays."""
    key = payload.idempotency_key
    request = _request(payload, bank_id=bank_id)
    if (first := await _replayed(db, company_id, key, request)) is not None:
        return first
    bank = await _bank(db, company_id, bank_id)
    lines = await _locked_lines(db, company_id, bank, payload.line_ids)
    result = await SL.ask_lines(db, company_id=company_id, key=key, lines=lines, note=payload.note)
    await _recorded(db, company_id, user.id, key, request, "ask", bank.id, result)
    await db.commit()
    return result


class LineIds(BaseModel):
    line_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=200)


@router.post(f"{API}/bank/{{bank_id}}/withdraw-question", summary="Take back questions asked about statement lines",
             openapi_extra=_agent("line_ids"))
async def withdraw_question(bank_id: uuid.UUID, payload: LineIds,
                            company_id: uuid.UUID = Depends(get_current_company_id),
                            _: None = require_permission("manage_accounting"), user=Depends(get_current_user),
                            db: AsyncSession = Depends(get_session)) -> dict:
    from celerp.services.company_lock import lock_company

    key = payload.idempotency_key
    request = _request(payload, bank_id=bank_id)
    if (first := await _replayed(db, company_id, key, request)) is not None:
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


@router.get("/bank-questions", summary="The questions asked about bank lines, newest first",
            openapi_extra={"x-celerp-agent": True, "x-celerp-agent-any-permission": _QUESTION_READERS})
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
    idempotency_key: str = Field(min_length=1, max_length=200)


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


@router.post("/bank-questions/{line_id}/answer", summary="Answer a question about a bank line, with receipts",
             openapi_extra=_agent(any_permission=_QUESTION_READERS))
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
    if (first := await _replayed(db, company_id, key, request)) is not None:
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
