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


async def _held(real_client, tok: str, kind: str = "funds") -> list[dict]:
    r = await real_client.get("/accounting/peer/holds", headers=auth(tok))
    assert r.status_code == 200, r.text
    return [i for i in r.json()["items"] if (i["fix"] or {}).get("kind") == kind]


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


async def test_held_lines_carry_balance_per_fund(real_client, real_engine, monkeypatch):
    """Each line of a record held for funds names the account it posts to and that account's
    posted balance per fund ("unrestricted" for the unrestricted group, on the account's normal
    side), read when Sync accounts lists it, so the user sees what each fund holds before choosing."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from celerp.services.journal_accounts import fund_account_balances
    from test_funds import _manual

    tok, _, client_company, _, fund, item, _ = await _held_firm_journal(
        real_client, real_engine, monkeypatch, [(BANK, 100.0, 0.0), (EXPENSE, 0.0, 100.0)])
    headers = {"headers": auth(tok)}
    assert (await _manual(real_client, headers, [(BANK, 60, 0, fund), ("4100", 0, 60, fund)])).status_code == 200
    [item] = await _held(real_client, tok)
    bank, expense = item["fund_lines"]
    assert bank["account"] == BANK and expense["account"] == EXPENSE
    assert bank["balances"][fund] == 60.0
    async with AsyncSession(real_engine) as session:
        held = await fund_account_balances(session, uuid.UUID(str(client_company)))
    assert bank["balances"].get("unrestricted", 0.0) == float(held.get(None, {}).get(BANK, 0))
    assert fund not in expense["balances"]
    # A record held for an account, not a fund, carries no fund lines.
    assert all("fund_lines" not in i for i in (await real_client.get(
        "/accounting/peer/holds", headers=auth(tok))).json()["items"] if (i["fix"] or {}).get("kind") != "funds")


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


async def _fund_total(engine, company_id: str, fund: str):
    from celerp.services.journal_accounts import fund_account_balances

    async with maker(engine)() as s:
        return sum((await fund_account_balances(s, uuid.UUID(company_id))).get(fund, {}).values())


async def test_held_journal_posted_by_client_user_with_funds(real_client, real_engine, monkeypatch):
    """A manager of the client chooses the restricted fund for both lines of the firm's
    journal and posts it: the entry posts under the firm journal's local id with the manager
    as its actor, a repeat with the same request key returns it, and the fund's balances are
    unchanged in total."""
    from celerp.services.peer_journals import local_id
    from test_fund_connection_defaults import _member

    tok, firm_tok, client_company, _, fund, item, firm_je = await _held_firm_journal(
        real_client, real_engine, monkeypatch, [(BANK, 100.0, 0.0), (EXPENSE, 0.0, 100.0)])
    manager, manager_id = await _member(real_client, tok, "manager")
    key = uuid.uuid4().hex
    r = await _post_held(real_client, manager, item, lines={1: fund, 2: fund}, key=key)
    assert r.status_code == 200, r.text
    je_id = local_id(uuid.UUID(_company_of(firm_tok)), firm_je)
    assert r.json()["id"] == je_id
    [(posted_id, state)] = _from_firm(await _journals(real_engine, client_company)).items()
    assert posted_id == je_id and state["status"] == "posted"
    assert [(e["account"], e.get("fund_id"), e.get("fund_choice")) for e in state["entries"]] == [
        (BANK, fund, fund), (EXPENSE, fund, fund)]
    assert await _actor(real_engine, client_company, je_id) == uuid.UUID(manager_id)
    assert (await _post_held(real_client, manager, item, lines={1: fund, 2: fund}, key=key)).json()["id"] == je_id
    assert len(_from_firm(await _journals(real_engine, client_company))) == 1
    assert await _fund_total(real_engine, client_company, fund) == 0
    assert await _held(real_client, tok) == []


async def test_held_journal_unbalanced_fund_assignment_refused(real_client, real_engine, monkeypatch):
    """Dr Bank 40 / Cr Program expense 40 with Bank Unrestricted and Program expense the
    restricted fund passes the choice checks and every line is covered, so the posting
    boundary refuses it as unbalanced, naming the restricted fund and the 40, before any
    movement shape is judged; the hold stays. Both lines in the restricted fund post."""
    tok, _, client_company, _, fund, item, _ = await _held_firm_journal(
        real_client, real_engine, monkeypatch, [(BANK, 40.0, 0.0), (EXPENSE, 0.0, 40.0)])
    r = await _post_held(real_client, tok, item, lines={1: "unrestricted", 2: fund})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "posting.fund.unbalanced", detail
    assert (detail["params"]["fund"], detail["params"]["difference"]) == ("Building fund", "40.00"), detail
    assert _from_firm(await _journals(real_engine, client_company)) == {}
    assert len(await _held(real_client, tok)) == 1

    r = await _post_held(real_client, tok, item, lines={1: fund, 2: fund})
    assert r.status_code == 200, r.text
    [state] = _from_firm(await _journals(real_engine, client_company)).values()
    assert {e.get("fund_id") for e in state["entries"]} == {fund}


async def test_held_journal_post_refusals_isolated(real_client, real_engine, monkeypatch):
    """Post from the hold refuses in check order, each case passing every earlier check and
    the hold staying: a user without manage_accounting; no request key; a revision other than
    the hold's; a fund of no company, and a line the entry does not have; an archived fund.
    The same post with an active fund then posts."""
    from test_fund_connection_defaults import _member
    from test_funds import _fund

    tok, _, client_company, _, fund, item, _ = await _held_firm_journal(
        real_client, real_engine, monkeypatch, [(BANK, 25.0, 0.0), (EXPENSE, 0.0, 25.0)])
    viewer, _ = await _member(real_client, tok, "viewer")
    archived = await _fund(real_client, {"headers": auth(tok)}, "Closed appeal")
    r = await real_client.post(f"/accounting/funds/{archived}/archive", headers=auth(tok))
    assert r.status_code == 200, r.text
    url = f"/accounting/peer/holds/{item['id']}/funds"
    body = {"revision": item["fix"]["revision"], "choices": {"all": fund, "lines": {}}}

    async def refused(status: int, key: str | None, tok_=tok, **changes):
        r = await real_client.post(url, headers=auth(tok_), json={**body, "request_key": uuid.uuid4().hex, **changes})
        assert r.status_code == status, r.text
        if key:
            assert r.json()["detail"]["message_key"] == key, r.text
        assert _from_firm(await _journals(real_engine, client_company)) == {}
        assert len(await _held(real_client, tok)) == 1

    await refused(403, None, tok_=viewer)
    r = await real_client.post(url, headers=auth(tok), json=body)
    assert r.status_code == 422 and "request_key" in r.text, r.text
    await refused(409, "posting.fund.hold_changed", revision=item["fix"]["revision"] + 1)
    await refused(422, "posting.fund.unknown", choices={"all": "fund:nowhere", "lines": {}})
    await refused(422, "posting.fund.choice_line_unknown", choices={"all": fund, "lines": {"3": fund}})
    await refused(422, "posting.fund.archived", choices={"all": archived, "lines": {}})

    r = await real_client.post(url, headers=auth(tok), json={**body, "request_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    [state] = _from_firm(await _journals(real_engine, client_company)).values()
    assert {e.get("fund_id") for e in state["entries"]} == {fund}


async def test_hold_funds_stale_revision_refused(real_client, real_engine, monkeypatch):
    """The accountant revises a held journal (40 becomes 60) and the next sync replaces the
    hold's lines: a post with the revision read before is refused as changed, nothing posts
    and the hold keeps the new lines; the post at the new revision posts the 60."""
    from celerp.services import peer_journals

    tok, _, client_company, credential, fund, item, _ = await _held_firm_journal(
        real_client, real_engine, monkeypatch, [(BANK, 40.0, 0.0), (EXPENSE, 0.0, 40.0)])
    feed = peer_journals.journal_feed

    async def revised(*args, **kwargs):
        body = await feed(*args, **kwargs)
        for journal in body["items"]:
            journal["revision"] += 1
            journal["entries"] = [{**e, **{k: 60.0 for k in ("debit", "credit") if e.get(k)}}
                                  for e in journal["entries"]]
        return body

    monkeypatch.setattr(peer_journals, "journal_feed", revised)
    await _two_way(client_company, credential)
    [again] = await _held(real_client, tok)
    assert again["fix"]["revision"] == item["fix"]["revision"] + 1
    new_lines = [(e["account"], e["debit"], e["credit"]) for e in again["fix"]["entries"]]
    assert new_lines == [(BANK, 60.0, 0.0), (EXPENSE, 0.0, 60.0)]

    r = await _post_held(real_client, tok, item, every=fund)
    assert r.status_code == 409 and r.json()["detail"]["message_key"] == "posting.fund.hold_changed", r.text
    assert _from_firm(await _journals(real_engine, client_company)) == {}
    [kept] = await _held(real_client, tok)
    assert [(e["account"], e["debit"], e["credit"]) for e in kept["fix"]["entries"]] == new_lines

    r = await _post_held(real_client, tok, again, every=fund)
    assert r.status_code == 200, r.text
    [state] = _from_firm(await _journals(real_engine, client_company)).values()
    assert [(e["account"], e["debit"], e["credit"]) for e in state["entries"]] == new_lines


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


async def test_client_reversal_of_firm_journal_keeps_its_fund(real_client, real_engine, monkeypatch):
    """The firm's copy uses funds and its own journal posted with a fund; the client reverses
    that journal and the reversal posts at the firm with each line's fund taken from the entry
    it reverses, with no hold and no choice asked."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    firm_company = _company_of(firm_tok)
    fund = await _funds_client(real_client, firm_tok)
    r = await real_client.post("/accounting/journal-entries", headers=auth(firm_tok), json={
        "ts": DAY, "memo": "Grant interest", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": BANK, "debit": 70.0, "credit": 0.0, "fund_id": fund},
                    {"account": EXPENSE, "debit": 0.0, "credit": 70.0, "fund_id": fund}]})
    assert r.status_code == 200, r.text
    firm_je = r.json()["je_id"]
    _route_to_app(monkeypatch, [])
    await _two_way(client_company, credential)
    [(client_je, _)] = _from_firm(await _journals(real_engine, client_company)).items()

    r = await real_client.post(f"/accounting/journal-entries/{client_je}/reverse", headers=auth(tok))
    assert r.status_code == 200, r.text
    results = await _two_way(client_company, credential)
    assert all(r.ok for r in results.values()), {k: r.errors for k, r in results.items()}
    assert await _held(real_client, firm_tok) == []
    [mirrored] = [s for s in (await _journals(real_engine, firm_company)).values() if s.get("reverses") == firm_je]
    assert mirrored["status"] == "posted"
    assert [(e["account"], e["debit"], e["credit"], e.get("fund_id"), e.get("posted_from"))
            for e in mirrored["entries"]] == [(BANK, 0.0, 70.0, fund, firm_je), (EXPENSE, 70.0, 0.0, fund, firm_je)]


