"""Earlier answers of a thread reach the model as history only while their asker may
still see what they showed: the same replay check as a kept read (service._supports),
run on each earlier answer's kept reads when the question starts and again just before
each model step leaves. The first answer no longer supported, or whose reads no journal
tells, is left out with every later answer (a later one may repeat it); the questions
stay, the new question still runs and the thread the user sees is unchanged. Every test
asserts on the model requests as the AI service received them."""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

from sqlalchemy import delete, select, update

from ai_fake_cloud import answer, cloud, tool_turn, until  # noqa: F401
from celerp.ai import jobs, service, tools
from test_ai_job_journal_auth import AUTH, COST, QUERY, _ask_again, _costs, _name_shown_to_admins_only, _settings
from test_ai_usage import _conversation, _messages, _settled, owner  # noqa: F401

SHOWN = f"Its cost is {COST}."
REPEATED = f"As I said, it costs {COST}."


async def _thread(client, owner_token, asker_token, cloud, monkeypatch, *, shown=SHOWN, repeat=True) -> dict:
    """A thread of ``asker_token``: question A reads an item through the real item
    capability and answers ``shown``; question B (when ``repeat``) reads nothing and
    repeats the cost. Every later question answers "Noted." at once."""
    from celerp.main import app
    from test_helpers import real_agent_app
    r = await client.post("/items", headers=AUTH(owner_token), json={
        "status": "available", "sku": f"HIST-{uuid.uuid4().hex[:6]}", "sell_by": "piece", "name": "Costly",
        "cost_price": COST, "quantity": 1})
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]
    real_agent_app()
    monkeypatch.setattr(service, "compile_agent_capabilities", tools.compile_agent_capabilities)
    caps = tools.compile_agent_capabilities(app, {}, None)
    name = next(n for n, c in caps.items() if c["method"] == "GET" and c["path"].endswith("/items/{entity_id}"))
    a, b = uuid.uuid4(), uuid.uuid4()

    def _script(identity, body):
        if identity == str(a):
            return tool_turn((name, {"path": {"entity_id": item_id}}, "call-1"))
        if body.get("step_id"):
            return answer(shown)
        return answer(REPEATED if identity == str(b) else "Noted.")
    cloud.script = _script
    conv = await _conversation(client, asker_token)
    assert (await _ask_again(client, asker_token, conv, a))["message"]["content"] == shown
    if repeat:
        assert (await _ask_again(client, asker_token, conv, b))["message"]["content"] == REPEATED
    return {"conv": conv, "a": a, "b": b, "item_id": item_id}


async def _asked(client, token, conv, cloud) -> tuple[dict, str]:
    """A new question in ``conv``: how it ended and every model request it sent."""
    sends = len(cloud.bodies)
    state = await _ask_again(client, token, conv, uuid.uuid4())
    return state, json.dumps(cloud.bodies[sends:])


def _history_of(sent: str) -> list[dict]:
    """The earlier messages the question's first model request carried."""
    first = json.loads(sent)[0]["messages"]
    return first[1:-1]


async def test_an_answer_that_showed_costs_and_every_later_answer_are_left_out_once_costs_are_taken_away(
        client, owner, cloud, session, monkeypatch):
    """Direct and transitive: A showed the cost, B read nothing but repeated it. Both are
    left out; the questions stay; the new question is answered; the thread is unchanged."""
    token, company_id, _ = owner
    thread = await _thread(client, token, token, cloud, monkeypatch)
    await _settings(session, company_id, _costs(False))
    assert str(COST) not in (await client.get(f"/items/{thread['item_id']}", headers=AUTH(token))).text
    state, sent = await _asked(client, token, thread["conv"], cloud)
    assert (state["state"], state["message"]["content"]) == ("answered", "Noted.")
    assert str(COST) not in sent
    assert [m["role"] for m in _history_of(sent)] == ["user", "user"]
    shown = [m["content"] for m in await _messages(client, token, thread["conv"])]
    assert SHOWN in shown and REPEATED in shown                       # the thread the user sees


async def test_answers_are_kept_in_history_while_their_asker_may_still_see_what_they_showed(
        client, owner, cloud, session, monkeypatch):
    token, _, _ = owner
    thread = await _thread(client, token, token, cloud, monkeypatch)
    state, sent = await _asked(client, token, thread["conv"], cloud)
    assert state["state"] == "answered"
    assert [m["content"] for m in _history_of(sent) if m["role"] == "assistant"] == [SHOWN, REPEATED]


async def test_an_answer_that_showed_a_field_is_left_out_once_the_field_is_hidden(
        client, owner, cloud, session, monkeypatch):
    from test_helpers import invite_user
    token, company_id, _ = owner
    manager = await invite_user(client, session, AUTH(token), "manager@history.example", "manager")
    thread = await _thread(client, token, manager, cloud, monkeypatch, shown="It is called Costly.", repeat=False)
    await _settings(session, company_id, _name_shown_to_admins_only)
    assert "Costly" not in (await client.get(f"/items/{thread['item_id']}", headers=AUTH(manager))).text
    state, sent = await _asked(client, manager, thread["conv"], cloud)
    assert state["state"] == "answered" and "Costly" not in sent


