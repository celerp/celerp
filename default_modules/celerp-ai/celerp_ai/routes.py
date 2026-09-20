# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""AI router: /ai/* and /settings/ai/*

Every route requires an authenticated user, a valid gateway session token
(the Connect subscription gate) and the ``use_ai_assistant`` permission.

Endpoints:
  POST   /ai/query                                   One-off question, no conversation
  POST   /ai/upload                                  Upload files for the assistant
  GET    /ai/file/{file_id}                          Retrieve an uploaded file
  POST   /ai/estimate-credits                        Credit cost of uploaded files
  GET    /ai/memory                                  Per-company assistant memory
  DELETE /ai/memory                                  Clear assistant memory
  POST   /ai/memory/notes                            Append a memory note
  POST   /ai/memory/kv                               Set a memory fact
  GET    /ai/quota                                   Credit balance and reset date
  POST   /ai/conversations                           Start a conversation
  GET    /ai/conversations                           List conversations
  GET    /ai/conversations/{id}                      Thread with messages and jobs
  PATCH  /ai/conversations/{id}                      Rename
  DELETE /ai/conversations/{id}                      Delete
  POST   /ai/conversations/{id}/query                Ask, or hand receipts to a job
  POST   /ai/conversations/{id}/confirm              Run one proposed change
  POST   /ai/conversations/{id}/confirm-all          Run every proposed change on a message
  POST   /ai/conversations/{id}/jobs/{job}/proposals Turn read receipts into bill proposals
  GET    /ai/batch/{job_id}                          Job status and per-file results
  GET    /settings/ai/usage-stats                    Per-user usage this month
