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
from ui.components.shell import refusal_fix
from ui.i18n import t

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
        from celerp.services.company_lock import refuse_held_transaction
        refuse_held_transaction("an email is sent")
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


async def test_withdrawn_question_leaves_the_clients_list(client, session, monkeypatch):
    _sent(monkeypatch)
    co, bk, client_h, lines = await _setup(client, session)
    lid = lines["WITHDRAWAL"]["id"]
    await _ask(client, co, bk, [lid])
    r = await client.post(f"{B.API}/bank/{bk['id']}/withdraw-question",
                          json={"line_ids": [lid], "idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 200 and r.json()["withdrawn"] == [lid], r.text
    assert (await client.get(Q, headers=client_h)).json()["items"] == []
    r = await client.post(f"{Q}/{lid}/answer", json={"answer": "rent", "idempotency_key": B.key()}, headers=client_h)
    assert r.status_code == 404


async def test_client_answers_through_the_questions_page(client, session, monkeypatch):
    _sent(monkeypatch)
    co, bk, client_h, lines = await _setup(client, session)
    lid = lines["WITHDRAWAL"]["id"]
    await _ask(client, co, bk, [lid])
    async with B.browser(client_h) as ui:
        r = await ui.post(f"/accounting/questions/{lid}/answer", data={
            "answer": "paid the plumber", "idempotency_key": B.key()})
    assert r.status_code == 200 and t("bank.answer_saved") in r.text, r.text[:500]
    got = (await client.get(Q, params={"status": "all"}, headers=client_h)).json()["items"]
    assert [q["line_id"] for q in got] == [lid] and got[0]["answered"], got


async def test_a_receipt_sent_with_a_generic_type_is_kept_by_its_name(client, session, monkeypatch):
    """A receipt's browser type is often generic (``application/octet-stream``); the
    questions page keeps it by its filename, like the bank drop and the AI chat
    (celerp/ai/files.py:upload_content_type), instead of refusing it as unsupported."""
    _sent(monkeypatch)
    co, bk, client_h, lines = await _setup(client, session)
    lid = lines["WITHDRAWAL"]["id"]
    await _ask(client, co, bk, [lid])
    async with B.browser(client_h) as ui:
        r = await ui.post(f"/accounting/questions/{lid}/answer", data={
            "answer": "paid the plumber", "idempotency_key": B.key()},
            files=[("files", ("slip.jpg", b"\xff\xd8\xff", "application/octet-stream"))])
    assert r.status_code == 200 and t("bank.answer_saved") in r.text, r.text[:500]


async def test_answer_after_post_is_refused(client, session, monkeypatch):
    _sent(monkeypatch)
    co, bk, client_h, lines = await _setup(client, session)
    lid = lines["WITHDRAWAL"]["id"]
    await _ask(client, co, bk, [lid])
    r = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [lid], "kind": "post", "account_code": "5100"}])
    assert r.json()["results"][0]["ok"]
    r = await client.post(f"{Q}/{lid}/answer", json={"answer": "rent", "idempotency_key": B.key()}, headers=client_h)
    assert r.status_code == 409 and "already" in r.json()["detail"]["message"].lower()


