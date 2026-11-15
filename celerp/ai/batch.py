# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""AI batch processing - the extraction stage for attached receipts and invoices.

A reading job reads 1 to MAX_BATCH_FILES files, each one a step of the job run by
celerp.ai.jobs inside a ``documents`` slot of the one model gate. Each file gets its
own model call that returns a structured extraction. Nothing is written to the books
here: the extractions feed the proposal step, and every document is created only
after the user confirms it.

Credits are metered per file from the gateway's usage report, so a job that
fails halfway charges only for the files it actually read.

Usage:
    job = await create_batch_job(session, company_id, user_id, query, file_ids)
    jobs.start_job(job.id)   # after the commit; progress rides the notification stream
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.ai import jobs, llm, service
from celerp.ai.files import TABULAR_UPLOAD_TYPES, load_file, load_file_for_llm
from celerp.ai.llm import RelayError
from ui.i18n import t

log = logging.getLogger(__name__)

MAX_BATCH_FILES = 100

_BATCH_SYSTEM_PROMPT = """\
You are reading one business document: a receipt, a supplier invoice, a bank or \
card statement, or something else.
Extract what is printed on it. Never guess a value that is not on the document; \
leave it null instead.

Output a JSON block:
```json
{
  "document_kind": "receipt|invoice|statement|other",
  "bill_count": 1,
  "vendor_name": "string or null",
  "date": "YYYY-MM-DD or null",
  "currency": "ISO 4217 code or null",
  "total": 0.00,
  "tax": 0.00,
  "reference": "invoice or receipt number, or null",
  "line_items": [
    {"description": "string", "quantity": 1, "unit_price": 0.00}
  ]
}
```

bill_count is how many separate bills, receipts or invoices the file holds. A bill \
that runs over several pages is 1, and a statement listing transactions is 1. When \
the file holds more than one, extract only the first.

If the document is unreadable, return a one-sentence explanation instead of JSON.
"""

# A statement reading job (read-statement) asks this; its steps use the statement shape.
STATEMENT_QUERY = "Read this bank or card statement."
STATEMENT_PAGES = 3
# Files a statement job never sends: their owning module parses them without credits.
STRUCTURED_TYPES = TABULAR_UPLOAD_TYPES
STRUCTURED_SUFFIXES = (".csv", ".xlsx", ".ofx", ".qfx")

_STATEMENT_SYSTEM_PROMPT = """\
You are reading pages of a bank or card statement, or a payment slip or receipt sent with one.
Extract what is printed. Never guess a value that is not on the page; leave it null instead.

For a statement, output a JSON block:
```json
{
  "kind": "statement",
  "header": {"period_start": "YYYY-MM-DD", "period_end": "YYYY-MM-DD", "opening_balance": "0.00",
             "closing_balance": "0.00", "currency": "ISO 4217 code", "account_tail": "last 4 digits or null"},
  "lines": [{"line_date": "YYYY-MM-DD", "description": "string", "amount": "-0.00 for money out, 0.00 for money in",
             "balance": "running balance or null", "base_amount": "books-currency amount if printed, or null",
             "page": 1}]
}
```
For a slip or receipt, output:
```json
{"kind": "receipt", "date": "YYYY-MM-DD", "total": "0.00", "tax": "0.00 or null", "tax_id": "string or null",
 "is_tax_invoice": false, "party": "who was paid or who paid"}
```
If the pages are unreadable, return a one-sentence explanation instead of JSON.
"""

_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def parse_extraction(answer: str) -> dict | None:
    """Return the JSON object in a model answer, or None when there is none.

    Tries a fenced ```json block first, then the whole answer, then the first
    balanced object in the text. Anything that is not a JSON object is None.
    """
    candidates: list[str] = []
    fenced = _FENCED_JSON.search(answer)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(answer.strip())
    start = answer.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(answer)):
            if answer[i] == "{":
                depth += 1
            elif answer[i] == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(answer[start:i + 1])
                    break
    for text in candidates:
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def bill_count(extraction: dict | None) -> int:
    """How many bills the model counted in a read file: a whole number of at least 1.
    A count that is missing or not a whole number above 0 is 1."""
    count = extraction.get("bill_count") if isinstance(extraction, dict) else None
    return count if type(count) is int and count > 1 else 1


