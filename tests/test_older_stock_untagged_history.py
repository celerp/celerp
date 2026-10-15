# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Older stock placed against an account that served several roles before posting
accounts were recorded on each line.

A line posted before then carries no roles of its own, so what it meant is read from
the roles its account has served. When that account served an inventory role and
another one too, the line may or may not have been stock. Such a line is uncertain
evidence: it never proves the stock was or was not booked. Placing books nothing for
it and computes no shortfall or surplus from it; the line is shown on its account, with
its amount and source entry, for the accountant to review, and nothing offers or
accepts booking it."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.accounting_roles import SCOPES_KEY
from celerp.models.projections import Projection
from stock_books import assert_books_carry_stock
from test_older_stock_placement import (_VIEW, _accounts, _imported, _marked, _off_books, _placements, _tab, _view,
                                        ui_client)
from test_posting_roles_older_stock import _net, _older_release, _settings
from test_posting_roles_rollout import _startup

pytestmark = pytest.mark.asyncio


async def _untagged(session, auth) -> None:
    """Every posted line as an older release left it: no roles recorded on it."""
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry"))).scalars().all()
    for row in rows:
        row.state = {**row.state, "entries": [{k: v for k, v in e.items() if k != "account_roles"}
                                              for e in row.state.get("entries") or []]}
    await session.commit()


async def _sources(session, auth, code: str) -> list[str]:
    """The posted entries with a line on ``code``."""
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry"))).scalars().all()
    return sorted(r.entity_id for r in rows if (r.state or {}).get("status") == "posted"
                  and any(e.get("account") == code for e in r.state.get("entries") or []))


async def _historical(client, auth, debit: str, amount: float) -> None:
    """An entry an older release posted: ``amount`` on ``debit`` against 3200."""
    r = await client.post("/accounting/journal-entries", headers=auth["headers"], json={
        "ts": "2026-01-15", "idempotency_token": uuid.uuid4().hex, "memo": "historical",
        "entries": [{"account": debit, "debit": amount, "credit": 0}, {"account": "3200", "debit": 0, "credit": amount}]})
    assert r.status_code in (200, 201), r.text


async def _older_books(session, client, auth, scopes: dict[str, list[str]], cost: float = 30.0) -> list[str]:
    """One older lot worth ``cost`` whose opening entry sits on 1130-OB with no roles on
    its lines, the accounts having served ``scopes`` before."""
    lots = await _imported(session, client, auth, [cost])
    await _older_release(session, auth)
    await _untagged(session, auth)
    await _settings(session, auth, **{SCOPES_KEY: scopes})
    await _marked(session, auth)
    return lots


async def test_uncertain_stock_is_shown_for_review_never_as_a_difference_or_a_booking(session, client, auth):
    """The opening 30 sits on 1130-OB, which served inventory and input tax: it may be the
    lot's stock or not. Nothing is booked, no shortfall is computed from it, the 30 is
    shown on 1130-OB with its entry, and booking it is refused."""
    lots = await _older_books(session, client, auth, {"inventory_opening": ["1130-OB"], "tax_input": ["1130-OB"]})
    before = await _net(session, auth, "1130-OB", "1130-P", "3200")
    await _startup(session)
    assert await _net(session, auth, "1130-OB", "1130-P", "3200") == before == (30.0, 0.0, -30.0)
    assert await _placements(session, auth) == []
    assert await _accounts(session, auth, lots) == ["1130-OB"]

    sources = await _sources(session, auth, "1130-OB")
    view = await _view(client, auth)
    (placement,) = view["placements"]
    assert (placement["classification"], placement["recognized"], placement["unresolved"]) == ("uncertain", 0.0, 0.0)
    assert placement["uncertain"] == [{"account": "1130-OB", "amount": 30.0, "entries": sources}]
    assert view["reconciliation"]["state"] == "review"
    assert view["reconciliation"]["open"] == [{"placement": placement["pid"], "account": "1130-OB",
                                               "kind": "uncertain", "amount": 30.0, "entries": sources}]
    assert await _off_books(session, auth) == [
        {"kind": "uncertain_evidence", "account": "1130-OB", "amount": 30.0, "entries": sources}]
    await assert_books_carry_stock(session, auth["company_id"])

    r = await client.post("/accounting/older-stock/corrections", headers=auth["headers"],
                          json={"placements": [placement["pid"]], "kind": "recognize"})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "older_stock.correct.uncertain"
    assert "1130-OB" in r.json()["detail"]["message"]
    assert await _net(session, auth, "1130-OB", "1130-P", "3200") == before

    # Starting again books nothing more and keeps the evidence open.
    await _startup(session)
    assert await _net(session, auth, "1130-OB", "1130-P", "3200") == before
    assert (await _view(client, auth))["reconciliation"]["open"] == view["reconciliation"]["open"]


