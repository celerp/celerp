# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Newly opened assistant tools, driven through the public conversation endpoints.

Adding a teammate makes a password the model never sees: the person who confirmed is
shown it once, and every later reader (the conversation, the history the model reads,
the ledger) holds a placeholder. Files attached to the chat reach a record through an
upload tool. Several changes in one request: the request carries on after the cards
are answered, a failed change stops the later changes to the same record, and many
records of one kind arrive as one card that states how many it changes.

A scripted model stands in for the provider, as in test_workflows.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select

from celerp.ai import tools as ai_tools
from celerp.ai.conversations import build_history_context
from celerp.models.ai import AIMessage
from celerp.models.company import User
from test_helpers import asked_in_thread, grant_permission, perm_setup, settled_question

from _fixtures import tool_call, tool_result
from test_action_outcomes import (
    _call, _confirm, _confirm_all, _crash_after_running, _location_count, _propose, _record,
)
from test_workflows import ScriptedModel, _new_conversation, _op, agent_caps, ai_session, xlsx_upload  # noqa: F401

_USERS = "/companies/me/users"
_LOCATIONS = "/companies/me/locations"
_GENERATED = "Generated-Once-Sentinel-7Q2"
_PLACEHOLDER = "(shown once to you)"


def text_result(content: str):
    return tool_result(content=content)


def _fixed_secret():
    return patch.object(ai_tools.secrets, "token_urlsafe", lambda _n=16: _GENERATED)


async def _propose_user(client, headers, caps, email: str):
    op = _op(caps, "POST", _USERS)
    assert "password" not in caps[op]["input_schema"]["properties"]["body"].get("properties", {})
    return await _propose(client, headers, ScriptedModel(
        _call(op, {"body": {"email": email, "name": "New teammate", "role": "operator"}}, "user1")))


async def _messages(session, conv_id) -> list[AIMessage]:
    session.expire_all()
    return list((await session.execute(
        select(AIMessage).where(AIMessage.conversation_id == uuid.UUID(conv_id))
        .order_by(AIMessage.created_at))).scalars().all())


async def _stored_text(session, conv_id) -> str:
    return " ".join(f"{m.content} {m.tools_called}" for m in await _messages(session, conv_id))


# -- Generated password -----------------------------------------------------------

