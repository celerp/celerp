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