"""

from __future__ import annotations

import json
import secrets
import uuid
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, model_validator
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.ai import memory as ai_memory
from celerp.ai.batch import create_batch_job, get_batch_job, list_conversation_jobs, run_batch
from celerp.ai.files import AGENT_UPLOAD_TYPES, XLSX_CONTENT_TYPE, load_file, upload_dir
from celerp.ai.conversations import (
    ERROR_MARKER,
    add_message,
    build_history_context,
    claim_tool_call,
    create_conversation,
    delete_conversation,
    finalize_tool_call,
    get_conversation,
    get_message,
    get_messages,
    list_conversations,
    message_error,
    pending_action_counts,
    pending_actions,
    record_credits,
    rename_conversation,
    tool_names,
)
from celerp.ai.memory import get_memory
from celerp.ai.page_count import calculate_credits, credits_for_pages, count_pages
from celerp.ai.quota import get_quota_status
from celerp.ai.service import PROPOSAL_TTL_S, AIResponse, AgentResult, run_agent, run_query
from celerp.ai.tools import compile_agent_capabilities, execute_agent_capability
from celerp.config import settings
from celerp.db import get_session
from celerp.models.ai import AIBatchJob
from celerp.services.auth import get_current_company_id, get_current_user
from celerp.services.permissions import get_current_company_settings, require_permission
from celerp.session_gate import require_session_token

# AI-specific rate limiter: tighter than the global 60/min default.
# LLM queries are expensive; uploads have file-size costs.
_limiter = Limiter(key_func=get_remote_address)

router = APIRouter(
    dependencies=[Depends(get_current_user), Depends(require_session_token), require_permission("use_ai_assistant")],
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
    pending_actions: list[dict] = []
    error: str | None = None


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


_TABLE_TYPES = frozenset({"text/csv", XLSX_CONTENT_TYPE})


def _attachment_kinds(file_ids: list[str] | None, company_id) -> set[str]:
    """Classify attachments as ``document`` (images, PDFs) or ``table`` (CSV, XLSX).

    Every id is loaded first, so a missing or foreign file fails the request
    before anything is stored.
    """
    kinds: set[str] = set()
    for fid in file_ids or []:
        _, meta = _load_file_http(fid, company_id)
        kinds.add("table" if meta.get("content_type") in _TABLE_TYPES else "document")
    return kinds


def _conflict(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": message})


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
    """Run a one-off AI query against live ERP data, outside any conversation."""
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
        pending_actions=[],
    )


@router.post("/estimate-credits", response_model=EstimateResponse)
async def estimate_credits(
    body: EstimateRequest,
    company_id=Depends(get_current_company_id),
) -> EstimateResponse:
    """Preview the credit cost for a list of uploaded files before submitting a query."""
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
    return EstimateResponse(total_credits=total, files=file_estimates)


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
        if file.content_type not in AGENT_UPLOAD_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"File {file.filename} has an unsupported type: {file.content_type}",
            )

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
    used = int(status.get("used", 0) or 0)
    legacy_limit = int(status.get("limit", 0) or 0)
    legacy_topup = int(status.get("topup_credits", 0) or 0)
    remaining_raw = status.get("remaining")
    remaining = (
        int(remaining_raw or 0)
        if remaining_raw is not None
        else max(0, legacy_limit + legacy_topup - used)
    )
    return {
        "allowed": bool(status.get("allowed", remaining > 0)),
        "used": used,
        "base_limit": int(status.get("base_limit", legacy_limit) or 0),
        "topup_balance": int(status.get("topup_balance", legacy_topup) or 0),
        "remaining": remaining,
        "resets_at": status.get("resets_at", ""),
        "tier": status.get("tier", ""),
        # Rolling-deploy compatibility for an older UI process.
        "limit": legacy_limit,
        "topup_credits": legacy_topup,
        "instance_id": settings.gateway_instance_id or "",
    }


# ── Conversation schemas ──────────────────────────────────────────────────────

class CreateConversationRequest(BaseModel):
    title: str | None = None


class RenameConversationRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


class ConversationQueryRequest(BaseModel):
    query: str = Field("", max_length=2000)
    file_ids: list[str] | None = None

    @model_validator(mode="after")
    def _require_query_or_files(self) -> "ConversationQueryRequest":
        if not self.query.strip() and not self.file_ids:
            raise ValueError("A question or at least one file is required.")
        return self


class ConfirmActionRequest(BaseModel):
    message_id: uuid.UUID
    tool_call_id: str = Field(..., min_length=1, max_length=128)


class ConfirmAllRequest(BaseModel):
    message_id: uuid.UUID
    tool_call_ids: list[str] | None = Field(default=None, max_length=200)


class MessageOut(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    model_used: str | None = None
    tools_called: list[str] | None = None
    pending_actions: list[dict] = []
    file_ids: list[str] | None = None
    credits_used: int = 0
    error: bool = False
    created_at: str

    model_config = {"from_attributes": True}


class BatchJobOut(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID | None = None
    status: str
    total_files: int
    completed_files: int
    failed_files: int
    credits_consumed: int
    results: dict | None = None
    error: str | None = None
    proposal_message_id: str | None = None
    created_at: str
    completed_at: str | None = None

    model_config = {"from_attributes": True}


class ConversationOut(BaseModel):
    id: uuid.UUID
    title: str | None
    created_at: str
    updated_at: str
    pending_count: int = 0

    model_config = {"from_attributes": True}


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]
    jobs: list[BatchJobOut] = []


def _message_out(m) -> MessageOut:
    return MessageOut(
        id=m.id, role=m.role, content=m.content,
        model_used=m.model_used, tools_called=tool_names(m.tools_called),
        pending_actions=[{**r, "message_id": str(m.id)} for r in pending_actions(m.tools_called)],
        file_ids=m.file_ids, credits_used=m.credits_used,
        error=message_error(m.tools_called),
        created_at=m.created_at.isoformat(),
    )


def _job_out(job: AIBatchJob) -> BatchJobOut:
    return BatchJobOut(
        id=job.id,
        conversation_id=job.conversation_id,
        status=job.status,
        total_files=job.total_files,
        completed_files=job.completed_files,
        failed_files=job.failed_files,
        credits_consumed=job.credits_consumed,
        results=job.results,
        error=job.error,
        proposal_message_id=(job.results or {}).get("proposal_message_id"),
        created_at=job.created_at.isoformat(),
        completed_at=job.completed_at.isoformat() if job.completed_at else None,
    )


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
    """List conversations, newest first, each with its count of open proposals."""
    convs = await list_conversations(session, company_id, user.id, limit=limit, offset=offset)
    open_counts = await pending_action_counts(session, [c.id for c in convs])
    return [
        ConversationOut(
            id=c.id, title=c.title,
            created_at=c.created_at.isoformat(), updated_at=c.updated_at.isoformat(),
            pending_count=open_counts.get(c.id, 0),
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
    """Get a conversation with its messages and the reading jobs started from it."""
    conv = await get_conversation(session, conversation_id, company_id, user.id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    msgs = await get_messages(session, conversation_id)
    jobs = await list_conversation_jobs(session, conversation_id, company_id)
    return ConversationDetail(
        id=conv.id, title=conv.title,
        created_at=conv.created_at.isoformat(), updated_at=conv.updated_at.isoformat(),
        messages=[_message_out(m) for m in msgs],
        jobs=[_job_out(j) for j in jobs],
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


def _launch_batch(
    background_tasks: BackgroundTasks, job: AIBatchJob, company_id, user_id, query: str, file_ids: list[str],
) -> None:
    """Run the job after the response is sent; progress rides the notification stream."""
    from celerp.db import SessionLocal
    from celerp.notifications.sse import publish as sse_publish

    async def _on_progress(job_id, completed, failed, total, result):
        await sse_publish(
            company_id, user_id,
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
        await run_batch(job.id, company_id, user_id, query, file_ids, SessionLocal, on_progress=_on_progress)

    background_tasks.add_task(_run)


@router.post("/conversations/{conversation_id}/query")
@_limiter.limit("20/minute")
async def query_in_conversation(
    request: Request,
    conversation_id: uuid.UUID,
    body: ConversationQueryRequest,
    background_tasks: BackgroundTasks,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    company_settings: dict = Depends(get_current_company_settings),
    session: AsyncSession = Depends(get_session),
):
    """Ask the assistant within a conversation.

    Images and PDFs are receipts or invoices: they go to a reading job and the
    reply is 202 with the job id; the thread shows progress from the
    notification stream and offers bill proposals when the job finishes.
    Text, CSV and XLSX run the agent inline: reads execute against the app the
    user sees and changes come back as pending actions to confirm. A failed run
    is stored as an assistant message so the thread keeps its history.
    """
    conv = await get_conversation(session, conversation_id, company_id, user.id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    kinds = _attachment_kinds(body.file_ids, company_id)
    if kinds == {"document", "table"}:
        raise HTTPException(
            status_code=400,
            detail="Attach receipts and statements in separate messages.",
        )

    if "document" in kinds:
        user_msg = await add_message(
            session, conversation_id, "user", body.query, file_ids=body.file_ids,
        )
        try:
            job = await create_batch_job(
                session, company_id, user.id, body.query, body.file_ids or [],
                conversation_id=conversation_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        await session.commit()
        await session.refresh(job)
        _launch_batch(background_tasks, job, company_id, user.id, body.query, body.file_ids or [])
        return JSONResponse(
            status_code=202,
            content={"job_id": str(job.id), "message_id": str(user_msg.id)},
        )

    # Assemble the read-only context, then commit so the session is quiescent
    # for the duration of the agent loop (which reaches the app over its own
    # request sessions).
    prior_msgs = await get_messages(session, conversation_id)
    history = build_history_context(prior_msgs)
    memory = await get_memory(session, company_id)
    user_msg = await add_message(session, conversation_id, "user", body.query, file_ids=body.file_ids)
    await session.commit()

    result: AgentResult = await run_agent(
        app=request.app,
        authorization=request.headers.get("authorization", ""),
        query=body.query,
        company_id=company_id,
        company_settings=company_settings,
        user_id=user.id,
        memory=memory,
        file_ids=body.file_ids,
        history=history,
    )

    await record_credits(session, user_msg.id, result.credits)
    if result.error:
        msg = await add_message(
            session, conversation_id, "assistant", result.error,
            model_used=result.model_used, tools_called=[*result.tools_called, ERROR_MARKER],
        )
        await session.commit()
        return QueryResponse(
            answer="", model_used=result.model_used,
            tools_called=tool_names(msg.tools_called), error=result.error,
        )

    msg = await add_message(
        session, conversation_id, "assistant", result.answer,
        model_used=result.model_used,
        tools_called=[*result.tools_called, *[asdict(p) for p in result.pending_actions]],
    )
    await session.commit()

    return QueryResponse(
        answer=result.answer,
        model_used=result.model_used,
        tools_called=tool_names(msg.tools_called),
        pending_actions=[{**asdict(p), "message_id": str(msg.id)} for p in result.pending_actions],
    )


async def _run_confirmed_action(
    request: Request,
    session: AsyncSession,
    capabilities: dict,
    *,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    tool_call_id: str,
    company_id,
    user_id,
) -> dict:
    """Claim, execute and finalize one pending action; never raises for action state.

    The action is claimed under a row lock and committed before execution, so a
    crash mid-flight leaves it in ``executing`` and never runs twice (fail
    closed). The capability re-enters the app with the user's bearer token, so
    the target route enforces every module permission.
    """
    record = await claim_tool_call(
        session,
        conversation_id=conversation_id,
        message_id=message_id,
        tool_call_id=tool_call_id,
        company_id=company_id,
        user_id=user_id,
    )
    if record is None:
        return {
            "tool_call_id": tool_call_id, "ok": False, "status": 409, "data": None,
            "error": {"code": "action_not_pending", "message": "This action is no longer pending."},
        }
    await session.commit()

    capability = capabilities.get(record["name"])
    if capability is None:
        await finalize_tool_call(
            session, message_id=message_id, tool_call_id=tool_call_id,
            status="failed", result=None,
        )
        await session.commit()
        return {
            "tool_call_id": tool_call_id, "ok": False, "status": 409, "data": None,
            "error": {
                "code": "capability_unavailable",
                "message": "The module this action needs is not enabled.",
            },
        }

    result = await execute_agent_capability(
        request.app,
        request.headers.get("authorization", ""),
        capability,
        record["arguments"],
        record["id"],
    )
    await finalize_tool_call(
        session, message_id=message_id, tool_call_id=tool_call_id,
        status="completed" if result["ok"] else "failed", result=result,
    )
    await session.commit()
    return {
        "tool_call_id": tool_call_id,
        "name": record["name"],
        "title": record.get("title") or record["name"],
        "ok": result["ok"],
        "status": result["status"],
        "data": result.get("data"),
        "error": _action_error(result),
    }


def _action_error(result: dict) -> dict | None:
    """Why a confirmed action failed, as {code, message}: the executor's own
    error, or the rejecting route's detail, so the reply always says the reason."""
    if result.get("ok"):
        return None
    if result.get("error"):
        return result["error"]
    data = result.get("data")
    detail = data.get("detail") if isinstance(data, dict) else None
    if isinstance(detail, dict):
        return {"code": detail.get("code") or "route_error",
                "message": detail.get("message") or detail.get("code") or str(detail)}
    if isinstance(detail, list):
        detail = "; ".join(str(e.get("msg") if isinstance(e, dict) else e) for e in detail)
    return {"code": "route_error",
            "message": str(detail) if detail else f"The request failed with status {result.get('status')}."}


