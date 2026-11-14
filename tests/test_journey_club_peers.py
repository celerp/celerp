# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The members' club with its connected companies: what its accounting firm and its suppliers
send into books that use funds, through the API the pages use.

Initial conditions: the demo company paired with its accountant's copy of it
(test_peer_two_way_sync._pair_journals), the key also opening journals. The club (the client)
turns funds on with a restricted fund (Building fund) and holds 300.00 of restricted bank money
from a gift, 120.00 of it spent on the fund's purpose, before anything syncs.
"""
from __future__ import annotations

import uuid

import pytest

from migration_support import auth, real_client, real_engine  # noqa: F401
from test_company_backup_lifecycle import _data_dir  # noqa: F401
from test_company_handoff_identity_restore import _link_verified  # noqa: F401
from test_peer_fund_holds import (BANK, EXPENSE, _fund_bank, _funds_client, _held, _journal, _post_held)
from test_peer_sync_end_to_end import _route_to_app
from test_peer_two_way_sync import _from_firm, _journals, _pair_journals, _two_way

pytestmark = pytest.mark.asyncio

CORRECTION = [(BANK, 20.0, 0.0), (EXPENSE, 0.0, 20.0)]


async def _club(real_client, real_engine, monkeypatch):
    """The paired club with its restricted gift and purpose spending posted:
    (tok, firm_tok, club company, credential, fund)."""
    from test_funds import _manual

    tok, firm_tok, club, credential, _ = await _pair_journals(real_client, real_engine)
    fund = await _funds_client(real_client, tok)
    headers = {"headers": auth(tok)}
    for lines in ([(BANK, 300, 0, fund), ("4100", 0, 300, fund)], [(EXPENSE, 120, 0, fund), (BANK, 0, 120, fund)]):
        r = await _manual(real_client, headers, lines)
        assert r.status_code == 200, r.text
    _route_to_app(monkeypatch, [])
    return tok, firm_tok, club, credential, fund


async def test_club_firm_correction_of_restricted_purpose_expense(real_client, real_engine, monkeypatch):
    """The firm corrects 20.00 of the purpose expense back to the bank. At the club it is held on
    Sync accounts with no fund chosen, each line showing its account's balance per fund. Post with
    no choice is refused naming both lines; Bank Unrestricted with the expense restricted is
    refused as unbalanced in the restricted fund; both lines on the restricted fund post. Reverse
    from accountant takes the restricted fund back to where it was, and the firm's corrected
    journal sent again is held and posts on the restricted fund."""
    tok, firm_tok, club, credential, fund = await _club(real_client, real_engine, monkeypatch)
    await _journal(real_client, firm_tok, "Correct program costs", CORRECTION)
    await _two_way(club, credential)
    [item] = await _held(real_client, tok)
    assert _from_firm(await _journals(real_engine, club)) == {}
    assert "fund_choice" not in item["fix"] and all(e.get("fund_choice") is None for e in item["fix"]["entries"])
    bank, expense = item["fund_lines"]
    assert (bank["account"], bank["balances"][fund]) == (BANK, 180.0)
    assert (expense["account"], expense["balances"][fund]) == (EXPENSE, 120.0)

    r = await _post_held(real_client, tok, item)
    assert r.status_code == 409 and r.json()["detail"]["message_key"] == "posting.fund.choice_needed", r.text
    assert r.json()["fix"]["lines"] == [1, 2]
    r = await _post_held(real_client, tok, item, lines={1: "unrestricted", 2: fund})
    assert r.status_code == 422 and r.json()["detail"]["message_key"] == "posting.fund.unbalanced", r.text
    assert _from_firm(await _journals(real_engine, club)) == {} and len(await _held(real_client, tok)) == 1

    r = await _post_held(real_client, tok, item, lines={1: fund, 2: fund})
    assert r.status_code == 200, r.text
    je_id = r.json()["id"]
    assert await _fund_bank(real_engine, club, fund) == 200
    assert await _held(real_client, tok) == []

    r = await real_client.post(f"/accounting/journal-entries/{je_id}/reverse", headers=auth(tok))
    assert r.status_code == 200, r.text
    reversal = (await _journals(real_engine, club))[r.json()["je_id"]]
    assert [(e["account"], e["debit"], e["credit"], e.get("fund_id")) for e in reversal["entries"]] == [
        (BANK, 0.0, 20.0, fund), (EXPENSE, 20.0, 0.0, fund)]
    assert await _fund_bank(real_engine, club, fund) == 180
    results = await _two_way(club, credential)
    assert all(res.ok for res in results.values()), {k: res.errors for k, res in results.items()}

    await _journal(real_client, firm_tok, "Correct program costs again", CORRECTION)
    await _two_way(club, credential)
    [item] = await _held(real_client, tok)
    assert item["fix"]["memo"] == "Correct program costs again"
    assert (await _post_held(real_client, tok, item, every=fund)).status_code == 200
    assert await _fund_bank(real_engine, club, fund) == 200
    assert await _held(real_client, tok) == []


def _funds_held(ledger: dict, fund) -> dict:
    return {code: v for code, v in ledger.get(fund, {}).items() if v}


async def _ledger(real_engine, company: str) -> dict:
    from celerp.services.journal_accounts import fund_account_balances

    from migration_support import maker

    async with maker(real_engine)() as s:
        return await fund_account_balances(s, uuid.UUID(company))


async def test_club_connected_supplier_documents_by_fund(real_client, real_engine, monkeypatch):
    """The books receiving a connected company's documents use funds, with 300.00 of gift money
    in the restricted fund. A draft credit note from it is held at Finalize until its line's fund
    is chosen. A draft bill received before a second fund was added is finalized with its lines
    at --: refused naming both lines, the draft unchanged. Set all outside lines to the restricted
    fund with line 2 changed to Unrestricted; a local edit of the connected draft is refused
    naming the company that sent it, its choices kept. On a draft brought in by the document
    import, a line editor save renaming a line keeps its choices and a unit price edit clears
    them, saying which lines. The connected draft finalizes and the fund balance sheet matches
    the ledger. Change fund to Unrestricted leaves the restricted fund with the gift and the
    credit note, and changing back restores what the first posting held."""
    from test_funds import _fund, _manual
    from test_income_accounts import _line
    from test_outside_lines import _as_sent, _imported
    from test_peer_fund_holds import _choose_on, _chosen, _connected_draft, _copy_of, _state
    from test_peer_sync_end_to_end import _company_of

    tok, firm_tok, client_company, credential, fund, bill, copy = await _connected_draft(
        real_client, real_engine, monkeypatch)
    books = _company_of(firm_tok)
    h = auth(firm_tok)
    r = await _manual(real_client, {"headers": h}, [(BANK, 300, 0, fund), ("4100", 0, 300, fund)])
    assert r.status_code == 200, r.text

    note = await real_client.post("/docs", headers=auth(tok), json={
        "doc_type": "credit_note", "date": "2026-03-31", "idempotency_key": uuid.uuid4().hex,
        "line_items": [{"name": "Returned books", "quantity": 1, "unit_price": 15.0, "line_total": 15.0}]})
    assert note.status_code in (200, 201), note.text
    results = await _two_way(client_company, credential)
    assert results["documents_out"].ok, results["documents_out"].errors
    note_copy = await _copy_of(real_engine, books, note.json()["id"])
    r = await real_client.post(f"/docs/{note_copy}/finalize", headers=h)
    assert r.status_code == 409 and r.json()["fix"]["lines"] == [1], r.text
    [line] = (await _state(real_client, firm_tok, note_copy))["line_items"]
    await _choose_on(real_client, firm_tok, note_copy, lines={line["line_id"]: fund})
    assert (await real_client.post(f"/docs/{note_copy}/finalize", headers=h)).status_code == 200
    from_note = _funds_held(await _ledger(real_engine, books), fund)
    assert from_note == {BANK: 300, "4100": -285, "1120": -15}, from_note

    await _fund(real_client, {"headers": h}, "Reading programme")
    before = await _state(real_client, firm_tok, copy)
    r = await real_client.post(f"/docs/{copy}/finalize", headers=h)
    assert r.status_code == 409 and r.json()["detail"]["message_key"] == "posting.fund.choice_needed", r.text
    assert r.json()["fix"]["lines"] == [1, 2]
    after = await _state(real_client, firm_tok, copy)
    assert (after["status"], after["version"]) == ("draft", before["version"])

    first, second = (li["line_id"] for li in after["line_items"])
    await _choose_on(real_client, firm_tok, copy, all=fund)
    await _choose_on(real_client, firm_tok, copy, lines={second: "unrestricted"})
    chosen = (None, [(fund, fund), ("unrestricted", None)])
    state = await _state(real_client, firm_tok, copy)
    assert _chosen(state) == chosen

    a, b = state["line_items"]
    r = await real_client.patch(f"/docs/{copy}", headers=h, json={"expected_version": state["version"], "fields_changed": {
        "line_items": {"new": [_as_sent(a, name="Electricity, March"), _as_sent(b)]}}})
    assert r.status_code == 422 and r.json()["detail"]["message_key"] == "documents.err_peer_synced", r.text
    assert _chosen(await _state(real_client, firm_tok, copy)) == chosen

    filed = await _imported(real_client, {"headers": h}, [_line(None, "Filed A", 10), _line(None, "Filed B", 20)])
    await _choose_on(real_client, firm_tok, filed, all=fund)
    kept = (None, [(fund, fund), (fund, fund)])
    for change, cleared in (({"name": "Filed A, March"}, None), ({"unit_price": 12, "line_total": 12},
                                                                 {"lines": [1, 2], "document": False})):
        state = await _state(real_client, firm_tok, filed)
        a, b = state["line_items"]
        r = await real_client.patch(f"/docs/{filed}", headers=h, json={"expected_version": state["version"], "fields_changed": {
            "line_items": {"new": [_as_sent(a, **change), _as_sent(b)]}}})
        assert r.status_code == 200 and r.json().get("fund_choices_cleared") == cleared, r.text
        assert _chosen(await _state(real_client, firm_tok, filed)) == (kept if cleared is None else (None, [(None, None)] * 2))

    assert (await real_client.post(f"/docs/{copy}/finalize", headers=h)).status_code == 200
    posted = await _ledger(real_engine, books)
    assert _funds_held(posted, fund).get("6300") == 50
    sheet = (await real_client.get("/accounting/funds/balance-sheet", headers=h)).json()
    for row in sheet["accounts"]:  # shown on the account's normal side
        side = -1 if row["code"][0] in "23" else 1
        assert {f: float(v) for f, v in row["amounts"].items()} == {
            f or "unrestricted": side * float(held[row["code"]]) for f, held in posted.items() if held.get(row["code"])}

    for choices, held in (({"lines": {first: "unrestricted", second: "unrestricted"}}, from_note), ({"lines": {first: fund, second: "unrestricted"}}, None)):
        version = (await _state(real_client, firm_tok, copy))["version"]
        r = await real_client.post(f"/docs/{copy}/change-fund", headers=h, json={
            "expected_version": version, "request_key": uuid.uuid4().hex, "reason": "Charged to the wrong fund",
            **choices})
        assert r.status_code == 200, r.text
        ledger = await _ledger(real_engine, books)
        assert _funds_held(ledger, fund) == (held if held is not None else _funds_held(posted, fund))
    assert await _ledger(real_engine, books) == posted


async def _waiting(real_client, tok: str) -> dict:
    r = await real_client.get("/accounting/funds/waiting", headers=auth(tok))
    assert r.status_code == 200, r.text
    return r.json()


async def test_club_lineless_bill_standing_default_and_supplier_void(real_client, real_engine, monkeypatch):
    """The books receiving a connected company's documents use funds, with 300.00 of gift money
    in the restricted fund. The company's issued bill that arrives without lines is held and
    counted as 1 outside document waiting for a fund choice; Fund for this document is refused,
    since a document with no lines never posts, and it stays held. The accountant sets a standing
    default for the company: its next bill arrives tagged with the restricted fund and posts. The
    company voids the lineless bill and the count is gone. The default put the fund on each line,
    so changing only the tagged bill's document fund is refused as moving nothing; with its lines
    moved to Unrestricted by Change fund and then voided by the company, every fund is as before
    it arrived."""
    from celerp.services import peer_documents
    from test_fund_connection_defaults import _client_doc, _set_default, _tag
    from test_funds import _manual
    from test_peer_accounts import _bill
    from test_peer_fund_holds import _copy_of, _state
    from test_peer_sync_end_to_end import _company_of

    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    books, h = _company_of(firm_tok), auth(firm_tok)
    fund = await _funds_client(real_client, firm_tok)
    r = await _manual(real_client, {"headers": h}, [(BANK, 300, 0, fund), ("4100", 0, 300, fund)])
    assert r.status_code == 200, r.text
    before = await _ledger(real_engine, books)

    sent = peer_documents.transport_document
    monkeypatch.setattr(peer_documents, "transport_document", lambda state: {**sent(state), "line_items": []})
    lineless = await _bill(real_client, tok, [{"name": "Electricity", "quantity": 1, "unit_price": 80.0,
                                               "line_total": 80.0, "account_code": "6300",
                                               "receive_as": "expense"}], "lineless-club")
    _route_to_app(monkeypatch, [])
    assert not (await _two_way(client_company, credential))["documents_out"].ok
    monkeypatch.setattr(peer_documents, "transport_document", sent)
    [item] = await _held(real_client, firm_tok, "doc_funds")
    assert (item["fix"]["document"], item["fix"]["lines"]) == (True, [])
    assert {k: (await _waiting(real_client, firm_tok))[k] for k in ("count", "held")} == {"count": 1, "held": 1}
    r = await _post_held(real_client, firm_tok, item, every=fund)
    assert r.status_code == 422 and r.json()["detail"]["message_key"] == "documents.err_finalize_no_lines", r.text
    assert len(await _held(real_client, firm_tok, "doc_funds")) == 1

    assert (await _set_default(real_client, firm_tok, "connected_company", client_company, fund)).status_code == 200
    bill = await _client_doc(real_client, tok, issued=True)
    r = await real_client.post(f"/docs/{lineless}/void", headers=auth(tok), json={"reason": "Sent without lines"})
    assert r.status_code == 200, r.text
    results = await _two_way(client_company, credential)
    assert results["documents_out"].ok, results["documents_out"].errors
    copy = await _copy_of(real_engine, books, bill)
    state = await _state(real_client, firm_tok, copy)
    assert (state["status"], state["fund_choice"], state["fund_choice_source"]) == (
        "awaiting_payment", fund, _tag("connected_company", client_company))
    assert _funds_held(await _ledger(real_engine, books), fund).get("6300") == 80
    assert await _held(real_client, firm_tok, "doc_funds") == []
    assert (await _waiting(real_client, firm_tok))["count"] == 0

    change = {"expected_version": state["version"], "reason": "Paid from general funds"}
    r = await real_client.post(f"/docs/{copy}/change-fund", headers=h, json={
        **change, "request_key": uuid.uuid4().hex, "document": "unrestricted"})
    assert r.status_code == 422 and r.json()["detail"]["message_key"] == "posting.fund.change_moves_nothing", r.text
    r = await real_client.post(f"/docs/{copy}/change-fund", headers=h, json={
        **change, "request_key": uuid.uuid4().hex, "document": "unrestricted",
        "lines": {li["line_id"]: "unrestricted" for li in state["line_items"]}})
    assert r.status_code == 200, r.text
    assert _funds_held(await _ledger(real_engine, books), fund) == _funds_held(before, fund)
    r = await real_client.post(f"/docs/{bill}/void", headers=auth(tok), json={"reason": "Billed twice"})
    assert r.status_code == 200, r.text
    results = await _two_way(client_company, credential)
    assert results["documents_out"].ok, results["documents_out"].errors
    assert (await _state(real_client, firm_tok, copy))["status"] == "void"
    after = await _ledger(real_engine, books)
    assert {f: _funds_held(after, f) for f in after} == {f: _funds_held(before, f) for f in before}


async def test_club_journal_at_fund_firm_posted_with_choices_then_voided(real_client, real_engine, monkeypatch):
    """The accounting firm's copy of the club uses funds. The club's own journal is held at the
    firm with no fund chosen; Post with no choice is refused naming both lines, and a firm user
    posts it with a fund chosen on each line. The club's void of its journal is the way back (a
    company's own journal is undone by voiding it; Reverse is for the accountant's journals):
    on the next sync the firm's entry is void with no choice asked and the firm's restricted
    fund is back where it was."""
    from test_peer_fund_holds import _from_client, _held_client_journal
    from test_peer_sync_end_to_end import _company_of

    tok, firm_tok, club, credential, fund, item, club_je = await _held_client_journal(
        real_client, real_engine, monkeypatch, [(BANK, 60.0, 0.0), (EXPENSE, 0.0, 60.0)])
    firm = _company_of(firm_tok)
    assert _from_client(await _journals(real_engine, firm)) == {}
    r = await _post_held(real_client, firm_tok, item)
    assert r.status_code == 409 and r.json()["fix"]["lines"] == [1, 2], r.text

    r = await _post_held(real_client, firm_tok, item, lines={1: fund, 2: fund})
    assert r.status_code == 200, r.text
    [(copy, state)] = _from_client(await _journals(real_engine, firm)).items()
    assert [(e["account"], e.get("fund_id")) for e in state["entries"]] == [(BANK, fund), (EXPENSE, fund)]
    assert await _fund_bank(real_engine, firm, fund) == 60

    r = await real_client.post(f"/accounting/journal-entries/{club_je}/void", headers=auth(tok),
                               json={"reason": "Paid twice"})
    assert r.status_code == 200, r.text
    results = await _two_way(club, credential)
    assert all(res.ok for res in results.values()), {k: res.errors for k, res in results.items()}
    assert await _held(real_client, firm_tok) == []
    assert (await _journals(real_engine, firm))[copy]["status"] == "void"
    assert await _fund_bank(real_engine, firm, fund) == 0


async def test_club_firm_journal_posts_from_accountant_default(real_client, real_engine, monkeypatch):
    """The firm's first journal to the club, 40.00 more of the restricted fund's purpose paid from
    the bank, is held and the club posts it on the restricted fund. The club then sets the
    accountant's standing default to that fund: the firm's next journal, 25.00 more, posts on
    arrival with the fund on every line, tagged with the default, and nothing waits on Sync
    accounts."""
    from test_fund_connection_defaults import _set_default, _tag
    from test_peer_sync_end_to_end import _company_of

    tok, firm_tok, club, credential, fund = await _club(real_client, real_engine, monkeypatch)
    firm = _company_of(firm_tok)
    await _journal(real_client, firm_tok, "Hall repairs", [(EXPENSE, 40.0, 0.0), (BANK, 0.0, 40.0)])
    await _two_way(club, credential)
    [item] = await _held(real_client, tok)
    assert (await _post_held(real_client, tok, item, every=fund)).status_code == 200
    assert await _fund_bank(real_engine, club, fund) == 140

    r = await _set_default(real_client, tok, "accountant", firm, fund)
    assert r.status_code == 200, r.text
    await _journal(real_client, firm_tok, "Hall paint", [(EXPENSE, 25.0, 0.0), (BANK, 0.0, 25.0)])
    results = await _two_way(club, credential)
    assert all(res.ok for res in results.values()), {k: res.errors for k, res in results.items()}
    assert await _held(real_client, tok) == []
    [entry] = [s for s in _from_firm(await _journals(real_engine, club)).values() if s["memo"] == "Hall paint"]
    assert [(e["account"], e.get("fund_id")) for e in entry["entries"]] == [(EXPENSE, fund), (BANK, fund)]
    assert entry["fund_choice_source"] == _tag("accountant", firm)
    assert await _fund_bank(real_engine, club, fund) == 115
