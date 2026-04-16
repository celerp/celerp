# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""AI router — /ai/*

All endpoints require:
  - User authentication (get_current_user)
  - Valid gateway session token (require_session_token) — Cloud+AI subscription gate

Endpoints:
  POST /ai/query            Run an AI query against ERP data
  POST /ai/upload           Upload files for AI processing
  GET  /ai/file/{file_id}   Retrieve an uploaded file
  POST /ai/estimate-credits Preview credit cost before submitting a query
  GET  /ai/memory           Get per-company AI memory
  DELETE /ai/memory         Clear per-company AI memory
  POST /ai/memory/notes     Append a note to AI memory
  POST /ai/memory/kv        Set a key-value fact in AI memory
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.ai import memory as ai_memory
from celerp.ai.batch import create_batch_job, get_batch_job, run_batch, MAX_BATCH_FILES
from celerp.ai.files import load_file, upload_dir
from celerp.ai.commands import DraftBill, create_bills, parse_bill_commands
from celerp.ai.conversations import (
    add_message,
    build_history_context,
    create_conversation,
    delete_conversation,
    get_conversation,
    get_messages,
    list_conversations,
    rename_conversation,
)
from celerp.ai.page_count import calculate_credits, credits_for_pages, count_pages
from celerp.ai.quota import check_ai_quota, get_quota_status, get_subscription_tier
from celerp.ai.service import AIResponse, run_query
from celerp.config import settings
from celerp.db import get_session
from celerp.services.auth import get_current_company_id, get_current_user
from celerp.session_gate import require_session_token

def _upgrade_url() -> str:
    from celerp.config import settings
    iid = settings.gateway_instance_id
    base = "https://celerp.com/subscribe"
    if iid:
        return f"{base}?instance_id={iid}#ai"
    return f"{base}#ai"
_CLOUD_FILE_LIMIT = 1

# AI-specific rate limiter: tighter than the global 60/min default.
# LLM queries are expensive; uploads have file-size costs.
_limiter = Limiter(key_func=get_remote_address)


router = APIRouter(
    dependencies=[Depends(get_current_user), Depends(require_session_token)],
)

# Settings endpoints (quota, usage) - user auth only, no session token required.
# These run in the API process which has the session token in-memory via the
# gateway client. The UI process does NOT have the session token, so gating
# these behind require_session_token breaks the separate-process architecture.
settings_router = APIRouter(
    dependencies=[Depends(get_current_user)],
)


# ── Request / Response schemas ────────────────────────────────────────────────

class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000, description="Natural language question")
    file_ids: list[str] | None = Field(default=None, description="List of file IDs returned from /ai/upload")


class QueryResponse(BaseModel):
    answer: str
    model_used: str
    tools_called: list[str]
    error: str | None = None
    pending_bills: list[dict] | None = None


class ConfirmBillsRequest(BaseModel):
    bills: list[dict] = Field(..., description="List of bill dicts from pending_bills")


class EstimateRequest(BaseModel):
    file_ids: list[str] = Field(..., description="List of file IDs to estimate credit cost for")


class FileEstimate(BaseModel):
    file_id: str
    filename: str
    pages: int
    credits: int


class EstimateResponse(BaseModel):
    total_credits: int
    files: list[FileEstimate]
    tier_limit: str


class MemoryResponse(BaseModel):
    notes: list[dict]
    kv: dict


class NoteRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=1000)


