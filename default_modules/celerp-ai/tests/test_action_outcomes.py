# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""What a confirmed change does when its outcome is not known.

A change whose route looks up its key (key store yes) is safe to send again: the
retry carries the same key and the route answers with the first result. A change
whose route has no key store cannot be sent again blindly: when the request left
and no answer came back (a lost connection, a 5xx, a crash between claim and
finalize), the outcome is unknown. The card says so and names the screen to
check; only an explicit Run again, after a warning, sends it a second time.

Every test drives the real app through the public conversation endpoints with
a scripted model, as test_workflows does. The number of locations or funds the
company holds is the count of times the route really ran.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
from sqlalchemy import select

from celerp.ai import conversations as ai_conversations
from celerp.ai import tools as ai_tools
from celerp.ai.conversations import EXECUTING_STALE_S
from celerp.models.ai import AIMessage
from test_helpers import asked_in_thread, perm_setup

from _fixtures import tool_call, tool_result
from test_workflows import ScriptedModel, _new_conversation, _op, agent_caps, ai_session  # noqa: F401

pytestmark = pytest.mark.usefixtures("tools_open_when_called", "model_answers_unscripted_runs")

_LOCATIONS = "/companies/me/locations"
# Location names are unique, so a create sent again after a run that did reach the
# route comes back with this refusal.
_NAME_TAKEN = "company.err_location_name_taken"


def _name_taken(answer: dict) -> bool:
    """Whether the card's answer, or one record of a for-each card, is that refusal."""
    errors = [answer.get("error") or {}] + [o.get("error") or {} for o in (answer.get("data") or {}).get("outcomes", [])]
    return any(e.get("message_key") == _NAME_TAKEN for e in errors)
_CHECK_SCREEN = "/settings/inventory"


def _call(name: str, arguments: dict, call_id: str):
    return lambda _messages: tool_result(calls=[tool_call(name, arguments, call_id)])