async def _copy_of(engine, company_id: str, source_doc: str) -> str | None:
    from celerp.models.company_peer import CompanyPeerIdentity

    async with maker(engine)() as s:
        return await s.scalar(select(CompanyPeerIdentity.target_entity_id).where(
            CompanyPeerIdentity.company_id == uuid.UUID(company_id),
            CompanyPeerIdentity.entity_type == "doc", CompanyPeerIdentity.source_entity_id == source_doc))


def _entries_of(journals: dict, record_id: str) -> list[tuple[str, dict]]:
    return [(eid, s) for eid, s in journals.items() if s.get("record_id") == record_id and s.get("status") == "posted"]


async def test_peer_document_held_for_fund_choices(real_client, real_engine, monkeypatch):
    """The firm's copy uses funds: a client's issued bill is refused there with the doc_funds
    fix, held once and posting nothing while the client sends it again on each sync; a firm
    user posts it from the hold with a fund, as that user, every leg on the chosen fund. An
    issued invoice held the same way and then voided by the client leaves no hold and posts
    nothing. Issued credit notes from a connected company are refused before they post, so
    the invoice stands for the second document type."""
    from test_peer_accounts import _bill

    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    firm_company = _company_of(firm_tok)
    fund = await _funds_client(real_client, firm_tok)
    bill = await _bill(real_client, tok, [{"name": "Electricity", "quantity": 1, "unit_price": 50.0,
                                           "line_total": 50.0, "account_code": "6300",
                                           "receive_as": "expense"}], "fund-bill")
    _route_to_app(monkeypatch, [])
    results = await _two_way(client_company, credential)
    assert not results["documents_out"].ok
    [item] = await _held(real_client, firm_tok, "doc_funds")
    fix = item["fix"]
    assert fix["lines"] == [1] and fix["document"] is True and fix["revision"] >= 1
    assert await _copy_of(real_engine, firm_company, bill) is None  # nothing posts

    results = await _two_way(client_company, credential)  # sent again, still held once
    assert not results["documents_out"].ok
    assert len(await _held(real_client, firm_tok, "doc_funds")) == 1
    assert await _copy_of(real_engine, firm_company, bill) is None

    r = await _post_held(real_client, firm_tok, item, every=fund)
    assert r.status_code == 200, r.text
    doc_id = await _copy_of(real_engine, firm_company, bill)
    assert doc_id and r.json()["id"] == doc_id
    [(je_id, entry)] = _entries_of(await _journals(real_engine, firm_company), doc_id)
    assert {e.get("fund_id") for e in entry["entries"]} == {fund}
    assert await _actor(real_engine, firm_company, je_id) == await _user_of(firm_tok)
    assert await _held(real_client, firm_tok, "doc_funds") == []
    results = await _two_way(client_company, credential)
    assert results["documents_out"].ok, results["documents_out"].errors

    invoice = await real_client.post("/docs", headers=auth(tok), json={
        "doc_type": "invoice", "date": DAY, "idempotency_key": uuid.uuid4().hex,
        "line_items": [{"name": "Consulting", "quantity": 1, "unit_price": 80.0, "line_total": 80.0}]})
    assert invoice.status_code in (200, 201), invoice.text
    invoice = invoice.json()["id"]
    assert (await real_client.post(f"/docs/{invoice}/finalize", headers=auth(tok))).status_code == 200
    await _two_way(client_company, credential)
    assert len(await _held(real_client, firm_tok, "doc_funds")) == 1
    r = await real_client.post(f"/docs/{invoice}/void", headers=auth(tok), json={"reason": "Wrong client"})
    assert r.status_code == 200, r.text
    results = await _two_way(client_company, credential)
    assert results["documents_out"].ok, results["documents_out"].errors
    assert await _held(real_client, firm_tok, "doc_funds") == []
    assert await _copy_of(real_engine, firm_company, invoice) is None


