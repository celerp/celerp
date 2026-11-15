# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Questions for the client: the answer_bank_questions permission, Ask and Remind with
their notice and email, the client's list and answers, and the suggestion an answer
becomes."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

import bank_statement_helpers as B
from celerp.services.email import EmailResult

pytestmark = pytest.mark.asyncio

Q = "/accounting/bank-questions"


async def _setup(client, session, rows=None):
    """A company with one statement, a viewer granted answer_bank_questions (the client)."""
    from test_helpers import grant_permission
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    rows = rows or [("2026-01-05", "WITHDRAWAL", "-200.00"), ("2026-01-06", "TRANSFER", "-50.00"),
                    ("2026-01-07", "BANK FEE", "-5.00")]
    closing = f"{sum(Decimal(a) for _, _, a in rows):.2f}"
    await B.statement(client, co, bk["id"], rows, opening="0.00", closing=closing, start="2026-01-01", end="2026-01-31")
    await grant_permission(client, co["h"], "answer_bank_questions", "viewer")
    client_h = await B.member(client, session, co, "viewer")
    lines = B.all_lines(await B.groups(client, co, bk["id"]))
    return co, bk, client_h, {l["description"]: l for l in lines}


async def _ask(client, co, bk, line_ids, note="What was this for?", idem=None):
    return await client.post(f"{B.API}/bank/{bk['id']}/ask", json={
        "line_ids": line_ids, "note": note, "idempotency_key": idem or B.key()}, headers=co["h"])


def _sent(monkeypatch, result=EmailResult(True, "sent")):
    calls = []

    async def fake(to, subject, body_html, body_text="", **kw):
        calls.append({"to": to, "subject": subject, **kw})
        return result
    monkeypatch.setattr("celerp.services.email.send_email", fake)
    return calls


async def _notices(session, company_id) -> int:
    from celerp.models.notification import Notification
    session.expire_all()
    return (await session.execute(select(func.count()).select_from(Notification).where(
        Notification.company_id == company_id, Notification.category == "bank_questions"))).scalar_one()


async def test_question_only_user_sees_one_menu_entry(client, session):
    from test_helpers import ui_signed_in
    co, bk, client_h, _ = await _setup(client, session)
    async with ui_signed_in(client_h["Authorization"].split(" ", 1)[1]) as ui:
        r = await ui.get("/dashboard")
    html = r.text
    assert "Questions from your accountant" in html and 'href="/accounting/questions"' in html
    assert 'href="/accounting/reconcile' not in html and 'href="/accounting/journal' not in html


async def test_viewer_cannot_see_bank_questions_by_default(client, session):
    co = await B.company(client)
    viewer = await B.member(client, session, co, "viewer")
    r = await client.get(Q, headers=viewer)
    assert r.status_code == 403


async def test_client_sees_only_asked_lines(client, session, monkeypatch):
    _sent(monkeypatch)
    co, bk, client_h, lines = await _setup(client, session)
    r = await _ask(client, co, bk, [lines["WITHDRAWAL"]["id"]])
    assert r.status_code == 200, r.text
    got = (await client.get(Q, headers=client_h)).json()
    assert [q["line_id"] for q in got["items"]] == [lines["WITHDRAWAL"]["id"]]
    other = await client.post(f"{Q}/{lines['BANK FEE']['id']}/answer", json={
        "answer": "fee", "idempotency_key": B.key()}, headers=client_h)
    assert other.status_code == 404


async def test_answer_after_post_is_refused(client, session, monkeypatch):
    _sent(monkeypatch)
    co, bk, client_h, lines = await _setup(client, session)
    lid = lines["WITHDRAWAL"]["id"]
    await _ask(client, co, bk, [lid])
    r = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [lid], "kind": "post", "account_code": "5100"}])
    assert r.json()["results"][0]["ok"]
    r = await client.post(f"{Q}/{lid}/answer", json={"answer": "rent", "idempotency_key": B.key()}, headers=client_h)
    assert r.status_code == 409 and "already" in r.json()["detail"]["message"].lower()


