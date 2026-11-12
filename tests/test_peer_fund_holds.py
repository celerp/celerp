# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Records a connected company sends into books that use funds: nothing posts until a local
user chooses a fund for every line. An accountant's journal at the client is held with the
funds fix and posted from the hold with the user's choices, judged at the posting boundary
under that user; a line key these books do not take is declined naming it.

Initial conditions: the demo company paired with its accountant's copy of it
(test_peer_sync_end_to_end._paired), the key also opening journals; the client turns funds
on and adds a restricted fund before the sync runs."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from migration_support import auth, maker, real_client, real_engine  # noqa: F401
from test_company_backup_lifecycle import _data_dir  # noqa: F401
from test_company_handoff_identity_restore import _link_verified  # noqa: F401
from test_peer_sync_end_to_end import _company_of, _route_to_app
from test_peer_two_way_sync import _attention, _from_firm, _journals, _notices, _pair_journals, _two_way

pytestmark = pytest.mark.asyncio

DAY = "2026-03-31"
BANK, EXPENSE = "1111", "6200"
WAITING = "Waiting for a fund on each line"


async def _funds_client(real_client, tok: str) -> str:
    """Funds on in the client's books, with a restricted fund; returns its id."""
    from test_funds import _fund, _funds_on

    headers = {"headers": auth(tok)}
    assert (await _funds_on(real_client, headers)).status_code == 200
    return await _fund(real_client, headers, "Building fund")


async def _journal(real_client, tok: str, memo: str, lines: list[tuple[str, float, float]]) -> str:
    r = await real_client.post("/accounting/journal-entries", headers=auth(tok), json={
        "ts": DAY, "memo": memo, "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": a, "debit": d, "credit": c} for a, d, c in lines]})
    assert r.status_code == 200, r.text
    return r.json()["je_id"]


async def _held(real_client, tok: str) -> list[dict]:
    r = await real_client.get("/accounting/peer/holds", headers=auth(tok))
    assert r.status_code == 200, r.text
    return [i for i in r.json()["items"] if (i["fix"] or {}).get("kind") == "funds"]


async def _post_held(real_client, tok: str, item: dict, *, every=None, lines: dict | None = None, key: str | None = None):
    choices = {"lines": {str(n): v for n, v in (lines or {}).items()}}
    if every is not None:
        choices["all"] = every
    return await real_client.post(f"/accounting/peer/holds/{item['id']}/funds", headers=auth(tok), json={
        "revision": item["fix"]["revision"], "choices": choices, "request_key": key or uuid.uuid4().hex})


async def _held_firm_journal(real_client, real_engine, monkeypatch, lines):
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    fund = await _funds_client(real_client, tok)
    firm_je = await _journal(real_client, firm_tok, "Reclass program costs", lines)
    _route_to_app(monkeypatch, [])
    await _two_way(client_company, credential)
    [item] = await _held(real_client, tok)
    return tok, firm_tok, client_company, credential, fund, item, firm_je


def _from_client(journals: dict) -> dict[str, dict]:
    return {eid: s for eid, s in journals.items() if s.get("peer_source")}