async def _state(real_client, tok: str, doc: str) -> dict:
    r = await real_client.get(f"/docs/{doc}", headers=auth(tok))
    assert r.status_code == 200, r.text
    return r.json()


async def _revise(real_client, tok: str, doc: str, **fields) -> None:
    version = (await _state(real_client, tok, doc))["version"]
    r = await real_client.patch(f"/docs/{doc}", headers=auth(tok), json={
        "expected_version": version, "fields_changed": {key: {"new": value} for key, value in fields.items()}})
    assert r.status_code == 200, r.text


async def _choose_on(real_client, tok: str, doc: str, **body):
    version = (await _state(real_client, tok, doc))["version"]
    r = await real_client.post(f"/docs/{doc}/fund-choices", headers=auth(tok),
                               json={"expected_version": version, **body})
    assert r.status_code == 200, r.text


def _chosen(state: dict) -> tuple:
    return state.get("fund_choice"), [(li.get("fund_choice"), li.get("fund_id")) for li in state["line_items"]]


def _reason(state: dict) -> str | None:
    return (state.get("fund_choice_reason") or {}).get("message_key")


_LINES = [{"name": "Electricity", "quantity": 1, "unit_price": 50.0, "line_total": 50.0,
           "account_code": "6300", "receive_as": "expense"},
          {"name": "Water", "quantity": 1, "unit_price": 30.0, "line_total": 30.0,
           "account_code": "6300", "receive_as": "expense"}]


