# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""AI batch processing - parallel multi-file processing via OpenRouter.

Processes 2-100 files in parallel (up to BATCH_CONCURRENCY concurrent LLM calls).
Each file gets its own API call with the bulk extraction model.

Usage:
    job = await start_batch(session, company_id, user_id, query, file_ids, credits)
    # Background task runs the batch
    # SSE events emitted per-file completion
    # Notification created on finish
"""

from __future__ import annotations

import asyncio
import base64
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from celerp.ai.files import load_file, load_file_for_llm
from celerp.ai.llm import call_llm
from celerp.ai.models import BULK_EXTRACTION
from celerp.models.ai import AIBatchJob

log = logging.getLogger(__name__)

BATCH_CONCURRENCY = 10
MAX_BATCH_FILES = 100

_BATCH_SYSTEM_PROMPT = """\
You are analyzing a business document (receipt, invoice, or contract).
Extract structured data: vendor name, date, total amount, line items with description/quantity/unit price.

Output a JSON block:
```json
{
  "vendor_name": "string",
  "date": "YYYY-MM-DD",
  "total": 0.00,
  "line_items": [
    {"description": "string", "quantity": 1, "unit_price": 0.00}
  ]
}
```

If you cannot extract structured data, return a brief text summary instead (no JSON block).
"""


async def _process_single_file(
    file_id: str,
    query: str,
    company_id: uuid.UUID,
    semaphore: asyncio.Semaphore,
) -> dict:
    """Process a single file through the LLM. Returns result dict."""
    try:
        data_bytes, meta = load_file(file_id, company_id)
    except (FileNotFoundError, PermissionError):
        return {"file_id": file_id, "status": "error", "error": f"File {file_id} not found"}

    b64 = base64.b64encode(data_bytes).decode("utf-8")
    files = [{"media_type": meta.get("content_type", "image/jpeg"), "data": b64}]

    prompt = f"{query}\n\nAnalyze the attached document." if query else "Analyze the attached document."

    async with semaphore:
        try:
            answer = await call_llm(BULK_EXTRACTION, _BATCH_SYSTEM_PROMPT, prompt, files=files)
            return {
                "file_id": file_id,
                "filename": meta.get("filename", file_id),
                "status": "success",
                "answer": answer,
            }
        except Exception as exc:
            log.warning("Batch file %s failed: %s", file_id, exc)
            return {
                "file_id": file_id,
                "filename": meta.get("filename", file_id),
                "status": "error",
                "error": "Processing failed for this file",
            }


async def create_batch_job(
    session: AsyncSession,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
    query: str,
    file_ids: list[str],
    credits: int,
    conversation_id: uuid.UUID | None = None,
) -> AIBatchJob:
    """Create a pending batch job record."""
    if len(file_ids) > MAX_BATCH_FILES:
        raise ValueError(f"Maximum {MAX_BATCH_FILES} files per batch")
    if len(file_ids) < 2:
        raise ValueError("Batch requires at least 2 files")

    job = AIBatchJob(
        company_id=company_id,
        user_id=user_id,
        conversation_id=conversation_id,
        query=query,
        file_ids=file_ids,
        total_files=len(file_ids),
        credits_consumed=credits,
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
    """Execute a batch job: parallel file processing.

    Args:
        db_factory: Callable that returns an AsyncSession context manager.
        on_progress: Optional async callback(job_id, completed, failed, total, result).
    """
    semaphore = asyncio.Semaphore(BATCH_CONCURRENCY)
    results: list[dict] = []
    completed = 0
    failed = 0

    # Update status to running
    async with db_factory() as session:
        job = await session.get(AIBatchJob, job_id)
        if job:
            job.status = "running"
            session.add(job)
            await session.commit()

    # Fan out: process all files concurrently (bounded by semaphore)
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

        # Update DB progress
        async with db_factory() as session:
            job = await session.get(AIBatchJob, job_id)
            if job:
                job.completed_files = completed
                job.failed_files = failed
                session.add(job)
                await session.commit()

        # Notify progress
        if on_progress:
            try:
                await on_progress(job_id, completed, failed, len(file_ids), result)
            except Exception:
                log.debug("on_progress callback failed", exc_info=True)

    # Finalize
    final_status = "failed" if failed == len(file_ids) else "completed"
    async with db_factory() as session:
        job = await session.get(AIBatchJob, job_id)
        if job:
            job.status = final_status
            job.results = {"files": results}
            job.completed_at = datetime.now(timezone.utc)
            session.add(job)
            await session.commit()

    # Create notification
    try:
        from celerp.notifications.service import create as create_notification
        async with db_factory() as session:
            if final_status == "completed":
                await create_notification(
                    session, company_id, "ai",
                    f"Batch complete: {completed}/{len(file_ids)} files processed",
                    f"{completed} files processed successfully, {failed} failed.",
                    user_id=user_id,
                    action_url="/ai",
                    priority="high",
                )
            else:
                await create_notification(
                    session, company_id, "ai",
                    f"Batch failed: {failed}/{len(file_ids)} files",
                    "All files failed to process. Please try again.",
                    user_id=user_id,
                    action_url="/ai",
                    priority="high",
                )
            await session.commit()
    except Exception:
        log.warning("Failed to create batch notification", exc_info=True)


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
