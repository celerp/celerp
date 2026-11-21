# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A proposed change shows each record it names by the record's name on its confirm
card, so the person confirming can check which bank, line or contact it writes to."""
from __future__ import annotations

import re
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


async def _proposed_resolve(client, h, monkeypatch, decisions=None) -> tuple[str, dict, str]:
    """A question whose answer proposes resolving a bank line, by default posting it (``decisions``
    takes the line id and gives the decisions): (conversation, bank, line id)."""
    from ai_fake_cloud import FakeCloud, answer, install, tool_turn

    bank, line_id = await _line(client, h)
    tool = _resolve_tool(monkeypatch)
    proposed = (decisions or (lambda lid: [{"line_ids": [lid], "kind": "post", "account_code": "6100"}]))(line_id)

    def script(identity: str, body: dict) -> dict:
        if not any(m.get("role") == "tool" for m in body["messages"]):
            return tool_turn((tool, {"path": {"bank_id": bank["id"]}, "body": {"decisions": proposed}},
                              "call-resolve"))
        return answer("Done.")

    install(monkeypatch, FakeCloud(script))
    conv = (await client.post("/ai/conversations", headers=h, json={"title": None})).json()["id"]
    r = await client.post(f"/ai/conversations/{conv}/query", headers=h,
                          json={"query": "Post the fee to bank charges", "operation_id": _QUESTION})
    assert r.status_code == 202, r.text
    return conv, bank, line_id


async def _card_lines(client, h, conv, lang: str = "en") -> str:
    """The argument lines of the conversation's one open card, as its reader (in ``lang``) sees it now."""
    from fasthtml.common import to_xml
    from celerp_ai.ui_routes import _action_card
    from test_helpers import settled_question

    state = await settled_question(client, h, conv, _QUESTION)
    (card,) = state["message"]["pending_actions"]
    html = to_xml(_action_card(conv, state["message"]["id"], card, lang))
    return html.split("ai-action__lines", 1)[1].split("<a ", 1)[0]


@pytest.mark.asyncio
@pytest.mark.usefixtures("tools_open_when_called")
async def test_a_bank_resolve_card_names_the_bank_and_the_line_never_their_ids(client, auth, monkeypatch, drained_jobs):
    h = auth["headers"]
    conv, bank, line_id = await _proposed_resolve(client, h, monkeypatch)
    shown = await _card_lines(client, h, conv)
    assert bank["bank_name"] in shown and "Monthly fee" in shown and "-50.00" in shown
    assert bank["id"] not in shown and line_id not in shown


@pytest.mark.asyncio
@pytest.mark.usefixtures("tools_open_when_called")
async def test_a_resolve_card_names_each_journal_entry_it_matches_or_posts_past_never_its_id(
        client, auth, monkeypatch, drained_jobs):
    """The person confirming a match, or a posting kept separate from an entry, sees each entry by
    its date, memo and amount in their own language, never a raw journal entry id."""
    from ui.i18n import set_lang

    h = auth["headers"]
    [bank] = (await client.get("/accounting/bank-accounts", headers=h)).json()["items"]
    entries = []
    for memo in ("Card fee keyed", "Wire fee keyed"):
        r = await client.post("/accounting/journal-entries", headers=h, json={
            "ts": "2026-03-01", "memo": memo, "idempotency_token": str(uuid.uuid4()),
            "entries": [{"account": bank["chart_account_code"], "credit": 50.0}, {"account": "6100", "debit": 50.0}]})
        assert r.status_code == 200, r.text
        entries.append(r.json()["je_id"])
    kept, matched = entries
    conv, _, _ = await _proposed_resolve(client, h, monkeypatch, lambda lid: [
        {"line_ids": [lid], "kind": "post", "account_code": "6100", "separate_from": [kept]},
        {"line_ids": [lid], "kind": "match", "je_ids": [matched]}])
    for lang in ("en", "th", "ar"):
        set_lang(lang)
        shown = re.sub(r"<[^>]+>", " ", await _card_lines(client, h, conv, lang))
        assert "Card fee keyed" in shown and "Wire fee keyed" in shown, (lang, shown)
        assert "je:" not in shown and kept not in shown and matched not in shown, (lang, shown)
        assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", shown), (lang, shown)