async def _lineless(shape, real_client, real_engine, monkeypatch):
    """An outside document with no lines in books that use funds, as ``shape`` brings it:
    (token, books, fund, draft document id or None, held item or None). The import is a
    record issued with header money only, the one way a lineless document posts."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    if shape == "peer":
        from celerp.services import peer_documents
        from test_peer_accounts import _bill

        fund = await _funds_client(real_client, firm_tok)
        sent = peer_documents.transport_document
        monkeypatch.setattr(peer_documents, "transport_document", lambda state: {**sent(state), "line_items": []})
        await _bill(real_client, tok, [{"name": "Electricity", "quantity": 1, "unit_price": 80.0, "line_total": 80.0,
                                        "account_code": "6300", "receive_as": "expense"}], f"lineless-{uuid.uuid4().hex[:6]}")
        _route_to_app(monkeypatch, [])
        assert not (await _two_way(client_company, credential))["documents_out"].ok
        [item] = await _held(real_client, firm_tok, "doc_funds")
        return firm_tok, _company_of(firm_tok), fund, None, item
    fund = await _funds_client(real_client, tok)
    ref = f"LL-{uuid.uuid4().hex[:6]}"
    if shape == "connector":
        from celerp.events.engine import connector_upsert

        async with maker(real_engine)() as s:
            await connector_upsert(s, company_id=uuid.UUID(client_company), entity_type="doc", event_type="doc.created",
                                   idem_key=f"quickbooks:bill:{ref}", origin="accounting_system", data={
                                       "doc_type": "bill", "ref_id": ref, "status": "draft", "total": 80,
                                       "currency": "USD", "line_items": []})
            await s.commit()
        return tok, client_company, fund, f"doc:quickbooks:bill:{ref}", None
    return tok, client_company, fund, None, None


async def _issued_lineless(real_client, tok: str, **fund_choices):
    eid = f"doc:IMP-{uuid.uuid4().hex[:8]}"
    r = await real_client.post("/docs/import/batch", headers=auth(tok), json={**fund_choices, "records": [{
        "entity_id": eid, "event_type": "doc.created", "source": "csv", "idempotency_key": uuid.uuid4().hex,
        "data": {"doc_type": "invoice", "status": "final", "ref_id": eid.split(":")[1], "issue_date": DAY,
                 "line_items": [], "shipping": 15, "total": 15, "import_treatment": "record_now"}}]})
    assert r.status_code == 200, r.text
    return eid, r.json()


@pytest.mark.parametrize("shape", ["connector", "import", "peer"])
async def test_lineless_outside_document_needs_document_choice(real_client, real_engine, monkeypatch, shape):
    """In books that use funds, an outside document with no lines posts nothing no one chose.
    A batch import invoice issued with header money only is refused naming the document and
    asking for its fund, and with the document's fund chosen on the import it posts every
    leg on that fund. A connected accounting system's draft bill of one total stays a draft,
    and a connected company's issued bill that arrives without lines stays held, with the
    fund chosen too: a document with no lines is never finalized."""
    tok, books, fund, doc, item = await _lineless(shape, real_client, real_engine, monkeypatch)
    before = dict(await _journals(real_engine, books))
    if shape == "import":
        eid, body = await _issued_lineless(real_client, tok)
        assert body["created"] == 0 and any(
            e.startswith(f"{eid}: ") and "choose the fund for this document" in e for e in body["errors"]), body
        assert await _journals(real_engine, books) == before
        eid, body = await _issued_lineless(real_client, tok, fund_choices={"all": fund})
        assert body["created"] == 1, body
        [(_, entry)] = _entries_of(await _journals(real_engine, books), eid)
        assert {e.get("fund_id") for e in entry["entries"]} == {fund}, entry["entries"]
        return
    if item is not None:
        assert (item["fix"]["document"], item["fix"]["lines"]) == (True, []), item
        assert item["reason"].endswith("choose the fund for this document."), item
        r = await _post_held(real_client, tok, item, every=fund)
        assert r.status_code == 422 and r.json()["detail"]["message_key"] == "documents.err_finalize_no_lines", r.text
        assert len(await _held(real_client, tok, "doc_funds")) == 1
    else:
        await _choose_on(real_client, tok, doc, document=fund)
        r = await real_client.post(f"/docs/{doc}/finalize", headers=auth(tok))
        assert r.status_code == 422 and r.json()["detail"]["message_key"] == "documents.err_finalize_no_lines", r.text
        assert (await _state(real_client, tok, doc))["status"] == "draft"
    assert await _journals(real_engine, books) == before

async def _connected_draft(real_client, real_engine, monkeypatch):
    """A client's draft bill of two expense lines copied as a draft into the firm's books,
    which use funds. Returns (tok, firm_tok, client_company, credential, fund, bill, copy)."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    fund = await _funds_client(real_client, firm_tok)
    vendor = await real_client.post("/crm/contacts", json={"name": "Utility Co", "contact_type": "vendor"},
                                    headers=auth(tok))
    assert vendor.status_code in (200, 201), vendor.text
    r = await real_client.post("/docs", headers=auth(tok), json={
        "doc_type": "bill", "contact_id": vendor.json()["id"], "date": DAY, "line_items": _LINES,
        "idempotency_key": uuid.uuid4().hex})
    assert r.status_code in (200, 201), r.text
    bill = r.json()["id"]
    _route_to_app(monkeypatch, [])
    results = await _two_way(client_company, credential)
    assert results["documents_out"].ok, results["documents_out"].errors
    copy = await _copy_of(real_engine, _company_of(firm_tok), bill)
    assert copy and (await _state(real_client, firm_tok, copy))["status"] == "draft"
    return tok, firm_tok, client_company, credential, fund, bill, copy