async def test_answer_naming_contact_suggests_without_model(client, session, monkeypatch):
    from celerp_accounting import suggest
    called = []
    monkeypatch.setattr(suggest, "model_rung", lambda groups, accounts: called.append(groups) or {})
    _sent(monkeypatch)
    co, bk, client_h, lines = await _setup(client, session)
    r = await client.post("/crm/contacts", json={"name": "Siam Landlord", "contact_type": "vendor"}, headers=co["h"])
    contact_id = r.json()["id"]
    r = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [lines["BANK FEE"]["id"]], "kind": "post",
                                                          "account_code": "6100", "contact": contact_id}])
    assert r.json()["results"][0]["ok"]
    lid = lines["WITHDRAWAL"]["id"]
    await _ask(client, co, bk, [lid])
    r = await client.post(f"{Q}/{lid}/answer", json={"answer": "rent", "contact_text": "Siam Landlord",
                                                      "idempotency_key": B.key()}, headers=client_h)
    assert r.status_code == 200, r.text
    line = B.line_by_text(await B.groups(client, co, bk["id"]), "WITHDRAWAL")
    assert line["suggestion"]["contact_id"] == contact_id and line["suggestion"]["account_code"] == "6100"
    assert not called


async def test_free_text_answer_waits_for_firm_suggest(client, session, monkeypatch):
    _sent(monkeypatch)
    co, bk, client_h, lines = await _setup(client, session)
    lid = lines["WITHDRAWAL"]["id"]
    await _ask(client, co, bk, [lid])
    r = await client.post(f"{Q}/{lid}/answer", json={"answer": "paid the plumber", "idempotency_key": B.key()},
                          headers=client_h)
    assert r.status_code == 200, r.text
    line = B.line_by_text(await B.groups(client, co, bk["id"]), "WITHDRAWAL")
    assert line["suggestion"] is None and line["client_query"]["answer"] == "paid the plumber"


