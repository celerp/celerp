# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A record from a connected company posts to an account here only when it says which
account it means: the account's details (type, currency, role) agree with the account it
lands on, or an existing link of that account says where it goes. A record whose account
details are missing, partial, conflicting or forged is held, writes nothing, and posts
once it arrives with details that place it."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from migration_support import auth, maker, real_client, real_engine  # noqa: F401
from test_company_backup_lifecycle import _data_dir  # noqa: F401
from test_company_handoff_identity_restore import _link_verified  # noqa: F401
from test_journal_reversal_invariant import _ledger_rows
from test_peer_accounts import _add, _chart, _fix, _holds, _lines, _sent_journal
from test_peer_sync_end_to_end import _company_of, _route_to_app
from test_peer_two_way_sync import ADJUSTED, _journals, _notices, _pair_journals, _post_journal, _two_way

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


def _send_accounts(monkeypatch, change) -> None:
    """Every record's account details as ``change`` rewrites them on the way out."""
    from celerp.services import peer_accounts
    from celerp.services.journal_accounts import chart_access

    real = peer_accounts.outgoing

    async def outgoing(session, company_id, peer_company, codes):
        sent = await real(session, company_id, peer_company, codes)
        mine = {a["code"]: a for a in await chart_access().list_accounts(session, company_id)}
        theirs = {a["code"]: a for a in await chart_access().list_accounts(session, peer_company)}
        return change(sent, mine, theirs)

    monkeypatch.setattr(peer_accounts, "outgoing", outgoing)


def _without(code):
    return lambda sent, mine, theirs: {c: a for c, a in sent.items() if c != code}


def _edited(code, **fields):
    def change(sent, mine, theirs):
        if code not in sent:
            return sent
        sent[code] = {**sent[code], **{k: v(mine, theirs) if callable(v) else v for k, v in fields.items()}}
        return sent
    return change


def _same_id(code, as_code):
    def change(sent, mine, theirs):
        if code not in sent or as_code not in sent:
            return sent
        sent[code] = {**sent[code], "id": mine[as_code]["id"]}
        sent[as_code] = {**sent[as_code], "id": mine[as_code]["id"]}
        return sent
    return change


async def _open(engine, company_id: str) -> list:
    from celerp.models.company_peer import CompanyPeerAttention
    async with maker(engine)() as s:
        return list((await s.scalars(select(CompanyPeerAttention).where(
            CompanyPeerAttention.company_id == uuid.UUID(company_id)))).all())


MAPS = {
    "absent": lambda sent, mine, theirs: {},
    "partial": _without("6150"),
    # The id of another account of the sender's, already linked to an asset account here.
    "forged-id": _edited("6150", id=lambda mine, theirs: mine["1140"]["id"]),
    # The sender claiming its account posts to an asset account of the receiver's.
    "forged-yours": _edited("6150", yours=lambda mine, theirs: theirs["1140"]["id"]),
    # Two of the record's accounts claiming one id.
    "conflicting": _same_id("6150", "6200"),
    # An expense account claimed as the sender's accounts receivable control account.
    "forged-role": _edited("6150", roles=["receivable"]),
}


@pytest.mark.parametrize("defect", list(MAPS))
async def test_a_record_without_trustworthy_account_details_is_held(real_client, real_engine, monkeypatch, defect):
    """The firm holds 6150 Subscriptions, the client 6150 Courier. A client journal on its
    Courier account whose account details are missing, partial, conflicting or forged is
    held and writes nothing at the firm; with its real details it is held as a code
    collision, and once the firm adds it under another code it posts there."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    firm_company = _company_of(firm_tok)
    await _add(real_client, firm_tok, "6150", "Subscriptions")
    await _add(real_client, tok, "6150", "Courier")
    courier = await _post_journal(real_client, tok, ADJUSTED, 5.0, "Courier", debit="6150")
    _route_to_app(monkeypatch, [])
    chart, rows = await _chart(real_client, firm_tok), await _ledger_rows(real_engine, firm_company)
    _send_accounts(monkeypatch, MAPS[defect])
    results = await _two_way(client_company, credential)

    assert await _ledger_rows(real_engine, firm_company) == rows, f"{defect}: the firm's books changed"
    assert await _chart(real_client, firm_tok) == chart
    assert not [s for s in (await _journals(real_engine, firm_company)).values()
                if (s.get("peer_source") or {}).get("journal") == courier]
    assert [a for a in await _open(real_engine, firm_company) if a.label == "Courier"], results
    assert results["journals_out"].errors, results

    monkeypatch.undo()
    _route_to_app(monkeypatch, [])
    await _two_way(client_company, credential)
    [held] = [h for h in await _holds(real_client, firm_tok) if h["label"] == "Courier"]
    assert held["fix"]["kind"] == "collision", held
    await _fix(real_client, firm_tok, held, action="add", code=held["fix"]["suggested"])
    await _two_way(client_company, credential)
    lines = _lines(_sent_journal(await _journals(real_engine, firm_company), courier))
    assert (held["fix"]["suggested"], 5.0, 0.0) in lines and ("6200", 0.0, 5.0) in lines, lines
    assert not [a for a in await _open(real_engine, firm_company) if a.label == "Courier"]


async def test_a_firm_journal_without_account_details_is_declined_by_the_client(
        real_client, real_engine, monkeypatch):
    """The other way: a firm journal on its 6160 Subscriptions, sent without account
    details, never lands on the client's 6160 Courier; it is declined with the reason and
    writes nothing, and with its details the client adds it under another code."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    await _add(real_client, firm_tok, "6160", "Subscriptions")
    await _add(real_client, tok, "6160", "Courier")
    await _post_journal(real_client, firm_tok, ADJUSTED, 9.0, "Software", debit="6160")
    _route_to_app(monkeypatch, [])
    rows = await _ledger_rows(real_engine, client_company)
    _send_accounts(monkeypatch, MAPS["absent"])
    await _two_way(client_company, credential)

    assert await _ledger_rows(real_engine, client_company) == rows
    assert not [s for s in (await _journals(real_engine, client_company)).values()
                if (s.get("peer_origin") or {}).get("journal")]
    declined = [b for _, b, _ in await _notices(real_engine, client_company) if "Software" in b]
    assert declined and "6160" in declined[-1], declined

    monkeypatch.undo()
    _route_to_app(monkeypatch, [])
    await _two_way(client_company, credential)
    [held] = [h for h in await _holds(real_client, tok) if h["label"] == "Software"]
    assert held["fix"]["kind"] == "collision", held
    await _fix(real_client, tok, held, action="add", code=held["fix"]["suggested"])
    await _two_way(client_company, credential)
    [posted] = [s for s in (await _journals(real_engine, client_company)).values()
                if (s.get("peer_origin") or {}).get("journal")]
    assert (held["fix"]["suggested"], 9.0, 0.0) in _lines(posted), posted