async def test_an_answer_whose_reads_no_journal_tells_is_left_out_with_every_later_answer(
        client, owner, cloud, session, monkeypatch):
    """Fail closed on provenance: an answer stored without a question journal, and one
    whose kept read lost its replay contract, are left out with the answers after them,
    though the asker's authority did not change."""
    from celerp.ai.conversations import add_message
    from celerp.models.ai import AIJobStep
    token, _, _ = owner
    thread = await _thread(client, token, token, cloud, monkeypatch, repeat=False)
    step = (await session.execute(select(AIJobStep).where(
        AIJobStep.job_id == thread["a"], AIJobStep.key == "tool:call-1"))).scalar_one()
    await session.execute(update(AIJobStep).where(AIJobStep.id == step.id).values(
        result={k: v for k, v in step.result.items() if k != "replay"}))
    await session.commit()
    state, sent = await _asked(client, token, thread["conv"], cloud)
    assert state["state"] == "answered" and str(COST) not in sent

    other = await _conversation(client, token)
    await _ask_again(client, token, other, uuid.uuid4())
    await add_message(session, uuid.UUID(other), "assistant", f"Stored without a journal: {COST}.")
    await session.commit()
    await _ask_again(client, token, other, uuid.uuid4())               # a later answer, no reads
    state, sent = await _asked(client, token, other, cloud)
    assert state["state"] == "answered" and str(COST) not in sent
    assert [m["role"] for m in _history_of(sent)] == ["user", "assistant", "user"]


async def test_an_answer_the_thread_no_longer_holds_still_leaves_out_the_answers_after_it(
        client, owner, cloud, session, monkeypatch):
    """A pruned answer's question journal is kept: B, which repeated its cost, is left
    out once costs are taken away though A itself is gone from the thread."""
    from celerp.models.ai import AIBatchJob, AIMessage
    token, company_id, _ = owner
    thread = await _thread(client, token, token, cloud, monkeypatch)
    job = await session.get(AIBatchJob, thread["a"])
    await session.execute(delete(AIMessage).where(AIMessage.id == uuid.UUID(job.results["message_id"])))
    await session.commit()
    await _settings(session, company_id, _costs(False))
    state, sent = await _asked(client, token, thread["conv"], cloud)
    assert state["state"] == "answered" and str(COST) not in sent


async def test_costs_taken_away_while_the_question_waits_for_a_slot_leave_the_answers_out_of_what_it_sends(
        client, owner, cloud, session, monkeypatch):
    """History is checked again with fresh authority just before the step leaves: the
    step goes without the answers no longer shown, and the question is answered."""
    token, company_id, _ = owner
    thread = await _thread(client, token, token, cloud, monkeypatch)
    monkeypatch.setattr(jobs, "gate", jobs.ModelGate(1))
    sends, qid = len(cloud.bodies), uuid.uuid4()
    async with jobs.gate.slot("interactive"):
        r = await client.post(f"/ai/conversations/{thread['conv']}/query", headers=AUTH(token),
                              json={"query": QUERY, "operation_id": str(qid)})
        assert r.status_code == 202, r.text
        await until(lambda: jobs.gate._waiting["interactive"] == 1)
        await _settings(session, company_id, _costs(False))
    state = await _settled(client, token, thread["conv"], qid)
    assert (state["state"], state["message"]["content"]) == ("answered", "Noted.")
    assert len(cloud.bodies) == sends + 1 and str(COST) not in json.dumps(cloud.bodies[sends:])


async def test_company_memory_and_the_thread_title_never_carry_an_answer_left_out(
        client, owner, cloud, session, monkeypatch):
    """Memory rides in every question's system message but only users write it, and a
    thread's title is its first question: neither takes an answer's text, so neither
    brings back what history leaves out."""
    token, company_id, _ = owner
    r = await client.post("/ai/memory/notes", headers=AUTH(token), json={"content": "Prefer short answers."})
    assert r.status_code == 201, r.text
    thread = await _thread(client, token, token, cloud, monkeypatch)
    assert str(COST) not in (await client.get("/ai/memory", headers=AUTH(token))).text
    convs = (await client.get("/ai/conversations", headers=AUTH(token))).text
    assert str(COST) not in convs
    await _settings(session, company_id, _costs(False))
    state, sent = await _asked(client, token, thread["conv"], cloud)
    assert state["state"] == "answered" and "Prefer short answers." in sent and str(COST) not in sent


def test_history_keeps_questions_and_answers_up_to_the_first_one_not_supported(monkeypatch):
    """The cut, alone: a failed run's message read nothing and stays; the first answer
    not supported (or without a journal) and every later answer go; questions stay."""
    def msg(role, content):
        return SimpleNamespace(role=role, content=content, tools_called=None)
    good, bad = [("cap", {"ok": True})], [("cap", {"bad": True})]
    caps = {"cap": {}}
    supports = lambda capability, kept, role, settings: "ok" in kept  # noqa: E731
    earlier = [jobs.Earlier(msg("user", "q1")), jobs.Earlier(msg("assistant", "a1"), good),
               jobs.Earlier(msg("assistant", "failed"), []), jobs.Earlier(msg("user", "q2")),
               jobs.Earlier(msg("assistant", "a2"), bad), jobs.Earlier(msg("user", "q3")),
               jobs.Earlier(msg("assistant", "a3"), [])]
    monkeypatch.setattr(service, "_supports", supports)
    assert [m.content for m in service._shown_history(earlier, caps, "owner", {})] == [
        "q1", "a1", "failed", "q2", "q3"]
    unknown = [jobs.Earlier(msg("user", "q1")), jobs.Earlier(msg("assistant", "a1"), None),
               jobs.Earlier(None, good), jobs.Earlier(msg("assistant", "a2"), [])]
    assert [m.content for m in service._shown_history(unknown, caps, "owner", {})] == ["q1"]
    assert service._shown_history([jobs.Earlier(msg("assistant", "a"), [("gone", {"ok": True})])],
                                  caps, "owner", {}) == []