class KVRequest(BaseModel):
    key: str = Field(..., min_length=1, max_length=200)
    value: str = Field(..., min_length=0, max_length=1000)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_file_http(fid: str, company_id) -> tuple[bytes, dict]:
    """Wrap load_file with HTTP error mapping."""
    try:
        return load_file(fid, company_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"File {fid} not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"File {fid} not accessible")


def _calculate_query_credits(file_ids: list[str] | None, company_id) -> int:
    """Calculate credits required for a query.

    Pure text → 1 credit.
    With files → sum of per-file credits based on page count (0 base + N files).
    Files that cannot be read raise immediately (no silent fallbacks).
    """
    if not file_ids:
        return 1
    page_counts = []
    for fid in file_ids:
        data, meta = _load_file_http(fid, company_id)
        pages = count_pages(data, meta.get("content_type", "application/octet-stream"))
        page_counts.append(pages)
    return calculate_credits(page_counts)


async def _enforce_cloud_file_limit(file_ids: list[str] | None) -> None:
    """Raise 403 if user is on Cloud tier and submits more than 1 file."""
    if not file_ids or len(file_ids) <= _CLOUD_FILE_LIMIT:
        return
    tier = await get_subscription_tier()
    if tier == "cloud":
        raise HTTPException(
            status_code=403,
            detail=(
                f"Batch file processing requires the AI Plan. "
                f"You can upload {_CLOUD_FILE_LIMIT} file at a time on your current plan. "
                f"Upgrade at {_upgrade_url()}"
            ),
        )


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/query", response_model=QueryResponse)
@_limiter.limit("20/minute")
async def ai_query(
    request: Request,
    body: QueryRequest,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> QueryResponse:
    """Run an AI query against live ERP data.

    Model is selected automatically (Haiku for lookups, Sonnet for files/analysis).
    Credits consumed: 1 for pure text; N = sum of per-file page credits when files attached.
    Cloud tier users are limited to 1 file per query.
    """
    await _enforce_cloud_file_limit(body.file_ids)
    credits = _calculate_query_credits(body.file_ids, company_id)
    await check_ai_quota(credits=credits)
    result: AIResponse = await run_query(
        query=body.query,
        session=session,
        company_id=company_id,
        file_ids=body.file_ids,
        user_id=user.id,
    )
    if result.error:
        raise HTTPException(status_code=502, detail=result.error)
    return QueryResponse(
        answer=result.answer,
        model_used=result.model_used,
        tools_called=result.tools_called,
        pending_bills=result.pending_bills,
    )


@router.post("/confirm-bills")
@_limiter.limit("10/minute")
async def confirm_bills(
    request: Request,
    body: ConfirmBillsRequest,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Confirm and create draft bills proposed by the AI assistant."""
    try:
        bills = [DraftBill.model_validate(b) for b in body.bills]
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid bill data: {exc}")
    if not bills:
        raise HTTPException(status_code=400, detail="No bills to create")
    feedback = await create_bills(session, company_id, user.id, bills)
    await session.commit()
    return {"feedback": feedback, "count": len(bills)}


@router.post("/estimate-credits", response_model=EstimateResponse)
async def estimate_credits(
    body: EstimateRequest,
    company_id=Depends(get_current_company_id),
) -> EstimateResponse:
    """Preview the credit cost for a list of uploaded files before submitting a query.

    Returns per-file breakdown and total credits.
    Cloud tier users with >1 file receive a 403 with upsell message.
    """
    await _enforce_cloud_file_limit(body.file_ids)

    tier = await get_subscription_tier()
    tier_limit = f"{tier or 'unknown'} tier"

    file_estimates: list[FileEstimate] = []
    for fid in body.file_ids:
        data, meta = _load_file_http(fid, company_id)
        pages = count_pages(data, meta.get("content_type", "application/octet-stream"))
        file_estimates.append(FileEstimate(
            file_id=fid,
            filename=meta.get("filename", fid),
            pages=pages,
            credits=credits_for_pages(pages),
        ))

    total = calculate_credits([f.pages for f in file_estimates]) if file_estimates else 1
    return EstimateResponse(
        total_credits=total,
        files=file_estimates,
        tier_limit=tier_limit,
    )


@router.post("/upload", status_code=201)
@_limiter.limit("30/minute")
async def ai_upload(
    request: Request,
    files: list[UploadFile] = File(...),
    company_id=Depends(get_current_company_id),
) -> dict:
    """Upload files for AI batch processing. Returns list of file IDs."""
    if len(files) > 20:
        raise HTTPException(status_code=400, detail="Maximum 20 files allowed per batch")

    file_ids = []
    ud = upload_dir()
    for file in files:
        # Check size limit (10MB)
        file.file.seek(0, 2)
        size = file.file.tell()
        file.file.seek(0)
        if size > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail=f"File {file.filename} exceeds 10MB limit")

        file_id = f"ai_up_{uuid.uuid4().hex}"
        bin_path = ud / f"{file_id}.bin"
        meta_path = ud / f"{file_id}.meta"

        content_bytes = await file.read()
        bin_path.write_bytes(content_bytes)

        meta = {
            "filename": file.filename,
            "content_type": file.content_type,
            "size": size,
            "company_id": str(company_id),
        }
        meta_path.write_text(json.dumps(meta))
        file_ids.append(file_id)

    return {"file_ids": file_ids}


@router.get("/file/{file_id}")
async def ai_file(file_id: str, company_id=Depends(get_current_company_id)):
    """Retrieve a previously uploaded file."""
    data, meta = _load_file_http(file_id, company_id)
    bin_path = upload_dir() / f"{file_id}.bin"
    return FileResponse(bin_path, media_type=meta.get("content_type"))


@router.get("/memory", response_model=MemoryResponse)
async def get_ai_memory(
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> MemoryResponse:
    """Return the per-company AI memory (notes and key-value facts)."""
    mem = await ai_memory.get_memory(session, company_id)
    return MemoryResponse(
        notes=mem.get("notes", []),
        kv=mem.get("kv", {}),
    )


@router.delete("/memory", status_code=204)
async def clear_ai_memory(
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Wipe all AI memory for this company."""
    await ai_memory.clear_memory(session, company_id)
    await session.commit()


@router.post("/memory/notes", status_code=201)
async def add_ai_memory_note(
    body: NoteRequest,
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Append a note to AI memory (max 50 notes, oldest trimmed)."""
    await ai_memory.add_note(session, company_id, body.content)
    await session.commit()
    return {"ok": True}


@router.post("/memory/kv", status_code=201)
async def set_ai_memory_kv(
    body: KVRequest,
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Set a key-value fact in AI memory (max 100 keys)."""
    await ai_memory.set_kv(session, company_id, body.key, body.value)
    await session.commit()
    return {"ok": True}


# ── Quota status (settings_router - no session token required) ────────────────

@settings_router.get("/quota-status")
async def quota_status() -> dict:
    """Return current AI quota usage for the UI badge.

    Returns used/limit/topup/remaining/tier. Never raises - returns
    empty dict if gateway not configured (local install).
    """
    status = await get_quota_status()
    if not status:
        return {"local": True}
    used = status.get("used", 0)
    limit = status.get("limit", 0)
    topup = status.get("topup_credits", 0)
    remaining = max(0, (limit + topup) - used)
    return {
        "used": used,
        "limit": limit,
        "topup_credits": topup,
        "remaining": remaining,
        "resets_at": status.get("resets_at", ""),
        "tier": status.get("tier", ""),
        "instance_id": settings.gateway_instance_id or "",
    }


# ── Conversation schemas ──────────────────────────────────────────────────────

class CreateConversationRequest(BaseModel):
    title: str | None = None


class RenameConversationRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


class ConversationQueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    file_ids: list[str] | None = None


class MessageOut(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    model_used: str | None = None
    tools_called: list[str] | None = None
    file_ids: list[str] | None = None
    credits_used: int = 0
    created_at: str

    model_config = {"from_attributes": True}


class ConversationOut(BaseModel):
    id: uuid.UUID
    title: str | None
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]


# ── Conversation endpoints ────────────────────────────────────────────────────

@router.post("/conversations", status_code=201)
@_limiter.limit("60/minute")
async def create_conv(
    request: Request,
    body: CreateConversationRequest,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ConversationOut:
    """Create a new conversation."""
    conv = await create_conversation(session, company_id, user.id, title=body.title)
    await session.commit()
    await session.refresh(conv)
    return ConversationOut(
        id=conv.id, title=conv.title,
        created_at=conv.created_at.isoformat(), updated_at=conv.updated_at.isoformat(),
    )


@router.get("/conversations")
@_limiter.limit("60/minute")
async def list_convs(
    request: Request,
    limit: int = 20,
    offset: int = 0,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ConversationOut]:
    """List conversations, newest first."""
    convs = await list_conversations(session, company_id, user.id, limit=limit, offset=offset)
    return [
        ConversationOut(
            id=c.id, title=c.title,
            created_at=c.created_at.isoformat(), updated_at=c.updated_at.isoformat(),
        )
        for c in convs
    ]


@router.get("/conversations/{conversation_id}")
async def get_conv(
    conversation_id: uuid.UUID,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ConversationDetail:
    """Get a conversation with all messages."""
    conv = await get_conversation(session, conversation_id, company_id, user.id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    msgs = await get_messages(session, conversation_id)
    return ConversationDetail(
        id=conv.id, title=conv.title,
        created_at=conv.created_at.isoformat(), updated_at=conv.updated_at.isoformat(),
        messages=[
            MessageOut(
                id=m.id, role=m.role, content=m.content,
                model_used=m.model_used, tools_called=m.tools_called,
                file_ids=m.file_ids, credits_used=m.credits_used,
                created_at=m.created_at.isoformat(),
            )
            for m in msgs
        ],
    )


@router.delete("/conversations/{conversation_id}", status_code=204)
@_limiter.limit("60/minute")
async def delete_conv(
    request: Request,
    conversation_id: uuid.UUID,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a conversation and all its messages."""
    found = await delete_conversation(session, conversation_id, company_id, user.id)
    if not found:
        raise HTTPException(status_code=404, detail="Conversation not found")
    await session.commit()


@router.patch("/conversations/{conversation_id}")
@_limiter.limit("60/minute")
async def rename_conv(
    request: Request,
    conversation_id: uuid.UUID,
    body: RenameConversationRequest,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ConversationOut:
    """Rename a conversation."""
    conv = await rename_conversation(session, conversation_id, company_id, user.id, body.title)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    await session.commit()
    await session.refresh(conv)
    return ConversationOut(
        id=conv.id, title=conv.title,
        created_at=conv.created_at.isoformat(), updated_at=conv.updated_at.isoformat(),
    )


@router.post("/conversations/{conversation_id}/query", response_model=QueryResponse)
@_limiter.limit("20/minute")
async def query_in_conversation(
    request: Request,
    conversation_id: uuid.UUID,
    body: ConversationQueryRequest,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> QueryResponse:
    """Send a query within a conversation, with history context."""
    conv = await get_conversation(session, conversation_id, company_id, user.id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    await _enforce_cloud_file_limit(body.file_ids)
    credits = _calculate_query_credits(body.file_ids, company_id)
    await check_ai_quota(credits=credits)

    # Build history from prior messages
    prior_msgs = await get_messages(session, conversation_id)
    history = build_history_context(prior_msgs)

    # Store user message
    await add_message(
        session, conversation_id, "user", body.query,
        file_ids=body.file_ids, credits_used=credits,
    )

    result: AIResponse = await run_query(
        query=body.query,
        session=session,
        company_id=company_id,
        file_ids=body.file_ids,
        history=history,
        user_id=user.id,
    )

    if result.error:
        raise HTTPException(status_code=502, detail=result.error)

    # Store assistant response
    await add_message(
        session, conversation_id, "assistant", result.answer,
        model_used=result.model_used, tools_called=result.tools_called,
    )
    await session.commit()

    return QueryResponse(
        answer=result.answer,
        model_used=result.model_used,
        tools_called=result.tools_called,
        pending_bills=result.pending_bills,
    )


# ── Batch schemas ─────────────────────────────────────────────────────────────

class BatchRequest(BaseModel):
    query: str = Field("", max_length=2000, description="Optional text query to send with each file")
    file_ids: list[str] = Field(..., min_length=2, max_length=MAX_BATCH_FILES)


class BatchJobOut(BaseModel):
    id: uuid.UUID
    status: str
    total_files: int
    completed_files: int
    failed_files: int
    credits_consumed: int
    results: dict | None = None
    created_at: str
    completed_at: str | None = None

    model_config = {"from_attributes": True}


# ── Batch endpoints ───────────────────────────────────────────────────────────

@router.post("/batch", status_code=202)
@_limiter.limit("10/minute")
async def submit_batch(
    request: Request,
    body: BatchRequest,
    background_tasks: BackgroundTasks,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Submit a batch processing job (2-100 files). Returns batch_job_id.

    Credits consumed upfront. Processing runs in background.
    Poll GET /ai/batch/{id} for status.
    """
    await _enforce_cloud_file_limit(body.file_ids)
    credits = _calculate_query_credits(body.file_ids, company_id)
    await check_ai_quota(credits=credits)

    job = await create_batch_job(
        session, company_id, user.id, body.query, body.file_ids, credits,
    )
    await session.commit()
    await session.refresh(job)

    # Launch background processing
    from celerp.db import SessionLocal
    from celerp.notifications.sse import publish as sse_publish

    def _db_factory():
        return SessionLocal()

    async def _on_progress(job_id, completed, failed, total, result):
        await sse_publish(
            company_id, user.id,
            {
                "type": "batch_progress",
                "job_id": str(job_id),
                "completed": completed,
                "failed": failed,
                "total": total,
                "file_id": result.get("file_id"),
                "status": result.get("status"),
            },
        )

    async def _run():
        await run_batch(
            job.id, company_id, user.id, body.query, body.file_ids,
            _db_factory, on_progress=_on_progress,
        )

    background_tasks.add_task(_run)

    return {"batch_job_id": str(job.id)}


@settings_router.get("/usage-stats")
async def usage_stats(
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Per-user AI usage for the current calendar month.

    Returns list of {user_id, user_name, query_count, credits_used, last_query_at}.
    """
    from datetime import date
    from sqlalchemy import func, select
    from celerp.models.ai import AIConversation, AIMessage
    from celerp.models.company import User

    today = date.today()
    month_start = datetime(today.year, today.month, 1, tzinfo=timezone.utc)

    rows = (await session.execute(
        select(
            AIConversation.user_id,
            func.count(AIMessage.id).label("query_count"),
            func.sum(AIMessage.credits_used).label("credits_used"),
            func.max(AIMessage.created_at).label("last_query_at"),
        )
        .join(AIConversation, AIMessage.conversation_id == AIConversation.id)
        .where(
            AIConversation.company_id == company_id,
            AIMessage.role == "user",
            AIMessage.created_at >= month_start,
        )
        .group_by(AIConversation.user_id)
        .order_by(func.count(AIMessage.id).desc())
    )).all()

    user_ids = [r.user_id for r in rows]
    users = {}
    if user_ids:
        user_rows = (await session.execute(
            select(User.id, User.name).where(User.id.in_(user_ids))
        )).all()
        users = {u.id: u.name for u in user_rows}

    return {
        "users": [
            {
                "user_id": str(r.user_id),
                "user_name": users.get(r.user_id, str(r.user_id)),
                "query_count": r.query_count,
                "credits_used": r.credits_used or 0,
                "last_query_at": r.last_query_at.isoformat() if r.last_query_at else None,
            }
            for r in rows
        ]
    }


@router.get("/batch/{job_id}")
async def batch_status(
    job_id: uuid.UUID,
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> BatchJobOut:
    """Get batch job status and results."""
    job = await get_batch_job(session, job_id, company_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Batch job not found")
    return BatchJobOut(
        id=job.id,
        status=job.status,
        total_files=job.total_files,
        completed_files=job.completed_files,
        failed_files=job.failed_files,
        credits_consumed=job.credits_consumed,
        results=job.results,
        created_at=job.created_at.isoformat(),
        completed_at=job.completed_at.isoformat() if job.completed_at else None,
    )