async def _location_count(client, headers) -> int:
    r = await client.get(_LOCATIONS, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    return len(body.get("items", body.get("locations", [])))


async def _fund_count(client, headers) -> int:
    r = await client.get("/accounting/funds", headers=headers)
    assert r.status_code == 200, r.text
    return len(r.json()["items"])


async def _propose(client, headers, scripted) -> tuple[str, str, str]:
    """Ask once; return (conversation id, message id, the one pending tool-call id)."""
    conv_id = await _new_conversation(client, headers)
    with patch("celerp.ai.service.complete", scripted):
        body = await asked_in_thread(client, headers, conv_id, {"query": "please do it"})
    assert len(body["pending_actions"]) == 1, body
    return conv_id, body["id"], body["pending_actions"][0]["id"]


async def _propose_location(client, headers, caps, name: str = "Back room"):
    op = _op(caps, "POST", _LOCATIONS)
    return await _propose(client, headers, ScriptedModel(
        _call(op, {"body": {"name": name, "type": "warehouse"}}, "loc1")))


async def _propose_locations_for_each(client, headers, caps):
    """One propose_for_each card over two location creates."""
    op = _op(caps, "POST", _LOCATIONS)
    return await _propose(client, headers, ScriptedModel(_call("propose_for_each", {
        "tool": op,
        "records": [{"body": {"name": "Shelf A", "type": "warehouse"}},
                    {"body": {"name": "Shelf B", "type": "warehouse"}}],
    }, "each1")))


def _lost_after_sending(calls: list):
    """execute_agent_capability that runs the route, then loses its answer.
    ``calls`` records the identity each run was sent under."""
    real = ai_tools.execute_agent_capability

    async def lost(app, auth, capability, arguments, identity, *a, **kw):
        calls.append(identity)
        await real(app, auth, capability, arguments, identity, *a, **kw)
        return {"ok": False, "status": 0, "data": None, "error": None}
    return lost


def _counting(calls: list, *, lose_index: int | None = None):
    """execute_agent_capability that records identities; the run at ``lose_index``
    reaches the route and then loses its answer."""
    real = ai_tools.execute_agent_capability

    async def run(app, auth, capability, arguments, identity, *a, **kw):
        calls.append(identity)
        result = await real(app, auth, capability, arguments, identity, *a, **kw)
        if lose_index is not None and len(calls) - 1 == lose_index:
            return {"ok": False, "status": 502, "data": None, "error": None}
        return result
    return run


async def _confirm(client, headers, conv_id, message_id, tool_call_id, **extra):
    return await client.post(
        f"/ai/conversations/{conv_id}/confirm", headers=headers,
        json={"message_id": message_id, "tool_call_id": tool_call_id, **extra},
    )


async def _confirm_all(client, headers, conv_id, message_id):
    return await client.post(
        f"/ai/conversations/{conv_id}/confirm-all", headers=headers,
        json={"message_id": message_id},
    )


async def _record(session, message_id, tool_call_id) -> dict:
    session.expire_all()
    msg = (await session.execute(select(AIMessage).where(AIMessage.id == message_id))).scalar_one()
    return next(r for r in msg.tools_called if r.get("id") == tool_call_id)


async def _crash_after_running(client, headers, session, conv_id, message_id, tool_call_id):
    """Run the route, then lose the process before finalize: the claim stays
    ``executing`` and is backdated past the stale limit."""
    async def no_finalize(*_a, **_kw):
        return None
    with patch.object(ai_conversations, "finalize_tool_call", no_finalize):
        await _confirm(client, headers, conv_id, message_id, tool_call_id)
    session.expire_all()
    msg = (await session.execute(select(AIMessage).where(AIMessage.id == message_id))).scalar_one()
    since = (datetime.now(timezone.utc) - timedelta(seconds=EXECUTING_STALE_S + 60)).isoformat()
    records = []
    for r in msg.tools_called:
        if r.get("id") == tool_call_id:
            assert r["status"] == "executing", r
            r = {**r, "executing_since": since}
        records.append(r)
    msg.tools_called = records
    await session.commit()


@pytest.mark.asyncio
async def test_keyed_retry_reuses_the_key(client, session, agent_caps, ai_session):
    """A fund create whose answer was lost is retryable; the retry carries the same
    key, so the company ends with one fund and the second answer equals the first."""
    ctx = await perm_setup(client, session)
    admin = ctx["admin_h"]
    on = await client.put("/accounting/funds/setting", headers=admin, json={"enabled": True})
    assert on.status_code == 200, on.text
    before = await _fund_count(client, admin)
    op = _op(agent_caps, "POST", "/accounting/funds")
    assert "request_key" not in agent_caps[op]["tool"]["function"]["parameters"]["properties"]["body"].get("properties", {})
    conv_id, message_id, call_id = await _propose(client, admin, ScriptedModel(
        _call(op, {"body": {"name": "Building fund"}}, "fund1")))

    identities: list = []
    with patch.object(ai_tools, "execute_agent_capability", _lost_after_sending(identities)):
        first = await _confirm(client, admin, conv_id, message_id, call_id)
    assert first.status_code == 200, first.text
    assert first.json()["action_status"] == "retryable"
    assert (await _record(session, message_id, call_id))["status"] == "retryable"
    assert await _fund_count(client, admin) == before + 1

    with patch.object(ai_tools, "execute_agent_capability", _counting(identities)):
        second = await _confirm(client, admin, conv_id, message_id, call_id)
    assert second.status_code == 200, second.text
    assert second.json()["ok"] is True
    assert identities[0] == identities[1]
    assert await _fund_count(client, admin) == before + 1
    funds = (await client.get("/accounting/funds", headers=admin)).json()["items"]
    assert second.json()["data"]["id"] in {f["id"] for f in funds}


@pytest.mark.asyncio
async def test_unkeyed_ambiguous_claim_shows_outcome_unknown(client, session, agent_caps, ai_session):
    """A location create whose answer was lost is unknown, not retryable: the card
    names the screen to check, plain confirm and confirm-all send nothing, and the
    history tells the model the outcome is unknown."""
    ctx = await perm_setup(client, session)
    admin = ctx["admin_h"]
    before = await _location_count(client, admin)
    conv_id, message_id, call_id = await _propose_location(client, admin, agent_caps)

    identities: list = []
    with patch.object(ai_tools, "execute_agent_capability", _lost_after_sending(identities)):
        first = await _confirm(client, admin, conv_id, message_id, call_id)
    assert first.json()["action_status"] == "unknown", first.text
    record = await _record(session, message_id, call_id)
    assert record["status"] == "unknown" and record["keyed"] is False
    assert await _location_count(client, admin) == before + 1

    with patch.object(ai_tools, "execute_agent_capability", _counting(identities)):
        again = await _confirm(client, admin, conv_id, message_id, call_id)
        assert again.status_code == 409
        assert again.json()["detail"]["code"] == "action_outcome_unknown"
        assert _CHECK_SCREEN in again.text
        everything = await _confirm_all(client, admin, conv_id, message_id)
    assert [r["action_status"] for r in everything.json()["results"]] == ["unknown"]
    assert len(identities) == 1
    assert await _location_count(client, admin) == before + 1

    conv = (await client.get(f"/ai/conversations/{conv_id}", headers=admin)).json()
    pending = [a for m in conv["messages"] for a in (m.get("pending_actions") or [])]
    assert [a["status"] for a in pending] == ["unknown"]
    msg = (await session.execute(select(AIMessage).where(AIMessage.id == message_id))).scalar_one()
    history = ai_conversations.build_history_context([msg])
    assert "[action outcome unknown:" in history[-1]["content"]


@pytest.mark.asyncio
async def test_run_again_after_warning_runs_once(client, session, agent_caps, ai_session):
    """Run again on an unknown record runs exactly once more, under its own identity, and
    the card takes the route's own answer: the first run did add the location, so the
    route refuses the same name again. The ended record then refuses a second Run again."""
    ctx = await perm_setup(client, session)
    admin = ctx["admin_h"]
    before = await _location_count(client, admin)
    conv_id, message_id, call_id = await _propose_location(client, admin, agent_caps)
    identities: list = []
    with patch.object(ai_tools, "execute_agent_capability", _lost_after_sending(identities)):
        await _confirm(client, admin, conv_id, message_id, call_id)

    plain = await _confirm(client, admin, conv_id, message_id, call_id)
    assert plain.status_code == 409
    assert plain.json()["detail"]["code"] == "action_outcome_unknown"

    with patch.object(ai_tools, "execute_agent_capability", _counting(identities)):
        again = await _confirm(client, admin, conv_id, message_id, call_id, run_again=True)
        assert again.status_code == 200, again.text
        assert _name_taken(again.json()), again.text
        twice = await _confirm(client, admin, conv_id, message_id, call_id, run_again=True)
    assert twice.status_code == 409
    assert twice.json()["detail"]["code"] == "action_not_pending"
    assert len(identities) == 2
    assert identities[1] == identities[0]
    assert (await _record(session, message_id, call_id))["status"] == "failed"
    assert await _location_count(client, admin) == before + 1


@pytest.mark.asyncio
async def test_unknown_in_pending_dismiss_and_dependent_stop(client, session, agent_caps, ai_session):
    """A stale unkeyed claim is listed as unknown and dismiss clears it; in a
    propose_for_each card only the interrupted record is offered for Run again."""
    ctx = await perm_setup(client, session)
    admin = ctx["admin_h"]
    conv_id, message_id, call_id = await _propose_location(client, admin, agent_caps)
    await _crash_after_running(client, admin, session, conv_id, message_id, call_id)
    conv = (await client.get(f"/ai/conversations/{conv_id}", headers=admin)).json()
    pending = [a for m in conv["messages"] for a in (m.get("pending_actions") or [])]
    assert [a["status"] for a in pending] == ["unknown"]
    r = await client.post(f"/ai/conversations/{conv_id}/dismiss", headers=admin,
                          json={"message_id": message_id, "tool_call_id": call_id})
    assert r.status_code == 200, r.text
    assert (await _record(session, message_id, call_id))["status"] == "dismissed"

    before = await _location_count(client, admin)
    conv_id, message_id, call_id = await _propose_locations_for_each(client, admin, agent_caps)
    identities: list = []
    with patch.object(ai_tools, "execute_agent_capability", _counting(identities, lose_index=1)):
        first = await _confirm(client, admin, conv_id, message_id, call_id)
    assert first.json()["action_status"] == "unknown", first.text
    record = await _record(session, message_id, call_id)
    assert [o["status"] for o in record["outcomes"]] == ["completed", "unknown"]
    with patch.object(ai_tools, "execute_agent_capability", _counting(identities)):
        again = await _confirm(client, admin, conv_id, message_id, call_id, run_again=True)
    assert again.status_code == 200, again.text
    assert _name_taken(again.json()), again.text
    assert len(identities) == 3
    assert await _location_count(client, admin) == before + 2


_SOURCES = ("stale_claim", "lost_answer", "for_each_record")
_READERS = ("confirm", "confirm_all", "dismiss")


@pytest.mark.asyncio
@pytest.mark.parametrize("source", _SOURCES)
@pytest.mark.parametrize("reader", _READERS)
async def test_ambiguous_outcome_never_reruns_without_run_again(
    client, session, agent_caps, ai_session, source, reader,
):
    """Every way an outcome becomes unknown, read by every claiming reader: the route
    ran once (the first run) and no reader sends it again. Run again on a fresh
    unknown record sends it exactly once more."""
    ctx = await perm_setup(client, session)
    admin = ctx["admin_h"]

    async def unknown_record():
        identities: list = []
        if source == "for_each_record":
            ids = await _propose_locations_for_each(client, admin, agent_caps)
            with patch.object(ai_tools, "execute_agent_capability", _counting(identities, lose_index=1)):
                await _confirm(client, admin, *ids)
        else:
            ids = await _propose_location(client, admin, agent_caps, name=f"Room {source}")
            if source == "stale_claim":
                await _crash_after_running(client, admin, session, *ids)
            else:
                with patch.object(ai_tools, "execute_agent_capability", _counting(identities, lose_index=0)):
                    await _confirm(client, admin, *ids)
        return ids

    conv_id, message_id, call_id = await unknown_record()
    ran = await _location_count(client, admin)
    calls: list = []
    with patch.object(ai_tools, "execute_agent_capability", _counting(calls)):
        if reader == "confirm":
            r = await _confirm(client, admin, conv_id, message_id, call_id)
            assert r.status_code == 409
            assert r.json()["detail"]["code"] == "action_outcome_unknown"
            assert _CHECK_SCREEN in r.text
        elif reader == "confirm_all":
            r = await _confirm_all(client, admin, conv_id, message_id)
            assert [x["action_status"] for x in r.json()["results"]] == ["unknown"]
            assert _CHECK_SCREEN in r.text
        else:
            r = await client.post(f"/ai/conversations/{conv_id}/dismiss", headers=admin,
                                  json={"message_id": message_id, "tool_call_id": call_id})
            assert r.status_code == 200, r.text
            assert (await _record(session, message_id, call_id))["status"] == "dismissed"
    assert calls == []
    assert await _location_count(client, admin) == ran

    conv_id, message_id, call_id = await unknown_record()
    ran = await _location_count(client, admin)
    with patch.object(ai_tools, "execute_agent_capability", _counting(calls)):
        r = await _confirm(client, admin, conv_id, message_id, call_id, run_again=True)
    assert r.status_code == 200, r.text
    assert len(calls) == 1
    assert _name_taken(r.json()), r.text
    assert await _location_count(client, admin) == ran


class _Stopped(Exception):
    """The process stopping in the middle of a confirmed run."""


def _stop_at(calls: list, k: int):
    """execute_agent_capability that runs ``k`` records, then stops before the next one."""
    real = ai_tools.execute_agent_capability

    async def run(app, auth, capability, arguments, identity, *a, **kw):
        if len(calls) == k:
            raise _Stopped
        calls.append(identity)
        return await real(app, auth, capability, arguments, identity, *a, **kw)
    return run


async def _backdate(session, message_id, tool_call_id) -> None:
    """Age a claim left ``executing`` past the stale limit."""
    session.expire_all()
    msg = (await session.execute(select(AIMessage).where(AIMessage.id == message_id))).scalar_one()
    since = (datetime.now(timezone.utc) - timedelta(seconds=EXECUTING_STALE_S + 60)).isoformat()
    msg.tools_called = [{**r, "executing_since": since} if r.get("id") == tool_call_id else r
                        for r in msg.tools_called]
    await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("k", [0, 1, 2])
async def test_for_each_stopped_after_k_records_runs_only_the_rest(client, session, agent_caps, ai_session, k):
    """Each record keeps its outcome as it runs: after a stop only the record in flight is
    unknown, and Run again runs only the records that did not finish."""
    ctx = await perm_setup(client, session)
    admin = ctx["admin_h"]
    before = await _location_count(client, admin)
    conv_id, message_id, call_id = await _propose_locations_for_each(client, admin, agent_caps)
    first: list = []

    async def no_finalize(*_a, **_kw):
        return None
    with patch.object(ai_tools, "execute_agent_capability", _stop_at(first, k)), \
            patch.object(ai_conversations, "finalize_tool_call", no_finalize):
        if k < 2:
            with pytest.raises(_Stopped):
                await _confirm(client, admin, conv_id, message_id, call_id)
        else:
            await _confirm(client, admin, conv_id, message_id, call_id)
    record = await _record(session, message_id, call_id)
    assert [o.get("status") for o in record["outcomes"]] == {0: ["executing", None], 1: ["completed", "executing"],
                                                             2: ["completed", "completed"]}[k]
    await _backdate(session, message_id, call_id)
    refused = await _confirm(client, admin, conv_id, message_id, call_id)
    assert refused.status_code == 409, refused.text

    again_ids: list = []
    with patch.object(ai_tools, "execute_agent_capability", _counting(again_ids)):
        again = await _confirm(client, admin, conv_id, message_id, call_id, run_again=True)
    assert again.status_code == 200 and again.json()["action_status"] == "completed", again.text
    assert [i.rsplit(":", 1)[1] for i in again_ids] == [str(i) for i in range(k, 2)]
    assert len({i.rsplit(":", 1)[0] for i in first + again_ids}) == 1
    assert await _location_count(client, admin) == before + 2


@pytest.mark.asyncio
async def test_run_again_on_a_keyed_record_never_runs_a_second_copy(client, session, agent_caps, ai_session):
    """Run again is refused on a keyed record; its plain retry carries the first key."""
    ctx = await perm_setup(client, session)
    admin = ctx["admin_h"]
    assert (await client.put("/accounting/funds/setting", headers=admin, json={"enabled": True})).status_code == 200
    before = await _fund_count(client, admin)
    op = _op(agent_caps, "POST", "/accounting/funds")
    conv_id, message_id, call_id = await _propose(client, admin, ScriptedModel(
        _call(op, {"body": {"name": "Building fund"}}, "fund1")))
    identities: list = []
    with patch.object(ai_tools, "execute_agent_capability", _lost_after_sending(identities)):
        await _confirm(client, admin, conv_id, message_id, call_id)
    assert (await _record(session, message_id, call_id))["status"] == "retryable"
    with patch.object(ai_tools, "execute_agent_capability", _counting(identities)):
        refused = await _confirm(client, admin, conv_id, message_id, call_id, run_again=True)
        assert refused.status_code == 409, refused.text
        retry = await _confirm(client, admin, conv_id, message_id, call_id)
    assert retry.status_code == 200 and retry.json()["action_status"] == "completed", retry.text
    assert identities == [identities[0]] * 2
    assert await _fund_count(client, admin) == before + 1


@pytest.mark.asyncio
async def test_outcomes_of_a_message_deleted_mid_run_are_dropped(session):
    """The conversation was deleted while its card ran: the outcomes have nowhere to go and
    the run goes on to its own result, never failing on the missing message."""
    import uuid

    await ai_conversations.note_outcomes(session, message_id=uuid.uuid4(), tool_call_id="t1",
                                         outcomes=[{"status": "executing"}])
