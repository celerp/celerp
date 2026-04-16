# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""AI conversation management - CRUD + history assembly for multi-turn context.

Conversations are per-company, per-user. Messages are stored in the DB
and assembled into a context window for the LLM on each query.

Token budget: ~4000 tokens of history (newest messages first, trim oldest).
Approximate: 1 token per 4 characters.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.ai import AIConversation, AIMessage

log = logging.getLogger(__name__)

MAX_CONVERSATIONS_PER_COMPANY = 100
MAX_MESSAGES_PER_CONVERSATION = 200
HISTORY_TOKEN_BUDGET = 8000
_CHARS_PER_TOKEN = 4  # conservative estimate


async def create_conversation(
    session: AsyncSession,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
    title: str | None = None,
) -> AIConversation:
    """Create a new conversation. Prunes oldest if limit exceeded."""
    conv = AIConversation(
        company_id=company_id,
        user_id=user_id,
        title=title,
    )
    session.add(conv)
    await session.flush()

    # Prune: keep only MAX_CONVERSATIONS_PER_COMPANY per company
    count = (await session.execute(
        select(func.count()).select_from(AIConversation).where(
            AIConversation.company_id == company_id,
        )
    )).scalar() or 0

    if count > MAX_CONVERSATIONS_PER_COMPANY:
        oldest_q = (
            select(AIConversation.id)
            .where(AIConversation.company_id == company_id)
            .order_by(AIConversation.updated_at.desc())
            .offset(MAX_CONVERSATIONS_PER_COMPANY)
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
        .order_by(AIConversation.updated_at.desc())
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
    tools_called: list[str] | None = None,
    file_ids: list[str] | None = None,
    credits_used: int = 0,
) -> AIMessage:
    """Add a message to a conversation. Prunes oldest if limit exceeded.

    Also sets the conversation title from the first user message if not already set,
    and updates the conversation's updated_at timestamp.
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
    """Get messages for a conversation, oldest first."""
    q = (
        select(AIMessage)
        .where(AIMessage.conversation_id == conversation_id)
        .order_by(AIMessage.created_at.asc())
        .limit(limit)
    )
    return list((await session.execute(q)).scalars().all())


def build_history_context(messages: list[AIMessage]) -> list[dict[str, str]]:
    """Build a token-budgeted history for LLM context.

    Takes messages (oldest first), returns newest-first truncated to
    HISTORY_TOKEN_BUDGET tokens. Output format: [{"role": "...", "content": "..."}].
    File content from previous turns is never included (just text).
    """
    # Reverse to newest-first for token budgeting
    reversed_msgs = list(reversed(messages))
    result: list[dict[str, str]] = []
    tokens_used = 0

    for msg in reversed_msgs:
        content = msg.content
        msg_tokens = len(content) // _CHARS_PER_TOKEN
        if tokens_used + msg_tokens > HISTORY_TOKEN_BUDGET:
            break
        result.append({"role": msg.role, "content": content})
        tokens_used += msg_tokens

    # Reverse back to chronological order
    result.reverse()
    return result