_ACTION_STATE_CODES = frozenset({"action_not_pending", "capability_unavailable"})


@router.post("/conversations/{conversation_id}/confirm")
@_limiter.limit("60/minute")
async def confirm_action(
    request: Request,
    conversation_id: uuid.UUID,
    body: ConfirmActionRequest,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    company_settings: dict = Depends(get_current_company_settings),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Execute one pending action the user has confirmed.

    No model turn resumes after a write; the user asks the next question.
    An action that is no longer pending, or whose module is not enabled, is 409.
    """
    outcome = await _run_confirmed_action(
        request, session, compile_agent_capabilities(request.app, company_settings),
        conversation_id=conversation_id, message_id=body.message_id,
        tool_call_id=body.tool_call_id, company_id=company_id, user_id=user.id,
    )
    error = outcome.get("error") or {}
    if error.get("code") in _ACTION_STATE_CODES:
        raise HTTPException(status_code=409, detail=error)
    return outcome


def _selected_action_ids(records: list[dict], selection: list[str] | None) -> list[str]:
    """The action ids one confirm-all call runs, in proposal order.

    Without a selection every pending action runs. With one, the pending
    actions it names run first in proposal order, then any selected id that is
    not pending, so its row reports ``action_not_pending`` instead of vanishing.
    An empty result means nothing selected is pending.
    """
    pending = [r["id"] for r in records if r.get("status", "pending") == "pending"]
    if selection is None:
        return pending
    wanted = list(dict.fromkeys(selection))
    chosen = [i for i in pending if i in wanted]
    if not chosen:
        return []
    return chosen + [i for i in wanted if i not in pending]


@router.post("/conversations/{conversation_id}/confirm-all")
@_limiter.limit("5/minute")
async def confirm_all(
    request: Request,
    conversation_id: uuid.UUID,
    body: ConfirmAllRequest,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    company_settings: dict = Depends(get_current_company_settings),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Execute the pending actions on one message, in the order they were proposed.

    ``tool_call_ids`` narrows the run to a selection; without it every pending
    action runs. Each action is claimed and finalized on its own, so one
    failure never rolls back the others; the reply lists the outcome per action.
    """
    conv = await get_conversation(session, conversation_id, company_id, user.id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    msg = await get_message(session, body.message_id, conversation_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")
    ids = _selected_action_ids(pending_actions(msg.tools_called), body.tool_call_ids)
    if not ids:
        raise _conflict("action_not_pending", "Nothing is pending on this message.")

    capabilities = compile_agent_capabilities(request.app, company_settings)
    results = []
    for tool_call_id in ids:
        results.append(await _run_confirmed_action(
            request, session, capabilities,
            conversation_id=conversation_id, message_id=body.message_id,
            tool_call_id=tool_call_id, company_id=company_id, user_id=user.id,
        ))
    completed = sum(1 for r in results if r["ok"])
    return {"results": results, "completed": completed, "failed": len(results) - completed}


# ── Bill proposals from a reading job ─────────────────────────────────────────
#
# Every lookup and every proposed change goes through the compiled capabilities,
# the same routes the agent and the confirm button use, so module permissions
# and idempotency apply exactly as they do for a typed question.

_BILL_KINDS = frozenset({"receipt", "invoice"})
_CONTACTS_LIST = "list_contacts_crm_contacts_get"
_CONTACTS_CREATE = "create_contact_crm_contacts_post"
_ITEMS_LIST = "list_items_items_get"
_DOCS_CREATE = "create_doc_docs_post"


def _num(value) -> float | None:
    """A number from an extracted value, or None when it is not numeric."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def _iso_date(value) -> str | None:
    """An ISO calendar date from an extracted value, or None."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10]).isoformat()
    except ValueError:
        return None


def _same_text(a, b: str) -> bool:
    return str(a or "").strip().casefold() == b.strip().casefold()


def _proposal_record(name: str, arguments: dict, *, title: str, warnings: list[str],
                     file_id: str, now: datetime) -> dict:
    return {
        "id": f"prop_{secrets.token_hex(12)}",
        "name": name,
        "arguments": arguments,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=PROPOSAL_TTL_S)).isoformat(),
        "title": title,
        "warnings": warnings,
        "file_id": file_id,
    }


class _Lookups:
    """Read-only capability calls used while building proposals."""

    def __init__(self, app, authorization: str, capabilities: dict) -> None:
        self._app = app
        self._authorization = authorization
        self._capabilities = capabilities

    def has(self, name: str) -> bool:
        return name in self._capabilities

    async def rows(self, name: str, query: dict) -> list[dict] | str:
        """Result rows of a list capability, or the error message when it failed."""
        result = await execute_agent_capability(
            self._app, self._authorization, self._capabilities[name],
            {"query": query}, f"lookup_{secrets.token_hex(8)}",
        )
        if not result["ok"]:
            return str((result.get("error") or {}).get("message") or "lookup failed")
        data = result.get("data") or {}
        return [r for r in (data.get("items") or []) if isinstance(r, dict)]

    async def contact_id(self, vendor: str, warnings: list[str]) -> str | None | bool:
        """The single contact matching ``vendor`` exactly.

        Returns the id on one match, None when several match or the lookup is
        unavailable, and False when the contact does not exist yet.
        """
        if not self.has(_CONTACTS_LIST):
            return None
        rows = await self.rows(_CONTACTS_LIST, {"q": vendor, "limit": 20})
        if isinstance(rows, str):
            warnings.append(f"Contact lookup failed: {rows}")
            return None
        matches = [r for r in rows if _same_text(r.get("name"), vendor)]
        if len(matches) == 1:
            return str(matches[0].get("id"))
        if matches:
            warnings.append(f"{len(matches)} contacts are named {vendor}; pick one on the bill.")
            return None
        return False

    async def item_id(self, description: str) -> str | None:
        """The item whose SKU or name equals ``description``, if any."""
        if not self.has(_ITEMS_LIST):
            return None
        for query in ({"sku": description}, {"q": description, "limit": 20}):
            rows = await self.rows(_ITEMS_LIST, query)
            if isinstance(rows, str):
                return None
            for row in rows:
                if _same_text(row.get("sku"), description) or _same_text(row.get("name"), description):
                    return str(row.get("id"))
        return None


async def _bill_proposals(lookups: _Lookups, entry: dict, extraction: dict, now: datetime) -> tuple[list[dict], str]:
    """Proposal records for one read receipt plus a one-line summary."""
    file_id = str(entry.get("file_id"))
    filename = entry.get("filename") or file_id
    warnings: list[str] = []
    records: list[dict] = []

    vendor = str(extraction.get("vendor_name") or "").strip()
    contact_id = None
    if vendor:
        found = await lookups.contact_id(vendor, warnings)
        if found is False:
            if lookups.has(_CONTACTS_CREATE):
                records.append(_proposal_record(
                    _CONTACTS_CREATE, {"body": {"name": vendor, "contact_type": "vendor"}},
                    title=f"Add vendor {vendor}", warnings=[], file_id=file_id, now=now,
                ))
            else:
                warnings.append(f"No contact is named {vendor}; the bill carries the name only.")
        elif found:
            contact_id = found
    else:
        warnings.append("No vendor name was found on the receipt.")

    line_items: list[dict] = []
    subtotal = 0.0
    for raw in extraction.get("line_items") or []:
        if not isinstance(raw, dict):
            continue
        description = str(raw.get("description") or "").strip()
        quantity = _num(raw.get("quantity"))
        unit_price = _num(raw.get("unit_price"))
        if not description or quantity is None or unit_price is None:
            warnings.append(f"A line could not be read in full: {raw}.")
            continue
        line = {
            "name": description, "description": description,
            "quantity": quantity, "unit_price": unit_price,
            "line_total": round(quantity * unit_price, 2),
        }
        item_id = await lookups.item_id(description)
        if item_id:
            line["item_id"] = item_id
        line_items.append(line)
        subtotal += quantity * unit_price
    subtotal = round(subtotal, 2)
    if not line_items:
        warnings.append("No line items were read; the bill has the total only.")

    tax = _num(extraction.get("tax")) or 0.0
    total = _num(extraction.get("total"))
    if total is None:
        total = round(subtotal + tax, 2)
        warnings.append("No total was found on the receipt; the lines and tax were added up.")
    elif line_items and abs(subtotal + tax - total) > 0.01:
        warnings.append(
            f"The lines and tax add up to {subtotal + tax:.2f} but the receipt total is {total:.2f}."
        )
    if not line_items and total:
        subtotal = round(total - tax, 2)

    issue_date = _iso_date(extraction.get("date"))
    if issue_date is None:
        warnings.append("No date was found; the bill is dated today.")

    reference = str(extraction.get("reference") or "").strip()
    notes = f"Receipt file: {file_id} ({filename})"
    if reference:
        notes = f"Vendor reference: {reference}. {notes}"

    body: dict = {
        "doc_type": "bill",
        "contact_name": vendor or None,
        "contact_id": contact_id,
        "issue_date": issue_date,
        "line_items": line_items,
        "subtotal": subtotal,
        "tax": tax,
        "total": total,
        "notes": notes,
    }
    currency = str(extraction.get("currency") or "").strip().upper()
    if len(currency) == 3 and currency.isalpha():
        body["currency"] = currency
    body = {k: v for k, v in body.items() if v is not None}

    label = vendor or filename
    records.append(_proposal_record(
        _DOCS_CREATE, {"body": body},
        title=f"Create bill from {label}", warnings=warnings, file_id=file_id, now=now,
    ))
    amount = f"{total:.2f} {currency}".strip()
    summary = f"{filename}: bill for {label}, {amount}, {len(line_items)} line(s)."
    if warnings:
        summary += f" {len(warnings)} point(s) need your attention."
    return records, summary


@router.post("/conversations/{conversation_id}/jobs/{job_id}/proposals")
@_limiter.limit("20/minute")
async def propose_from_job(
    request: Request,
    conversation_id: uuid.UUID,
    job_id: uuid.UUID,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    company_settings: dict = Depends(get_current_company_settings),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Turn a finished reading job into bill proposals the user confirms.

    Vendors are matched to contacts and lines to items by exact name or SKU;
    an unknown vendor becomes a proposed contact ahead of its bill. Nothing is
    written until the user confirms each card. Calling again returns the same
    proposals.
    """
    conv = await get_conversation(session, conversation_id, company_id, user.id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    job = await get_batch_job(session, job_id, company_id)
    if job is None or job.conversation_id != conversation_id or job.user_id != user.id:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status in ("pending", "running"):
        raise _conflict("job_not_finished", "The files are still being read.")
    if job.status == "failed":
        raise _conflict("job_failed", job.error or "None of the files could be read.")

    existing_id = (job.results or {}).get("proposal_message_id")
    if existing_id:
        msg = await get_message(session, uuid.UUID(existing_id), conversation_id)
        if msg is not None:
            return {
                "message_id": str(msg.id), "answer": msg.content,
                "pending_actions": _message_out(msg).pending_actions,
            }

    capabilities = compile_agent_capabilities(request.app, company_settings)
    if _DOCS_CREATE not in capabilities:
        raise _conflict("capability_unavailable", "The documents module is not enabled, so bills cannot be created.")

    lookups = _Lookups(request.app, request.headers.get("authorization", ""), capabilities)
    now = datetime.now(timezone.utc)
    records: list[dict] = []
    lines: list[str] = []
    for entry in (job.results or {}).get("files") or []:
        filename = entry.get("filename") or entry.get("file_id")
        extraction = entry.get("extraction")
        if entry.get("status") != "success":
            lines.append(f"{filename}: could not be read. {entry.get('error') or ''}".strip())
        elif not isinstance(extraction, dict):
            lines.append(f"{filename}: no receipt details could be read from this file.")
        elif extraction.get("document_kind") not in _BILL_KINDS:
            kind = extraction.get("document_kind") or "document"
            lines.append(f"{filename}: read as a {kind}, so no bill was proposed.")
        else:
            new_records, summary = await _bill_proposals(lookups, entry, extraction, now)
            records.extend(new_records)
            lines.append(summary)

    msg = await add_message(session, conversation_id, "assistant", "\n".join(lines), tools_called=records)
    job.results = {**(job.results or {}), "proposal_message_id": str(msg.id)}
    await session.commit()
    return {
        "message_id": str(msg.id), "answer": msg.content,
        "pending_actions": _message_out(msg).pending_actions,
    }


@settings_router.get("/usage-stats")
async def usage_stats(
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Per-user AI usage for the current calendar month.

    Returns list of {user_id, user_name, query_count, credits_used, last_query_at}.
    """
    from datetime import date, datetime, timezone
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
    return _job_out(job)