@pytest.mark.asyncio
async def test_generated_password_never_reaches_model_and_shows_once(client, session, agent_caps, ai_session):
    ctx = await perm_setup(client, session)
    admin = ctx["admin_h"]
    conv_id, message_id, call_id = await _propose_user(client, admin, agent_caps, "teammate@example.com")
    with _fixed_secret():
        r = await _confirm(client, admin, conv_id, message_id, call_id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["shown_once"] == {"password": _GENERATED}
    assert _GENERATED not in str(body["data"])
    record = await _record(session, message_id, call_id)
    assert _GENERATED not in str(record) and _PLACEHOLDER in str(record["result"])
    login = await client.post("/auth/login", json={"email": "teammate@example.com", "password": _GENERATED})
    assert login.status_code == 200, login.text


@pytest.mark.asyncio
async def test_create_user_existing_email_shows_no_password(client, session, agent_caps, ai_session):
    """An email that already has a user links that person; they keep their own password,
    so the card shows none and the generated value is thrown away."""
    ctx = await perm_setup(client, session)
    session.add(User(id=uuid.uuid4(), email="known@example.com", name="Known", auth_hash="x", is_active=True))
    await session.commit()
    admin = ctx["admin_h"]
    conv_id, message_id, call_id = await _propose_user(client, admin, agent_caps, "known@example.com")
    with _fixed_secret():
        r = await _confirm(client, admin, conv_id, message_id, call_id)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert "shown_once" not in r.json()
    assert r.json()["data"]["linked"] is True
    assert _GENERATED not in r.text and _GENERATED not in await _stored_text(session, conv_id)


@pytest.mark.asyncio
async def test_generated_password_absent_from_history_model_and_ledger(client, session, agent_caps, ai_session):
    ctx = await perm_setup(client, session)
    admin = ctx["admin_h"]
    conv_id, message_id, call_id = await _propose_user(client, admin, agent_caps, "second@example.com")
    with _fixed_secret():
        assert (await _confirm(client, admin, conv_id, message_id, call_id)).status_code == 200
    reloaded = await client.get(f"/ai/conversations/{conv_id}", headers=admin)
    assert reloaded.status_code == 200 and _GENERATED not in reloaded.text
    assert _GENERATED not in await _stored_text(session, conv_id)
    history = build_history_context(await _messages(session, conv_id))
    assert _GENERATED not in str(history)
    ledger = await client.get("/ledger", headers=admin)
    assert ledger.status_code == 200 and _GENERATED not in ledger.text
    for entry in ledger.json().get("items", [])[:20]:
        one = await client.get(f"/ledger/{entry['id']}", headers=admin)
        assert _GENERATED not in one.text

    # An interrupted run is unknown and shows no password; Run again on the member who
    # now exists is refused by the route and still shows none.
    conv2, msg2, call2 = await _propose_user(client, admin, agent_caps, "third@example.com")
    with _fixed_secret():
        await _crash_after_running(client, admin, session, conv2, msg2, call2)
        plain = await _confirm(client, admin, conv2, msg2, call2)
        again = await _confirm(client, admin, conv2, msg2, call2, run_again=True)
    assert plain.status_code == 409 and plain.json()["detail"]["code"] == "action_outcome_unknown"
    assert again.json()["ok"] is False and "shown_once" not in again.json()
    assert _GENERATED not in plain.text + again.text + await _stored_text(session, conv2)


# -- A refusal that depends on the argument ---------------------------------------

@pytest.mark.asyncio
async def test_label_source_refused_by_argument_reaches_card(client, session, agent_caps, ai_session):
    """The label tools are shown to a role that can print some labels; asking for a kind
    of record the role cannot see returns the route's own refusal to the model."""
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "view_fixed_assets", "admin")
    op = _op(agent_caps, "GET", "/labels/sources/{source}")
    model = ScriptedModel(
        _call(op, {"path": {"source": "fa_asset"}}, "lbl1"),
        lambda _m: text_result("Done."),
    )
    conv_id = await _new_conversation(client, ctx["operator_h"])
    with patch("celerp.ai.service.complete", model):
        await asked_in_thread(client, ctx["operator_h"], conv_id, {"query": "asset tag templates"})
    fed = str(model.seen[-1])
    assert "Your role cannot see these records" in fed


# -- Upload ---------------------------------------------------------------------------