@pytest.mark.asyncio
@pytest.mark.usefixtures("tools_open_when_called")
async def test_a_card_names_its_records_for_the_role_its_reader_holds_now(client, auth, session, monkeypatch,
                                                                          drained_jobs):
    """A reader whose role loses the bank pages after the proposal no longer sees the
    bank's or the line's name on the card, only that the name is not available."""
    from sqlalchemy import update
    from celerp.models.accounting import UserCompany
    from ui.i18n import t

    h = auth["headers"]
    conv, bank, _line_id = await _proposed_resolve(client, h, monkeypatch)
    assert bank["bank_name"] in await _card_lines(client, h, conv)

    await session.execute(update(UserCompany).where(UserCompany.company_id == auth["company_id"])
                          .values(role="operator"))
    await session.commit()
    shown = await _card_lines(client, h, conv)
    assert bank["bank_name"] not in shown and "Monthly fee" not in shown
    assert t("ai.card_name_unavailable") in shown


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

    owner = await record_names(session, company_id, uuid.uuid4(), {}, "owner", args)
    assert owner[contact_id] == "Harbour Supplies" and bank["bank_name"] in owner[bank["id"]]
    assert "Monthly fee" in owner[line_id] and "not-a-record" not in owner

    viewer = await record_names(session, company_id, uuid.uuid4(), {}, "viewer", args)
    assert viewer[contact_id] == "Harbour Supplies" and line_id not in viewer and bank["id"] not in viewer

    assert await record_names(session, company_id, uuid.uuid4(), {}, "no-such-role", {"body": {"contact_id": contact_id}}) == {}
    assert await record_names(session, uuid.uuid4(), uuid.uuid4(), {}, "owner", args) == {}


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


@pytest.mark.parametrize("number", [float("inf"), float("-inf"), float("nan"), 10**30, 1e30, -0.0, 1e-30, 80])
def test_a_card_shows_any_number_the_model_sends(number):
    """A quantity or amount past what the formatters keep exactly still reads on the card."""
    from fasthtml.common import to_xml
    from celerp_ai.ui_routes import _action_card

    action = {"id": "c1", "name": "adjust", "title": "Adjust",
              "shown": {"body": {"quantity": number, "amount": number, "unit_price": number}}}
    html = to_xml(_action_card("conv", "msg", action))
    assert html.count("ai-action__line-val") == 3


def test_a_card_reads_a_yes_or_no_choice_and_its_title_in_the_reader_language():
    """A true or false field reads as yes or no, and the title the card kept reads in the
    reader's language, with its params, or as written when no catalog has its key."""
    from fasthtml.common import to_xml
    from celerp_ai.ui_routes import _action_card
    from ui import i18n
    from ui.i18n import read_kept, t

    action = {"id": "c1", "name": "adjust", "title": {"key": "tool.no_such_tool", "params": {}, "text": "Adjust stock"},
              "shown": {"body": {"is_active": True, "taxable": False}}}
    kept = {"key": "error.fix_ask_roles", "params": {"place": "P", "roles": "R"}, "text": "as written"}
    i18n.set_lang("de")
    try:
        html = to_xml(_action_card("conv", "msg", action, "de"))
        assert read_kept(kept) == read_kept(kept, "de")
    finally:
        i18n.set_lang("en")
    assert t("settings.yes", "de") in html and t("settings.no", "de") in html and "Adjust stock" in html
    assert read_kept(kept, "de") == t("error.fix_ask_roles", "de", place="P", roles="R") != "as written"
    assert read_kept({**kept, "params": {}}, "de") == "as written"
    assert read_kept({**kept, "key": None}, "de") == "as written"
    assert read_kept("plain", "de") == "plain" and read_kept(None) == ""


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

    names = await record_names(session, company_id, ids["user_id"], {}, "owner",
                               {"file_ids": [march], "body": {"job_id": str(job.id)}})
    assert names == {march: "march-statement.pdf", str(job.id): "march-statement.pdf, april-statement.pdf"}
    assert await record_names(session, uuid.uuid4(), uuid.uuid4(), {}, "owner", {"file_ids": [march]}) == {}
    nameless = files.save_upload(company_id, ids["user_id"], None, "application/pdf", b"%PDF-1.4")
    assert await record_names(session, company_id, ids["user_id"], {}, "owner", {"file_ids": [nameless]}) == {}


