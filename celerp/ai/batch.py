# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""AI batch processing - the extraction stage for attached receipts and invoices.

A batch job reads 1 to MAX_BATCH_FILES files in parallel (at most
BATCH_CONCURRENCY model calls at once). Each file gets its own model call that
returns a structured extraction. Nothing is written to the books here: the
extractions feed the proposal step, and every document is created only after
the user confirms it.

Credits are metered per file from the gateway's usage report, so a job that
fails halfway charges only for the files it actually read.

Usage:
    job = await create_batch_job(session, company_id, user_id, query, file_ids)
    # run_batch runs in the background, reports per-file progress through
    # on_progress, and creates a notification when it finishes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.ai.files import load_file_for_llm
from celerp.ai.llm import RelayError, call_llm
from celerp.ai.models import BULK_EXTRACTION
from celerp.models.ai import AIBatchJob

log = logging.getLogger(__name__)

BATCH_CONCURRENCY = 10
MAX_BATCH_FILES = 100

INTERRUPTED_ERROR = "This job was interrupted by a restart. Attach the files again and resend."

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


async def _process_single_file(
    file_id: str,
    query: str,
    company_id: uuid.UUID,
    semaphore: asyncio.Semaphore,
) -> dict:
    """Read one file through the model. Never raises; the result dict carries the outcome."""
    try:
        file = load_file_for_llm(file_id, company_id)
    except (FileNotFoundError, PermissionError):
        return {
            "file_id": file_id,
            "filename": file_id,
            "status": "error",
            "error": "This file is no longer available. Attach it again and resend.",
        }

    filename = file["filename"]
    files = [file]
    prompt = f"{query}\n\nRead the attached document." if query else "Read the attached document."

    async with semaphore:
        try:
            result = await call_llm(BULK_EXTRACTION, _BATCH_SYSTEM_PROMPT, prompt, files=files)
        except Exception as exc:
            log.warning("Batch file %s failed: %s", file_id, exc)
            return {
                "file_id": file_id,
                "filename": filename,
                "status": "error",
                "error": _file_error(exc),
            }

    answer = result.message.get("content") or ""
    return {
        "file_id": file_id,
        "filename": filename,
        "status": "success",
        "answer": answer,
        "extraction": parse_extraction(answer),
        "credits": int(result.usage.get("credits") or 0),
    }


async def create_batch_job(
    session: AsyncSession,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
    query: str,
    file_ids: list[str],
    conversation_id: uuid.UUID | None = None,
) -> AIBatchJob:
    """Create a pending batch job record. Credits accrue per file as it runs."""
    if not file_ids:
        raise ValueError("Attach at least one file")
    if len(file_ids) > MAX_BATCH_FILES:
        raise ValueError(f"Maximum {MAX_BATCH_FILES} files per batch")

    job = AIBatchJob(
        company_id=company_id,
        user_id=user_id,
        conversation_id=conversation_id,
        query=query,
        file_ids=file_ids,
        total_files=len(file_ids),
        credits_consumed=0,
        status="pending",
    )
    session.add(job)
    await session.flush()
    return job


async def run_batch(
    job_id: uuid.UUID,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
    query: str,
    file_ids: list[str],
    db_factory,
    on_progress=None,
) -> None:
    """Execute a batch job: parallel file extraction with per-file progress.

    Args:
        db_factory: Callable that returns an AsyncSession context manager.
        on_progress: Optional async callback(job_id, completed, failed, total, result).
    """
    semaphore = asyncio.Semaphore(BATCH_CONCURRENCY)
    results: list[dict] = []
    completed = 0
    failed = 0
    credits = 0

    async with db_factory() as session:
        job = await session.get(AIBatchJob, job_id)
        if job:
            job.status = "running"
            session.add(job)
            await session.commit()

    tasks = [
        _process_single_file(fid, query, company_id, semaphore)
        for fid in file_ids
    ]

    for coro in asyncio.as_completed(tasks):
        result = await coro
        results.append(result)

        if result["status"] == "success":
            completed += 1
        else:
            failed += 1
        credits += result.get("credits", 0)

        async with db_factory() as session:
            job = await session.get(AIBatchJob, job_id)
            if job:
                job.completed_files = completed
                job.failed_files = failed
                job.credits_consumed = credits
                session.add(job)
                await session.commit()

        if on_progress:
            try:
                await on_progress(job_id, completed, failed, len(file_ids), result)
            except Exception:
                log.debug("on_progress callback failed", exc_info=True)

    all_failed = failed == len(file_ids)
    final_status = "failed" if all_failed else "completed"
    async with db_factory() as session:
        job = await session.get(AIBatchJob, job_id)
        if job:
            job.status = final_status
            job.results = {"files": results}
            job.credits_consumed = credits
            job.completed_at = datetime.now(timezone.utc)
            if all_failed:
                job.error = "None of the files could be read."
            session.add(job)
            await session.commit()

    try:
        from celerp.notifications.service import create as create_notification
        async with db_factory() as session:
            if all_failed:
                title = f"Could not read {failed} file(s)"
                body = "None of the attached files could be read. Open the conversation for details."
            else:
                title = f"{completed} of {len(file_ids)} file(s) read"
                body = (
                    f"{completed} read, {failed} could not be read. "
                    "Open the conversation to review the proposed entries."
                )
            await create_notification(
                session, company_id, "ai", title, body,
                user_id=user_id,
                action_url="/ai",
                priority="high",
            )
            await session.commit()
    except Exception:
        log.warning("Failed to create batch notification", exc_info=True)


async def fail_interrupted_jobs(session: AsyncSession) -> int:
    """Mark every pending or running job as failed. Returns how many were marked.

    Runs at process start: a job can only run inside the process that started
    it, so anything still pending or running at boot was lost to the restart.
    """
    rows = (await session.execute(
        select(AIBatchJob).where(AIBatchJob.status.in_(("pending", "running")))
    )).scalars().all()
    now = datetime.now(timezone.utc)
    for job in rows:
        job.status = "failed"
        job.error = INTERRUPTED_ERROR
        job.completed_at = now
        session.add(job)
    return len(rows)


async def get_batch_job(
    session: AsyncSession,
    job_id: uuid.UUID,
    company_id: uuid.UUID,
) -> AIBatchJob | None:
    """Get a batch job by ID, scoped to company."""
    job = await session.get(AIBatchJob, job_id)
    if job is None or job.company_id != company_id:
        return None
    return job
