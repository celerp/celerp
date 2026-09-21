# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""AI conversation management - CRUD + history assembly for multi-turn context.

Conversations are per-company, per-user. Messages are stored in the DB
and assembled into a context window for the LLM on each query.

Token budget: HISTORY_TOKEN_BUDGET tokens of history (newest messages kept first).
Approximate: 1 token per 4 characters.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Literal, Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.ai import AIBatchJob, AIConversation, AIMessage

log = logging.getLogger(__name__)

MAX_CONVERSATIONS_PER_USER = 100
MAX_MESSAGES_PER_CONVERSATION = 200
HISTORY_TOKEN_BUDGET = 8000
_CHARS_PER_TOKEN = 4  # conservative estimate
# A claimed action may outlive the process that started it. Once stale, expose it
# as retryable: every agent mutation is compiled only after proving canonical
# idempotency, so the same tool-call id can be retried safely.
EXECUTING_STALE_S = 5 * 60
UNFINISHED_ACTION_TEXT = (
    "The previous attempt did not return a definite result. Retry safely to check or finish it."
)


async def _protected_conversation_ids(
    session: AsyncSession,
    conversation_ids: Sequence[uuid.UUID],
) -> set[uuid.UUID]:
    """Conversation ids whose unresolved work must remain reachable."""
    ids = list(conversation_ids)
    if not ids:
        return set()

    active_ids = set((await session.execute(
        select(AIBatchJob.conversation_id).where(
            AIBatchJob.conversation_id.in_(ids),
            AIBatchJob.status.in_(("pending", "running")),
        )
    )).scalars().all())
    action_rows = await session.execute(
        select(AIMessage.conversation_id, AIMessage.tools_called).where(
            AIMessage.conversation_id.in_(ids),
            AIMessage.tools_called.isnot(None),
        )
    )
    action_ids = {
        conversation_id
        for conversation_id, tools_called in action_rows.all()
        if _has_protected_action(tools_called)
    }
    return active_ids | action_ids


async def create_conversation(
    session: AsyncSession,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
    title: str | None = None,
) -> AIConversation:
    """Create a new conversation. Prunes the user's oldest if the limit is exceeded."""
    conv = AIConversation(
        company_id=company_id,
        user_id=user_id,
        title=title,
    )
    session.add(conv)
    await session.flush()

    # Prune: keep only MAX_CONVERSATIONS_PER_USER per user (conversations are per user).
    count = (await session.execute(
        select(func.count()).select_from(AIConversation).where(
            AIConversation.company_id == company_id,
            AIConversation.user_id == user_id,
        )
    )).scalar() or 0

    if count > MAX_CONVERSATIONS_PER_USER:
        oldest_q = (
            select(AIConversation.id)
            .where(
                AIConversation.company_id == company_id,
                AIConversation.user_id == user_id,
            )
            .order_by(AIConversation.updated_at.desc(), AIConversation.id.desc())
            .offset(MAX_CONVERSATIONS_PER_USER)
        )
        old_ids = list((await session.execute(oldest_q)).scalars().all())
        if old_ids:
            # Prefer exceeding the soft cap briefly over destroying unresolved
            # actions or active file processing.
            protected_ids = await _protected_conversation_ids(session, old_ids)
            prunable = [cid for cid in old_ids if cid not in protected_ids]
            if prunable:
                await session.execute(delete(AIConversation).where(AIConversation.id.in_(prunable)))

    return conv


async def list_conversations(
    session: AsyncSession,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    limit: int = 20,
    offset: int = 0,
) -> list[AIConversation]:
    """List conversations for a user, newest first."""
    q = (
        select(AIConversation)
        .where(
            AIConversation.company_id == company_id,
            AIConversation.user_id == user_id,
        )
        .order_by(AIConversation.updated_at.desc(), AIConversation.id.desc())  # id tiebreaker → stable pagination
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(q)).scalars().all())


async def list_conversation_history(
    session: AsyncSession,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    limit: int = MAX_CONVERSATIONS_PER_USER,
) -> list[AIConversation]:
    """Recent history plus older conversations whose unresolved state must stay reachable."""
    recent = await list_conversations(
        session, company_id, user_id, limit=limit, offset=0,
    )
    if limit <= 0 or len(recent) < limit:
        return recent

    recent_ids = {c.id for c in recent}
    older_ids = list((await session.execute(
        select(AIConversation.id)
        .where(
            AIConversation.company_id == company_id,
            AIConversation.user_id == user_id,
        )
        .order_by(AIConversation.updated_at.desc(), AIConversation.id.desc())
        .offset(limit)
    )).scalars().all())
    protected_ids = await _protected_conversation_ids(session, older_ids)
    protected_ids.difference_update(recent_ids)
    if not protected_ids:
        return recent

    protected = list((await session.execute(
        select(AIConversation).where(
            AIConversation.id.in_(protected_ids),
            AIConversation.company_id == company_id,
            AIConversation.user_id == user_id,
        )
    )).scalars().all())
    combined = [*recent, *protected]
    combined.sort(key=lambda c: (c.updated_at, c.id.int), reverse=True)
    return combined


