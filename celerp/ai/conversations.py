# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""AI conversation management - CRUD + history assembly for multi-turn context.

Conversations are per-company, per-user. Messages are stored in the DB
and assembled into a context window for the LLM on each query.

Token budget: HISTORY_TOKEN_BUDGET tokens of history (newest messages kept first).
Approximate: 1 token per 4 characters.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Callable, Literal, Sequence, TypeVar

from sqlalchemy import delete, func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.ai import jobs
from celerp.models.ai import AIConversation, AIMessage
from celerp.ai.service import NEW_QUESTION_ERRORS
from celerp.ai.tools import keyed_error
from celerp.ai.usage import record_usage
from celerp.services.company_lock import holds_company_lock

T = TypeVar("T")

log = logging.getLogger(__name__)

MAX_CONVERSATIONS_PER_USER = 100
MAX_MESSAGES_PER_CONVERSATION = 200
# Rows one older-actions read examines at most (protected_before), so a request's work stays
# bounded however many finished actions lie between the cards still protected.
PROTECTED_SCAN_BUDGET = 5 * MAX_MESSAGES_PER_CONVERSATION
HISTORY_TOKEN_BUDGET = 8000
_CHARS_PER_TOKEN = 4  # conservative estimate
# A claimed action may outlive the process that started it. Once stale, a keyed claim
# (its route has a key store) is retryable and the retry reuses the same tool-call id
# and canonical key; an unkeyed claim's outcome is unknown, and only a confirm carrying
# run_again repeats it.
EXECUTING_STALE_S = 5 * 60


async def _protected_conversation_ids(
    session: AsyncSession,
    conversation_ids: Sequence[uuid.UUID],
) -> set[uuid.UUID]:
    """Conversation ids whose unresolved work must remain reachable."""
    ids = list(conversation_ids)
    if not ids:
        return set()

    active_ids = {job.conversation_id for job in await jobs.open_jobs(session, ids)}
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
    """Create a new conversation, under the company's lock as every AI writer of the company.
    Prunes the user's oldest if the limit is exceeded."""
    await jobs.lock_for_ai(session, company_id)
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
                await session.execute(delete(AIConversation).where(
                    AIConversation.company_id == company_id, AIConversation.id.in_(prunable)))

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


async def _lock_conversation(session: AsyncSession, conversation_id: uuid.UUID) -> None:
    """Lock the conversation's company, unless this transaction holds it already, then the
    conversation's row, before any of its messages. Every writer of a conversation's
    messages takes this first, so two writers never wait on each other in opposite order
    (company, job, conversation, messages)."""
    company_id = await session.scalar(select(AIConversation.company_id).where(AIConversation.id == conversation_id))
    if company_id is not None and not holds_company_lock(session, company_id):
        await jobs.lock_for_ai(session, company_id)
    await session.execute(
        select(AIConversation.id)
        .where(AIConversation.id == conversation_id, AIConversation.company_id == company_id)
        .with_for_update(key_share=True)
    )


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
    """Delete a conversation and its messages, and end its open jobs as stopped in the
    same transaction, so none outlives it open. Returns True if found. Raises
    ``ConversationBusy``, and deletes nothing, while a job runs in it or a confirmed
    action of it is being run.

    One DELETE of the conversation's row: the row is locked first and the database's
    foreign-key cascade then removes the messages, the same order every message writer
    takes (conversation, then messages)."""
    await jobs.lock_for_ai(session, company_id)
    conv = await get_conversation(session, conversation_id, company_id, user_id)
    if conv is None:
        return False
    if (await _runs_an_action(session, conversation_id)
            or not await jobs.end_conversation_jobs(session, conversation_id, company_id=company_id,
                                                    user_id=user_id)):
        raise ConversationBusy
    await session.execute(delete(AIConversation).where(
        AIConversation.id == conversation_id, AIConversation.company_id == company_id,
        AIConversation.user_id == user_id))
    return True


class ConversationBusy(Exception):
    """A job or a confirmed action runs in the conversation: it can be deleted once that ends."""


