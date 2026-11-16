# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A proposed change shows each record it names by the record's name on its confirm
card, so the person confirming can check which bank, line or contact it writes to."""
from __future__ import annotations

import json
import uuid

import pytest

import bank_statement_helpers as B


_QUESTION = "0b6f5a54-7c1d-4e8b-9f3a-1d2c3b4a5e6f"


_ACCOUNTING = {"name": "celerp-accounting", "api_routes": "celerp_accounting.api_setup", "first_party": True}


def _resolve_tool(monkeypatch) -> str:
    from celerp.ai import tools as ai_tools
    from celerp.main import app

    monkeypatch.setattr(ai_tools, "loaded_modules", lambda: [_ACCOUNTING])
    return next(name for name, cap in ai_tools.compile_agent_capabilities(app, {}).items()
                if cap["method"] == "POST" and cap["path"].endswith("/bank/{bank_id}/resolve"))


async def _line(client, h) -> tuple[dict, str]:
    [bank] = (await client.get("/accounting/bank-accounts", headers=h)).json()["items"]
    r = await client.post("/accounting/reconciliation/start", headers=h, json={
        "bank_account_id": bank["id"], "statement_date": "2026-03-31", "statement_balance": -50})
    assert r.status_code == 200, r.text
    got = await B.import_into(client, h, r.json()["id"], B.csv_bytes([("2026-03-01", "Monthly fee", "-50")]))
    return bank, got["lines"][0]["id"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("tools_open_when_called")
async def test_a_bank_resolve_card_names_the_bank_and_the_line_never_their_ids(client, auth, monkeypatch, drained_jobs):
    from ai_fake_cloud import FakeCloud, answer, install, tool_turn
    from fasthtml.common import to_xml
    from celerp_ai.ui_routes import _action_card
    from test_helpers import settled_question

    h = auth["headers"]
    bank, line_id = await _line(client, h)
    tool = _resolve_tool(monkeypatch)

    def script(identity: str, body: dict) -> dict:
        if not any(m.get("role") == "tool" for m in body["messages"]):
            return tool_turn((tool, {"path": {"bank_id": bank["id"]}, "body": {"decisions": [
                {"line_ids": [line_id], "kind": "post", "account_code": "6100"}]}}, "call-resolve"))
        return answer("Done.")

    install(monkeypatch, FakeCloud(script))
    conv = (await client.post("/ai/conversations", headers=h, json={"title": None})).json()["id"]
    r = await client.post(f"/ai/conversations/{conv}/query", headers=h,
                          json={"query": "Post the fee to bank charges", "operation_id": _QUESTION})
    assert r.status_code == 202, r.text
    state = await settled_question(client, h, conv, _QUESTION)
    (card,) = state["message"]["pending_actions"]
    html = to_xml(_action_card(conv, state["message"]["id"], card))
    shown = html.split("ai-action__lines", 1)[1].split("<a ", 1)[0]
    assert bank["bank_name"] in shown and "Monthly fee" in shown and "-50.00" in shown
    assert bank["id"] not in shown and line_id not in shown


@pytest.mark.asyncio
async def test_a_name_is_kept_only_for_a_record_the_asker_may_read(client, auth, session):
    from celerp.ai.names import record_names

    h = auth["headers"]
    bank, line_id = await _line(client, h)
    r = await client.post("/crm/contacts", headers=h, json={"name": "Harbour Supplies"})
    assert r.status_code in (200, 201), r.text
    contact_id = r.json()["id"]
    company_id = auth["company_id"]
    args = {"path": {"bank_id": bank["id"]}, "body": {"contact_id": contact_id, "decisions": [
        {"line_ids": [line_id, "not-a-record"]}], "memo": line_id}}

    owner = await record_names(session, company_id, {}, "owner", args)
    assert owner[contact_id] == "Harbour Supplies" and bank["bank_name"] in owner[bank["id"]]
    assert "Monthly fee" in owner[line_id] and "not-a-record" not in owner

    viewer = await record_names(session, company_id, {}, "viewer", args)
    assert viewer[contact_id] == "Harbour Supplies" and line_id not in viewer and bank["id"] in viewer

    assert await record_names(session, company_id, {}, "no-such-role", {"body": {"contact_id": contact_id}}) == {}
    assert await record_names(session, uuid.uuid4(), {}, "owner", args) == {}


def test_a_card_shows_a_named_id_by_its_name_and_an_unnamed_one_as_it_is():
    from fasthtml.common import to_xml
    from celerp_ai.ui_routes import _action_card

    action = {"id": "c1", "name": "resolve", "title": "Resolve", "names": {"b-1": "Main account (****1234)"},
              "arguments": {"path": {"bank_id": "b-1"}, "body": {"decisions": [
                  {"line_ids": ["l-1"], "kind": "post"}], "memo": "b-1"}}}
    html = to_xml(_action_card("conv", "msg", action))
    assert "Main account (****1234)" in html and "l-1" in html
    assert json.dumps("b-1") not in html and html.count("b-1") == 1  # the memo, which is not an id


def _field_keys(schema, out: set[str]) -> set[str]:
    """Every field name a tool's arguments can hold, at any depth."""
    if isinstance(schema, dict):
        for key, sub in (schema.get("properties") or {}).items():
            out.add(key)
            _field_keys(sub, out)
        for key in ("items", "anyOf", "oneOf", "allOf"):
            sub = schema.get(key)
            for part in sub if isinstance(sub, list) else [sub]:
                _field_keys(part, out)
    return out


def test_every_field_a_bank_change_shows_is_labelled_in_every_language(monkeypatch):
    """A bank change's confirm card labels each field in the reader's language, never by
    the field's internal name."""
    from celerp.ai import tools as ai_tools
    from celerp.main import app
    from celerp_ai.ui_routes import _LABEL_KEYS
    from ui import i18n

    monkeypatch.setattr(ai_tools, "loaded_modules", lambda: [_ACCOUNTING])
    keys = set()
    for capability in ai_tools.compile_agent_capabilities(app, {}).values():
        if capability["requires_confirmation"] and capability["path"].startswith(
                ("/accounting/reconciliation", "/accounting/bank")):
            _field_keys(capability["tool"]["function"]["parameters"], keys)
    assert keys
    unlabelled = sorted(
        f"{lang}:{key}" for key in keys - {"after_previous"} for lang in i18n.available_langs()
        if _LABEL_KEYS.get(key, f"field.{key}") not in i18n._cached_load(lang))
    assert unlabelled == []
