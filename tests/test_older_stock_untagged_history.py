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


async def _cash_sale_in_stock_history(session, client, auth, trigger: str) -> list[str]:
    """Bank account 1111 holds an untagged 5000 cash sale posted by ``trigger``, and older
    data put 1111 in purchased inventory's history beside its cash role; one lot of 30 is
    covered by its opening entry. Returns the 1111 entries."""
    from celerp.services.auto_je import _emit_auto_posted_je
    from celerp.services.company_lock import locked_company
    await _imported(session, client, auth, [30.0])
    await _older_release(session, auth)
    je = f"je:{uuid.uuid4()}"
    await _emit_auto_posted_je(
        session, company_id=auth["company_id"], user_id=auth["user_id"], je_id=je, idem_create=f"{je}:c",
        idem_posted=f"{je}:p", memo="Cash sale", metadata_={"trigger": trigger},
        entries=[{"account": "1111", "debit": 5000.0, "credit": 0.0}, {"account": "4100", "debit": 0.0, "credit": 5000.0}])
    await session.commit()
    await _untagged(session, auth)
    company = await locked_company(session, auth["company_id"])
    scopes = {k: list(v) for k, v in (company.settings.get(SCOPES_KEY) or {}).items()}
    scopes.setdefault("inventory_purchased", []).append("1111")
    company.settings = {**company.settings, SCOPES_KEY: scopes}
    await session.commit()
    await _marked(session, auth)
    await _startup(session)
    return [je]


async def test_a_payment_on_a_bank_account_is_never_possible_stock_evidence(session, client, auth):
    """The cash sale was posted as a payment received: its posting says it moved cash, so
    its lines are never read as possible stock, listed for review, or swept onto
    inventory."""
    await _cash_sale_in_stock_history(session, client, auth, "doc.payment.received")
    view = await _view(client, auth)
    assert view["reconciliation"]["state"] == "reconciled", view["reconciliation"]
    assert all(p["uncertain"] == [] for p in view["placements"])
    assert await _off_books(session, auth) == []
    assert await _placements(session, auth) == []
    assert (await _net(session, auth, "1111"))[0] == 5000.0


async def test_an_untagged_entry_on_a_bank_account_with_stock_history_is_reviewed_never_swept(session, client, auth):
    """The same 5000 posted by hand says nothing of what it was for, and 1111 being a bank
    account today does not prove it: it is uncertain evidence for the accountant, and
    nothing is moved or recognized for it."""
    sources = await _cash_sale_in_stock_history(session, client, auth, "manual")
    view = await _view(client, auth)
    assert view["reconciliation"]["state"] == "review", view["reconciliation"]
    assert [(o["account"], o["kind"], o["amount"], o["entries"]) for o in view["reconciliation"]["open"]] == [
        ("1111", "uncertain", 5000.0, sources)]
    assert await _off_books(session, auth) == [
        {"kind": "uncertain_evidence", "account": "1111", "amount": 5000.0, "entries": sources}]
    assert await _placements(session, auth) == []
    assert (await _net(session, auth, "1111"))[0] == 5000.0
    await assert_books_carry_stock(session, auth["company_id"])


async def test_a_lot_whose_stock_evidence_is_uncertain_still_sells(session, client, auth):
    """Only booking the uncertain evidence is refused: the lot itself sells on an invoice
    as any other, and the sale recognizes its cost."""
    (lot,) = await _older_books(session, client, auth, {"inventory_opening": ["1130-OB"], "tax_input": ["1130-OB"]})
    await _startup(session)
    (placement,) = (await _view(client, auth))["placements"]
    assert placement["classification"] == "uncertain"
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "contact_name": "Buyer",
        "line_items": [{"entity_id": lot, "name": "Older lot", "quantity": 1, "unit_price": 100.0}]})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    r = await client.post(f"/docs/{doc}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text
    session.expire_all()
    fin = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": f"je:auto:{doc}:fin"})
    assert fin is not None and fin.state["status"] == "posted"
    assert sum(e.get("debit") or 0 for e in fin.state["entries"] if e["account"].startswith("5")) == 30.0


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


async def _je_ids(session, auth, prefix: str) -> list[str]:
    session.expire_all()
    return sorted((await session.execute(select(Projection.entity_id).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry",
        Projection.entity_id.like(f"{prefix}%")))).scalars())


