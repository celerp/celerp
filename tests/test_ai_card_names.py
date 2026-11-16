# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A proposed change shows each record it names by the record's name on its confirm
card, so the person confirming can check which bank, line or contact it writes to."""
from __future__ import annotations

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


def test_a_card_shows_a_named_id_by_its_name_and_never_shows_an_unnamed_one():
    from fasthtml.common import to_xml
    from celerp_ai.ui_routes import _action_card
    from ui.i18n import t

    line_id = str(uuid.uuid4())
    action = {"id": "c1", "name": "resolve", "title": "Resolve", "names": {"b-1": "Main account (****1234)"},
              "shown": {"path": {"bank_id": "b-1"}, "body": {"decisions": [
                  {"line_ids": [line_id], "kind": "post"}], "memo": "Bank fee"}}}
    html = to_xml(_action_card("conv", "msg", action))
    assert "Main account (****1234)" in html and "b-1" not in html
    assert line_id not in html and t("ai.card_name_unavailable") in html
    assert "Bank fee" in html


@pytest.mark.asyncio
async def test_a_card_names_an_assistant_upload_and_a_reading_by_their_file_names(auth, ids, session, tmp_path, monkeypatch):
    """A statement read names the files it reads, and its import names the reading by those files."""
    from celerp.ai import files
    from celerp.ai.names import record_names
    from celerp.models.ai import AIBatchJob

    monkeypatch.setattr(files, "upload_dir", lambda: tmp_path)
    company_id = auth["company_id"]
    march = files.save_upload(company_id, ids["user_id"], "march-statement.pdf", "application/pdf", b"%PDF-1.4")
    april = files.save_upload(company_id, ids["user_id"], "april-statement.pdf", "application/pdf", b"%PDF-1.4")
    job = AIBatchJob(company_id=company_id, user_id=ids["user_id"], status="done", query="read",
                     file_ids=[march, april])
    session.add(job)
    await session.flush()

    names = await record_names(session, company_id, {}, "owner",
                               {"file_ids": [march], "body": {"job_id": str(job.id)}})
    assert names == {march: "march-statement.pdf", str(job.id): "march-statement.pdf, april-statement.pdf"}
    assert await record_names(session, uuid.uuid4(), {}, "owner", {"file_ids": [march]}) == {}


async def test_a_namer_that_fails_leaves_its_ids_as_they_are_and_the_next_namer_still_names(auth, session, monkeypatch):
    from celerp.ai.names import record_names
    from celerp.modules import slots

    async def broken(**_):
        raise RuntimeError("namer down")

    async def working(ids, **_):
        return {i: "Named record" for i in ids}

    monkeypatch.setattr(slots, "get", lambda slot: [{"handler": "broken"}, {"handler": "working"}]
                        if slot == "record_names" else [])
    monkeypatch.setattr(slots, "resolve_handler", {"broken": broken, "working": working}.__getitem__)
    record = str(uuid.uuid4())

    assert await record_names(session, auth["company_id"], {}, "owner", {"body": {"contact_id": record}}) == {
        record: "Named record"}
