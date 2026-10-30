# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Per-user AI question usage (GET /ai/usage-stats): a question counts once, when the
AI service charged it; the newest asker comes first; the period is this month or all time.

Every path that asks a question records its charges in the usage record under the
question's id: conversation questions, one-off queries and module queries here, and
receipt reading jobs in the module's batch tests. A thread question runs as a job:
its send returns 202 and the tests read its state until the job ends.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import httpx
import pytest

from fastapi import HTTPException
from sqlalchemy import select, update

from celerp.ai import jobs, llm, service, usage
from celerp.ai.conversations import record_credits
from celerp.ai.llm import ModelResult, RelayError
from celerp.ai.service import AgentResult, run_agent
from celerp.gateway.state import set_session_token
from celerp.models.ai import AIConversation, AIMessage, AIUsage
from celerp.services.auth import decode_access_token
from celerp.services.business_time import business_today
from migration_support import maker, real_client, real_engine  # noqa: F401
from test_helpers import invite_user, register_admin, settled_question, signed_request, until, until_a_lock_waits


def _claims(token: str) -> tuple[uuid.UUID, uuid.UUID]:
    claims = decode_access_token(token)
    return uuid.UUID(claims["company_id"]), uuid.UUID(claims["sub"])


async def _question(session, company_id, user_id, *, credits: int, when: datetime | None = None) -> AIMessage:
    """A conversation question charged ``credits``, stored as the app stores it."""
    conversation = AIConversation(company_id=company_id, user_id=user_id)
    session.add(conversation)
    await session.flush()
    message = AIMessage(conversation_id=conversation.id, role="user", content="q",
                        created_at=when or datetime.now(timezone.utc))
    session.add(message)
    await session.flush()
    await record_credits(session, message, credits, known=True, company_id=company_id, user_id=user_id)
    if when is not None:
        await session.flush()
        for row in (await session.execute(select(AIUsage).where(AIUsage.operation_id == message.id))).scalars():
            row.created_at = when
    await session.flush()
    return message


def _answer(content: str = "Open Contacts and choose New.", credits: int = 1,
            operation_credits: int | None = None) -> ModelResult:
    """A reply from the current service, which names the question's whole charge on
    every first-turn reply (``operation_credits``, by default ``credits``)."""
    return ModelResult(message={"role": "assistant", "content": content}, model_used="test-model",
                       usage={"credits": credits}, reservation_id=None, remaining=10,
                       operation_credits=credits if operation_credits is None else operation_credits)


def _relay(monkeypatch, replies: list) -> list:
    """Answer model calls from ``replies`` in order (a reply, or an exception to raise),
    each after the caller's ``before_send`` ran, as the model client calls it; returns
    the operation id each call sent."""
    sent: list = []

    async def _complete(messages, *, tools=None, reservation_id=None, operation_id=None, before_send=None, **_kw):
        if before_send is not None:
            await before_send(0)
        sent.append(operation_id)
        reply = replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    set_session_token("s" * 64)
    monkeypatch.setattr(service, "complete", _complete)
    monkeypatch.setattr(service, "compile_agent_capabilities", lambda app, settings, role=None: {})
    return sent


async def _ask(client, token: str, conversation_id: str, operation_id: uuid.UUID, query: str = "supplier?"):
    return await client.post(
        f"/ai/conversations/{conversation_id}/query", headers={"Authorization": f"Bearer {token}"},
        json={"query": query, "operation_id": str(operation_id)},
    )