async def test_voiding_the_only_possible_evidence_leaves_a_gap_to_review_never_reconciled(session, client, auth):
    """The lot of 30 came with no opening entry, and its one possible piece of evidence is
    an untagged journal of 30 on 1130-X, which served purchased inventory and input tax,
    so placing books nothing. Voiding that journal does not reconcile the older stock:
    the books no longer hold the stock recorded on 1130-P, and the review says so with
    the same gap the books check reports. Selling the lot recognizes its cost once, books
    no opening stock again, and the gap stays open."""
    r = await client.post("/accounting/accounts", headers=auth["headers"], json={
        "code": "1130-X", "name": "Account 1130-X", "account_type": "asset", "parent_code": "1100"})
    assert r.status_code == 200, r.text
    await _historical(client, auth, "1130-X", 30.0)
    (lot,) = await _older_books(session, client, auth, {"inventory_opening": ["1130-OB"],
                                                         "inventory_purchased": ["1130-X", "1130-P"],
                                                         "tax_input": ["1130-X"]})
    for opening in await _sources(session, auth, "1130-OB"):
        await _set_status(session, auth, opening, "void")
    await _startup(session)
    assert await _placements(session, auth) == []
    (placement,) = (await _view(client, auth))["placements"]
    assert (placement["classification"], placement["account"]) == ("uncertain", "1130-P")
    (je,) = await _sources(session, auth, "1130-X")
    r = await client.post(f"/accounting/journal-entries/{je}/void", headers=auth["headers"], json={"reason": "review"})
    assert r.status_code == 200, r.text
    gap = {"kind": "stock_gap", "account": "1130-P", "books": 0.0, "stock": 30.0}
    assert await _off_books(session, auth) == [gap]
    view = await _view(client, auth)
    assert view["reconciliation"]["state"] == "review", view["reconciliation"]
    assert view["reconciliation"]["open"] == [{"account": "1130-P", "kind": "stock_gap", "amount": -30.0,
                                               "books": 0.0, "stock": 30.0}]
    assert view["placements"][0]["uncertain"] == []

    placed = await _je_ids(session, auth, "je:auto:older-stock-placed:")
    retained = (await _net(session, auth, "3200"))[0]
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "contact_name": "Buyer",
        "line_items": [{"entity_id": lot, "name": "Older lot", "quantity": 1, "unit_price": 100.0}]})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    r = await client.post(f"/docs/{doc}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text
    await _startup(session)
    assert await _je_ids(session, auth, "je:auto:older-stock-placed:") == placed
    assert (await _net(session, auth, "3200"))[0] == retained
    session.expire_all()
    fin = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": f"je:auto:{doc}:fin"})
    assert sum(e.get("debit") or 0 for e in fin.state["entries"] if e["account"].startswith("5")) == 30.0
    view = await _view(client, auth)
    assert view["reconciliation"]["state"] == "review"
    assert [o["kind"] for o in view["reconciliation"]["open"]] == ["stock_gap"]


async def test_unvoiding_an_older_sale_restores_its_lines_as_they_were_recorded(session, client, auth):
    """An older sale's untagged entry relieved 30 from 1130-OB, which served opening
    inventory and input tax, so it is possible stock evidence. Voiding the sale and then
    unvoiding it puts the same entry back: its lines keep no roles, never today's, so the
    review and the books check read the same 40 as before the void."""
    (lot,) = await _imported(session, client, auth, [30.0])
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "contact_name": "Buyer",
        "line_items": [{"entity_id": lot, "name": "Older lot", "quantity": 1, "unit_price": 100.0}]})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    assert (await client.post(f"/docs/{doc}/finalize", headers=auth["headers"])).status_code == 200
    await _older_books(session, client, auth, {"inventory_opening": ["1130-OB"], "tax_input": ["1130-OB"]}, 40.0)
    await _startup(session)
    before = await _off_books(session, auth)
    assert [(f["kind"], f["account"], f["amount"]) for f in before] == [("uncertain_evidence", "1130-OB", 40.0)]
    assert (await client.post(f"/docs/{doc}/void", headers=auth["headers"], json={"reason": "x"})).status_code == 200
    assert (await client.post(f"/docs/{doc}/unvoid", headers=auth["headers"], json={})).status_code == 200
    await _startup(session)
    (restored,) = await _je_ids(session, auth, f"je:auto:{doc}:fin:unvoid:")
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": restored})
    assert [e.get("account_roles") for e in row.state["entries"]] == [None] * 4
    after = await _off_books(session, auth)
    assert [(f["kind"], f["account"], f["amount"]) for f in after] == [("uncertain_evidence", "1130-OB", 40.0)]
    view = await _view(client, auth)
    assert view["reconciliation"]["state"] == "review"
    assert [(o["kind"], o["amount"]) for o in view["reconciliation"]["open"]] == [("uncertain", 40.0)]
    assert await _net(session, auth, "1130-OB") == (40.0,)