async def test_client_answer_becomes_client_said_suggestion(client, session, monkeypatch):
    from celerp_accounting import suggest
    monkeypatch.setattr(suggest, "model_rung", lambda groups, accounts: {
        g["key"]: {"account_code": "5100", "contact_id": None, "reason": "Repairs"} for g in groups})
    _sent(monkeypatch)
    co, bk, client_h, lines = await _setup(client, session)
    lid = lines["WITHDRAWAL"]["id"]
    await _ask(client, co, bk, [lid])
    await client.post(f"{Q}/{lid}/answer", json={"answer": "paid the plumber", "idempotency_key": B.key()},
                      headers=client_h)
    r = await client.post(f"{B.API}/bank/{bk['id']}/suggest", json={"idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 200, r.text
    line = B.line_by_text(await B.groups(client, co, bk["id"]), "WITHDRAWAL")
    assert line["suggestion"]["reason"].startswith("Client said: paid the plumber")


async def test_ask_without_client_user_explains(client):
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    await B.statement(client, co, bk["id"], [("2026-01-05", "WITHDRAWAL", "-200.00")], opening="0.00",
                      closing="-200.00", start="2026-01-01", end="2026-01-31")
    lid = B.all_lines(await B.groups(client, co, bk["id"]))[0]["id"]
    r = await _ask(client, co, bk, [lid])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["message_key"] == "reconciliation.no_client_user" and "/settings/users" in detail["message"]
    line = B.all_lines(await B.groups(client, co, bk["id"]))[0]
    assert line["client_query"] is None


async def test_remind_renotifies_once_per_key(client, session, monkeypatch):
    _sent(monkeypatch)
    co, bk, client_h, lines = await _setup(client, session)
    lid = lines["WITHDRAWAL"]["id"]
    idem = B.key()
    await _ask(client, co, bk, [lid], idem=idem)
    await _ask(client, co, bk, [lid], idem=idem)
    assert await _notices(session, co["company_id"]) == 1
    await _ask(client, co, bk, [lid])  # Remind: a new key
    assert await _notices(session, co["company_id"]) == 2


async def test_ask_notice_rolls_back_with_question(client, session, monkeypatch):
    _sent(monkeypatch)
    co, bk, client_h, lines = await _setup(client, session)
    from celerp_accounting import statement_lines

    async def boom(*a, **kw):
        raise RuntimeError("commit failed")
    monkeypatch.setattr(statement_lines, "after_ask_written", boom)
    with pytest.raises(RuntimeError):
        await _ask(client, co, bk, [lines["WITHDRAWAL"]["id"]])
    assert await _notices(session, co["company_id"]) == 0
    line = B.line_by_text(await B.groups(client, co, bk["id"]), "WITHDRAWAL")
    assert line["client_query"] is None


@pytest.mark.parametrize("result,words", [
    (EmailResult(False, "relay down", reason="email.relay_unavailable"), "could not be sent"),
    (EmailResult(False, "timeout", unconfirmed=True), "may have gone out"),
], ids=["failed", "unconfirmed"])
async def test_ask_client_email_failure_keeps_question(client, session, monkeypatch, result, words):
    _sent(monkeypatch, result)
    co, bk, client_h, lines = await _setup(client, session)
    r = await _ask(client, co, bk, [lines["WITHDRAWAL"]["id"]])
    assert r.status_code == 200, r.text
    out = r.json()
    assert words in out["outcome"] and "Viewer" in out["outcome"]
    assert out["email"]["state"] == ("unconfirmed" if result.unconfirmed else "failed")
    assert await _notices(session, co["company_id"]) == 1
    got = (await client.get(Q, headers=client_h)).json()
    assert len(got["items"]) == 1


async def test_ask_client_email_replay_sends_once(client, session, monkeypatch):
    calls = _sent(monkeypatch)
    co, bk, client_h, lines = await _setup(client, session)
    lid = lines["WITHDRAWAL"]["id"]
    idem = B.key()
    first = await _ask(client, co, bk, [lid], idem=idem)
    again = await _ask(client, co, bk, [lid], idem=idem)
    assert len(calls) == 1 and again.json() == first.json()
    assert calls[0]["operation_id"] and calls[0]["fail_closed_on_uncertain"] is True
    await _ask(client, co, bk, [lid])
    assert len(calls) == 2 and calls[1]["operation_id"] != calls[0]["operation_id"]


async def test_answer_receipt_refused_saves_nothing(client, session, monkeypatch):
    _sent(monkeypatch)
    co, bk, client_h, lines = await _setup(client, session)
    lid = lines["WITHDRAWAL"]["id"]
    await _ask(client, co, bk, [lid])
    fid = B.upload(co["company_id"], B.user_id(client_h), b"MZ\x90\x00", "tool.exe", "application/x-msdownload")
    r = await client.post(f"{Q}/{lid}/answer", json={"answer": "receipt", "file_ids": [fid], "idempotency_key": B.key()},
                          headers=client_h)
    assert r.status_code == 413 and "cannot be attached" in r.text
    line = B.line_by_text(await B.groups(client, co, bk["id"]), "WITHDRAWAL")
    assert not (line["client_query"] or {}).get("answer") and not line.get("attachments")


async def test_answer_receipt_store_failure_keeps_form(client, session, monkeypatch):
    _sent(monkeypatch)
    co, bk, client_h, lines = await _setup(client, session)
    lid = lines["WITHDRAWAL"]["id"]
    await _ask(client, co, bk, [lid])
    fid = B.upload(co["company_id"], B.user_id(client_h), b"%PDF-1.4\n%%EOF\n", "slip.pdf", "application/pdf")
    from celerp.services import attachments
    real = attachments.store_file

    async def fail(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(attachments, "store_file", fail)
    body = {"answer": "receipt attached", "file_ids": [fid], "idempotency_key": B.key()}
    r = await client.post(f"{Q}/{lid}/answer", json=body, headers=client_h)
    assert r.status_code == 503 and "receipt could not be saved" in r.text.lower()
    line = B.line_by_text(await B.groups(client, co, bk["id"]), "WITHDRAWAL")
    assert not (line["client_query"] or {}).get("answer")
    monkeypatch.setattr(attachments, "store_file", real)
    for _ in range(2):
        r = await client.post(f"{Q}/{lid}/answer", json=body, headers=client_h)
        assert r.status_code == 200, r.text
    line = B.line_by_text(await B.groups(client, co, bk["id"]), "WITHDRAWAL")
    assert len(line["attachments"]) == 1