async def _held_client_journal(real_client, real_engine, monkeypatch, lines):
    """The firm's copy uses funds and has a restricted fund; the client's manual journal is
    held there. Returns (tok, firm_tok, client_company, credential, fund, item, client_je)."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    fund = await _funds_client(real_client, firm_tok)
    client_je = await _journal(real_client, tok, "Petty cash rent", lines)
    _route_to_app(monkeypatch, [])
    results = await _two_way(client_company, credential)
    assert not results["journals_out"].ok, results["journals_out"]
    [item] = await _held(real_client, firm_tok)
    return tok, firm_tok, client_company, credential, fund, item, client_je


async def _held_on(shape, real_client, real_engine, monkeypatch, lines):
    """A journal held for funds in the books that ``shape`` names: (posting user's token, the
    books' company, credential, client company, fund, hold, memo, origin, posted-entry filter)."""
    if shape == "firm_journal":
        tok, _, client_company, credential, fund, item, _ = await _held_firm_journal(
            real_client, real_engine, monkeypatch, lines)
        return tok, client_company, credential, client_company, fund, item, "Reclass program costs", "accountant", _from_firm
    _, firm_tok, client_company, credential, fund, item, _ = await _held_client_journal(
        real_client, real_engine, monkeypatch, lines)
    return firm_tok, _company_of(firm_tok), credential, client_company, fund, item, "Petty cash rent", "client", _from_client


async def test_firm_journal_in_fund_company_held_for_fund_assignment(real_client, real_engine, monkeypatch):
    """A client with funds receives Dr Bank 100 / Cr Program expense 100 from its accountant:
    nothing posts, the hold carries the funds fix with the placed lines and their origin, a
    notice opens From accountant, and the firm hears the journal was refused, waiting for a
    fund on each line. A void from the firm clears the hold."""
    tok, firm_tok, client_company, credential, _, item, firm_je = await _held_firm_journal(
        real_client, real_engine, monkeypatch, [(BANK, 100.0, 0.0), (EXPENSE, 0.0, 100.0)])
    assert _from_firm(await _journals(real_engine, client_company)) == {}
    fix = item["fix"]
    assert fix["memo"] == "Reclass program costs" and fix["ts"] == DAY and fix["revision"] >= 1
    assert [(e["account"], e["debit"], e["credit"], e["origin"]) for e in fix["entries"]] == [
        (BANK, 100.0, 0.0, "accountant"), (EXPENSE, 0.0, 100.0, "accountant")]
    assert item["reason"] == WAITING
    notices = [n for n in await _notices(real_engine, client_company) if "Reclass program costs" in n[1]]
    assert len(notices) == 1 and notices[0][2] == "/settings/connectors/celerp#from-accountant", notices
    declined = [i for i in await _attention(real_client, firm_tok) if i["kind"] == "declined"]
    assert [i["label"] for i in declined] == ["Reclass program costs"]
    assert declined[0]["reason"].endswith(f"did not post it: {WAITING}"), declined

    await _two_way(client_company, credential)  # held again, told once
    assert len(await _held(real_client, tok)) == 1
    assert len([n for n in await _notices(real_engine, client_company) if "Reclass program costs" in n[1]]) == 1

    r = await real_client.post(f"/accounting/journal-entries/{firm_je}/void", headers=auth(firm_tok), json={"reason": "Wrong"})
    assert r.status_code == 200, r.text
    await _two_way(client_company, credential)
    assert await _held(real_client, tok) == []
    assert _from_firm(await _journals(real_engine, client_company)) == {}


@pytest.mark.parametrize("shape", ["firm_journal", "client_journal"])
async def test_held_journal_line_without_choice_refused(real_client, real_engine, monkeypatch, shape):
    """With no entry choice and one line's choice missing, or cleared to null, Post is refused
    by check_outside_record naming that line, nothing posts and the hold stays; with the entry
    choice Unrestricted it posts with no fund on any line, as the posting user, and a repeat
    with the same request key returns the same entry. The same on the client for a journal of
    the firm's and on the firm for a journal of the client's."""
    tok, books, credential, client_company, fund, item, memo, origin, posted = await _held_on(
        shape, real_client, real_engine, monkeypatch, [(BANK, 100.0, 0.0), (EXPENSE, 0.0, 100.0)])
    for lines in ({1: fund}, {1: fund, 2: None}):
        r = await _post_held(real_client, tok, item, lines=lines)
        assert r.status_code == 409 and r.json()["detail"]["message_key"] == "posting.fund.choice_needed", r.text
        assert r.json()["detail"]["params"]["record"] == memo
        assert r.json()["detail"]["params"]["source"]["message_key"] == f"fund.origin.{origin}"
        assert r.json()["fix"]["lines"] == [2], r.json()
        assert posted(await _journals(real_engine, books)) == {}
        assert len(await _held(real_client, tok)) == 1

    key = uuid.uuid4().hex
    r = await _post_held(real_client, tok, item, every="unrestricted", key=key)
    assert r.status_code == 200, r.text
    [(je_id, state)] = posted(await _journals(real_engine, books)).items()
    assert r.json()["id"] == je_id
    assert state["fund_choice"] == "unrestricted" and state["status"] == "posted"
    assert [e.get("fund_id") for e in state["entries"]] == [None, None]
    assert {e.get("origin") for e in state["entries"]} == {origin}
    assert await _held(real_client, tok) == []
    assert (await _post_held(real_client, tok, item, every="unrestricted", key=key)).json()["id"] == je_id
    assert len(posted(await _journals(real_engine, books))) == 1

    actor = await _actor(real_engine, books, je_id)
    assert actor is not None and actor == await _user_of(tok)
    results = await _two_way(client_company, credential)  # the posted entry is not posted again
    assert all(r.ok for r in results.values()), {k: r.errors for k, r in results.items()}
    assert len(posted(await _journals(real_engine, books))) == 1
    assert await _held(real_client, tok) == []


async def test_client_journal_held_at_fund_firm_copy(real_client, real_engine, monkeypatch):
    """The firm's copy of a client uses funds: the client's journal is refused there with the
    funds fix, recorded once as a hold with the lines marked as the client's, and nothing
    posts; the client sends it again on each sync until a firm user posts it with a fund.
    The client's void of it then voids the firm's entry, with no choice asked."""
    tok, firm_tok, client_company, credential, fund, item, client_je = await _held_client_journal(
        real_client, real_engine, monkeypatch, [(BANK, 100.0, 0.0), (EXPENSE, 0.0, 100.0)])
    firm_company = _company_of(firm_tok)
    assert _from_client(await _journals(real_engine, firm_company)) == {}
    fix = item["fix"]
    assert fix["memo"] == "Petty cash rent" and fix["ts"] == DAY and fix["revision"] >= 1
    assert [(e["account"], e["debit"], e["credit"], e["origin"]) for e in fix["entries"]] == [
        (BANK, 100.0, 0.0, "client"), (EXPENSE, 0.0, 100.0, "client")]
    assert fix["lines"] == [1, 2]

    results = await _two_way(client_company, credential)  # sent again, still held once
    assert not results["journals_out"].ok
    assert len(await _held(real_client, firm_tok)) == 1
    assert _from_client(await _journals(real_engine, firm_company)) == {}

    r = await _post_held(real_client, firm_tok, item, every=fund)
    assert r.status_code == 200, r.text
    [(je_id, state)] = _from_client(await _journals(real_engine, firm_company)).items()
    assert state["peer_source"]["journal"] == client_je
    assert {e.get("fund_id") for e in state["entries"]} == {fund}

    r = await real_client.post(f"/accounting/journal-entries/{client_je}/void", headers=auth(tok), json={"reason": "Wrong"})
    assert r.status_code == 200, r.text
    results = await _two_way(client_company, credential)
    assert all(r.ok for r in results.values()), {k: r.errors for k, r in results.items()}
    assert await _held(real_client, firm_tok) == []
    state = (await _journals(real_engine, firm_company))[je_id]
    assert state["status"] == "void"


async def _actor(engine, company_id: str, entity_id: str):
    from celerp.models.ledger import LedgerEntry

    async with maker(engine)() as s:
        return await s.scalar(select(LedgerEntry.actor_id).where(
            LedgerEntry.company_id == uuid.UUID(company_id), LedgerEntry.entity_id == entity_id,
            LedgerEntry.event_type == "acc.journal_entry.created"))


async def _user_of(tok: str):
    from celerp.services.auth import decode_access_token

    return uuid.UUID(str(decode_access_token(tok)["sub"]))


async def test_held_journal_fund_moving_assignment_refused(real_client, real_engine, monkeypatch):
    """A held journal 1 Dr Bank 40, 2 Cr Program expense 40, 3 Dr Program expense 40,
    4 Cr Bank 40 with lines 1-2 Unrestricted and 3-4 the restricted fund balances in each
    fund but takes 40 of bank out of the restricted fund into the unrestricted one: refused
    as a restricted fund movement under the posting user, the hold stays. All four lines in
    the restricted fund post."""
    tok, _, client_company, _, fund, item, _ = await _held_firm_journal(
        real_client, real_engine, monkeypatch,
        [(BANK, 40.0, 0.0), (EXPENSE, 0.0, 40.0), (EXPENSE, 40.0, 0.0), (BANK, 0.0, 40.0)])
    r = await _post_held(real_client, tok, item, lines={1: "unrestricted", 2: "unrestricted", 3: fund, 4: fund})
    assert r.status_code in (403, 409, 422) and r.json()["detail"]["message_key"].startswith("posting.fund."), r.text
    assert r.json()["detail"]["message_key"] != "posting.fund.choice_needed", r.text
    assert _from_firm(await _journals(real_engine, client_company)) == {}
    assert len(await _held(real_client, tok)) == 1

    r = await _post_held(real_client, tok, item, every=fund)
    assert r.status_code == 200, r.text
    [state] = _from_firm(await _journals(real_engine, client_company)).values()
    assert {e.get("fund_id") for e in state["entries"]} == {fund}


def _feed_with(monkeypatch, key: str, value) -> None:
    """The firm's journal feed with ``key`` added to the first line of every journal."""
    from celerp.services import peer_journals

    feed = peer_journals.journal_feed

    async def with_key(*args, **kwargs):
        body = await feed(*args, **kwargs)
        for journal in body["items"]:
            journal["entries"][0][key] = value
        return body

    monkeypatch.setattr(peer_journals, "journal_feed", with_key)


@pytest.mark.parametrize("key", ["fund_id", "budget_line"])
async def test_firm_journal_fund_id_refused(real_client, real_engine, monkeypatch, key):
    """A firm line carrying fund_id, or any key the journal transport does not carry, is
    declined naming the key; nothing posts and nothing is held for funds."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    await _funds_client(real_client, tok)
    await _journal(real_client, firm_tok, "Carries a fund", [(BANK, 10.0, 0.0), (EXPENSE, 0.0, 10.0)])
    _feed_with(monkeypatch, key, "fund:elsewhere")
    _route_to_app(monkeypatch, [])
    results = await _two_way(client_company, credential)
    assert _from_firm(await _journals(real_engine, client_company)) == {}
    assert await _held(real_client, tok) == []
    [entry] = results["journals"].attention
    assert key in entry["reason"], entry


async def test_firm_journal_from_release_before_funds_posts(real_client, real_engine, monkeypatch):
    """A firm on a release before funds sends its lines with account_roles: the journal is not
    declined; it posts in books without funds, each line with the roles these books give it."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    await _journal(real_client, firm_tok, "Older firm", [(BANK, 10.0, 0.0), (EXPENSE, 0.0, 10.0)])
    _feed_with(monkeypatch, "account_roles", ["sent_by_firm"])
    _route_to_app(monkeypatch, [])
    results = await _two_way(client_company, credential)
    assert results["journals"].attention == []
    [state] = _from_firm(await _journals(real_engine, client_company)).values()
    assert all(e.get("account_roles") != ["sent_by_firm"] for e in state["entries"]), state["entries"]