async def _state(client, token: str, conversation_id: str, question_id) -> dict:
    r = await client.get(f"/ai/conversations/{conversation_id}/questions/{question_id}",
                         headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return r.json()


async def _settled(client, token: str, conversation_id: str, question_id) -> dict:
    """The question's state once its job has ended."""
    return await settled_question(client, {"Authorization": f"Bearer {token}"}, conversation_id, question_id)


async def _asked(client, token: str, conversation_id: str, operation_id: uuid.UUID,
                 query: str = "supplier?") -> tuple[dict, dict]:
    """Send a question that starts a job and wait for it: the send's body and the
    question's assistant message once the job ended."""
    r = await _ask(client, token, conversation_id, operation_id, query)
    assert r.status_code == 202, r.text
    return r.json(), (await _settled(client, token, conversation_id, operation_id))["message"]


async def _age(session, minutes: int = 5) -> None:
    """Let every row pass its reconcile time, as time would."""
    past = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    await session.execute(update(AIUsage).values(created_at=past, reconcile_at=past))
    await session.commit()


async def _conversation(client, token: str) -> str:
    r = await client.post("/ai/conversations", headers={"Authorization": f"Bearer {token}"}, json={"title": None})
    return r.json()["id"]


async def _messages(client, token: str, conversation_id: str) -> list[dict]:
    r = await client.get(f"/ai/conversations/{conversation_id}", headers={"Authorization": f"Bearer {token}"})
    return r.json()["messages"]


def _counts(rows: list[dict]) -> list[tuple[int, int]]:
    return [(r["query_count"], r["credits_used"]) for r in rows]


async def _usage(client, token: str, **params):
    return await client.get("/ai/usage-stats", headers={"Authorization": f"Bearer {token}"}, params=params)


@pytest.fixture
async def owner(client, drained_jobs):
    """The owner of a new company. Its question jobs are drained before the client
    and its session close."""
    token = await register_admin(client)
    company_id, user_id = _claims(token)
    return token, company_id, user_id


async def test_usage_counts_only_charged_questions(client, session, owner):
    token, company_id, user_id = owner
    await _question(session, company_id, user_id, credits=0)  # refused by the relay
    await _question(session, company_id, user_id, credits=1)
    await session.commit()
    rows = (await _usage(client, token)).json()["users"]
    assert _counts(rows) == [(1, 1)]

    # A question charged in several parts counts once: a later run's credits add.
    second = await _question(session, company_id, user_id, credits=2)
    await record_credits(session, second, 1, known=False, company_id=company_id, user_id=user_id)
    await session.commit()
    rows = (await _usage(client, token)).json()["users"]
    assert _counts(rows) == [(2, 4)]


async def test_credits_kept_when_a_later_turn_fails(client, session, owner, monkeypatch):
    token, company_id, user_id = owner
    turns = [ModelResult(
        message={"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "no_such_tool", "arguments": "{}"}}]},
        model_used="test-model", usage={"credits": 1}, reservation_id="r", remaining=10, operation_credits=1,
    )]

    async def _complete(messages, *, tools=None, reservation_id=None, **_kw):
        if turns:
            return turns.pop(0)
        raise RelayError("service_unavailable", "relay down")

    monkeypatch.setattr(service, "complete", _complete)
    monkeypatch.setattr(service, "compile_agent_capabilities", lambda app, settings, role=None: {})
    operation = uuid.uuid4()
    async with jobs.gate.slot("interactive"):
        result = await run_agent(
            app=object(), bearer=lambda: "Bearer tok", query="how do I add a supplier?",
            company_id=company_id, company_settings={}, user_id=user_id, role="owner",
            memory={"notes": [], "kv": {}}, file_ids=None, history=[], operation_id=operation,
            journal=jobs.Journal(operation),
        )
    assert result.error and result.credits == 1

    await _question(session, company_id, user_id, credits=result.credits)
    await session.commit()
    rows = (await _usage(client, token)).json()["users"]
    assert [(r["query_count"], r["credits_used"]) for r in rows] == [(1, 1)]


async def test_credits_kept_when_the_relay_refuses_a_later_turn(client, session, owner, monkeypatch):
    """The service charges a free question's first turn, then refuses the continuation
    as too large once tool results have grown the history. The question ends with a
    too-much-data answer, and the charged question stays in the usage table."""
    token, company_id, user_id = owner
    set_session_token("s" * 64)
    turns = [ModelResult(
        message={"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "no_such_tool", "arguments": "{}"}}]},
        model_used="test-model", usage={"credits": 1}, reservation_id="r", remaining=10, operation_credits=1,
    )]

    async def _complete(messages, *, tools=None, reservation_id=None, **_kw):
        if turns:
            return turns.pop(0)
        raise HTTPException(status_code=413, detail={"code": "free_text_too_large", "message": "too long"})

    monkeypatch.setattr(service, "complete", _complete)
    monkeypatch.setattr(service, "compile_agent_capabilities", lambda app, settings, role=None: {})
    conv = await _conversation(client, token)
    _, reply = await _asked(client, token, conv, uuid.uuid4())
    assert reply["error_code"] == "too_much_data"
    rows = (await _usage(client, token)).json()["users"]
    assert [(r["query_count"], r["credits_used"]) for r in rows] == [(1, 1)]


async def test_usage_newest_first(client, session, owner):
    token, company_id, user_id = owner
    other_token = await invite_user(client, session, {"Authorization": f"Bearer {token}"}, "staff@perm.example", "manager")
    _, other_id = _claims(other_token)
    now = datetime.now(timezone.utc)
    for minutes in (30, 20):
        await _question(session, company_id, user_id, credits=1, when=now - timedelta(minutes=minutes))
    await _question(session, company_id, other_id, credits=1, when=now - timedelta(minutes=5))
    await session.commit()
    rows = (await _usage(client, token)).json()["users"]
    assert [(r["user_id"], r["query_count"]) for r in rows] == [(str(other_id), 1), (str(user_id), 2)]


async def test_usage_period_all_counts_earlier_months(client, session, owner):
    token, company_id, user_id = owner
    await _question(session, company_id, user_id, credits=1, when=datetime.now(timezone.utc) - timedelta(days=70))
    await _question(session, company_id, user_id, credits=1)
    await session.commit()

    month = (await _usage(client, token)).json()
    assert [r["query_count"] for r in month["users"]] == [1]
    assert month["period"]["kind"] == "month"
    tz = (await client.get("/companies/me", headers={"Authorization": f"Bearer {token}"})).json()["settings"].get("timezone")
    assert month["period"]["start"] == business_today(tz).replace(day=1).isoformat()

    every = (await _usage(client, token, period="all")).json()
    assert [r["query_count"] for r in every["users"]] == [2]
    assert every["period"] == {"kind": "all", "start": None}


async def test_usage_period_rejects_unknown_value(client, owner):
    token, _, _ = owner
    r = await _usage(client, token, period="year")
    assert r.status_code == 422, r.text


async def test_usage_refused_without_manage_users(client, session, owner):
    token, _, _ = owner
    viewer = await invite_user(client, session, {"Authorization": f"Bearer {token}"}, "viewer@perm.example", "viewer")
    r = await _usage(client, viewer)
    assert r.status_code == 403, r.text


async def test_timed_out_question_asked_again_gets_its_answer(client, owner, monkeypatch):
    """A question whose answer did not arrive in time ends failed and is not sent again
    on its own; asked again under the same question id, the service returns the answer
    it kept and the app adds no question."""
    token, _, _ = owner
    sent = _relay(monkeypatch, [
        httpx.ReadTimeout("slow"),
        _answer(credits=0),
    ])
    conv = await _conversation(client, token)
    question = uuid.uuid4()

    first, failed = await _asked(client, token, conv, question)
    assert (first["message_id"], first["asked_again"]) == (str(question), False)
    assert (failed["error_code"], failed["retryable"], failed["new_question"]) == ("timeout", True, False)
    assert sent == [question]

    again, reply = await _asked(client, token, conv, question)
    assert (again["message_id"], again["asked_again"]) == (str(question), True)
    assert reply["content"] == "Open Contacts and choose New."
    assert sent == [question, question]
    thread = await _messages(client, token, conv)
    assert [m["role"] for m in thread] == ["user", "assistant", "assistant"]
    assert thread[0]["id"] == str(question)


async def test_answered_question_returns_its_answer_without_asking(client, owner, monkeypatch):
    token, _, _ = owner
    sent = _relay(monkeypatch, [_answer()])
    conv = await _conversation(client, token)
    question = uuid.uuid4()
    _, reply = await _asked(client, token, conv, question)
    again = await _ask(client, token, conv, question)
    assert again.status_code == 200, again.text
    assert sent == [question]
    assert again.json()["answer"] == reply["content"] and again.json()["asked_again"] is True
    assert len(await _messages(client, token, conv)) == 2
    assert _counts((await _usage(client, token)).json()["users"]) == [(1, 1)]


def _refused_as_operation_conflict(r) -> None:
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "operation_conflict"


async def test_delayed_repeat_after_a_later_question_returns_the_first_answer(client, owner, monkeypatch):
    """Question A is answered under id X, then question B is asked. A delayed copy of
    the send of A under X arrives: it returns A's answer without asking the AI service
    again, and nothing is added or charged."""
    token, _, _ = owner
    sent = _relay(monkeypatch, [_answer("Answer A"), _answer("Answer B")])
    conv = await _conversation(client, token)
    x, y = uuid.uuid4(), uuid.uuid4()
    await _asked(client, token, conv, x, query="question A")
    await _asked(client, token, conv, y, query="question B")

    late = await _ask(client, token, conv, x, query="question A")
    assert late.status_code == 200, late.text
    assert (late.json()["answer"], late.json()["message_id"], late.json()["asked_again"]) == (
        "Answer A", str(x), True)
    assert sent == [x, y]
    assert len(await _messages(client, token, conv)) == 4
    assert _counts((await _usage(client, token)).json()["users"]) == [(2, 2)]


@pytest.mark.parametrize("change", ["text", "files", "conversation"])
async def test_question_id_reused_for_another_question_is_refused(client, owner, monkeypatch, change):
    """An id already naming a question is never given to a different one: other text,
    other files or another conversation under it is refused, and nothing is asked,
    stored or charged."""
    token, _, _ = owner
    sent = _relay(monkeypatch, [_answer()])
    conv = await _conversation(client, token)
    question = uuid.uuid4()
    # Uploaded before the question: the upload rolls back the test's one shared
    # session, which would take the job's stored answer with it.
    upload = await client.post("/ai/upload", headers={"Authorization": f"Bearer {token}"},
                               files={"files": ("rows.csv", b"a,b\n1,2\n", "text/csv")})
    assert upload.status_code == 201, upload.text
    await _asked(client, token, conv, question)

    if change == "text":
        r = await _ask(client, token, conv, question, query="customer?")
    elif change == "conversation":
        r = await _ask(client, token, await _conversation(client, token), question)
    else:
        r = await client.post(
            f"/ai/conversations/{conv}/query", headers={"Authorization": f"Bearer {token}"},
            json={"query": "supplier?", "operation_id": str(question),
                  "file_ids": upload.json()["file_ids"]},
        )
    _refused_as_operation_conflict(r)
    assert sent == [question]
    assert len(await _messages(client, token, conv)) == 2
    assert _counts((await _usage(client, token)).json()["users"]) == [(1, 1)]


@pytest.mark.parametrize("code", ["outcome_unknown", "operation_expired", "operation_mismatch"])
async def test_question_id_the_service_will_not_take_again_is_refused(client, owner, monkeypatch, code):
    """After the AI service refuses an id for good, or reports its outcome unknown (final
    for that id), sending it again is refused with a conflict asking for a new question
    without calling the service; it is never asked again under a substituted id."""
    token, _, _ = owner
    sent = _relay(monkeypatch, [RelayError(code, "refused", status=409)])
    conv = await _conversation(client, token)
    question = uuid.uuid4()
    _, failed = await _asked(client, token, conv, question)
    assert (failed["error_code"], failed["retryable"], failed["new_question"]) == (code, False, True)

    _refused_as_operation_conflict(await _ask(client, token, conv, question))
    assert sent == [question]
    assert [m["role"] for m in await _messages(client, token, conv)] == ["user", "assistant"]


async def test_failed_question_followed_by_another_is_not_asked_again(client, owner, monkeypatch):
    """A question that failed and was followed by a later question is closed: a delayed
    send of it is refused, never run after the later question."""
    token, _, _ = owner
    sent = _relay(monkeypatch, [RelayError("service_unavailable", "down", status=503), _answer("Answer B")])
    conv = await _conversation(client, token)
    x, y = uuid.uuid4(), uuid.uuid4()
    _, failed = await _asked(client, token, conv, x, query="question A")
    assert failed["retryable"] is True
    await _asked(client, token, conv, y, query="question B")

    _refused_as_operation_conflict(await _ask(client, token, conv, x, query="question A"))
    assert sent == [x, y]
    assert len(await _messages(client, token, conv)) == 4


async def test_question_without_an_id_is_a_new_question(client, owner, monkeypatch):
    """A caller that sends no id still gets an answer, under an id the app chooses."""
    token, _, _ = owner
    sent = _relay(monkeypatch, [_answer(), _answer()])
    conv = await _conversation(client, token)
    for _ in range(2):
        r = await client.post(f"/ai/conversations/{conv}/query",
                              headers={"Authorization": f"Bearer {token}"}, json={"query": "supplier?"})
        assert r.status_code == 202 and r.json()["asked_again"] is False, r.text
        assert (await _settled(client, token, conv, r.json()["message_id"]))["state"] == "answered"
    assert len(set(sent)) == 2
    assert len(await _messages(client, token, conv)) == 4


async def test_refusal_is_kept_in_the_thread(client, session, owner, monkeypatch):
    """A refused question stays in the thread with the refusal, so a reloaded page
    explains it; nothing is counted when nothing was charged: the question awaits
    confirmation until the service reports it was never charged."""
    token, _, _ = owner
    refusal = {"code": "quota_exceeded", "message": "No questions left this month."}
    _relay(monkeypatch, [HTTPException(status_code=402, detail=refusal)])
    conv = await _conversation(client, token)
    question = uuid.uuid4()
    await _asked(client, token, conv, question)
    assert (await _state(client, token, conv, question))["state"] == "failed"

    user_msg, reply = await _messages(client, token, conv)
    assert user_msg["id"] == str(question) and user_msg["content"] == "supplier?"
    assert (reply["error"], reply["error_code"], reply["refusal_status"], reply["refusal"]) == (
        True, "quota_exceeded", 402, refusal)
    assert reply["content"] == refusal["message"]
    (row,) = (await _usage(client, token)).json()["users"]
    assert (row["query_count"], row["credits_used"], row["pending_count"]) == (0, 0, 1)

    async def _operations_status(ids):
        return [{"operation_id": str(i), "state": "not_charged", "credits": 0} for i in ids]

    monkeypatch.setattr(llm, "operations_status", _operations_status)
    await _age(session)
    await usage.reconcile_usage(session)
    assert (await _usage(client, token)).json()["users"] == []


async def test_question_sent_twice_at_once_is_refused_once(client, session, owner, monkeypatch):
    """Two sends of one new question race to store it under its id: the second is told
    the question is already being answered, and nothing is stored twice."""
    from celerp.ai import conversations

    token, _, _ = owner
    _relay(monkeypatch, [])
    conv = await _conversation(client, token)
    question = uuid.uuid4()
    real_add = conversations.add_message

    async def _add_after_the_other_send(sess, conversation_id, role, content, **kw):
        # The other send stores the question between this send's check and its save.
        await sess.execute(AIMessage.__table__.insert().values(
            id=question, conversation_id=uuid.UUID(conv), role="user", content="supplier?",
            credits_used=0, created_at=datetime.now(timezone.utc)))
        return await real_add(sess, conversation_id, role, content, **kw)

    monkeypatch.setattr(conversations, "add_message", _add_after_the_other_send)
    r = await _ask(client, token, conv, question)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "question_in_progress"


async def test_question_sent_again_while_it_is_answered_runs_once(client, owner, monkeypatch):
    """The same question sent again while its job is still being answered is told it
    is in progress: one model run, one stored answer, one charge. Once answered,
    sending it again returns that answer without asking."""
    token, _, _ = owner
    sent: list = []
    in_model, release = asyncio.Event(), asyncio.Event()

    async def _held(messages, *, tools=None, reservation_id=None, operation_id=None, **_kw):
        sent.append(operation_id)
        in_model.set()
        await release.wait()
        return _answer()

    set_session_token("s" * 64)
    monkeypatch.setattr(service, "complete", _held)
    monkeypatch.setattr(service, "compile_agent_capabilities", lambda app, settings, role=None: {})
    conv = await _conversation(client, token)
    question = uuid.uuid4()

    first = await _ask(client, token, conv, question)
    assert first.status_code == 202, first.text
    await asyncio.wait_for(in_model.wait(), 10)
    second = await _ask(client, token, conv, question)
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["code"] == "question_in_progress"
    release.set()
    reply = (await _settled(client, token, conv, question))["message"]
    assert sent == [question]
    thread = await _messages(client, token, conv)
    assert [m["role"] for m in thread] == ["user", "assistant"]
    assert _counts((await _usage(client, token)).json()["users"]) == [(1, 1)]

    again = await _ask(client, token, conv, question)
    assert (again.status_code, again.json()["asked_again"], again.json()["answer"]) == (
        200, True, reply["content"])
    assert sent == [question]
    assert len(await _messages(client, token, conv)) == 2


@pytest.mark.parametrize("window", ["not_yet_stored", "already_answered"])
async def test_repeat_send_in_a_read_window_runs_once(real_client, real_engine, drained_jobs, monkeypatch, window):
    """A repeat that reads the question just as another write for it moves on is still
    one model run and one charge. The repeat reads under the company lock, so the other
    write waits for it: the first send of a question nothing stored yet, or the job's
    answer to a question stored but not answered. The repeat acts on what it read, and
    the other write then sees what the repeat did. Both windows are forced with events
    and the database's own lock wait, not sleeps. On real connections: a refused insert
    rolls back only its own transaction, as in production."""
    from celerp.ai import conversations

    monkeypatch.setattr(jobs, "SessionLocal", maker(real_engine))
    client = real_client
    token = await register_admin(client)
    sent: list = []
    in_model, release = asyncio.Event(), asyncio.Event()

    async def _blocked(messages, *, tools=None, reservation_id=None, operation_id=None, **_kw):
        sent.append(operation_id)
        if len(sent) == 1:
            in_model.set()
            await release.wait()
        return _answer()

    set_session_token("s" * 64)
    monkeypatch.setattr(service, "complete", _blocked)
    monkeypatch.setattr(service, "compile_agent_capabilities", lambda app, settings, role=None: {})
    conv = await _conversation(client, token)
    question = uuid.uuid4()
    real_find = conversations.find_question
    other: list[asyncio.Task] = []
    if window == "already_answered":
        other.append(asyncio.ensure_future(_ask(client, token, conv, question)))
        await in_model.wait()

    async def _read_while_another_write_moves_on(*args, **kw):
        if window == "not_yet_stored" and other:
            return await real_find(*args, **kw)     # the other send, after the repeat
        read = await real_find(*args, **kw)
        if window == "not_yet_stored":
            other.append(asyncio.ensure_future(_ask(client, token, conv, question)))
        else:
            release.set()
        await until_a_lock_waits(real_engine)
        return read

    monkeypatch.setattr(conversations, "find_question", _read_while_another_write_moves_on)
    repeat = await _ask(client, token, conv, question)
    release.set()
    first = await other[0]
    reply = (await _settled(client, token, conv, question))["message"]
    started, refused = (repeat, first) if window == "not_yet_stored" else (first, repeat)
    assert started.status_code == 202, started.text
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "question_in_progress"
    assert reply["role"] == "assistant"
    assert sent == [question]
    assert [m["role"] for m in await _messages(client, token, conv)] == ["user", "assistant"]
    assert _counts((await _usage(client, token)).json()["users"]) == [(1, 1)]


async def test_one_off_query_is_counted_and_cites_help(client, owner, monkeypatch):
    token, _, user_id = owner
    calls: list = []

    async def _run_agent(**kw):
        calls.append(kw["operation_id"])
        return AgentResult(answer="See the help page.", model_used="test-model", tools_called=[],
                           pending_actions=[], credits=1, charge_known=True, help_pages=["contacts.add"])

    set_session_token("s" * 64)
    monkeypatch.setattr(service, "run_agent", _run_agent)
    headers = {"Authorization": f"Bearer {token}"}
    operation = uuid.uuid4()
    r = await client.post("/ai/query", headers=headers, json={"query": "supplier?", "operation_id": str(operation)})
    assert r.status_code == 200, r.text
    assert (r.json()["answer"], r.json()["help_pages"]) == ("See the help page.", ["contacts.add"])
    assert calls == [operation]
    assert _counts((await _usage(client, token)).json()["users"]) == [(1, 1)]

    async def _lost(**kw):
        return AgentResult(answer="", model_used="test-model", tools_called=[], pending_actions=[],
                           credits=1, charge_known=True, error="lost", error_code="outcome_unknown")

    monkeypatch.setattr(service, "run_agent", _lost)
    r = await client.post("/ai/query", headers=headers, json={"query": "customer?"})
    assert r.status_code == 502, r.text
    assert _counts((await _usage(client, token)).json()["users"]) == [(2, 2)]


async def test_module_query_is_counted(client, session, drained_jobs, monkeypatch):
    from celerp.modules.api import ai_query
    from test_helpers import seed_member

    monkeypatch.setattr("celerp.session_gate.get_session_token", lambda: "session-1")

    async def _run_agent(**kw):
        return AgentResult(answer="ok", model_used="test", tools_called=[], pending_actions=[],
                           credits=1, charge_known=False)

    monkeypatch.setattr(service, "run_agent", _run_agent)
    company_id, user_id = await seed_member(session)
    async with signed_request(session, company_id, user_id):
        out = await ai_query(query="hello", company_id=str(company_id), session_token="session-1",
                             db_session=session)
    assert (out["status"], out["answer"]) == ("completed", "ok")
    rows = (await session.execute(select(AIUsage).where(AIUsage.company_id == company_id))).scalars().all()
    assert [(r.user_id, r.credits) for r in rows] == [(user_id, 1)]


async def test_module_query_that_timed_out_or_failed_is_not_sent_again(client, session, drained_jobs, monkeypatch):
    """A module's question that timed out after it was sent, whose outcome the service
    reports unknown, or that fails for another reason ends failed with its reason and
    is never sent again on its own: the service would only answer the same id again,
    and asking again is the caller's choice. Each question has its usage row from
    before it left."""
    from celerp.modules.api import ai_query
    from test_helpers import seed_member

    monkeypatch.setattr("celerp.session_gate.get_session_token", lambda: "session-1")
    sent = _relay(monkeypatch, [
        httpx.ReadTimeout("slow"),
        RelayError("outcome_unknown", "lost", status=503, credits=1),
        RelayError("service_unavailable", "down", status=503),
    ])
    company_id, user_id = await seed_member(session)
    async with signed_request(session, company_id, user_id):
        outs = [await ai_query(query=q, company_id=str(company_id), session_token="session-1", db_session=session)
                for q in ("slow", "lost", "down")]
    assert [o["status"] for o in outs] == ["failed"] * 3
    assert all(o["answer"] for o in outs)
    assert len(set(sent)) == 3
    rows = (await session.execute(select(AIUsage).where(AIUsage.company_id == company_id))).scalars().all()
    assert sorted((str(r.operation_id), r.credits) for r in rows) == sorted(
        [(str(sent[0]), 0), (str(sent[1]), 1), (str(sent[2]), 0)])


async def test_refusal_after_a_charged_turn_is_counted(client, owner, monkeypatch):
    """The service charges a question's first turn and then refuses the continuation:
    the charged credit is counted on the conversation and the one-off paths alike."""
    token, _, _ = owner
    tool_turn = ModelResult(
        message={"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "no_such_tool", "arguments": "{}"}}]},
        model_used="test-model", usage={"credits": 1}, reservation_id="r", remaining=10, operation_credits=1)
    refusal = {"code": "quota_exceeded", "message": "No questions left this month."}
    _relay(monkeypatch, [tool_turn, HTTPException(status_code=402, detail=refusal),
                         tool_turn, HTTPException(status_code=402, detail=refusal)])
    conv = await _conversation(client, token)
    _, reply = await _asked(client, token, conv, uuid.uuid4())
    assert (reply["refusal_status"], reply["refusal"]) == (402, refusal)
    assert _counts((await _usage(client, token)).json()["users"]) == [(1, 1)]

    r = await client.post("/ai/query", headers={"Authorization": f"Bearer {token}"}, json={"query": "customer?"})
    assert r.status_code == 402, r.text
    assert _counts((await _usage(client, token)).json()["users"]) == [(2, 2)]
