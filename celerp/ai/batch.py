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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.ai import jobs, llm
from celerp.ai.files import load_file_for_llm
from celerp.ai.llm import RelayError
from celerp.models.ai import AIBatchJob, AIJobStep
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

If the document is unreadable, return a one-sentence explanation instead of JSON.
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


def _file_error(exc: BaseException) -> str:
    """The user-facing reason a single file could not be read."""
    if isinstance(exc, HTTPException) and exc.status_code == 402:
        return "You are out of AI credits. Add credits and resend this file."
    if isinstance(exc, RelayError):
        return str(exc)
    if isinstance(exc, asyncio.TimeoutError):
        return "The model did not answer in time for this file. Resend it to try again."
    return "This file could not be read. Check that it is a clear image or PDF and resend it."



@dataclass
class FileRun:
    """What every file of one reading job is read with."""
    job_id: uuid.UUID
    company_id: uuid.UUID
    user_id: uuid.UUID
    conversation_id: uuid.UUID | None
    query: str
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


async def read_file(run: FileRun, file_id: str, operation_id: uuid.UUID, *,
                    before_send: Callable[[int], Awaitable[None]]) -> FileOutcome:
    """Read one file under ``operation_id`` (the caller holds its gate slot). It is
    sent again only under that id: while its first send still runs, and when the
    service surely did not run it or no reply came back. An outcome the service
    cannot report, or an id it no longer accepts, ends the file without running it."""
    try:
        file = load_file_for_llm(file_id, run.company_id, run.user_id)
    except (FileNotFoundError, PermissionError):
        return FileOutcome("failed", {"filename": file_id, "error": t("error.file_gone")})
    filename = file["filename"]
    prompt = f"{run.query}\n\nRead the attached document." if run.query else "Read the attached document."
    messages = [{"role": "system", "content": _BATCH_SYSTEM_PROMPT},
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
    return FileOutcome("done", {"filename": filename, "answer": answer, "extraction": parse_extraction(answer)},
                       credits, known, sent)


async def create_batch_job(
    session: AsyncSession,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
    query: str,
    file_ids: list[str],
    conversation_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
) -> AIBatchJob:
    """Create a pending reading job and one step per file; the caller commits and
    then starts it (celerp.ai.jobs.start_job).

    ``job_id`` is the id of the question that asked for the job, when there is one."""
    request = jobs.JobRequest(company_id=company_id, user_id=user_id, kind="documents",
                              conversation_id=conversation_id, query=query, file_ids=tuple(file_ids))
    file_ids = list(dict.fromkeys(file_ids))
    if not file_ids:
        raise ValueError("Attach at least one file")
    if len(file_ids) > MAX_BATCH_FILES:
        raise ValueError(t("error.batch_too_many", max=MAX_BATCH_FILES))

    job = AIBatchJob(
        id=job_id or uuid.uuid4(),
        company_id=company_id,
        user_id=user_id,
        conversation_id=conversation_id,
        kind="documents",
        mode="background",
        query=query,
        file_ids=file_ids,
        status="pending",
        request_digest=request.digest(),
    )
    session.add(job)
    await session.flush()
    for seq, file_id in enumerate(file_ids):
        op = jobs.file_operation_id(job.id, file_id, 0)
        session.add(AIJobStep(job_id=job.id, seq=seq, key=file_id, operation_id=op, step_id=op))
    await session.flush()
    return job


async def publish_progress(run: FileRun, file_id: str, state: str) -> None:
    """Tell the user's open pages that one more file of the job has ended. Best effort:
    the file's result is already written, so a failure here never sends it again."""
    from celerp.notifications.sse import publish
    try:
        async with jobs.SessionLocal() as session:
            counts = dict((await session.execute(
                select(AIJobStep.state, func.count()).where(AIJobStep.job_id == run.job_id).group_by(AIJobStep.state)
            )).all())
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
    if completed == 0:
        title = f"Could not read {failed} file(s)"
        body = "None of the attached files could be read. Open the conversation for details."
    else:
        title = f"{completed} of {completed + failed} file(s) read"
        body = f"{completed} read, {failed} could not be read. Open the conversation to review the proposed entries."
    await jobs.notify(run.company_id, run.user_id, run.conversation_id, title, body)


async def get_batch_job(
    session: AsyncSession,
    job_id: uuid.UUID,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
) -> AIBatchJob | None:
    """Get a reading job by ID, scoped to company and owning user."""
    job = await session.get(AIBatchJob, job_id)
    if job is None or job.kind != "documents" or job.company_id != company_id or job.user_id != user_id:
        return None
    return job


async def list_conversation_jobs(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
) -> list[AIBatchJob]:
    """Every reading job started from a conversation, oldest first."""
    q = (
        select(AIBatchJob)
        .where(AIBatchJob.conversation_id == conversation_id, AIBatchJob.company_id == company_id,
               AIBatchJob.user_id == user_id, AIBatchJob.kind == "documents")
        .order_by(AIBatchJob.created_at.asc())
    )
    return list((await session.execute(q)).scalars().all())
