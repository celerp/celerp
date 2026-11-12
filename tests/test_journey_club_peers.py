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