def _file_error(exc: BaseException) -> str:
    """The user-facing reason a single file could not be read."""
    if isinstance(exc, HTTPException) and exc.status_code == 402:
        return t("ai.file_out_of_credits")
    # A free account's read is refused while free questions are paused. The relay's
    # other free refusal (text too long) cannot reach a read: its text is the prompt.
    if isinstance(exc, HTTPException) and isinstance(exc.detail, dict) and exc.detail.get("code") == "free_paused":
        return t("ai.free_paused", date=str(exc.detail.get("resets_at") or "")[:10])
    if isinstance(exc, RelayError):
        if exc.code in service.NEW_QUESTION_ERRORS:  # its read cannot be taken again; a retry reads it anew
            return t("ai.file_lost")
        return service.user_error(exc, unknown="ai.file_unreadable")
    if isinstance(exc, asyncio.TimeoutError):
        return t("ai.file_timeout")
    return t("ai.file_unreadable")



@dataclass
class FileRun:
    """What every file of one reading job is read with."""
    job_id: uuid.UUID
    company_id: uuid.UUID
    user_id: uuid.UUID
    conversation_id: uuid.UUID | None
    query: str
    # The claim the job's runner holds its lease under (celerp.ai.jobs._claim).
    runner_id: str | None = None
    # The admission refusal that stopped the job's unsent steps (an i18n key).
    stop_reason: str | None = None


@dataclass
class FileOutcome:
    """How one send of a file ended: the step state, what the user is shown, and the
    charge (``known``: the service named the operation's whole charge). ``sent``: the
    file left for the service, so it has a usage row to settle."""
    state: str
    result: dict
    credits: int = 0
    known: bool = False
    sent: bool = False


def reads_without_model(run: FileRun, file_key: str) -> bool:
    """Whether a step of ``run`` is read here without the model: a tabular or OFX file of a
    statement job, which its owning module parses itself. Such a step needs no AI access."""
    from celerp.ai.files import _file_paths_and_meta
    if run.query != STATEMENT_QUERY:
        return False
    try:
        _path, meta = _file_paths_and_meta(file_key.partition(":")[0], run.company_id, run.user_id)
    except (FileNotFoundError, PermissionError):
        return False
    return is_structured(meta.get("content_type") or "", meta.get("filename") or "")


async def read_file(run: FileRun, file_id: str, operation_id: uuid.UUID, *,
                    before_send: Callable[[int], Awaitable[None]]) -> FileOutcome:
    """Read one file under ``operation_id`` (the caller holds its gate slot). It is
    sent again only under that id: while its first send still runs, and when the
    service surely did not run it or no reply came back. An outcome the service
    cannot report, or an id it no longer accepts, ends the file without running it."""
    statement = run.query == STATEMENT_QUERY
    file_id, _, pages = file_id.partition(":")
    try:
        file = load_file_for_llm(file_id, run.company_id, run.user_id)
    except (FileNotFoundError, PermissionError):
        return FileOutcome("failed", {"filename": file_id, "error": t("error.file_gone")})
    filename = file["filename"]
    if statement and is_structured(file["media_type"], filename):
        return FileOutcome("done", {"filename": filename, "structured": True})
    if pages:
        file = {**file, "data": _page_slice(file_id, run, pages)}
    # The file's name goes with it: "slip.jpg" beside a statement tells the model what it holds.
    read = f"Read the attached document, {filename}."
    prompt = f"{run.query}\n\n{read}" if run.query else read
    system = _STATEMENT_SYSTEM_PROMPT if statement else _BATCH_SYSTEM_PROMPT
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": llm._build_user_content(prompt, [file])}]
    hints = {"query": prompt[:500], "file_count": 1, "is_batch": False}
    sent = False

    async def _send(nbytes: int) -> None:
        nonlocal sent
        await before_send(nbytes)
        sent = True

    def _ended(state: str, exc: BaseException, credits: int = 0, known: bool = False) -> FileOutcome:
        return FileOutcome(state, {"filename": filename, "error": _file_error(exc)}, credits, known, sent)

    try:
        result = await jobs.send_step(
            lambda: llm.complete(messages, operation_id=operation_id, hints=hints, before_send=_send), resend=True)
    except RelayError as exc:
        if exc.code == "outcome_unknown":
            charge = exc.credits if exc.operation_credits is None else exc.operation_credits
            return _ended("unknown", exc, charge, True)
        if exc.code == "operation_expired":
            return _ended("expired", exc, 0, True)
        return _ended("failed", exc, exc.credits)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        log.warning("File %s of job %s got no reply: %s", file_id, run.job_id, exc)
        return _ended("failed", asyncio.TimeoutError())
    except HTTPException as exc:
        return _ended("failed", exc)
    answer = result.message.get("content") or ""
    known = result.operation_credits is not None
    credits = result.operation_credits if known else int((result.usage or {}).get("credits") or 0)
    extraction = parse_extraction(answer)
    if statement and (extraction or {}).get("kind") not in ("statement", "receipt"):
        # Nothing readable came back: the step ends failed with the model's reason, to retry.
        return FileOutcome("failed", {"filename": filename, "pages": pages or None,
                                      "error": answer.strip()[:300] or t("ai.err_file_unexpected")},
                           credits, known, sent)
    return FileOutcome("done", {"filename": filename, "pages": pages or None, "answer": answer,
                                "extraction": extraction}, credits, known, sent)