async def _runs_an_action(session: AsyncSession, conversation_id: uuid.UUID) -> bool:
    """Whether a confirmed action of the conversation is being run right now. The caller
    holds the company's lock, which every claim takes, so none starts meanwhile."""
    rows = await session.execute(select(AIMessage.tools_called).where(
        AIMessage.conversation_id == conversation_id, AIMessage.tools_called.isnot(None)))
    now = datetime.now(timezone.utc)
    return any(_running(record, now) for (tools_called,) in rows.all() for record in tools_called or [])


async def _protected_message_ids(
    session: AsyncSession, conversation_id: uuid.UUID, rows: Sequence[tuple[uuid.UUID, list | None]],
) -> set[uuid.UUID]:
    """Of ``rows`` (message id, tools_called) of one conversation, the messages carrying
    protected work: an action under ``_protected``, or the question an open job answers.
    Pruning keeps them and the conversation read reaches them, however old (protected_before)."""
    asked = {job.question_id for job in await jobs.open_jobs(session, [conversation_id])}
    return {message_id for message_id, tools_called in rows
            if message_id in asked or _has_protected_action(tools_called)}


async def rename_conversation(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
    title: str,
) -> AIConversation | None:
    """Rename a conversation, under the company's lock. Returns updated conversation or None."""
    await jobs.lock_for_ai(session, company_id)
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
    message_id: uuid.UUID | None = None,
) -> AIMessage:
    """Add a message to a conversation. Prunes oldest if limit exceeded.

    ``message_id`` is the question id of a user message, chosen by the caller.

    ``tools_called`` is stored verbatim: a mix of executed read operation-id
    strings and pending-action records (dicts). Also sets the conversation title
    from the first user message if not already set, and bumps ``updated_at``.
    """
    await _lock_conversation(session, conversation_id)
    msg = AIMessage(
        **({"id": message_id} if message_id is not None else {}),
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
            .order_by(AIMessage.created_at.desc(), AIMessage.id.desc())
            .offset(MAX_MESSAGES_PER_CONVERSATION)
        )).all())
        if old_rows:
            # Protected work is a durable user decision or a change being run, not
            # disposable chat history: it stays until it ends, even past the soft cap.
            protected = await _protected_message_ids(session, conversation_id, old_rows)
            prunable = [message_id for message_id, _ in old_rows if message_id not in protected]
            if prunable:
                await session.execute(delete(AIMessage).where(AIMessage.conversation_id == conversation_id,
                                                              AIMessage.id.in_(prunable)))

    return msg


Cursor = tuple[datetime, uuid.UUID]


async def get_messages(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    limit: int | None = None,
) -> tuple[list[AIMessage], Cursor | None]:
    """The newest ``limit`` messages (MAX_MESSAGES_PER_CONVERSATION by default) plus the
    first page of older ones carrying protected work (protected_before), in time order
    (created_at, then id), and the cursor of the next older page, None when the read reached
    the start of the conversation. Bounded: a read returns at most twice ``limit`` and
    examines at most ``limit`` plus PROTECTED_SCAN_BUDGET rows."""
    limit = limit or MAX_MESSAGES_PER_CONVERSATION
    newest = list((await session.execute(
        select(AIMessage)
        .where(AIMessage.conversation_id == conversation_id)
        .order_by(AIMessage.created_at.desc(), AIMessage.id.desc())
        .limit(limit)
    )).scalars().all())
    older = None
    if newest and len(newest) == limit:
        page, older = await protected_before(session, conversation_id, (newest[-1].created_at, newest[-1].id),
                                             limit=limit)
        newest += page
    newest.sort(key=lambda m: (m.created_at, m.id))
    return newest, older