async def test_a_linked_account_still_posts_without_its_details(real_client, real_engine, monkeypatch):
    """An account the firm already links (here every account of the copy it opened) is
    placed by that link even when a record leaves its details out."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    firm_company = _company_of(firm_tok)
    linked = await _post_journal(real_client, tok, ADJUSTED, 4.0, "Linked only")
    _route_to_app(monkeypatch, [])
    _send_accounts(monkeypatch, MAPS["absent"])
    await _two_way(client_company, credential)
    assert _lines(_sent_journal(await _journals(real_engine, firm_company), linked)) == {
        ("6500", 4.0, 0.0), ("6200", 0.0, 4.0)}


@pytest.mark.parametrize("claim", [{"currency": "EUR"}, {"account_type": "asset"}], ids=["currency", "type"])
async def test_a_linked_account_claimed_as_something_else_is_held(real_client, real_engine, monkeypatch, claim):
    """A record claiming a linked account is now kept in another currency, or is of another
    type, than the account it is linked to here is held and writes nothing; with its real
    details it posts by the link."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    firm_company = _company_of(firm_tok)
    linked = await _post_journal(real_client, tok, ADJUSTED, 4.0, "Linked claim")
    _route_to_app(monkeypatch, [])
    rows = await _ledger_rows(real_engine, firm_company)
    _send_accounts(monkeypatch, _edited("6200", **claim))
    await _two_way(client_company, credential)
    assert await _ledger_rows(real_engine, firm_company) == rows
    [held] = [h for h in await _holds(real_client, firm_tok) if h["label"] == "Linked claim"]
    assert held["fix"]["kind"] == next(iter(claim)).replace("account_", ""), held
    assert "6200" in held["reason"], held

    monkeypatch.undo()
    _route_to_app(monkeypatch, [])
    await _two_way(client_company, credential)
    assert _lines(_sent_journal(await _journals(real_engine, firm_company), linked)) == {
        ("6500", 4.0, 0.0), ("6200", 0.0, 4.0)}
    assert not [h for h in await _holds(real_client, firm_tok) if h["label"] == "Linked claim"]


async def test_the_receivers_role_account_is_never_matched_by_code_alone(real_client, real_engine, monkeypatch):
    """The firm uses 6955 General costs as its general expense account; the client has a
    6955 General costs it does not use for that role. The client's journal on it is held
    as a collision naming the role, and posts there once the firm chooses to use it."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    firm_company = _company_of(firm_tok)
    await _add(real_client, firm_tok, "6955", "General costs")
    await _add(real_client, tok, "6955", "General costs")
    r = await real_client.put("/accounting/posting-accounts/general_expense", json={"code": "6955"},
                              headers=auth(firm_tok))
    assert r.status_code == 200, r.text
    costs = await _post_journal(real_client, tok, ADJUSTED, 3.0, "Sundry", debit="6955")
    _route_to_app(monkeypatch, [])
    rows = await _ledger_rows(real_engine, firm_company)
    await _two_way(client_company, credential)
    assert await _ledger_rows(real_engine, firm_company) == rows
    [held] = [h for h in await _holds(real_client, firm_tok) if h["label"] == "Sundry"]
    assert held["fix"]["kind"] == "collision" and "General expense" in held["reason"], held
    await _fix(real_client, firm_tok, held, action="use", account="6955")
    await _two_way(client_company, credential)
    assert ("6955", 3.0, 0.0) in _lines(_sent_journal(await _journals(real_engine, firm_company), costs))