async def _contact(client, headers) -> str:
    r = await client.post("/crm/contacts", headers=headers, json={"name": "File holder"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def _propose_upload(client, headers, caps, contact_id, file_id):
    op = _op(caps, "POST", "/crm/contacts/{contact_id}/files")
    assert "file" not in caps[op]["input_schema"]["properties"].get("body", {}).get("properties", {})
    return await _propose(client, headers, ScriptedModel(
        _call(op, {"path": {"contact_id": contact_id}, "file_ids": [file_id]}, "up1")))


@pytest.mark.asyncio
async def test_upload_tool_posts_the_attached_file(client, session, agent_caps, ai_session, xlsx_upload):
    ctx = await perm_setup(client, session)
    admin = ctx["admin_h"]
    contact_id = await _contact(client, admin)
    file_id = await xlsx_upload(client, admin, {"Sheet": [["a"]]}, filename="terms.xlsx")
    conv_id, message_id, call_id = await _propose_upload(client, admin, agent_caps, contact_id, file_id)
    r = await _confirm(client, admin, conv_id, message_id, call_id)
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    got = (await client.get(f"/crm/contacts/{contact_id}", headers=admin)).json()
    assert "terms.xlsx" in str(got.get("files"))


@pytest.mark.asyncio
async def test_upload_tool_refuses_a_file_of_another_user(client, session, agent_caps, ai_session, xlsx_upload):
    ctx = await perm_setup(client, session)
    contact_id = await _contact(client, ctx["admin_h"])
    theirs = await xlsx_upload(client, ctx["manager_h"], {"Sheet": [["a"]]}, filename="theirs.xlsx")
    conv_id, message_id, call_id = await _propose_upload(client, ctx["admin_h"], agent_caps, contact_id, theirs)
    r = await _confirm(client, ctx["admin_h"], conv_id, message_id, call_id)
    assert r.json()["ok"] is False
    assert r.json()["error"]["code"] == "file_not_found"
    got = (await client.get(f"/crm/contacts/{contact_id}", headers=ctx["admin_h"])).json()
    assert "theirs.xlsx" not in str(got.get("files"))


@pytest.mark.asyncio
async def test_upload_tool_refuses_a_missing_file(client, session, agent_caps, ai_session):
    ctx = await perm_setup(client, session)
    contact_id = await _contact(client, ctx["admin_h"])
    conv_id, message_id, call_id = await _propose_upload(
        client, ctx["admin_h"], agent_caps, contact_id, str(uuid.uuid4()))
    r = await _confirm(client, ctx["admin_h"], conv_id, message_id, call_id)
    assert r.json()["ok"] is False
    assert r.json()["error"]["code"] == "file_not_found"


# -- Several changes in one request ------------------------------------------------

def _two_patches_and_a_create(caps, missing_id: str):
    patch_op = _op(caps, "PATCH", "/companies/me/locations/{location_id}")
    create_op = _op(caps, "POST", _LOCATIONS)
    return lambda _m: tool_result(calls=[
        tool_call(patch_op, {"path": {"location_id": missing_id}, "body": {"name": "First"}}, "p1"),
        tool_call(create_op, {"body": {"name": "Independent", "type": "warehouse"}}, "c1"),
        tool_call(patch_op, {"path": {"location_id": missing_id}, "body": {"address": "Second"}}, "p2"),
    ])


@pytest.mark.asyncio
async def test_confirm_all_stops_dependent_actions_at_first_failure(client, session, agent_caps, ai_session):
    ctx = await perm_setup(client, session)
    admin = ctx["admin_h"]
    conv_id = await _new_conversation(client, admin)
    with patch("celerp.ai.service.complete", ScriptedModel(_two_patches_and_a_create(agent_caps, str(uuid.uuid4())))):
        body = await asked_in_thread(client, admin, conv_id, {"query": "rename it twice"})
    r = await _confirm_all(client, admin, conv_id, body["id"])
    assert r.status_code == 200, r.text
    assert (await _record(session, body["id"], "p1"))["status"] == "failed"
    second = await _record(session, body["id"], "p2")
    assert second["status"] == "pending"
    assert "an earlier change to the same record failed" in str(second.get("error") or second.get("reason"))


@pytest.mark.asyncio
async def test_confirm_all_independent_failure_continues(client, session, agent_caps, ai_session):
    ctx = await perm_setup(client, session)
    admin = ctx["admin_h"]
    before = await _location_count(client, admin)
    conv_id = await _new_conversation(client, admin)
    with patch("celerp.ai.service.complete", ScriptedModel(_two_patches_and_a_create(agent_caps, str(uuid.uuid4())))):
        body = await asked_in_thread(client, admin, conv_id, {"query": "rename and add"})
    await _confirm_all(client, admin, conv_id, body["id"])
    assert (await _record(session, body["id"], "c1"))["status"] == "succeeded"
    assert await _location_count(client, admin) == before + 1


@pytest.mark.asyncio
async def test_confirm_resumes_the_request(client, session, agent_caps, ai_session):
    """Answering the last card carries the request on with the outcome, so the user does
    not have to ask again for the next step."""
    ctx = await perm_setup(client, session)
    admin = ctx["admin_h"]
    op = _op(agent_caps, "POST", _LOCATIONS)
    model = ScriptedModel(
        _call(op, {"body": {"name": "Annex", "type": "warehouse"}}, "a1"),
        lambda _m: text_result("The annex is set up."),
    )
    with patch("celerp.ai.service.complete", model):
        conv_id, message_id, call_id = await _propose(client, admin, model)
        r = await _confirm(client, admin, conv_id, message_id, call_id)
        assert r.status_code == 200, r.text
        job_id = r.json()["continued_job_id"]
        state = await settled_question(client, admin, conv_id, job_id)
    assert state["state"] == "done"
    assert "Annex" in str(model.seen[-1])
    assert "The annex is set up." in state["message"]["content"]


@pytest.mark.asyncio
async def test_dismiss_all_does_not_continue(client, session, agent_caps, ai_session):
    ctx = await perm_setup(client, session)
    admin = ctx["admin_h"]
    op = _op(agent_caps, "POST", _LOCATIONS)
    model = ScriptedModel(_call(op, {"body": {"name": "Never", "type": "warehouse"}}, "n1"))
    conv_id, message_id, _ = await _propose(client, admin, model)
    r = await client.post(f"/ai/conversations/{conv_id}/dismiss-all", headers=admin, json={"message_id": message_id})
    assert r.status_code == 200, r.text
    assert r.json().get("continued_job_id") is None
    assert len(model.seen) == 1


@pytest.mark.asyncio
async def test_twenty_record_ask_is_one_bulk_card(client, session, agent_caps, ai_session):
    """Twenty records of one kind arrive as one card that says how many it changes; a
    turn that proposes the same change twice is refused and pointed at propose_for_each."""
    ctx = await perm_setup(client, session)
    admin = ctx["admin_h"]
    op = _op(agent_caps, "POST", _LOCATIONS)
    records = [{"body": {"name": f"Bay {i}", "type": "warehouse"}} for i in range(20)]
    conv_id, message_id, call_id = await _propose(client, admin, ScriptedModel(
        _call("propose_for_each", {"tool": op, "records": records}, "bulk1")))
    card = await _record(session, message_id, call_id)
    assert len(card["arguments"]["records"]) == 20
    assert "This changes 20 records." in " ".join(card["warnings"])

    model = ScriptedModel(
        lambda _m: tool_result(calls=[
            tool_call(op, {"body": {"name": "One", "type": "warehouse"}}, "d1"),
            tool_call(op, {"body": {"name": "Two", "type": "warehouse"}}, "d2"),
        ]),
        lambda _m: text_result("Understood."),
    )
    conv2 = await _new_conversation(client, admin)
    with patch("celerp.ai.service.complete", model):
        body = await asked_in_thread(client, admin, conv2, {"query": "add two"})
    assert body["pending_actions"] == []
    assert "propose_for_each" in str(model.seen[-1])


@pytest.mark.asyncio
async def test_bulk_tool_card_states_the_selection_size(client, session, agent_caps, ai_session):
    ctx = await perm_setup(client, session)
    op = _op(agent_caps, "POST", "/items/bulk/expire")
    ids = [str(uuid.uuid4()) for _ in range(3)]
    field = next(iter(agent_caps[op]["input_schema"]["properties"]["body"]["properties"]))
    conv_id, message_id, call_id = await _propose(client, ctx["admin_h"], ScriptedModel(
        _call(op, {"body": {field: ids}}, "bx1")))
    card = await _record(session, message_id, call_id)
    assert "This changes 3 records." in " ".join(card["warnings"])


@pytest.mark.asyncio
async def test_bulk_tool_refuses_an_empty_selection(client, session, agent_caps, ai_session):
    ctx = await perm_setup(client, session)
    op = _op(agent_caps, "POST", "/items/bulk/expire")
    field = next(iter(agent_caps[op]["input_schema"]["properties"]["body"]["properties"]))
    model = ScriptedModel(_call(op, {"body": {field: []}}, "be1"), lambda _m: text_result("Nothing chosen."))
    conv_id = await _new_conversation(client, ctx["admin_h"])
    with patch("celerp.ai.service.complete", model):
        body = await asked_in_thread(client, ctx["admin_h"], conv_id, {"query": "expire none"})
    assert body["pending_actions"] == []
    assert "empty_selection" in str(model.seen[-1])


@pytest.mark.asyncio
async def test_propose_for_each_prefers_the_bulk_route(client, session, agent_caps, ai_session):
    pair = next(
        ((cap["bulk_of"], name) for name, cap in agent_caps.items() if cap.get("bulk_of") in agent_caps), None)
    assert pair is not None, "no bulk tool names the single-record tool it replaces"
    single, bulk = pair
    model = ScriptedModel(
        _call("propose_for_each", {"tool": single, "records": [{"path": {}}, {"path": {}}]}, "pf1"),
        lambda _m: text_result("Using the bulk tool."),
    )
    ctx = await perm_setup(client, session)
    conv_id = await _new_conversation(client, ctx["admin_h"])
    with patch("celerp.ai.service.complete", model):
        body = await asked_in_thread(client, ctx["admin_h"], conv_id, {"query": "all of them"})
    assert body["pending_actions"] == []
    assert bulk in str(model.seen[-1])