async def _synced(real_client, firm_tok, client_company, credential, copy) -> dict:
    results = await _two_way(client_company, credential)
    assert results["documents_out"].ok, results["documents_out"].errors
    return await _state(real_client, firm_tok, copy)


@pytest.mark.parametrize("shape", ["document", "firm_journal"])
async def test_local_fund_choice_survives_unchanged_peer_revision(real_client, real_engine, monkeypatch, shape):
    """A firm user chose the fund on both lines of a client's draft bill: a revision that
    changes no money keeps them; one changing only the shipping clears every choice; one
    changing line 2's quantity and adding line 3 clears them and Finalize names lines 1 to 3;
    every line keeps where it came from. A held firm journal stores no choice, so a refused
    post leaves the hold as it was and its next revision starts with none."""
    if shape == "firm_journal":
        tok, _, client_company, credential, fund, item, _ = await _held_firm_journal(
            real_client, real_engine, monkeypatch, [(EXPENSE, 40.0, 0.0), (BANK, 0.0, 40.0)])
        assert (await _post_held(real_client, tok, item, lines={1: fund})).status_code == 409
        await _two_way(client_company, credential)
        [again] = await _held(real_client, tok)
        assert again["fix"] == item["fix"] and fund not in str(again["fix"])
        return
    tok, firm_tok, client_company, credential, fund, bill, copy = await _connected_draft(
        real_client, real_engine, monkeypatch)
    lines = (await _state(real_client, firm_tok, copy))["line_items"]
    await _choose_on(real_client, firm_tok, copy, lines={li["line_id"]: fund for li in lines})
    chosen = (None, [(fund, fund), (fund, fund)])

    await _revise(real_client, tok, bill, notes="Meter read on the 30th")
    state = await _synced(real_client, firm_tok, client_company, credential, copy)
    assert state["notes"] == "Meter read on the 30th"
    assert _chosen(state) == chosen and _reason(state) is None

    await _revise(real_client, tok, bill, shipping=5.0)
    state = await _synced(real_client, firm_tok, client_company, credential, copy)
    assert _chosen(state) == (None, [(None, None), (None, None)])
    assert _reason(state) == "fund.choices_cleared_by_source.total"

    await _choose_on(real_client, firm_tok, copy, lines={li["line_id"]: fund for li in state["line_items"]})
    assert _chosen(await _state(real_client, firm_tok, copy)) == chosen
    revised = [_LINES[0], {**_LINES[1], "quantity": 2, "line_total": 60.0},
               {"name": "Sewer", "quantity": 1, "unit_price": 10.0, "line_total": 10.0,
                "account_code": "6300", "receive_as": "expense"}]
    await _revise(real_client, tok, bill, line_items=revised)
    state = await _synced(real_client, firm_tok, client_company, credential, copy)
    assert _chosen(state) == (None, [(None, None)] * 3)
    assert _reason(state) == "fund.choices_cleared_by_source.lines"
    assert [li.get("origin") for li in state["line_items"]] == ["connected_company"] * 3
    r = await real_client.post(f"/docs/{copy}/finalize", headers=auth(firm_tok))
    assert r.status_code == 409 and r.json()["fix"]["lines"] == [1, 2, 3], r.text
    assert (await _state(real_client, firm_tok, copy))["status"] == "draft"