async def get_conversation(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
) -> AIConversation | None:
    """Get a conversation by ID, scoped to company + user."""
    q = select(AIConversation).where(
        AIConversation.id == conversation_id,
        AIConversation.company_id == company_id,
        AIConversation.user_id == user_id,
    )
    return (await session.execute(q)).scalars().first()


async def delete_conversation(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    """Delete a conversation and its messages. Returns True if found."""
    conv = await get_conversation(session, conversation_id, company_id, user_id)
    if conv is None:
        return False
    await session.delete(conv)
    return True


async def rename_conversation(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
    title: str,
) -> AIConversation | None:
    """Rename a conversation. Returns updated conversation or None."""
    conv = await get_conversation(session, conversation_id, company_id, user_id)
    if conv is None:
        return None
    conv.title = title
    session.add(conv)
    return conv


async def add_message(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    role: str,
    content: str,
    *,
    model_used: str | None = None,
    tools_called: list | None = None,
    file_ids: list[str] | None = None,
    credits_used: int = 0,
) -> AIMessage:
    """Add a message to a conversation. Prunes oldest if limit exceeded.

    ``tools_called`` is stored verbatim: a mix of executed read operation-id
    strings and pending-action records (dicts). Also sets the conversation title
    from the first user message if not already set, and bumps ``updated_at``.
    """
    msg = AIMessage(
        conversation_id=conversation_id,
        role=role,
        content=content,
        model_used=model_used,
        tools_called=tools_called,
        file_ids=file_ids,
        credits_used=credits_used,
    )
    session.add(msg)
    await session.flush()

    # Update conversation: auto-title from first user message + timestamp
    conv = await session.get(AIConversation, conversation_id)
    if conv:
        if role == "user" and not conv.title:
            conv.title = content[:60]
        conv.updated_at = datetime.now(timezone.utc)
        session.add(conv)

    # Prune old messages
    count = (await session.execute(
        select(func.count()).select_from(AIMessage).where(
            AIMessage.conversation_id == conversation_id,
        )
    )).scalar() or 0

    if count > MAX_MESSAGES_PER_CONVERSATION:
        old_rows = list((await session.execute(
            select(AIMessage.id, AIMessage.tools_called)
            .where(AIMessage.conversation_id == conversation_id)
            .order_by(AIMessage.created_at.desc())
            .offset(MAX_MESSAGES_PER_CONVERSATION)
        )).all())
        if old_rows:
            # Unresolved proposals and ambiguous/retryable writes are durable user
            # decisions, not disposable chat history. Preserve them until they reach
            # a terminal state, even if that means temporarily exceeding the soft
            # history cap.
            prunable = [
                message_id for message_id, tools_called in old_rows
                if not pending_actions(tools_called)
            ]
            if prunable:
                await session.execute(delete(AIMessage).where(AIMessage.id.in_(prunable)))

    return msg


async def get_messages(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    limit: int = MAX_MESSAGES_PER_CONVERSATION,
) -> list[AIMessage]:
    """Get the newest ``limit`` messages, returned oldest-first (chronological)."""
    q = (
        select(AIMessage)
        .where(AIMessage.conversation_id == conversation_id)
        .order_by(AIMessage.created_at.desc())
        .limit(limit)
    )
    newest_first = list((await session.execute(q)).scalars().all())
    newest_first.reverse()
    return newest_first


async def get_message(
    session: AsyncSession,
    message_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> AIMessage | None:
    """One message by id, only when it belongs to ``conversation_id``."""
    msg = await session.get(AIMessage, message_id)
    if msg is None or msg.conversation_id != conversation_id:
        return None
    return msg


async def record_credits(session: AsyncSession, message_id: uuid.UUID, credits: int) -> None:
    """Store the credits a run consumed on the user message that started it."""
    await session.execute(
        update(AIMessage).where(AIMessage.id == message_id).values(credits_used=credits)
    )


# -- Tool-call record helpers ------------------------------------------------
#
# The assistant message's ``tools_called`` list mixes executed read operation-id
# strings with pending-action records (dicts). A record carries no status when
# first stored (the model has only proposed it); it gains a status of
# "executing" once claimed and "completed"/"failed" once finalized. A record
# with no explicit status is therefore still pending.


ERROR_MARKER = {"error": True}


def message_error(tools_called: list | None) -> bool:
    """True when a stored assistant message records a failed agent run.

    The marker has no ``name`` and no ``expires_at``, so ``tool_names`` and
    ``pending_actions`` both ignore it.
    """
    return any(
        isinstance(item, dict) and item.get("error") is True and "name" not in item
        for item in (tools_called or [])
    )


def tool_names(tools_called: list | None) -> list[str]:
    """Operation-id strings from a stored ``tools_called`` list.

    Strings pass through unchanged; dict records yield ``record["name"]``.
    """
    if not tools_called:
        return []
    names: list[str] = []
    for item in tools_called:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and item.get("name"):
            names.append(item["name"])
    return names


def _still_pending(record: object, now: datetime) -> bool:
    """True while an unclaimed proposal is still inside its confirmation TTL."""
    if not isinstance(record, dict) or record.get("status", "pending") != "pending":
        return False
    expires_at = record.get("expires_at")
    if not isinstance(expires_at, str):
        return False
    try:
        return datetime.fromisoformat(expires_at) > now
    except ValueError:
        return False


def _stale_executing(record: object, now: datetime) -> bool:
    if not isinstance(record, dict) or record.get("status") != "executing":
        return False
    since = record.get("executing_since")
    if not isinstance(since, str):
        return False
    try:
        return (now - datetime.fromisoformat(since)).total_seconds() > EXECUTING_STALE_S
    except ValueError:
        return False


def _claimable(record: object, now: datetime) -> bool:
    if not isinstance(record, dict):
        return False
    status = record.get("status", "pending")
    return _still_pending(record, now) or status == "retryable" or _stale_executing(record, now)


def _has_protected_action(tools_called: list | None) -> bool:
    """True while an action must keep its conversation reachable."""
    if not tools_called:
        return False
    now = datetime.now(timezone.utc)
    for record in tools_called:
        if _still_pending(record, now):
            return True
        if isinstance(record, dict) and record.get("status") in {"retryable", "executing"}:
            return True
    return False


def pending_actions(tools_called: list | None) -> list[dict]:
    """Proposal records still requiring user attention.

    Fresh proposals remain ``pending``. Ambiguous transport/server failures and
    stale ``executing`` claims are surfaced as ``retryable`` using the same
    immutable tool-call id. Completed/failed/dismissed actions stay in audit
    history but no longer render as open proposals.
    """
    if not tools_called:
        return []
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    for record in tools_called:
        if _still_pending(record, now):
            out.append(record)
        elif isinstance(record, dict) and record.get("status") == "retryable":
            out.append(record)
        elif _stale_executing(record, now):
            out.append({**record, "status": "retryable", "error": UNFINISHED_ACTION_TEXT})
    return out


async def pending_action_counts(
    session: AsyncSession, conversation_ids: list[uuid.UUID],
) -> dict[uuid.UUID, int]:
    """Open (pending, unexpired) proposals per conversation, one query for the list."""
    if not conversation_ids:
        return {}
    rows = await session.execute(
        select(AIMessage.conversation_id, AIMessage.tools_called).where(
            AIMessage.conversation_id.in_(conversation_ids),
            AIMessage.tools_called.isnot(None),
        )
    )
    counts: dict[uuid.UUID, int] = {}
    for conversation_id, tools_called in rows.all():
        open_count = len(pending_actions(tools_called))
        if open_count:
            counts[conversation_id] = counts.get(conversation_id, 0) + open_count
    return counts


async def claim_tool_call(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    tool_call_id: str,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
) -> dict | None:
    """Atomically move a pending action to ``executing``; return its copy or None.

    Locks the owning message row (FOR UPDATE) so two confirmations of the same
    action cannot both claim it. Never mutates the JSON in place: a new list is
    assigned so SQLAlchemy persists the change.
    """
    q = (
        select(AIMessage)
        .join(AIConversation, AIMessage.conversation_id == AIConversation.id)
        .where(
            AIMessage.id == message_id,
            AIMessage.conversation_id == conversation_id,
            AIConversation.company_id == company_id,
            AIConversation.user_id == user_id,
        )
        .with_for_update(of=AIMessage)
    )
    msg = (await session.execute(q)).scalars().first()
    if msg is None or not msg.tools_called:
        return None

    now = datetime.now(timezone.utc)
    claimed: dict | None = None
    new_list: list = []
    for item in msg.tools_called:
        if (
            claimed is None
            and isinstance(item, dict)
            and item.get("id") == tool_call_id
            and _claimable(item, now)
        ):
            claimed = dict(item)
            running = {**item, "status": "executing", "executing_since": now.isoformat()}
            running.pop("error", None)
            new_list.append(running)
        else:
            new_list.append(item)

    if claimed is None:
        return None

    msg.tools_called = new_list
    session.add(msg)
    await session.flush()
    return claimed


async def finalize_tool_call(
    session: AsyncSession,
    *,
    message_id: uuid.UUID,
    tool_call_id: str,
    status: Literal["completed", "failed", "retryable", "dismissed"],
    result: dict | None,
    error: str | None = None,
) -> None:
    """Persist action state while retaining the immutable proposal/audit record."""
    msg = await session.get(AIMessage, message_id)
    if msg is None or not msg.tools_called:
        return

    summary: dict = {}
    data = (result or {}).get("data")
    if isinstance(data, dict):
        for key in ("id", "entity_id", "ref_id", "event_id"):
            value = data.get(key)
            if value not in (None, ""):
                summary[key] = value
    result_status = (result or {}).get("status")
    if result_status is not None:
        summary["http_status"] = result_status

    now = datetime.now(timezone.utc).isoformat()
    new_list: list = []
    for item in msg.tools_called:
        if isinstance(item, dict) and item.get("id") == tool_call_id:
            updated = {**item, "status": status, "finished_at": now}
            updated.pop("executing_since", None)
            if summary:
                updated["result_summary"] = summary
            else:
                updated.pop("result_summary", None)
            if error:
                updated["error"] = error
            else:
                updated.pop("error", None)
            new_list.append(updated)
        else:
            new_list.append(item)

    msg.tools_called = new_list
    session.add(msg)
    await session.flush()


async def dismiss_tool_calls(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    tool_call_ids: list[str],
    company_id: uuid.UUID,
    user_id: uuid.UUID,
) -> list[str]:
    """Atomically dismiss claimable proposals owned by this user.

    The owning message is locked once for the whole selection so bulk dismissal
    cannot race individual confirmations into partially overwritten JSON state.
    Unknown or already-terminal ids are harmless and simply omitted.
    """
    wanted = set(tool_call_ids)
    if not wanted:
        return []
    q = (
        select(AIMessage)
        .join(AIConversation, AIMessage.conversation_id == AIConversation.id)
        .where(
            AIMessage.id == message_id,
            AIMessage.conversation_id == conversation_id,
            AIConversation.company_id == company_id,
            AIConversation.user_id == user_id,
        )
        .with_for_update(of=AIMessage)
    )
    msg = (await session.execute(q)).scalars().first()
    if msg is None or not msg.tools_called:
        return []

    now = datetime.now(timezone.utc)
    dismissed_ids: list[str] = []
    updated_items: list = []
    for item in msg.tools_called:
        item_id = item.get("id") if isinstance(item, dict) else None
        if item_id in wanted and _claimable(item, now):
            dismissed = {**item, "status": "dismissed", "finished_at": now.isoformat()}
            dismissed.pop("executing_since", None)
            dismissed.pop("error", None)
            updated_items.append(dismissed)
            dismissed_ids.append(item_id)
        else:
            updated_items.append(item)
    if not dismissed_ids:
        return []

    msg.tools_called = updated_items
    session.add(msg)
    await session.flush()
    return dismissed_ids


async def dismiss_tool_call(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    tool_call_id: str,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    """Dismiss one proposal through the canonical bulk-safe transition."""
    return bool(await dismiss_tool_calls(
        session,
        conversation_id=conversation_id,
        message_id=message_id,
        tool_call_ids=[tool_call_id],
        company_id=company_id,
        user_id=user_id,
    ))


def build_history_context(messages: list[AIMessage]) -> list[dict[str, str]]:
    """Build a token-budgeted history for LLM context.

    Takes messages (oldest first), returns them chronologically, truncated to
    HISTORY_TOKEN_BUDGET tokens (newest kept first). An assistant message that
    still holds pending action records appends "[proposed action: <name>]" lines
    so the model knows what it offered. File content and tool results from prior
    turns are never included.
    """
    # Reverse to newest-first for token budgeting
    reversed_msgs = list(reversed(messages))
    result: list[dict[str, str]] = []
    tokens_used = 0

    for msg in reversed_msgs:
        content = msg.content
        for record in pending_actions(msg.tools_called):
            label = "action awaiting retry" if record.get("status") == "retryable" else "proposed action"
            content += f"\n[{label}: {record.get('name')}]"
        for record in (msg.tools_called or []):
            if not isinstance(record, dict) or record.get("status") != "completed":
                continue
            summary = record.get("result_summary") or {}
            identity = summary.get("id") or summary.get("entity_id") or summary.get("ref_id") or summary.get("event_id")
            suffix = f" -> {identity}" if identity not in (None, "") else ""
            content += f"\n[completed action: {record.get('name')}{suffix}]"
        msg_tokens = len(content) // _CHARS_PER_TOKEN
        if tokens_used + msg_tokens > HISTORY_TOKEN_BUDGET:
            break
        result.append({"role": msg.role, "content": content})
        tokens_used += msg_tokens

    # Reverse back to chronological order
    result.reverse()
    return result