def is_structured(content_type: str, filename: str) -> bool:
    return content_type in STRUCTURED_TYPES or filename.lower().endswith(STRUCTURED_SUFFIXES)


def statement_step_keys(file_ids: list[str], company_id: uuid.UUID, user_id: uuid.UUID) -> list[str]:
    """One step per file, and a PDF one step per range of up to STATEMENT_PAGES pages
    (``file_id:first-last``), read in page order."""
    import io
    import pypdf
    keys: list[str] = []
    for file_id in file_ids:
        data, meta = load_file(file_id, company_id, user_id)
        if meta.get("content_type") != "application/pdf":
            keys.append(file_id)
            continue
        count = len(pypdf.PdfReader(io.BytesIO(data)).pages)
        keys += [f"{file_id}:{a}-{min(a + STATEMENT_PAGES - 1, count)}"
                 for a in range(1, count + 1, STATEMENT_PAGES)]
    return keys


def _page_slice(file_id: str, run: "FileRun", pages: str) -> str:
    """The pages ``first-last`` of a PDF as a PDF of their own, base64 for the model."""
    import base64
    import io
    import pypdf
    data, _meta = load_file(file_id, run.company_id, run.user_id)
    first, last = (int(n) for n in pages.split("-"))
    reader, writer = pypdf.PdfReader(io.BytesIO(data)), pypdf.PdfWriter()
    for i in range(first - 1, last):
        writer.add_page(reader.pages[i])
    buf = io.BytesIO()
    writer.write(buf)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


async def create_batch_job(
    session: AsyncSession,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
    query: str,
    file_ids: list[str],
    conversation_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    step_keys: list[str] | None = None,
):
    """Create a pending reading job, held by this process, and one step per file, under
    the company's lock (lock_for_ai, a retake for a caller that holds it); the caller
    commits and then starts it (celerp.ai.jobs.start_job).

    ``job_id`` is the id of the question that asked for the job, when there is one."""
    request = jobs.JobRequest(company_id=company_id, user_id=user_id, kind="documents",
                              conversation_id=conversation_id, query=query, file_ids=tuple(file_ids))
    file_ids = list(dict.fromkeys(file_ids))
    if not file_ids:
        raise ValueError(t("ai.attach_one_file"))
    if len(file_ids) > MAX_BATCH_FILES:
        raise ValueError(t("error.batch_too_many", max=MAX_BATCH_FILES))
    job, steps = jobs.documents_job(request=request, file_ids=file_ids, job_id=job_id, step_keys=step_keys)
    await jobs.lock_for_ai(session, company_id)
    session.add(job)
    await session.flush()
    session.add_all(steps)
    await session.flush()
    return job


async def publish_progress(run: FileRun, file_id: str, state: str) -> None:
    """Tell the user's open pages that one more file of the job has ended. Best effort:
    the file's result is already written, so a failure here never sends it again."""
    from celerp.notifications.sse import publish
    try:
        async with jobs.SessionLocal() as session:
            steps = (await jobs.job_steps(session, [run.job_id]))[run.job_id]
        counts = Counter(step.state for step in steps)
        open_steps = sum(counts.get(s, 0) for s in jobs.OPEN_STEPS)
        completed = counts.get("done", 0)
        await publish(run.company_id, run.user_id, {
            "type": "batch_progress", "job_id": str(run.job_id), "completed": completed,
            "failed": sum(counts.values()) - completed - open_steps, "total": sum(counts.values()),
            "file_id": file_id, "status": jobs.FILE_STATUS[state],
        })
    except Exception:
        log.warning("Progress event for job %s not published", run.job_id, exc_info=True)


async def notify_finished(run: FileRun, completed: int, failed: int) -> None:
    """The notification a finished reading job leaves for its user."""
    key = "ai.files_read" if completed else "ai.files_none_read"
    await jobs.notify(run.company_id, run.user_id, run.conversation_id, key + ("_one" if completed + failed == 1 else ""),
                      completed=completed, failed=failed, total=completed + failed)