@pytest.mark.parametrize("name", ["Siam Landlord", "ร้านเช่า สยาม"])
async def test_answer_naming_contact_suggests_without_model(client, session, monkeypatch, name):
    from celerp_accounting import suggest
    called = []
    monkeypatch.setattr(suggest, "model_rung", lambda groups, accounts: called.append(groups) or {})
    _sent(monkeypatch)
    co, bk, client_h, lines = await _setup(client, session)
    r = await client.post("/crm/contacts", json={"name": name, "contact_type": "vendor"}, headers=co["h"])
    contact_id = r.json()["id"]
    r = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [lines["BANK FEE"]["id"]], "kind": "post",
                                                          "account_code": "6100", "contact": contact_id}])
    assert r.json()["results"][0]["ok"]
    lid = lines["WITHDRAWAL"]["id"]
    await _ask(client, co, bk, [lid])
    r = await client.post(f"{Q}/{lid}/answer", json={"answer": "rent", "contact_text": name,
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
    assert detail["message_key"] == "reconciliation.no_client_user"
    assert detail["params"]["permission"]["message_key"] == "perm.name.answer_bank_questions"
    assert detail["params"]["books"]["message_key"] == "perm.name.manage_accounting"
    assert refusal_fix(detail) == {"href": "/settings/general?tab=users", "label": t("settings.tab_users")}
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


async def test_a_second_ask_counts_the_question_still_waiting_from_the_first(client, session, monkeypatch):
    """A question asked and not yet answered stays open, so the notice of a later Ask about
    another line counts both."""
    from celerp.models.notification import Notification
    _sent(monkeypatch)
    co, bk, client_h, lines = await _setup(client, session)
    await _ask(client, co, bk, [lines["WITHDRAWAL"]["id"]])
    await _ask(client, co, bk, [lines["TRANSFER"]["id"]])
    session.expire_all()
    notes = (await session.execute(select(Notification).where(
        Notification.company_id == co["company_id"], Notification.category == "bank_questions"))).scalars().all()
    assert sorted(n.i18n["params"]["count"] for n in notes) == [1, 2]


async def test_ask_notice_rolls_back_with_question(client, session, monkeypatch):
    _sent(monkeypatch)
    co, bk, client_h, lines = await _setup(client, session)
    from celerp_accounting import statement_lines

    real = statement_lines.write_ask

    async def boom(*a, **kw):
        await real(*a, **kw)
        raise RuntimeError("commit failed")
    monkeypatch.setattr(statement_lines, "write_ask", boom)
    with pytest.raises(RuntimeError):
        await _ask(client, co, bk, [lines["WITHDRAWAL"]["id"]])
    assert await _notices(session, co["company_id"]) == 0
    line = B.line_by_text(await B.groups(client, co, bk["id"]), "WITHDRAWAL")
    assert line["client_query"] is None


@pytest.mark.parametrize("result,words", [
    (EmailResult(False, "no transport", reason="email.err_no_transport"), "could not be sent"),
    (EmailResult(False, "timeout", unconfirmed=True), "may have gone out"),
], ids=["failed", "unconfirmed"])
async def test_ask_client_email_failure_keeps_question(client, session, monkeypatch, result, words):
    _sent(monkeypatch, result)
    co, bk, client_h, lines = await _setup(client, session)
    r = await _ask(client, co, bk, [lines["WITHDRAWAL"]["id"]])
    assert r.status_code == 200, r.text
    out = r.json()
    assert words in out["outcome"] and "Viewer" in out["outcome"]
    assert out["outcome"].startswith("Asked about 1 line. ") and ".." not in out["outcome"], out["outcome"]
    assert out["email"]["state"] == ("unconfirmed" if result.unconfirmed else "failed")
    assert await _notices(session, co["company_id"]) == 1
    got = (await client.get(Q, headers=client_h)).json()
    assert len(got["items"]) == 1



async def test_an_ask_whose_emails_all_fail_gives_the_reason_of_the_first_recipient_by_name(client, session,
                                                                                            monkeypatch):
    """Each recipient's email failing for its own reason: the outcome names every one of them and
    gives the reason of the first by name, the same recipient the list starts with."""
    import uuid
    from sqlalchemy import select, update
    from celerp.models.company import User

    async def refuse(to, subject, body_html, body_text="", **kw):
        return EmailResult(False, f"refused for {to}")
    monkeypatch.setattr("celerp.services.email.send_email", refuse)
    co, bk, ids, lines = await _two_clients(client, session)
    for user, name in zip(ids, ("Bea Client", "Al Client")):
        await session.execute(update(User).where(User.id == uuid.UUID(user)).values(name=name))
    await session.commit()
    people = dict((await session.execute(select(User.name, User.email).where(
        User.id.in_([uuid.UUID(i) for i in ids])))).all())
    r = await _ask(client, co, bk, [lines["WITHDRAWAL"]["id"]])
    assert r.status_code == 200, r.text
    out = r.json()
    first = out["email"]["recipients"][0]["name"]
    assert sorted(people) == sorted(p["name"] for p in out["email"]["recipients"]), out
    assert f"refused for {people[first]}" in out["outcome"], out
    assert all(f"refused for {e}" not in out["outcome"] for n, e in people.items() if n != first), out

@pytest.mark.parametrize("lang,stop", [("en", "."), ("ja", "。")])
@pytest.mark.parametrize("detail", ["The mail server refused the message", "The mail server refused the message."])
async def test_a_failed_send_reason_reads_as_one_sentence_in_the_readers_language(client, session, monkeypatch,
                                                                                    lang, stop, detail):
    """A reason the mail service gave in its own words is closed by the reader's full stop when it has none,
    and is never given a second one when it has."""
    _sent(monkeypatch, EmailResult(False, detail))
    co, bk, client_h, lines = await _setup(client, session)
    r = await client.post(f"{B.API}/bank/{bk['id']}/ask", json={
        "line_ids": [lines["WITHDRAWAL"]["id"]], "note": "What was this for?", "idempotency_key": B.key()},
        headers={**co["h"], "Accept-Language": lang})
    assert r.status_code == 200, r.text
    outcome = r.json()["outcome"]
    closed = detail if detail.endswith(".") else detail + stop
    after = str(t("acct.recon_asked_email_failed_one", lang)).split("{reason}", 1)[1]
    assert closed + after in outcome, outcome
    assert ".." not in outcome and ".。" not in outcome and "。。" not in outcome, outcome


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


async def test_ask_under_a_key_naming_another_asks_email_gets_its_own_plan(client, session, monkeypatch):
    """An Ask's email outcome is kept on the Ask's own record, so no client key can name it:
    an Ask under "K:email" with K's payload is an Ask of its own, and K still replays K."""
    calls = _sent(monkeypatch)
    co, bk, client_h, lines = await _setup(client, session)
    lid = lines["WITHDRAWAL"]["id"]
    idem = B.key()
    first = await _ask(client, co, bk, [lid], idem=idem)
    derived = await _ask(client, co, bk, [lid], idem=f"{idem}:email")
    assert derived.status_code == 200, derived.text
    assert len(calls) == 2 and calls[1]["operation_id"] != calls[0]["operation_id"]
    again = await _ask(client, co, bk, [lid], idem=idem)
    assert again.status_code == 200 and again.json() == first.json() and len(calls) == 2


async def _between_plan_and_send(monkeypatch, change):
    """Run ``change(db)`` after the Ask's plan and questions are written, before its email."""
    from celerp_accounting import statement_lines as SL
    real = SL.write_ask

    async def write_then_change(db, **kw):
        await real(db, **kw)
        await change(db)
    monkeypatch.setattr(SL, "write_ask", write_then_change)


async def _two_clients(client, session):
    co, bk, client_h, lines = await _setup(client, session)
    other_h = await B.member(client, session, co, "viewer")
    return co, bk, (B.user_id(client_h), B.user_id(other_h)), lines


async def _removed(db, user_id, company_id):
    import uuid
    from sqlalchemy import update
    from celerp.models.accounting import UserCompany
    await db.execute(update(UserCompany).where(UserCompany.user_id == uuid.UUID(user_id),
                                               UserCompany.company_id == uuid.UUID(company_id)).values(is_active=False))


async def test_a_recipient_removed_before_the_email_is_not_mailed_and_is_named(client, session, monkeypatch):
    calls = _sent(monkeypatch)
    co, bk, (gone, kept), lines = await _two_clients(client, session)
    from celerp.models.company import User
    import uuid
    gone_user, kept_user = (await session.get(User, uuid.UUID(gone))), (await session.get(User, uuid.UUID(kept)))
    await _between_plan_and_send(monkeypatch, lambda db: _removed(db, gone, co["company_id"]))
    r = await _ask(client, co, bk, [lines["WITHDRAWAL"]["id"]])
    assert r.status_code == 200, r.text
    assert [c["to"] for c in calls] == [kept_user.email]
    assert gone_user.name in r.json()["outcome"] and "not emailed" in r.json()["outcome"], r.json()


async def test_a_recipient_whose_permission_is_revoked_before_the_email_is_not_mailed(client, session, monkeypatch):
    """Every recipient lost the permission between plan and send: nothing goes out and the
    send is refused saying why."""
    calls = _sent(monkeypatch)
    co, bk, client_h, lines = await _setup(client, session)

    async def revoke(db):
        import uuid
        from celerp.services.company_lock import locked_company
        company = await locked_company(db, uuid.UUID(co["company_id"]))
        grants = dict((company.settings or {}).get("role_grants") or {})
        grants["answer_bank_questions"] = [r for r in grants.get("answer_bank_questions", []) if r != "viewer"]
        company.settings = {**(company.settings or {}), "role_grants": grants}
    await _between_plan_and_send(monkeypatch, revoke)
    r = await _ask(client, co, bk, [lines["WITHDRAWAL"]["id"]])
    assert calls == []
    assert r.status_code == 409 and r.json()["detail"]["message_key"] == "reconciliation.no_client_user", r.text


async def _before_the_emails_go(monkeypatch, change):
    """Run ``change()`` once the Ask's attempts are recorded and committed, before any goes out."""
    from celerp.services import email as email_service
    real = email_service.transmit

    async def change_then_transmit(attempts, current=None):
        await change()
        return await real(attempts, current)
    monkeypatch.setattr(email_service, "transmit", change_then_transmit)


@pytest.mark.parametrize("revoked", ["sole", "all", "one_of_two"])
async def test_an_ask_reads_as_sent_only_when_an_email_went_out(client, session, monkeypatch, revoked):
    """Recipients removed after their attempts are recorded, just before the emails go: with
    nobody left the Ask says no email was sent and names who was left out, on the first answer
    and on a run of the same key; with one still answering it reads as sent to that one."""
    import uuid

    from celerp.models.company import User
    calls = _sent(monkeypatch)
    if revoked == "sole":
        co, bk, client_h, lines = await _setup(client, session)
        members = (B.user_id(client_h),)
    else:
        co, bk, members, lines = await _two_clients(client, session)
    users = [await session.get(User, uuid.UUID(m)) for m in members]
    known = [(u.email, u.name) for u in users]
    gone = members[:1] if revoked == "one_of_two" else members

    async def revoke():
        from celerp.services.company_lock import lock_company
        await lock_company(session, uuid.UUID(co["company_id"]))
        for member in gone:
            await _removed(session, member, co["company_id"])
        await session.commit()
    await _before_the_emails_go(monkeypatch, revoke)
    idem = B.key()
    r = await _ask(client, co, bk, [lines["WITHDRAWAL"]["id"]], idem=idem)
    assert r.status_code == 200, r.text
    out = r.json()
    for _email, name in known[:len(gone)]:
        assert name in out["outcome"] and "not emailed" in out["outcome"], out
    if revoked == "one_of_two":
        assert [c["to"] for c in calls] == [known[1][0]]
        assert out["email"] == {"state": "sent", "recipients": [{"name": known[1][1], "state": "sent"}]}, out
        assert "got an email" in out["outcome"], out
        return
    assert calls == []
    assert out["email"] == {"state": "not_sent", "recipients": []}, out
    assert out["outcome"].startswith(str(t("acct.recon_asked_no_email_one"))), out
    assert "got an email" not in out["outcome"], out
    again = await _ask(client, co, bk, [lines["WITHDRAWAL"]["id"]], idem=idem)
    assert again.status_code == 200 and again.json() == out and calls == [], again.text


async def test_a_key_used_by_one_bank_operation_is_refused_by_another(client):
    """Two bank operations with the same body under one key: the second is refused, never
    answered with the first operation's result."""
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    key = B.key()
    done = await client.post(f"{B.API}/bank/{bk['id']}/complete", json={"idempotency_key": key}, headers=co["h"])
    assert done.status_code == 200, done.text
    other = await client.post(f"{B.API}/bank/{bk['id']}/suggest", json={"idempotency_key": key}, headers=co["h"])
    assert other.status_code == 409 and "key" in other.text.lower(), other.text


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


async def test_asking_about_several_lines_counts_them_and_says_each_reason_once(client, session, monkeypatch):
    """Two lines read as lines, and the send failure reads as its own sentence, never with a doubled full stop."""
    from ui.i18n import t

    _sent(monkeypatch, EmailResult(False, "no transport", reason="email.err_no_transport"))
    co, bk, client_h, lines = await _setup(client, session)
    r = await _ask(client, co, bk, [lines["WITHDRAWAL"]["id"], lines["TRANSFER"]["id"]])
    out = r.json()["outcome"]
    assert out.startswith("Asked about 2 lines. ") and ".." not in out, out
    assert f" {t('email.err_no_transport', 'en')} They see" in out, out


class _Crash(BaseException):
    """The process stopping after the transport took the email, before its outcome is kept."""


async def test_a_slow_email_never_blocks_a_write_to_the_company(client, session, monkeypatch):
    """The Ask's email goes out with no transaction open, so another writer of the company
    does not wait on it past its lock timeout."""
    import asyncio
    co, bk, client_h, lines = await _setup(client, session)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(to, subject, body_html, body_text="", **kw):
        entered.set()
        await asyncio.wait_for(release.wait(), 10)
        return EmailResult(True, "sent")
    monkeypatch.setattr("celerp.services.email.send_email", slow)
    asking = asyncio.create_task(_ask(client, co, bk, [lines["WITHDRAWAL"]["id"]]))
    await asyncio.wait_for(entered.wait(), 10)
    other = await B.bank(client, co["h"])
    release.set()
    assert other["id"] and (await asking).status_code == 200


@pytest.mark.parametrize("change", ["revoked", "moved"])
async def test_a_recipient_who_changes_while_an_earlier_email_is_slow_is_not_mailed(client, session, monkeypatch, change):
    """Each recipient is read again just before their own email: one removed, or at a new
    address, while an earlier email of the same Ask is with the transport gets nothing at
    the address the Ask began with."""
    import asyncio
    import uuid

    from sqlalchemy import update

    from celerp.models.company import User
    from celerp.services.company_lock import refuse_held_transaction
    co, bk, members, lines = await _two_clients(client, session)
    known = {m: ((u := await session.get(User, uuid.UUID(m))).email, u.name) for m in members}
    entered, release, calls = asyncio.Event(), asyncio.Event(), []

    async def slow(to, subject, body_html, body_text="", **kw):
        refuse_held_transaction("an email is sent")
        calls.append(to)
        if not entered.is_set():
            entered.set()
            await asyncio.wait_for(release.wait(), 10)
        return EmailResult(True, "sent")
    monkeypatch.setattr("celerp.services.email.send_email", slow)
    idem = B.key()
    asking = asyncio.create_task(_ask(client, co, bk, [lines["WITHDRAWAL"]["id"]], idem=idem))
    await asyncio.wait_for(entered.wait(), 10)
    later = next(m for m in members if known[m][0] != calls[0])
    if change == "revoked":
        await _removed(session, later, co["company_id"])
    else:
        await session.execute(update(User).where(User.id == uuid.UUID(later)).values(email="moved@test.test"))
    await session.commit()
    release.set()
    r = await asking
    assert r.status_code == 200, r.text
    assert calls == [calls[0]] and known[later][0] not in calls, calls
    out = r.json()
    if change == "revoked":
        assert known[later][1] in out["outcome"] and "not emailed" in out["outcome"], out
        assert out["email"]["state"] == "sent", out
    else:
        assert {"name": known[later][1], "state": "failed"} in out["email"]["recipients"], out
        assert "changed just before" in out["outcome"], out
        again = await _ask(client, co, bk, [lines["WITHDRAWAL"]["id"]], idem=idem)
        assert again.json()["email"]["state"] == "sent" and calls[1:] == ["moved@test.test"], (calls, again.text)


async def test_an_ask_read_while_its_email_is_slow_says_it_is_still_being_sent_then_how_it_went(
        client, session, monkeypatch):
    """A run of the Ask's key while its email is with the transport answers that the email is
    still being sent, read from the stored attempt, never that it failed or went unconfirmed;
    once the send settles, the same key says how it went."""
    import asyncio

    co, bk, client_h, lines = await _setup(client, session)
    entered, release, calls = asyncio.Event(), asyncio.Event(), []

    async def slow(to, subject, body_html, body_text="", **kw):
        calls.append(to)
        entered.set()
        await asyncio.wait_for(release.wait(), 10)
        return EmailResult(True, "sent")
    monkeypatch.setattr("celerp.services.email.send_email", slow)
    idem = B.key()
    asking = asyncio.create_task(_ask(client, co, bk, [lines["WITHDRAWAL"]["id"]], idem=idem))
    await asyncio.wait_for(entered.wait(), 10)
    during = await _ask(client, co, bk, [lines["WITHDRAWAL"]["id"]], idem=idem)
    assert during.status_code == 200, during.text
    out = during.json()
    assert out["email"]["state"] == "sending" and {r["state"] for r in out["email"]["recipients"]} == {"sending"}, out
    assert "still being sent" in out["outcome"] and "not confirmed" not in out["outcome"], out
    release.set()
    assert (await asking).json()["email"]["state"] == "sent"
    after = (await _ask(client, co, bk, [lines["WITHDRAWAL"]["id"]], idem=idem)).json()
    assert after["email"]["state"] == "sent" and "got an email" in after["outcome"] and len(calls) == 1, (after, calls)


@pytest.mark.parametrize("relay", [False, True], ids=["smtp", "relay"])
async def test_an_email_taken_before_a_crash_is_never_sent_again_unless_the_relay_deduplicates(
        client, session, monkeypatch, relay):
    co, bk, client_h, lines = await _setup(client, session)
    calls = []
    monkeypatch.setattr("celerp.services.email.relay_identity", lambda: "account" if relay else None)

    async def crash(to, subject, body_html, body_text="", **kw):
        calls.append(kw["operation_id"])
        raise _Crash()
    monkeypatch.setattr("celerp.services.email.send_email", crash)
    idem = B.key()
    with pytest.raises(_Crash):
        await _ask(client, co, bk, [lines["WITHDRAWAL"]["id"]], idem=idem)
    later_calls = _sent(monkeypatch)
    again = await _ask(client, co, bk, [lines["WITHDRAWAL"]["id"]], idem=idem)
    assert again.status_code == 200, again.text
    # Cut off a moment ago, it reads as one still with the transport: never sent again.
    assert again.json()["email"]["state"] == "sending" and not later_calls, again.json()
    from datetime import timedelta
    monkeypatch.setattr("celerp.services.email.SEND_TIMEOUT", timedelta(0))  # past the longest a send takes
    later = (await _ask(client, co, bk, [lines["WITHDRAWAL"]["id"]], idem=idem)).json()
    if relay:
        assert later["email"]["state"] == "sent" and [c["operation_id"] for c in later_calls] == calls, later
    else:
        assert later["email"] == {"state": "unconfirmed", "recipients": [{"name": "Viewer", "state": "unconfirmed"}]}
        assert "may have gone out" in later["outcome"] and not later_calls, later


@pytest.mark.parametrize("relay", [False, True], ids=["smtp", "relay"])
async def test_an_unconfirmed_email_is_replayed_under_its_operation_id_only_through_a_deduplicating_relay(
        client, session, monkeypatch, relay):
    co, bk, client_h, lines = await _setup(client, session)
    monkeypatch.setattr("celerp.services.email.relay_identity", lambda: "account" if relay else None)
    first_calls = _sent(monkeypatch, EmailResult(False, "timeout", unconfirmed=True))
    idem = B.key()
    first = await _ask(client, co, bk, [lines["WITHDRAWAL"]["id"]], idem=idem)
    assert first.json()["email"]["state"] == "unconfirmed"
    calls = _sent(monkeypatch)
    again = (await _ask(client, co, bk, [lines["WITHDRAWAL"]["id"]], idem=idem)).json()
    if relay:
        assert [c["operation_id"] for c in calls] == [first_calls[0]["operation_id"]]
        assert again["email"]["state"] == "sent"
    else:
        assert calls == [] and again == first.json()


@pytest.mark.parametrize("change", ["past_the_window", "another_relay_account"])
async def test_an_unconfirmed_email_the_relay_may_no_longer_know_is_never_replayed(client, session, monkeypatch, change):
    """The relay answers for an operation id only for a while, and only to the account that sent
    it: past the replay window, or through another account, the Ask answers as stored."""
    from datetime import timedelta
    co, bk, client_h, lines = await _setup(client, session)
    monkeypatch.setattr("celerp.services.email.relay_identity", lambda: "account")
    _sent(monkeypatch, EmailResult(False, "timeout", unconfirmed=True))
    idem = B.key()
    first = await _ask(client, co, bk, [lines["WITHDRAWAL"]["id"]], idem=idem)
    assert first.json()["email"]["state"] == "unconfirmed"
    if change == "past_the_window":
        monkeypatch.setattr("celerp.services.email.REPLAY_WINDOW", timedelta(0))
    else:
        monkeypatch.setattr("celerp.services.email.relay_identity", lambda: "another account")
    calls = _sent(monkeypatch)
    again = (await _ask(client, co, bk, [lines["WITHDRAWAL"]["id"]], idem=idem)).json()
    assert calls == [] and again == first.json()


async def test_the_answer_names_each_recipients_outcome(client, session, monkeypatch):
    _sent(monkeypatch)
    co, bk, client_h, lines = await _setup(client, session)
    out = (await _ask(client, co, bk, [lines["WITHDRAWAL"]["id"]])).json()
    assert out["email"]["recipients"] == [{"name": "Viewer", "state": "sent"}]


async def test_a_bank_key_never_takes_a_key_the_books_write_under(client, session, monkeypatch):
    """A bank request's key lives in its own namespace: sent as the key a manual journal
    entry posts under, the Ask still works and the entry still posts."""
    _sent(monkeypatch)
    co, bk, _, lines = await _setup(client, session)
    taken = "je:manual:shared-token:c"
    assert (await _ask(client, co, bk, [lines["WITHDRAWAL"]["id"]], idem=taken)).status_code == 200
    r = await client.post("/accounting/journal-entries", headers=co["h"], json={
        "ts": "2026-01-15", "memo": "Adjustment", "idempotency_token": "shared-token",
        "entries": [{"account": "1111", "debit": 10, "credit": 0}, {"account": "4100", "debit": 0, "credit": 10}]})
    assert r.status_code == 200, r.text


async def test_a_replayed_ask_never_goes_to_an_address_the_member_no_longer_has(client, session, monkeypatch):
    """The recipient is read when the email goes out, never from the stored attempt: a member whose
    address changed after an unconfirmed send is not emailed at the old one by the relay's replay."""
    import uuid

    from sqlalchemy import update

    from celerp.models.company import User
    co, bk, client_h, lines = await _setup(client, session)
    monkeypatch.setattr("celerp.services.email.relay_identity", lambda: "account")
    first = _sent(monkeypatch, EmailResult(False, "timeout", unconfirmed=True))
    idem = B.key()
    assert (await _ask(client, co, bk, [lines["WITHDRAWAL"]["id"]], idem=idem)).json()["email"]["state"] == "unconfirmed"
    old = first[0]["to"]
    await session.execute(update(User).where(User.id == uuid.UUID(B.user_id(client_h))).values(email="moved@test.test"))
    await session.commit()
    calls = _sent(monkeypatch)
    again = (await _ask(client, co, bk, [lines["WITHDRAWAL"]["id"]], idem=idem)).json()
    assert old not in [c["to"] for c in calls] and again["email"]["state"] == "unconfirmed", (calls, again)


@pytest.mark.parametrize("url", ["", "https://books.example.com/"])
async def test_the_ask_email_links_the_questions_only_where_the_install_has_an_address(client, session, monkeypatch,
                                                                                         url):
    """With a public address the email links the questions page; without one it says where to find
    them, never a link to an address the install does not have."""
    from celerp.config import settings
    from celerp_accounting.statement_lines import QUESTIONS_URL
    from ui.i18n import t
    texts = []

    async def fake(to, subject, body_html, body_text="", **kw):
        texts.append(body_text)
        return EmailResult(True, "sent")
    monkeypatch.setattr("celerp.services.email.send_email", fake)
    monkeypatch.setattr(settings, "celerp_public_url", url)
    co, bk, client_h, lines = await _setup(client, session)
    assert (await _ask(client, co, bk, [lines["WITHDRAWAL"]["id"]])).status_code == 200
    where = str(t("acct.recon_questions_where"))
    assert texts and all((("https://books.example.com" + QUESTIONS_URL) in x) == bool(url) and (where in x) != bool(url)
                         for x in texts), texts