async def test_uncertain_value_beside_matched_stock_stays_open(session, client, auth):
    """1130-X served purchased inventory and input tax and holds an untagged 50; the
    opening 30 covers the one lot of 30. The 50 is not dropped: it stays open for review."""
    r = await client.post("/accounting/accounts", headers=auth["headers"], json={
        "code": "1130-X", "name": "Account 1130-X", "account_type": "asset", "parent_code": "1100"})
    assert r.status_code == 200, r.text
    await _historical(client, auth, "1130-X", 50.0)
    lots = await _older_books(session, client, auth, {"inventory_opening": ["1130-OB"],
                                                      "inventory_purchased": ["1130-X", "1130-P"],
                                                      "tax_input": ["1130-X"]})
    before = await _net(session, auth, "1130-OB", "1130-P", "1130-X", "3200")
    await _startup(session)
    assert await _net(session, auth, "1130-OB", "1130-P", "1130-X", "3200") == before
    assert await _accounts(session, auth, lots) == ["1130-OB"]
    view = await _view(client, auth)
    (placement,) = view["placements"]
    assert (placement["recognized"], placement["unresolved"]) == (0.0, 0.0)
    assert view["reconciliation"]["state"] == "review"
    assert view["reconciliation"]["open"] == [{
        "placement": placement["pid"], "account": "1130-X", "kind": "uncertain", "amount": 50.0,
        "entries": await _sources(session, auth, "1130-X")}]
    await assert_books_carry_stock(session, auth["company_id"])


async def test_a_surplus_beyond_uncertain_value_stays_open_and_can_be_written_off(session, client, auth):
    """Proven stock of 40 for a lot of 30 is a surplus of 10 whatever the untagged 50 on
    1130-X was: it stays open with its write-off, the 50 beside it as evidence only."""
    r = await client.post("/accounting/accounts", headers=auth["headers"], json={
        "code": "1130-X", "name": "Account 1130-X", "account_type": "asset", "parent_code": "1100"})
    assert r.status_code == 200, r.text
    await _historical(client, auth, "1130-X", 50.0)
    await _historical(client, auth, "1130-OB", 10.0)
    await _older_books(session, client, auth, {"inventory_opening": ["1130-OB"],
                                                "inventory_purchased": ["1130-X", "1130-P"],
                                                "tax_input": ["1130-X"]})
    await _startup(session)
    view = await _view(client, auth)
    (placement,) = view["placements"]
    assert (placement["recognized"], placement["unresolved"]) == (0.0, 10.0)
    assert {(o["account"], o["kind"], o["amount"]) for o in view["reconciliation"]["open"]} == {
        ("1130-OB", "unresolved", 10.0), ("1130-X", "uncertain", 50.0)}
    r = await client.post("/accounting/older-stock/corrections", headers=auth["headers"],
                          json={"placements": [placement["pid"]], "kind": "recognize"})
    assert r.status_code == 200, r.text
    assert await _net(session, auth, "1130-OB", "1130-X") == (30.0, 50.0)
    await assert_books_carry_stock(session, auth["company_id"])


async def test_untagged_stock_on_an_account_only_inventory_served_is_still_counted(session, client, auth):
    lots = await _older_books(session, client, auth, {"inventory_opening": ["1130-OB"]})
    before = await _net(session, auth, "1130-OB", "1130-P", "3200")
    await _startup(session)
    assert await _accounts(session, auth, lots) == ["1130-OB"]
    assert await _placements(session, auth) == []
    assert await _net(session, auth, "1130-OB", "1130-P", "3200") == before == (30.0, 0.0, -30.0)
    view = await _view(client, auth)
    assert view["reconciliation"]["state"] == "reconciled"
    assert view["placements"][0]["classification"] == "exact"
    assert view["placements"][0]["uncertain"] == []
    assert await _off_books(session, auth) == []


async def _uncertain_x(session, client, auth) -> list[str]:
    """1130-X served purchased inventory and input tax and holds an untagged 50 beside a
    lot of 30 its opening entry covers: the 50 is uncertain evidence. Returns the 1130-X
    entries."""
    r = await client.post("/accounting/accounts", headers=auth["headers"], json={
        "code": "1130-X", "name": "Account 1130-X", "account_type": "asset", "parent_code": "1100"})
    assert r.status_code == 200, r.text
    await _historical(client, auth, "1130-X", 50.0)
    await _older_books(session, client, auth, {"inventory_opening": ["1130-OB"],
                                                "inventory_purchased": ["1130-X", "1130-P"],
                                                "tax_input": ["1130-X"]})
    await _startup(session)
    return await _sources(session, auth, "1130-X")


async def _set_status(session, auth, je_id: str, status: str) -> None:
    session.expire_all()
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": je_id})
    row.state = {**row.state, "status": status}
    await session.commit()