async def test_outside_money_change_clears_document_choice_with_reason(real_client, real_engine, monkeypatch):
    """A firm user chose the document's fund on a client's draft bill: a revision changing the
    tax clears it with the reason naming the tax and where it came from; chosen again, an
    unchanged revision keeps it; a price change clears it naming the total, and when the
    client then issues the bill it is held for funds with that reason shown in the hold."""
    tok, firm_tok, client_company, credential, fund, bill, copy = await _connected_draft(
        real_client, real_engine, monkeypatch)
    await _choose_on(real_client, firm_tok, copy, document=fund)
    assert _chosen(await _state(real_client, firm_tok, copy))[0] == fund

    await _revise(real_client, tok, bill, doc_taxes=[
        {"code": "VAT", "rate": 7.0, "order": 1, "is_compound": False, "label": "VAT"}])
    state = await _synced(real_client, firm_tok, client_company, credential, copy)
    assert _chosen(state) == (None, [(None, None), (None, None)])
    reason = state["fund_choice_reason"]
    assert reason["message_key"] == "fund.choices_cleared_by_source.tax"
    assert reason["params"]["source"]["message_key"] == "fund.origin.connected_company"

    await _choose_on(real_client, firm_tok, copy, document=fund)
    state = await _state(real_client, firm_tok, copy)
    assert _chosen(state)[0] == fund and _reason(state) is None
    await _revise(real_client, tok, bill, notes="Second notice")
    state = await _synced(real_client, firm_tok, client_company, credential, copy)
    assert _chosen(state)[0] == fund and _reason(state) is None

    lines = (await _state(real_client, tok, bill))["line_items"]
    await _revise(real_client, tok, bill, line_items=[{**lines[0], "unit_price": 55.0, "line_total": 55.0}, lines[1]])
    state = await _synced(real_client, firm_tok, client_company, credential, copy)
    assert _chosen(state)[0] is None and _reason(state) == "fund.choices_cleared_by_source.total"

    assert (await real_client.post(f"/docs/{bill}/finalize", headers=auth(tok))).status_code == 200
    results = await _two_way(client_company, credential)
    assert not results["documents_out"].ok
    [item] = await _held(real_client, firm_tok, "doc_funds")
    assert item["fix"]["reason"]["message_key"] == "fund.choices_cleared_by_source.total"
    assert (await _state(real_client, firm_tok, copy))["status"] == "draft"


