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
from typing import Literal

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.ai import AIConversation, AIMessage

log = logging.getLogger(__name__)

MAX_CONVERSATIONS_PER_USER = 100
MAX_MESSAGES_PER_CONVERSATION = 200
HISTORY_TOKEN_BUDGET = 8000
_CHARS_PER_TOKEN = 4  # conservative estimate
# An action claimed for execution that never finalized (the process died mid-call)
# is reported as failed after this long so the user is not left with a card that
# can neither be confirmed nor dismissed.
EXECUTING_STALE_S = 5 * 60
UNFINISHED_ACTION_TEXT = (
    "This action did not finish. Check whether it was applied before asking for it again."
)


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
            .order_by(AIConversation.updated_at.desc())
            .offset(MAX_CONVERSATIONS_PER_USER)
        )
        old_ids = list((await session.execute(oldest_q)).scalars().all())
        if old_ids:
            await session.execute(
                delete(AIConversation).where(AIConversation.id.in_(old_ids))
            )

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
        oldest_q = (
            select(AIMessage.id)
            .where(AIMessage.conversation_id == conversation_id)
            .order_by(AIMessage.created_at.desc())
            .offset(MAX_MESSAGES_PER_CONVERSATION)
        )
        old_ids = list((await session.execute(oldest_q)).scalars().all())
        if old_ids:
            await session.execute(
                delete(AIMessage).where(AIMessage.id.in_(old_ids))
            )

    return msg


async def get_messages(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    limit: int = 50,
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


# -- Tool-call record helpers ------------------------------------------------
#
# The assistant message's ``tools_called`` list mixes executed read operation-id
# strings with pending-action records (dicts). A record carries no status when
# first stored (the model has only proposed it); it gains a status of
# "executing" once claimed and "completed"/"failed" once finalized. A record
# with no explicit status is therefore still pending.


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
    """A dict record is pending when its status is pending and it has not expired."""
    if not isinstance(record, dict):
        return False
    if record.get("status", "pending") != "pending":
        return False
    expires_at = record.get("expires_at")
    if not isinstance(expires_at, str):
        return False
    try:
        return datetime.fromisoformat(expires_at) > now
    except ValueError:
        return False


def _stale_executing(record: object, now: datetime) -> bool:
    """A dict record claimed for execution more than EXECUTING_STALE_S ago."""
    if not isinstance(record, dict) or record.get("status") != "executing":
        return False
    since = record.get("executing_since")
    if not isinstance(since, str):
        return False
    try:
        return (now - datetime.fromisoformat(since)).total_seconds() > EXECUTING_STALE_S
    except ValueError:
        return False


def pending_actions(tools_called: list | None) -> list[dict]:
    """Action records the user still needs to see, from a stored ``tools_called`` list.

    Returns pending unexpired records as stored, plus records stuck in
    ``executing`` past EXECUTING_STALE_S rewritten as failed with an
    explanation, so the card shows what happened instead of a dead button.
    """
    if not tools_called:
        return []
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    for record in tools_called:
        if _still_pending(record, now):
            out.append(record)
        elif _stale_executing(record, now):
            out.append({**record, "status": "failed", "error": UNFINISHED_ACTION_TEXT})
    return out


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
            and _still_pending(item, now)
        ):
            claimed = dict(item)
            new_list.append({**item, "status": "executing", "executing_since": now.isoformat()})
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
    status: Literal["completed", "failed"],
    result: dict | None,
) -> None:
    """Record the terminal state of a claimed action; never store result payloads."""
    msg = await session.get(AIMessage, message_id)
    if msg is None or not msg.tools_called:
        return

    new_list: list = []
    for item in msg.tools_called:
        if isinstance(item, dict) and item.get("id") == tool_call_id:
            new_list.append({
                "id": item.get("id"),
                "name": item.get("name"),
                "status": status,
                "result_status": (result or {}).get("status"),
                "finished_at": datetime.now(timezone.utc).isoformat(),
            })
        else:
            new_list.append(item)

    msg.tools_called = new_list
    session.add(msg)
    await session.flush()


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
            if record.get("status") != "failed":
                content += f"\n[proposed action: {record.get('name')}]"
        msg_tokens = len(content) // _CHARS_PER_TOKEN
        if tokens_used + msg_tokens > HISTORY_TOKEN_BUDGET:
            break
        result.append({"role": msg.role, "content": content})
        tokens_used += msg_tokens

    # Reverse back to chronological order
    result.reverse()
    return result