async def test_uncertain_evidence_follows_the_ledger_when_its_entry_is_voided_and_restored(session, client, auth):
    """The evidence is read from the posted entries each time, never from what placing
    saw: voiding the 1130-X entry the review names reconciles, and the entry posted
    again is evidence again."""
    (je,) = await _uncertain_x(session, client, auth)
    view = await _view(client, auth)
    assert view["reconciliation"]["state"] == "review"
    r = await client.post(f"/accounting/journal-entries/{je}/void", headers=auth["headers"], json={"reason": "review"})
    assert r.status_code == 200, r.text
    view = await _view(client, auth)
    assert view["reconciliation"] == {"state": "reconciled", "waiting": 0, "reasons": [], "open": []}
    assert view["placements"][0]["uncertain"] == []
    assert await _off_books(session, auth) == []

    await _set_status(session, auth, je, "posted")
    view = await _view(client, auth)
    assert view["reconciliation"]["state"] == "review"
    assert [(o["account"], o["kind"], o["amount"], o["entries"]) for o in view["reconciliation"]["open"]] == [
        ("1130-X", "uncertain", 50.0, [je])]
    assert view["placements"][0]["uncertain"] == [{"account": "1130-X", "amount": 50.0, "entries": [je]}]


async def test_a_bank_account_is_never_possible_stock_evidence(session, client, auth):
    """Bank account 1111 holds an untagged 5000 cash sale and older data put it in
    purchased inventory's history beside its cash role: it holds cash, so its lines are
    never read as possible stock, listed for review, or swept onto inventory."""
    from test_older_stock_reconciliation import _posted
    await _imported(session, client, auth, [30.0])
    await _older_release(session, auth)
    await _posted(session, auth, "1111", "4100", 5000.0, "Cash sale")
    await _untagged(session, auth)
    from celerp.services.company_lock import locked_company
    company = await locked_company(session, auth["company_id"])
    scopes = {k: list(v) for k, v in (company.settings.get(SCOPES_KEY) or {}).items()}
    scopes.setdefault("inventory_purchased", []).append("1111")
    company.settings = {**company.settings, SCOPES_KEY: scopes}
    await session.commit()
    await _marked(session, auth)
    await _startup(session)
    view = await _view(client, auth)
    assert view["reconciliation"]["state"] == "reconciled", view["reconciliation"]
    assert all(p["uncertain"] == [] for p in view["placements"])
    assert await _off_books(session, auth) == []
    assert (await _net(session, auth, "1111"))[0] == 5000.0


async def test_refusing_to_book_uncertain_evidence_names_every_account(session, client, auth):
    """Uncertain lines on 1130-X and 1130-Y: the refusal names both, each with its amount
    and entries, not only the first."""
    for code in ("1130-X", "1130-Y"):
        r = await client.post("/accounting/accounts", headers=auth["headers"], json={
            "code": code, "name": f"Account {code}", "account_type": "asset", "parent_code": "1100"})
        assert r.status_code == 200, r.text
    await _historical(client, auth, "1130-X", 50.0)
    await _historical(client, auth, "1130-Y", 20.0)
    await _older_books(session, client, auth, {"inventory_opening": ["1130-OB"],
                                                "inventory_purchased": ["1130-X", "1130-Y", "1130-P"],
                                                "tax_input": ["1130-X", "1130-Y"]})
    await _startup(session)
    (placement,) = (await _view(client, auth))["placements"]
    for kind in ("recognize", "unresolve"):
        r = await client.post("/accounting/older-stock/corrections", headers=auth["headers"],
                              json={"placements": [placement["pid"]], "kind": kind})
        assert r.status_code == 409, r.text
        message = r.json()["detail"]["message"]
        assert "1130-X holds 50.00" in message and "1130-Y holds 20.00" in message, message
        for code in ("1130-X", "1130-Y"):
            for je in await _sources(session, auth, code):
                assert je in message


async def test_the_tab_shows_uncertain_evidence_with_no_booking_and_confirms_every_booking(ui_client):
    """Uncertain evidence is a plain line naming its account, amount and entry, with no
    action; booking a shortfall against retained earnings asks first, naming the account."""
    uncertain = {"pid": "pl-u", "day": "2026-10-15", "count": 1, "account": "1130-OB", "classification": "uncertain",
                 "transferred": 0.0, "recognized": 0.0, "unresolved": 0.0, "provisional": 0.0, "journal": [],
                 "corrections": [], "uncertain": [{"account": "1130-OB", "amount": 30.0, "entries": ["je:old-1"]}]}
    short = {**uncertain, "pid": "pl-s", "classification": "opening_stock", "unresolved": -20.0, "uncertain": []}
    body, _ = await _tab(ui_client, {
        **_VIEW, "placements": [uncertain, short], "retained": {"code": "3200", "name": "Retained earnings"},
        "reconciliation": {"state": "review", "waiting": 0, "reasons": [], "open": [
            {"placement": "pl-u", "account": "1130-OB", "kind": "uncertain", "amount": 30.0,
             "entries": ["je:old-1"]}]}})
    banner = body.split('id="older-stock-reconciliation"')[1].split("</div>")[0]
    assert "1130-OB" in banner and "$30.00" in banner and "je:old-1" in banner
    row_u = body.split('value="pl-u"')[1].split("</tr>")[0]
    assert "je:old-1" in row_u and "Book against retained earnings" not in row_u and "hx-post" not in row_u
    row_s = body.split('value="pl-s"')[1].split("</tr>")[0]
    assert "Book against retained earnings" in row_s
    assert 'hx-confirm="Book $20.00 against 3200' in row_s