async def test_an_inventory_role_pointed_at_an_account_later_reviews_its_history(session, client, auth):
    """1130-T held an untagged 50 while it served input tax only, so placing the older lot
    weighed nothing on it and the review reconciles. Pointing purchased inventory at 1130-T
    afterwards makes that 50 possible stock: the books check names it, and the review
    names the same evidence, on no placement, instead of staying reconciled."""
    r = await client.post("/accounting/accounts", headers=auth["headers"], json={
        "code": "1130-T", "name": "Account 1130-T", "account_type": "asset", "parent_code": "1100"})
    assert r.status_code == 200, r.text
    await _historical(client, auth, "1130-T", 50.0)
    await _older_books(session, client, auth, {"inventory_opening": ["1130-OB"], "tax_input": ["1130-T"]})
    await _startup(session)
    assert await _off_books(session, auth) == []
    assert (await _view(client, auth))["reconciliation"]["state"] == "reconciled"
    r = await client.put("/accounting/posting-accounts/inventory_purchased", headers=auth["headers"],
                         json={"code": "1130-T"})
    assert r.status_code == 200, r.text
    await _startup(session)
    sources = await _sources(session, auth, "1130-T")
    assert await _off_books(session, auth) == [
        {"kind": "uncertain_evidence", "account": "1130-T", "amount": 50.0, "entries": sources}]
    view = await _view(client, auth)
    assert view["reconciliation"]["state"] == "review"
    assert view["reconciliation"]["open"] == [
        {"kind": "uncertain", "account": "1130-T", "amount": 50.0, "entries": sources}]
    assert all(p["uncertain"] == [] for p in view["placements"])


async def _bank(session, auth, code: str) -> None:
    """``code`` made a bank account, as a later release or an import may have made it."""
    from celerp_accounting.models import BankAccount
    session.add(BankAccount(company_id=auth["company_id"], chart_account_code=code, bank_name="Bank",
                            account_number="****0001", bank_type="checking", currency="USD"))
    await session.commit()


async def test_an_account_made_a_bank_account_later_still_holds_its_possible_stock(session, client, auth):
    """1130-OB served opening inventory and deposits before lines recorded their roles, and
    is a bank account today. Being one now says nothing of what its untagged opening 30
    was for, so the 30 stays uncertain evidence to review: no shortfall is read and no
    opening stock is booked for the lot."""
    await _older_books(session, client, auth, {"inventory_opening": ["1130-OB"], "default_deposit": ["1130-OB"]})
    await _bank(session, auth, "1130-OB")
    before = await _net(session, auth, "1130-OB", "1130-P", "3200")
    await _startup(session)
    assert await _net(session, auth, "1130-OB", "1130-P", "3200") == before == (30.0, 0.0, -30.0)
    assert await _placements(session, auth) == []
    sources = await _sources(session, auth, "1130-OB")
    view = await _view(client, auth)
    (placement,) = view["placements"]
    assert (placement["classification"], placement["recognized"]) == ("uncertain", 0.0)
    assert placement["uncertain"] == [{"account": "1130-OB", "amount": 30.0, "entries": sources}]
    assert view["reconciliation"]["state"] == "review"
    assert await _off_books(session, auth) == [
        {"kind": "uncertain_evidence", "account": "1130-OB", "amount": 30.0, "entries": sources}]


async def _more_placements(session, auth, n: int, start: int, evidence: list[str]) -> None:
    """``n`` further placement records on 1130-OB, each weighing the same entries, half of
    them keeping provisional cost."""
    from celerp.events.engine import emit_event
    from celerp.services.lot_origin import PLACED, PLACEMENT
    for i in range(start, start + n):
        pid = f"extra-{i:04d}"
        await emit_event(
            session, company_id=auth["company_id"], entity_id=f"older-stock-placement:{pid}", entity_type=PLACEMENT,
            event_type=PLACED, actor_id=None, location_id=None, source="system",
            idempotency_key=f"older-stock-placement:{pid}", metadata_={},
            data={"placement": pid, "day": "2026-10-15", "count": 1, "value": 30.0, "account": "1130-OB",
                  "rooms": {"1130-OB": 0.0}, "transferred": 0.0, "recognized": 0.0, "unresolved": 0.0,
                  "provisional": 5.0 if i % 2 else 0.0, "evidence_entries": evidence,
                  "classification": "uncertain", "journal": []})
    await session.commit()