async def test_another_user_of_the_same_company_never_reads_a_file_or_reading_name(
        auth, ids, session, tmp_path, monkeypatch):
    """Another person in the company, with the same role, sees neither the file name of an
    upload made for someone else's assistant nor that of a reading of it: the card says the
    name is not available, as the file and reading routes refuse that person the records."""
    from celerp.ai import files
    from celerp.ai.names import named_for_reader
    from celerp.models.ai import AIBatchJob
    from celerp.models.accounting import UserCompany
    from celerp.models.company import User

    monkeypatch.setattr(files, "upload_dir", lambda: tmp_path)
    company_id = auth["company_id"]
    other = uuid.uuid4()
    session.add(User(id=other, email=f"other-{other.hex[:8]}@test.example", name="Other", auth_hash="x",
                     is_active=True))
    await session.flush()
    session.add(UserCompany(user_id=other, company_id=company_id, role="owner", is_active=True))
    upload = files.save_upload(company_id, ids["user_id"], "payroll-march.pdf", "application/pdf", b"%PDF-1.4")
    job = AIBatchJob(company_id=company_id, user_id=ids["user_id"], status="done", query="read", file_ids=[upload])
    session.add(job)
    await session.flush()
    actions = [{"id": "c1", "arguments": {"file_ids": [upload], "body": {"job_id": str(job.id)}}}]

    mine = (await named_for_reader(session, company_id, ids["user_id"], actions))[0]["names"]
    assert mine == {upload: "payroll-march.pdf", str(job.id): "payroll-march.pdf"}
    assert (await named_for_reader(session, company_id, other, actions))[0]["names"] == {}

    # A colleague's reading is not named for this person either, even one that lists this
    # person's own upload: a reading is named only for the person it belongs to.
    theirs = AIBatchJob(company_id=company_id, user_id=other, status="done", query="read", file_ids=[upload])
    session.add(theirs)
    await session.flush()
    asked = [{"id": "c2", "arguments": {"body": {"job_id": str(theirs.id)}}}]
    assert (await named_for_reader(session, company_id, ids["user_id"], asked))[0]["names"] == {}


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

    assert await record_names(session, auth["company_id"], uuid.uuid4(), {}, "owner", {"body": {"contact_id": record}}) == {
        record: "Named record"}


async def test_a_namer_of_a_module_the_company_does_not_use_is_never_asked(auth, session, monkeypatch):
    """Only the namers of modules the company uses name its records; a record no namer
    names (its module has no namer, or the company does not use it) keeps no name."""
    from celerp.ai.names import record_names
    from celerp.modules import slots

    async def namer(ids, **_):
        return {i: "Named record" for i in ids}

    monkeypatch.setattr(slots, "get", lambda slot: [{"handler": "namer", "_module": "celerp-extra"}]
                        if slot == "record_names" else [])
    monkeypatch.setattr(slots, "resolve_handler", {"namer": namer}.__getitem__)
    record = str(uuid.uuid4())
    args = {"body": {"contact_id": record}}

    assert await record_names(session, auth["company_id"], uuid.uuid4(), {"enabled_modules": ["celerp-extra"]}, "owner",
                              args) == {record: "Named record"}
    assert await record_names(session, auth["company_id"], uuid.uuid4(), {"enabled_modules": ["celerp-accounting"]}, "owner",
                              args) == {}
    monkeypatch.setattr(slots, "get", lambda slot: [])
    assert await record_names(session, auth["company_id"], uuid.uuid4(), {}, "owner", args) == {}


@pytest.mark.asyncio
async def test_a_card_names_an_entry_by_its_amount_on_the_bank_in_the_bank_currency(client, auth, session):
    """An entry on a bank kept in another currency than the books is named by the figure the
    bank's statement shows, in the bank's currency and with its sign, not its books-currency debits."""
    from celerp.ai.names import record_names

    h = auth["headers"]
    r = await client.post("/accounting/bank-accounts", headers=h, json={
        "bank_name": "Euro Bank", "account_number": "0009", "bank_type": "checking", "currency": "EUR",
        "opening_balance": 0})
    assert r.status_code == 200, r.text
    eur = r.json()
    r = await client.post("/accounting/journal-entries", headers=h, json={
        "ts": "2026-03-01", "memo": "Euro fee keyed", "idempotency_token": str(uuid.uuid4()),
        "entries": [{"account": eur["chart_account_code"], "credit": 30.0, "currency": "EUR",
                     "rate": 40.0},
                    {"account": "6100", "debit": 1200.0}]})
    assert r.status_code == 200, r.text
    je_id = r.json()["je_id"]
    names = await record_names(session, auth["company_id"], uuid.uuid4(), {}, "owner",
                               {"body": {"decisions": [{"separate_from": [je_id]}]}})
    assert names[je_id] == "2026-03-01, Euro fee keyed, -30.00", names


def test_a_record_with_several_name_fields_is_named_by_the_first_that_holds_text():
    """A document carries both its number and a party name: the card reads the number, the
    first of the name fields in order, never the last one filled in."""
    from celerp.ai.names import state_name

    assert state_name({"doc_number": "INV-0042", "name": "Acme Ltd", "description": "Widgets"}) == "INV-0042"
    assert state_name({"doc_number": "  ", "name": "Acme Ltd", "description": "Widgets"}) == "Acme Ltd"