def _fund_keys(lines: list[dict]) -> list:
    return [(li.get("fund_id"), li.get("fund_choice")) for li in lines if li.get("fund_id") or li.get("fund_choice")]


def _sent_with(monkeypatch, key: str, value) -> None:
    """Every document the client sends with ``key`` added to its first line."""
    from celerp.services import peer_documents

    send = peer_documents.transport_document

    def with_key(state):
        data = send(state)
        data["line_items"][0][key] = value
        return data

    monkeypatch.setattr(peer_documents, "transport_document", with_key)


@pytest.mark.parametrize("direction", ["firm_journal", "client_journal", "outbound_document",
                                       "inbound_document", "document_import"])
async def test_fund_id_never_crosses_companies(real_client, real_engine, monkeypatch, direction):
    """A fund is one company's own: books that use funds send journals and documents with no
    fund on any line, so nothing arrives with one in the other company; a document line that
    arrives carrying a fund is refused naming the line, and an import file carrying one is
    refused for that record, pointing to the import screen."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    firm_company = _company_of(firm_tok)
    _route_to_app(monkeypatch, [])
    if direction in ("firm_journal", "client_journal"):
        sender, books, copies = ((firm_tok, client_company, _from_firm) if direction == "firm_journal"
                                 else (tok, firm_company, _from_client))
        fund = await _funds_client(real_client, sender)
        r = await real_client.post("/accounting/journal-entries", headers=auth(sender), json={
            "ts": DAY, "memo": "Grant interest", "idempotency_token": uuid.uuid4().hex,
            "entries": [{"account": BANK, "debit": 70.0, "credit": 0.0, "fund_id": fund},
                        {"account": EXPENSE, "debit": 0.0, "credit": 70.0, "fund_id": fund}]})
        assert r.status_code == 200, r.text
        await _two_way(client_company, credential)
        [copy] = copies(await _journals(real_engine, books)).values()
        assert len(copy["entries"]) == 2 and _fund_keys(copy["entries"]) == [], copy["entries"]
        return
    if direction == "document_import":
        await _funds_client(real_client, tok)
        good, carried = (f"doc:IMP-{uuid.uuid4().hex[:8]}" for _ in range(2))
        r = await real_client.post("/docs/import/batch", headers=auth(tok), json={"records": [{
            "entity_id": eid, "event_type": "doc.created", "source": "csv", "idempotency_key": uuid.uuid4().hex,
            "data": {"doc_type": "invoice", "status": "draft", "ref_id": eid.split(":")[1], "total": 30.0,
                     "line_items": [_LINES[1], {**_LINES[0], "fund_id": "fund:elsewhere"} if eid == carried
                                    else _LINES[0]]}} for eid in (good, carried)]})
        assert r.status_code == 200 and r.json()["created"] == 1, r.text
        assert "Choose funds on the import screen, not in the file" in r.text, r.text
        assert (await real_client.get(f"/docs/{carried}", headers=auth(tok))).status_code == 404
        return
    if direction == "outbound_document":
        fund = await _funds_client(real_client, tok)
        lines = [{**li, "fund_id": fund} for li in _LINES]
    else:
        await _funds_client(real_client, firm_tok)
        _sent_with(monkeypatch, "fund_id", "fund:elsewhere")
        lines = _LINES
    vendor = await real_client.post("/crm/contacts", json={"name": "Utility Co", "contact_type": "vendor"},
                                    headers=auth(tok))
    assert vendor.status_code in (200, 201), vendor.text
    r = await real_client.post("/docs", headers=auth(tok), json={
        "doc_type": "bill", "contact_id": vendor.json()["id"], "date": DAY, "line_items": lines,
        "idempotency_key": uuid.uuid4().hex})
    assert r.status_code in (200, 201), r.text
    bill = r.json()["id"]
    results = await _two_way(client_company, credential)
    copy = await _copy_of(real_engine, firm_company, bill)
    if direction == "outbound_document":
        assert results["documents_out"].ok, results["documents_out"].errors
        assert _fund_keys((await _state(real_client, firm_tok, copy))["line_items"]) == []
        return
    assert copy is None and await _held(real_client, firm_tok, "doc_funds") == []
    [entry] = results["documents_out"].attention
    assert "Line 1" in entry["reason"] and "fund_id" in entry["reason"], entry