async def test_the_older_stock_view_reads_the_same_number_of_times_however_many_placements(session, client, auth):
    """The view's page, review and summary come from grouped reads and the visible page:
    60 placements and 150 take the same queries and read the same rows, and the evidence
    they share is shown once, on the newest."""
    from sqlalchemy import event

    from celerp.services.lot_origin import older_stock_view

    await _older_books(session, client, auth, {"inventory_opening": ["1130-OB"], "tax_input": ["1130-OB"]})
    await _startup(session)
    sources = await _sources(session, auth, "1130-OB")
    statements: list[str] = []

    def _count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    execute, rows = session.execute, []

    async def _counted(*args, **kwargs):
        frozen = (await execute(*args, **kwargs)).freeze()
        rows.append(len(frozen().all()))
        return frozen()

    async def _read() -> tuple[tuple[int, int], dict]:
        session.expire_all()
        statements.clear()
        rows.clear()
        event.listen(session.bind.sync_engine, "before_cursor_execute", _count)
        session.execute = _counted
        try:
            view = await older_stock_view(session, auth["company_id"])
        finally:
            del session.execute
            event.remove(session.bind.sync_engine, "before_cursor_execute", _count)
        return (len(statements), sum(rows)), view

    await _more_placements(session, auth, 59, 0, sources)
    few, view = await _read()
    await _more_placements(session, auth, 90, 59, sources)
    many, more = await _read()
    assert (view["totals"]["placements"], more["totals"]["placements"]) == (60, 150)
    assert len(view["placements"]) == len(more["placements"]) == 50
    for shown in (view, more):
        newest, *rest = shown["placements"]
        assert newest["uncertain"] == [{"account": "1130-OB", "amount": 30.0, "entries": sources}]
        assert all(p["uncertain"] == [] for p in rest)
        assert shown["reconciliation"]["state"] == "review"
        assert [(o["kind"], o["account"], o["amount"]) for o in shown["reconciliation"]["open"]] == [
            ("uncertain", "1130-OB", 30.0)]
    assert many == few, (few, many)


async def test_the_tab_shows_a_stock_gap_with_what_the_books_and_the_stock_hold(ui_client):
    body, _ = await _tab(ui_client, {**_VIEW, "reconciliation": {"state": "review", "waiting": 0, "reasons": [], "open": [
        {"account": "1130-P", "kind": "stock_gap", "amount": -30.0, "books": 0.0, "stock": 30.0}]}})
    banner = body.split('id="older-stock-reconciliation"')[1].split("</div>")[0]
    assert "1130-P: the books hold $0.00 as stock on this account, but the stock recorded on it is worth $30.00" in banner


async def test_the_older_stock_view_reads_the_lots_still_held_never_every_lot_ever_placed(session, client, auth):
    """200 lots placed automatically, then 190 of them sold and shipped: the view reads
    the lots still in stock, never every lot a placement recorded, so its queries stay
    the same and the rows it reads fall with what is held."""
    from test_older_stock_invoices import _ship
    from test_posting_roles_lots import _sell

    from celerp.services.lot_origin import older_stock_view

    execute, rows = session.execute, []

    async def _counted(*args, **kwargs):
        frozen = (await execute(*args, **kwargs)).freeze()
        rows.append(len(frozen().all()))
        return frozen()

    async def _read() -> tuple[int, int, dict]:
        session.expire_all()
        rows.clear()
        session.execute = _counted
        try:
            view = await older_stock_view(session, auth["company_id"])
        finally:
            del session.execute
        return len(rows), sum(rows), view

    lots = await _imported(session, client, auth, [10.0] * 200)
    await _older_release(session, auth)
    await _marked(session, auth)
    await _startup(session)
    held_queries, held_rows, held = await _read()
    inv = await _sell(client, auth, *[(lot, 1) for lot in lots[:190]])
    await _ship(client, auth, inv, *lots[:190])
    sold_queries, sold_rows, sold = await _read()
    assert (held["totals"]["lots"], sold["totals"]["lots"]) == (200, 10)
    assert {lot["item_id"] for lot in sold["lots"]} == set(lots[190:])
    assert sold["matching"]["count"] == 10 and sold["matching"]["value"] == 100.0
    assert sold_queries <= held_queries
    assert sold_rows < held_rows / 2, (held_rows, sold_rows)