async def protected_before(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    before: Cursor,
    *,
    limit: int | None = None,
) -> tuple[list[AIMessage], Cursor | None]:
    """Up to ``limit`` of the newest messages older than ``before`` (created_at, id) that
    carry protected work (_protected_message_ids), in time order, and the cursor the next
    older page starts from, None once the read reached the start of the conversation.
    Paging from the cursor reaches every such message once, so a card pruning kept is never
    lost.

    The older messages are read newest first in keyset batches of ``limit + 1``, and one
    request examines at most PROTECTED_SCAN_BUDGET rows however long the history between
    two protected messages (finished actions are kept rows that no longer protect). The
    read stops at the first protected message past the page (cursor: the oldest message of
    the page) or when the budget is spent (cursor: the last row examined, the page may be
    short or empty and the next request carries on from there)."""
    limit = limit or MAX_MESSAGES_PER_CONVERSATION
    keep: list[uuid.UUID] = []
    cursor: Cursor | None = before
    scanned = 0
    while len(keep) <= limit:
        if scanned >= PROTECTED_SCAN_BUDGET:
            break
        size = min(limit + 1, PROTECTED_SCAN_BUDGET - scanned)
        batch = (await session.execute(
            select(AIMessage.id, AIMessage.tools_called, AIMessage.created_at)
            .where(AIMessage.conversation_id == conversation_id,
                   tuple_(AIMessage.created_at, AIMessage.id) < tuple_(*cursor))
            .order_by(AIMessage.created_at.desc(), AIMessage.id.desc())
            .limit(size)
        )).all()
        scanned += len(batch)
        protected = await _protected_message_ids(session, conversation_id, [(i, tools) for i, tools, _ in batch])
        keep += [message_id for message_id, _, _ in batch if message_id in protected]
        if len(batch) < size:
            cursor = None
            break
        cursor = (batch[-1].created_at, batch[-1].id)
    page = sorted((await session.execute(select(AIMessage).where(AIMessage.id.in_(keep[:limit])))).scalars().all(),
                  key=lambda m: (m.created_at, m.id)) if keep else []
    if len(keep) > limit:
        return page, (page[0].created_at, page[0].id)
    return page, cursor


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


async def record_credits(
    session: AsyncSession,
    message: AIMessage,
    credits: int,
    *,
    known: bool,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """Record what a run of the question in ``message`` was charged, on the message and
    in the usage record under the message's id (celerp.ai.usage.record_usage): the
    question's whole charge when the service named it (``known``), else the credits
    this run's replies added."""
    await _lock_conversation(session, message.conversation_id)
    total = credits if known else AIMessage.credits_used + credits
    await session.execute(update(AIMessage).where(AIMessage.id == message.id,
                                                  AIMessage.conversation_id == message.conversation_id)
                          .values(credits_used=total))
    await record_usage(session, company_id=company_id, user_id=user_id, operation_id=message.id,
                       credits=credits, known=known)


# -- Tool-call record helpers ------------------------------------------------
#
# The assistant message's ``tools_called`` list mixes executed read operation-id
# strings with pending-action records (dicts). A record carries no status when
# first stored (the model has only proposed it); it gains a status of
# "executing" once claimed and "completed"/"failed" once finalized. A record
# with no explicit status is therefore still pending.


def failure_record(code: str | None, *, status: int | None = None, refusal: dict | None = None) -> dict:
    """The record that marks a stored assistant message as a failed run: why it
    failed and, for a question the AI service refused, the refusal it sent with
    its status, so a reloaded thread explains it the same way.

    The record has no ``name`` and no ``expires_at``, so ``tool_names`` and
    ``pending_actions`` both ignore it.
    """
    record: dict = {"error": True, "code": code}
    if refusal is not None:
        record.update(status=status, refusal=refusal)
    return record


def message_failure(tools_called: list | None) -> dict | None:
    """The failure record of a stored assistant message; None when the run answered."""
    for item in tools_called or []:
        if isinstance(item, dict) and item.get("error") is True and "name" not in item:
            return item
    return None


def message_error(tools_called: list | None) -> bool:
    """True when a stored assistant message records a failed agent run."""
    return message_failure(tools_called) is not None


class QuestionIdConflict(Exception):
    """The question id names a question that can no longer be asked under it."""


class QuestionNotFound(Exception):
    """The question id names a message no job asks, or a job whose question is gone
    (its job is ended, unless a runner holds it: its run ends it)."""


async def find_question(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    question_id: uuid.UUID,
    request: jobs.JobRequest,
) -> tuple:
    """The job ``question_id`` names for ``request`` (``jobs.reused_job``, the one
    comparison of a send with what the question asked), its user message, and the
    answer it already has.

    The message is read before the job: both are committed together, so a send that
    finds the message finds its job. (None, None, None) when nothing holds the id yet:
    a new question. A message under the id without a job, or a job whose message is
    gone, is no question to ask: ``QuestionNotFound``. The answer is the first reply after the
    question, before the next question, that is not a failure; a question with an
    answer returns it however many questions came after. Without an answer the question
    is asked again only while it is the conversation's latest and the AI service still
    takes its id; otherwise ``QuestionIdConflict``.
    """
    asked = await session.get(AIMessage, question_id)
    job = await jobs.reused_job(session, question_id, request)
    if job is None:
        if asked is not None:
            raise QuestionNotFound
        return None, None, None
    if asked is None:
        await jobs.end_question(session, job.id)
        raise QuestionNotFound
    later = (await session.execute(
        select(AIMessage)
        .where(AIMessage.conversation_id == conversation_id, AIMessage.created_at > asked.created_at)
        .order_by(AIMessage.created_at)
    )).scalars().all()
    next_question = next((n for n, m in enumerate(later) if m.role == "user"), len(later))
    replies = later[:next_question]
    for reply in replies:
        if not message_error(reply.tools_called):
            return job, asked, reply
    if next_question < len(later) or (
        replies and (message_failure(replies[-1].tools_called) or {}).get("code") in NEW_QUESTION_ERRORS
    ):
        raise QuestionIdConflict
    return job, asked, None


def answer_links(pages: list[str] | None, files: list[str], *, cut: bool = False) -> list[dict]:
    """The record of what an answer links to: the help pages it cites (when it searched
    help) and the files it offers, and that it was cut at the length limit. Nothing when
    none of these holds.

    The record has no ``name``, ``expires_at`` or ``error``, so ``tool_names``,
    ``pending_actions`` and ``message_failure`` all ignore it.
    """
    record = {**({} if pages is None else {"help_pages": list(pages)}),
              **({"downloads": list(files)} if files else {}),
              **({"cut": True} if cut else {})}
    return [record] if record else []


def answer_cut(tools_called: list | None) -> bool:
    """True for a stored answer the service cut at its length limit."""
    return any(isinstance(item, dict) and item.get("cut") is True for item in tools_called or [])


def help_pages(tools_called: list | None) -> list[str] | None:
    """The help pages a stored answer cites; None when the answer did not search help."""
    for item in tools_called or []:
        if isinstance(item, dict) and isinstance(item.get("help_pages"), list):
            return item["help_pages"]
    return None


def downloads(tools_called: list | None) -> list[str]:
    """The links to the files a stored answer offers."""
    for item in tools_called or []:
        if isinstance(item, dict) and isinstance(item.get("downloads"), list):
            return item["downloads"]
    return []


CONTINUATION = {"continuation": True}  # marks the question that carries a request on after its cards


def is_continuation(tools_called: list | None) -> bool:
    """True for the user-role question the app added to carry a request on after its
    cards were answered, not words the user typed."""
    return CONTINUATION in (tools_called or [])


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


def _outcome_unknown(record: object, now: datetime) -> bool:
    """True when nobody knows whether the change ran: stored ``unknown``, or a stale
    claim of a tool with no key store (a record without ``keyed`` counts as unkeyed)."""
    if not isinstance(record, dict):
        return False
    if record.get("status") == "unknown":
        return True
    return _stale_executing(record, now) and record.get("keyed") is not True


def _claimable(record: object, now: datetime, *, allow_unknown: bool = False) -> bool:
    """Whether a reader may claim ``record``. An unknown outcome is claimable only with
    ``allow_unknown``: a confirm carrying run_again, or a dismiss (which runs nothing)."""
    if not isinstance(record, dict):
        return False
    if _outcome_unknown(record, now):
        return allow_unknown
    return _still_pending(record, now) or record.get("status") == "retryable" or _stale_executing(record, now)


class ActionOutcomeUnknown(Exception):
    """A plain confirm reached a change whose outcome is unknown; only Run again repeats it."""

    def __init__(self, record: dict) -> None:
        super().__init__(record.get("id"))
        self.record = record


def _protected(record: object, now: datetime) -> bool:
    """The one definition of protected work on an action record: a decision still the
    user's (a proposal inside its TTL, retryable, or outcome unknown) or a change being
    run (executing, a fresh claim or a stale one). Retention, the conversation read,
    deletion and the continuation claim all ask this."""
    if _still_pending(record, now):
        return True
    return isinstance(record, dict) and record.get("status") in {"retryable", "unknown", "executing"}


def _running(record: object, now: datetime) -> bool:
    """Protected work being run right now: a claim not yet stale."""
    return _protected(record, now) and record.get("status") == "executing" and not _stale_executing(record, now)


def _has_protected_action(tools_called: list | None) -> bool:
    """True while an action must keep its message and conversation reachable."""
    now = datetime.now(timezone.utc)
    return any(_protected(record, now) for record in tools_called or [])


def pending_actions(tools_called: list | None) -> list[dict]:
    """Proposal records still requiring user attention.

    Fresh proposals remain ``pending``. Ambiguous transport/server failures and stale
    executing claims of a keyed tool are surfaced as retryable with the same immutable
    tool-call id; those of an unkeyed tool are surfaced as unknown, which only a
    confirm carrying run_again repeats. Completed/failed/dismissed actions stay in
    audit history but no longer render as open proposals.
    """
    if not tools_called:
        return []
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    for record in tools_called:
        if _still_pending(record, now):
            out.append(record)
        elif _outcome_unknown(record, now):
            out.append(record if record.get("status") == "unknown" else {
                **record, "status": "unknown", "error": outcome_unknown_error(record)})
        elif isinstance(record, dict) and record.get("status") == "retryable":
            out.append(record)
        elif _stale_executing(record, now):
            out.append({**record, "status": "retryable",
                        "error": keyed_error("action_unfinished", "ai.action_unfinished")})
    return out


def outcome_unknown_error(record: dict) -> dict:
    """Why a change will not run without Run again, with the screen where the user sees
    whether it happened."""
    return {**keyed_error("action_outcome_unknown", "ai.action_outcome_unknown"),
            "check_screen": record.get("check_screen")}


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


async def change_tools_called(
    session: AsyncSession,
    message_id: uuid.UUID,
    change: Callable[[list], tuple[list | None, T]],
    *,
    conversation_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> T | None:
    """The one transition of a message's action records; nothing else assigns
    ``tools_called``.

    Takes the conversation lock first, then reads the message row FOR UPDATE afresh
    from the database (``populate_existing``), so a copy this session loaded earlier can
    never be written back over another transaction's change. ``change`` gets that fresh
    list and returns ``(new_list, outcome)``; a ``new_list`` of None writes nothing.
    Returns the outcome, or None when the message is gone or has no records. With
    ``company_id`` and ``user_id`` the message must belong to that user's conversation."""
    if conversation_id is None:
        conversation_id = await session.scalar(select(AIMessage.conversation_id).where(AIMessage.id == message_id))
        if conversation_id is None:
            return None
    await _lock_conversation(session, conversation_id)
    q = select(AIMessage).where(AIMessage.id == message_id, AIMessage.conversation_id == conversation_id)
    if company_id is not None:
        q = q.join(AIConversation, AIMessage.conversation_id == AIConversation.id).where(
            AIConversation.company_id == company_id, AIConversation.user_id == user_id)
    q = q.with_for_update(of=AIMessage).execution_options(populate_existing=True)
    msg = (await session.execute(q)).scalars().first()
    if msg is None or not msg.tools_called:
        return None
    new_list, outcome = change(list(msg.tools_called))
    if new_list is not None:
        msg.tools_called = new_list
        session.add(msg)
        await session.flush()
    return outcome


def _replace(items: list, tool_call_id: str, update) -> tuple[list | None, dict | None]:
    """``items`` with the record ``tool_call_id`` replaced by ``update(record)``; (None,
    None) when no record matches or ``update`` returns None (nothing to write). Two records
    under one id raise: the agent loop never stores such a pair, and updating either would
    act on a record nobody chose."""
    found = [i for i, item in enumerate(items) if isinstance(item, dict) and item.get("id") == tool_call_id]
    if len(found) > 1:
        raise ValueError(f"{len(found)} action records share the id {tool_call_id!r}")
    if not found:
        return None, None
    i = found[0]
    updated = update(items[i])
    if updated is None:
        return None, None
    return [*items[:i], updated, *items[i + 1:]], updated


async def claim_tool_call(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    tool_call_id: str,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
    run_again: bool = False,
) -> dict | None:
    """Atomically move a pending action to ``executing``; return its copy or None.

    Runs inside ``change_tools_called``, so two confirmations of the same action cannot
    both claim it.

    A record whose outcome is unknown raises ``ActionOutcomeUnknown`` unless
    ``run_again``. ``run_again`` claims only a record whose outcome is unknown (an unkeyed
    tool, after the user chose to repeat it); on any other record it claims nothing. Every
    run keeps the record's own identity, so a keyed retry always carries the first key.
    """
    now = datetime.now(timezone.utc)

    def claim(item: dict) -> dict | None:
        unknown = _outcome_unknown(item, now)
        if unknown and not run_again:
            raise ActionOutcomeUnknown(item)
        if run_again != unknown or not _claimable(item, now, allow_unknown=unknown):
            return None
        running = {**item, "status": "executing", "executing_since": now.isoformat()}
        running.pop("error", None)
        return running

    claimed = await change_tools_called(
        session, message_id, lambda items: _replace(items, tool_call_id, claim),
        conversation_id=conversation_id, company_id=company_id, user_id=user_id)
    return dict(claimed) if claimed is not None else None


async def claim_continuation(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
    job_id: uuid.UUID,
) -> tuple[str, bool] | None:
    """Claim, once per proposal message, the question job that carries its request on.

    Decided inside ``change_tools_called``: nothing is claimed while any action is open
    or still running, or when none completed. The claim is a ``continued_job_id`` record
    stored on the message, so a second completer or a retry gets the same job back.
    Returns ``(job id, newly claimed)``, or None when nothing continues."""
    def claim(items: list) -> tuple[list | None, tuple[str, bool] | None]:
        claimed = next((r["continued_job_id"] for r in items if isinstance(r, dict) and r.get("continued_job_id")),
                       None)
        if claimed:
            return None, (claimed, False)
        records = [r for r in items if isinstance(r, dict) and r.get("name")]
        if (not records or _has_protected_action(records)
                or not any(r.get("status") == "completed" for r in records)):
            return None, None
        return [*items, {"continued_job_id": str(job_id)}], (str(job_id), True)

    return await change_tools_called(session, message_id, claim, conversation_id=conversation_id,
                                     company_id=company_id, user_id=user_id)


async def finalize_tool_call(
    session: AsyncSession,
    *,
    message_id: uuid.UUID,
    tool_call_id: str,
    status: Literal["completed", "failed", "retryable", "unknown", "dismissed"],
    result: dict | None,
    error: dict | None = None,
    outcomes: list[dict] | None = None,
    kept: dict | None = None,
) -> None:
    """Persist action state while retaining the immutable proposal/audit record.

    ``error`` is kept whole ({code, message, message_key, params}), so the reloaded
    thread reads it in its reader's language, as the live reply did. ``outcomes`` is
    each record's own outcome of a propose_for_each card, so Run again runs only the
    records still open. ``kept`` is what the record stores as its result when the
    route's answer must not be stored (a generated password reads as SHOWN_ONCE)."""
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

    def finish(item: dict) -> dict:
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
        if outcomes is not None:
            updated["outcomes"] = outcomes
        if kept is not None:
            updated["result"] = kept
        return updated

    await change_tools_called(session, message_id, lambda items: _replace(items, tool_call_id, finish))


async def note_pending(
    session: AsyncSession, *, message_id: uuid.UUID, tool_call_id: str, error: dict,
) -> None:
    """Say why a pending action was not run (an earlier change to its record failed); it
    stays pending, and its next claim clears the note."""
    await change_tools_called(
        session, message_id, lambda items: _replace(items, tool_call_id, lambda item: {**item, "error": error}))


async def note_outcomes(
    session: AsyncSession, *, message_id: uuid.UUID, tool_call_id: str, outcomes: list[dict],
) -> None:
    """Store each record's outcome of a propose_for_each card while it runs, the claim
    still held and its time renewed, so a stopped run leaves only the record in flight
    unknown."""
    now = datetime.now(timezone.utc).isoformat()
    await change_tools_called(session, message_id, lambda items: _replace(
        items, tool_call_id, lambda item: {**item, "outcomes": outcomes, "executing_since": now}
        if item.get("status") == "executing" else None))


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

    One ``change_tools_called`` transition covers the whole selection, so bulk dismissal
    cannot race individual confirmations into partially overwritten JSON state.
    Unknown or already-terminal ids are harmless and simply omitted.
    """
    wanted = set(tool_call_ids)
    if not wanted:
        return []
    now = datetime.now(timezone.utc)

    def dismiss(items: list) -> tuple[list | None, list[str]]:
        dismissed_ids: list[str] = []
        updated_items: list = []
        for item in items:
            item_id = item.get("id") if isinstance(item, dict) else None
            if item_id in wanted and _claimable(item, now, allow_unknown=True):
                dismissed = {**item, "status": "dismissed", "finished_at": now.isoformat()}
                dismissed.pop("executing_since", None)
                dismissed.pop("error", None)
                updated_items.append(dismissed)
                dismissed_ids.append(item_id)
            else:
                updated_items.append(item)
        return (updated_items if dismissed_ids else None), dismissed_ids

    return await change_tools_called(
        session, message_id, dismiss,
        conversation_id=conversation_id, company_id=company_id, user_id=user_id) or []


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


_ARGUMENTS_IN_HISTORY = 400  # characters of a completed action's arguments the model reads back


def _brief(arguments: object) -> str:
    """A completed action's arguments, compact, so a continued request knows what it did."""
    text = json.dumps(arguments or {}, ensure_ascii=False, separators=(",", ":"), default=str)
    return text if len(text) <= _ARGUMENTS_IN_HISTORY else text[:_ARGUMENTS_IN_HISTORY] + "..."


def build_history_context(messages: list[AIMessage]) -> list[dict[str, str]]:
    """Build a token-budgeted history for LLM context.

    Takes messages (oldest first), returns them chronologically, truncated to
    HISTORY_TOKEN_BUDGET tokens (newest kept first). An assistant message that
    still holds pending action records appends "[proposed action: <name>]" lines
    so the model knows what it offered, and a completed action its arguments. File content and tool results from prior
    turns are never included.
    """
    # Reverse to newest-first for token budgeting
    reversed_msgs = list(reversed(messages))
    result: list[dict[str, str]] = []
    tokens_used = 0

    for msg in reversed_msgs:
        content = msg.content
        for record in pending_actions(msg.tools_called):
            if record.get("status") == "unknown":
                content += (f"\n[action outcome unknown: {record.get('name')}; do not propose it again "
                            "unless you tell the user the earlier outcome is unknown]")
                continue
            label = "action awaiting retry" if record.get("status") == "retryable" else "proposed action"
            content += f"\n[{label}: {record.get('name')}]"
        for record in (msg.tools_called or []):
            if not isinstance(record, dict) or record.get("status") != "completed":
                continue
            summary = record.get("result_summary") or {}
            identity = summary.get("id") or summary.get("entity_id") or summary.get("ref_id") or summary.get("event_id")
            suffix = f" -> {identity}" if identity not in (None, "") else ""
            content += f"\n[completed action: {record.get('name')} {_brief(record.get('arguments'))}{suffix}]"
        msg_tokens = len(content) // _CHARS_PER_TOKEN
        if tokens_used + msg_tokens > HISTORY_TOKEN_BUDGET:
            break
        result.append({"role": msg.role, "content": content})
        tokens_used += msg_tokens

    # Reverse back to chronological order
    result.reverse()
    return result
