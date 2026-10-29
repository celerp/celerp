# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Accounts travel with the records that need them, between a client company and its
accountant: each side links the other's accounts by id, adds an account it lacks with
the record, holds a record it cannot place with a one-click fix, and never lets a held
record write anything."""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import select

from company_backup_support import company, owner, token
from migration_support import auth, maker, real_client, real_engine  # noqa: F401
from test_company_backup_lifecycle import _data_dir  # noqa: F401
from test_company_handoff_identity_restore import _link_verified  # noqa: F401
from test_peer_sync_end_to_end import _company_of, _iid, _route_to_app, seed_demo
from test_peer_two_way_sync import (
    ADJUSTED, _journals, _notices, _pair_journals, _post_journal, _two_way,
)
from ui.i18n import t

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]  # two companies each seed the demo

SCOPES = ["contacts", "products", "documents", "inventory", "journals"]


# ---- helpers -----------------------------------------------------------------------------

async def _add(client, tok: str, code: str, name: str, account_type: str = "expense",
               parent: str | None = "6000", cash_flow: str | None = None) -> None:
    r = await client.post("/accounting/accounts", json={
        "code": code, "name": name, "account_type": account_type, "parent_code": parent,
        "cash_flow_category": cash_flow}, headers=auth(tok))
    assert r.status_code == 200, r.text


async def _patch(client, tok: str, code: str, **fields) -> None:
    r = await client.patch(f"/accounting/accounts/{code}", json=fields, headers=auth(tok))
    assert r.status_code == 200, r.text


async def _chart(client, tok: str) -> dict[str, dict]:
    r = await client.get("/accounting/chart", headers=auth(tok))
    assert r.status_code == 200, r.text
    return {a["code"]: a for a in r.json()["items"]}


async def _added_told(engine, company_id: str, account: str) -> list[tuple]:
    """The notices that told the company sync added ``account`` to its chart."""
    return [n for n in await _notices(engine, company_id)
            if n[0] == t("notice.peer_account_added.title") and account in n[1]]


async def _holds(client, tok: str) -> list[dict]:
    r = await client.get("/accounting/peer/holds", headers=auth(tok))
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def _fix(client, tok: str, item: dict, **body) -> dict:
    r = await client.post(f"/accounting/peer/holds/{item['id']}/fix", json=body, headers=auth(tok))
    assert r.status_code == 200, r.text
    return r.json()


async def _links(engine, company_id: str) -> list:
    from celerp.models.company_peer import CompanyPeerAccount
    async with maker(engine)() as s:
        return list((await s.scalars(select(CompanyPeerAccount).where(
            CompanyPeerAccount.company_id == uuid.UUID(company_id)))).all())


def _sent_journal(books: dict, je_id: str) -> dict:
    [state] = [s for s in books.values() if (s.get("peer_source") or {}).get("journal") == je_id]
    return state


def _lines(state: dict) -> set[tuple]:
    return {(l["account"], l["debit"], l["credit"]) for l in state["entries"]}


async def _bill(client, tok: str, lines: list[dict], key: str) -> str:
    """A finalized bill from a supplier, its lines posting to the accounts they name."""
    vendor = await client.post("/crm/contacts", json={"name": f"Supplier {key}", "contact_type": "vendor"},
                               headers=auth(tok))
    assert vendor.status_code in (200, 201), vendor.text
    r = await client.post("/docs", json={
        "doc_type": "bill", "contact_id": vendor.json()["id"], "date": ADJUSTED,
        "line_items": lines, "idempotency_key": key}, headers=auth(tok))
    assert r.status_code in (200, 201), r.text
    doc_id = r.json()["id"]
    done = await client.post(f"/docs/{doc_id}/finalize", headers=auth(tok))
    assert done.status_code == 200, done.text
    return doc_id


async def _paired_without_copy(client, engine, *, client_name: str, books_name: str):
    """A client company and the company its accountant keeps its books in, made from the
    firm's own chart instead of a copy, paired by a key with journals."""
    r = await client.post("/auth/register", json={
        "company_name": client_name, "email": "demo-owner@example.test",
        "name": "Demo Owner", "password": "demopw1234"})
    assert r.status_code == 200, r.text
    tok = r.json()["access_token"]
    await seed_demo.seed_items(client, tok)
    contacts = await seed_demo.seed_contacts(client, tok)
    firm_user = await owner(engine, "firm@example.test", "Firm Owner")
    practice = await company(engine, firm_user, "Firm Office", "firm-office")
    firm_tok = await token(engine, firm_user, practice)
    made = await client.post("/companies", json={"name": books_name}, headers=auth(firm_tok))
    assert made.status_code == 200, made.text
    books_tok = made.json()["access_token"]
    granted = await client.post("/company-peers/grants", json={
        "source_company_id": _company_of(tok), "label": client_name, "firm": "Firm Practice", "scopes": SCOPES},
        headers=auth(books_tok))
    assert granted.status_code == 201, granted.text
    return tok, books_tok, granted.json()["credential"], contacts, firm_user, firm_tok


# ---- AC30 identity ---------------------------------------------------------------------

async def test_accounts_follow_their_link_never_their_name(real_client, real_engine, monkeypatch):
    """AC30 / A1, A8: renaming an account on either side changes nothing on the other and
    breaks no link; an account named like another but coded differently never matches."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    firm_company = _company_of(firm_tok)
    await _patch(real_client, firm_tok, "6500", name="Audit and Accounting")
    await _patch(real_client, tok, "6200", name="Office Rent")
    await _add(real_client, tok, "6150", "Audit and Accounting")
    firm_je = await _post_journal(real_client, firm_tok, ADJUSTED, 30.0, "Firm fee")
    own_je = await _post_journal(real_client, tok, ADJUSTED, 12.0, "Own fee", debit="6150")
    _route_to_app(monkeypatch, [])
    results = await _two_way(client_company, credential)
    assert all(r.ok for r in results.values()), {k: r.errors for k, r in results.items()}

    received = [s for s in (await _journals(real_engine, client_company)).values()
                if (s.get("peer_origin") or {}).get("journal") == firm_je]
    assert _lines(received[0]) == {("6500", 30.0, 0.0), ("6200", 0.0, 30.0)}
    client_chart, firm_chart = await _chart(real_client, tok), await _chart(real_client, firm_tok)
    assert client_chart["6500"]["name"] == "Professional Fees"
    assert client_chart["6200"]["name"] == "Office Rent"
    assert firm_chart["6200"]["name"] == "Rent"
    assert firm_chart["6500"]["name"] == "Audit and Accounting"
    sent = _sent_journal(await _journals(real_engine, firm_company), own_je)
    assert _lines(sent) == {("6150", 12.0, 0.0), ("6200", 0.0, 12.0)}  # never the same-name 6500
    assert firm_chart["6150"]["name"] == "Audit and Accounting"
    by_code = {l.code: l for l in await _links(real_engine, firm_company)}
    assert by_code["6200"].other_name == "Office Rent"  # the other side's name as last seen
    assert by_code["6500"].other_name == "Professional Fees"


# ---- AC31 first pairing ----------------------------------------------------------------

async def test_first_pairing_through_a_copy_links_every_account(real_client, real_engine, monkeypatch):
    """AC31 / A2: the copy links every account on opening, with nothing to review."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    firm_company = _company_of(firm_tok)
    links = await _links(real_engine, firm_company)
    client_chart = await _chart(real_client, tok)
    assert {l.other_code for l in links} == set(client_chart)
    assert {l.how for l in links} == {"copy"}
    assert all(l.other_account_id == client_chart[l.other_code]["id"] for l in links)
    review = (await real_client.get("/accounting/peer/review", headers=auth(firm_tok))).json()
    assert review["companies"] == []


async def test_first_pairing_without_a_copy_waits_for_the_review(real_client, real_engine, monkeypatch):
    """AC31 / A3, T22: without a copy, an account with the same code, type, name and
    currency links; every other one waits for the firm's review, and no record needing it
    posts until then. The firm's answers for one client are offered, marked, for the next."""
    second_tok, second_books, second_key, _, firm_user, firm_tok = await _paired_without_copy(
        real_client, real_engine, client_name="Second Trading", books_name="Second Trading books")
    second_company = _company_of(second_tok)
    await _add(real_client, second_tok, "6150", "Courier")
    first = await _post_journal(real_client, second_tok, ADJUSTED, 9.0, "Courier run", debit="6150")
    plain = await _post_journal(real_client, second_tok, ADJUSTED, 4.0, "Rent top-up", debit="6200",
                                credit="6300")
    _route_to_app(monkeypatch, [])
    results = await _two_way(second_company, second_key)
    books = await _journals(real_engine, _company_of(second_books))
    assert _sent_journal(books, plain)  # same code, type, name and currency: posts
    assert not [s for s in books.values() if (s.get("peer_source") or {}).get("journal") == first]
    waiting = [a for a in results["journals_out"].attention if a["id"] == first]
    assert waiting and waiting[0]["reason"] == "Waiting for Second Trading books to review accounts."

    review = (await real_client.get("/accounting/peer/review", headers=auth(second_books))).json()
    [pending] = review["companies"]
    assert pending["client"] == "Second Trading"
    [row] = pending["accounts"]
    assert (row["code"], row["name"], row["suggested"], row["previous"]) == ("6150", "Courier", None, False)
    assert row["why"] == "Only in the client's chart"
    r = await real_client.post("/accounting/peer/review", json={
        "source_company_id": second_company, "finish": True,
        "answers": [{"account": row["id"], "use": "6800"}]}, headers=auth(second_books))
    assert r.status_code == 200, r.text
    # Until the next sync the held record says it is fixed, here and on Client companies.
    [held] = (await real_client.get("/accounting/peer/holds", headers=auth(second_books))).json()["items"]
    assert held["label"] == "Courier run" and held["fix"]["fixed"]
    assert held["reason"] == "Accounts reviewed. It posts on the next sync."
    view = (await real_client.get("/auth/firm-view", headers=auth(firm_tok))).json()["items"]
    [item] = [i for row in view if row["company_name"] == "Second Trading books" for i in row["attention"]
              if i["label"] == "Courier run"]
    assert item["reason"] == held["reason"] and not item["href"]
    await _two_way(second_company, second_key)
    books = await _journals(real_engine, _company_of(second_books))
    assert _lines(_sent_journal(books, first)) == {("6800", 9.0, 0.0), ("6200", 0.0, 9.0)}

    # The next client the firm takes on without a copy: the answer above is offered.
    made = await real_client.post("/companies", json={"name": "Demo Trading"}, headers=auth(second_tok))
    assert made.status_code == 200, made.text
    tok = made.json()["access_token"]
    client_company = _company_of(tok)
    books_made = await real_client.post("/companies", json={"name": "Demo Trading books"},
                                        headers=auth(firm_tok))
    books_tok = books_made.json()["access_token"]
    granted = await real_client.post("/company-peers/grants", json={
        "source_company_id": client_company, "label": "Demo Trading", "firm": "Firm Practice", "scopes": SCOPES},
        headers=auth(books_tok))
    credential = granted.json()["credential"]
    await _add(real_client, tok, "6150", "Courier")
    held = await _post_journal(real_client, tok, ADJUSTED, 6.0, "Courier again", debit="6150")
    await _two_way(client_company, credential)
    review = (await real_client.get("/accounting/peer/review", headers=auth(books_tok))).json()
    [row] = review["companies"][0]["accounts"]
    assert (row["suggested"], row["previous"]) == ("6800", True)
    r = await real_client.post("/accounting/peer/review", json={
        "source_company_id": client_company, "finish": True,
        "answers": [{"account": row["id"], "use": row["suggested"]}]}, headers=auth(books_tok))
    assert r.status_code == 200, r.text
    await _two_way(client_company, credential)
    assert _lines(_sent_journal(await _journals(real_engine, _company_of(books_tok)), held)) == {
        ("6800", 6.0, 0.0), ("6200", 0.0, 6.0)}
    assert (await real_client.get("/accounting/peer/review", headers=auth(books_tok))).json()["companies"] == []


# ---- AC32 missing account --------------------------------------------------------------

async def test_a_missing_account_is_added_with_the_record_both_ways(real_client, real_engine, monkeypatch):
    """AC32 / A4, A16: the receiver adds the account with the sender's code, name, type,
    parent and cash flow section, with a notice naming it; a second run adds nothing."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    firm_company = _company_of(firm_tok)
    await _add(real_client, tok, "6150", "Courier")
    await _add(real_client, firm_tok, "6160", "Audit adjustments", cash_flow="financing")
    own_je = await _post_journal(real_client, tok, ADJUSTED, 21.0, "Courier run", debit="6150")
    firm_je = await _post_journal(real_client, firm_tok, ADJUSTED, 33.0, "Adjust", debit="6160")
    _route_to_app(monkeypatch, [])
    results = await _two_way(client_company, credential)
    assert all(r.ok for r in results.values()), {k: r.errors for k, r in results.items()}

    firm_chart, client_chart = await _chart(real_client, firm_tok), await _chart(real_client, tok)
    added = firm_chart["6150"]
    assert (added["name"], added["account_type"], added["parent_code"]) == ("Courier", "expense", "6000")
    came = client_chart["6160"]
    assert (came["name"], came["account_type"], came["parent_code"], came["cash_flow_category"]) == (
        "Audit adjustments", "expense", "6000", "financing")
    assert _lines(_sent_journal(await _journals(real_engine, firm_company), own_je)) == {
        ("6150", 21.0, 0.0), ("6200", 0.0, 21.0)}
    received = [s for s in (await _journals(real_engine, client_company)).values()
                if (s.get("peer_origin") or {}).get("journal") == firm_je]
    assert _lines(received[0]) == {("6160", 33.0, 0.0), ("6200", 0.0, 33.0)}
    firm_told = await _added_told(real_engine, firm_company, "6150 Courier")
    client_told = await _added_told(real_engine, client_company, "6160 Audit adjustments")
    assert len(firm_told) == 1 and "Demo Trading" in firm_told[0][1], firm_told
    assert len(client_told) == 1 and "Firm Practice" in client_told[0][1], client_told
    [link] = [l for l in await _links(real_engine, firm_company) if l.code == "6150"]
    assert (link.how, link.added_from) == ("added", "Demo Trading")

    await _two_way(client_company, credential)
    assert await _chart(real_client, firm_tok) == firm_chart
    assert await _chart(real_client, tok) == client_chart
    assert len(await _added_told(real_engine, firm_company, "6150 Courier")) == 1
    assert len(await _added_told(real_engine, client_company, "6160 Audit adjustments")) == 1


# ---- AC33 collision, type and currency -------------------------------------------------

async def test_collision_type_and_currency_hold_until_fixed(real_client, real_engine, monkeypatch):
    """AC33 / A5-A7: each holds the record, writes nothing, names both accounts, and the
    record posts after `Use <account>` or `Add as <code>`."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    firm_company = _company_of(firm_tok)
    await _add(real_client, firm_tok, "6150", "Subscriptions")
    await _add(real_client, tok, "6150", "Courier")
    await _add(real_client, firm_tok, "6170", "Freight")
    await _add(real_client, tok, "6170", "Fuel")
    await _add(real_client, firm_tok, "6180", "Bank charges")
    await _add(real_client, tok, "6180", "Deposits paid", account_type="asset", parent=None)
    collision = await _post_journal(real_client, tok, ADJUSTED, 5.0, "Courier", debit="6150")
    fuel = await _post_journal(real_client, tok, ADJUSTED, 6.0, "Fuel", debit="6170")
    typed = await _post_journal(real_client, tok, ADJUSTED, 7.0, "Deposit", debit="6180")
    _route_to_app(monkeypatch, [])
    before = await _chart(real_client, firm_tok)
    await _two_way(client_company, credential)
    assert await _chart(real_client, firm_tok) == before
    books = await _journals(real_engine, firm_company)
    assert not [s for s in books.values() if (s.get("peer_source") or {}).get("journal")
                in (collision, fuel, typed)]

    holds = {h["label"]: h for h in await _holds(real_client, firm_tok)}
    assert holds["Courier"]["fix"]["kind"] == "collision"
    assert "6150 Courier" in holds["Courier"]["reason"] and "6150 Subscriptions" in holds["Courier"]["reason"]
    assert holds["Fuel"]["fix"]["suggested"] == "6171"
    assert holds["Deposit"]["fix"]["kind"] == "type"
    assert "asset" in holds["Deposit"]["reason"] and "expense" in holds["Deposit"]["reason"]

    await _fix(real_client, firm_tok, holds["Courier"], action="use", account="6150")
    # Until the next sync the held record says it is fixed, as a confirmed review's do.
    courier = {h["label"]: h for h in await _holds(real_client, firm_tok)}["Courier"]
    assert courier["fix"]["fixed"] and courier["reason"] == "Fixed. It posts on the next sync."
    await _fix(real_client, firm_tok, holds["Fuel"], action="add", code="6171")
    r = await real_client.post(f"/accounting/peer/holds/{holds['Deposit']['id']}/fix",
                               json={"action": "use", "account": "6500"}, headers=auth(firm_tok))
    assert r.status_code == 422 and "asset" in r.text  # another account of the right type only
    await _fix(real_client, firm_tok, holds["Deposit"], action="use", account="1140")
    await _two_way(client_company, credential)
    books = await _journals(real_engine, firm_company)
    assert ("6150", 5.0, 0.0) in _lines(_sent_journal(books, collision))
    assert ("6171", 6.0, 0.0) in _lines(_sent_journal(books, fuel))
    assert ("1140", 7.0, 0.0) in _lines(_sent_journal(books, typed))
    assert (await _chart(real_client, firm_tok))["6171"]["name"] == "Fuel"
    assert await _holds(real_client, firm_tok) == []

    # A7: an account kept in another currency links only to one in the same currency.
    r = await real_client.post("/accounting/bank-accounts", json={
        "bank_name": "Euro account", "account_number": "1234", "bank_type": "checking",
        "currency": "EUR", "account_code": "1119"}, headers=auth(tok))
    assert r.status_code in (200, 201), r.text
    euro = await _post_journal(real_client, tok, ADJUSTED, 8.0, "Euro deposit", debit="1119")
    await _two_way(client_company, credential)
    [held] = [h for h in await _holds(real_client, firm_tok) if h["label"] == "Euro deposit"]
    assert held["fix"]["kind"] == "currency" and "EUR" in held["reason"]
    assert held["fix"]["add"] is False  # a currency lives on a bank account record
    r = await real_client.post(f"/accounting/peer/holds/{held['id']}/fix",
                               json={"action": "use", "account": "1140"}, headers=auth(firm_tok))
    assert r.status_code == 422 and "EUR" in r.text
    r = await real_client.post("/accounting/bank-accounts", json={
        "bank_name": "Euro account", "account_number": "9876", "bank_type": "checking",
        "currency": "EUR", "account_code": "1118"}, headers=auth(firm_tok))
    assert r.status_code in (200, 201), r.text
    await _fix(real_client, firm_tok, held, action="use", account="1118")
    await _two_way(client_company, credential)
    assert ("1118", 8.0, 0.0) in _lines(_sent_journal(await _journals(real_engine, firm_company), euro))


# ---- AC34 archived ---------------------------------------------------------------------

async def test_an_account_archived_on_the_receiver_holds_the_record(real_client, real_engine, monkeypatch):
    """AC34 / A10: a firm journal to an account the client archived is held with a notice
    naming it; Unarchive posts it, Use another account relinks and posts it; a client
    document to an account the firm archived is held on the firm the same way."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    firm_company = _company_of(firm_tok)
    await _patch(real_client, tok, "6700", is_active=False)
    await _patch(real_client, tok, "6800", is_active=False)
    insured = await _post_journal(real_client, firm_tok, ADJUSTED, 11.0, "Insurance accrual", debit="6700")
    supplies = await _post_journal(real_client, firm_tok, ADJUSTED, 12.0, "Supplies accrual", debit="6800")
    _route_to_app(monkeypatch, [])
    await _two_way(client_company, credential)

    def _from(books, je):
        return [s for s in books.values() if (s.get("peer_origin") or {}).get("journal") == je]

    books = await _journals(real_engine, client_company)
    assert not _from(books, insured) and not _from(books, supplies)
    told = [n for n in await _notices(real_engine, client_company) if "Insurance accrual" in n[1]]
    assert told and "6700 Insurance, which you archived" in told[-1][1], told
    holds = {h["label"]: h for h in await _holds(real_client, tok)}
    assert holds["Insurance accrual"]["fix"]["kind"] == "archived"

    await _fix(real_client, tok, holds["Insurance accrual"], action="unarchive")
    await _fix(real_client, tok, holds["Supplies accrual"], action="use", account="6600")
    fixed = {h["label"]: h["fix"].get("fixed") for h in await _holds(real_client, tok)}
    assert fixed["Insurance accrual"] is True and fixed["Supplies accrual"] is True  # shown as fixed until it posts
    await _two_way(client_company, credential)
    books = await _journals(real_engine, client_company)
    assert ("6700", 11.0, 0.0) in _lines(_from(books, insured)[0])
    assert ("6600", 12.0, 0.0) in _lines(_from(books, supplies)[0])
    again = await _post_journal(real_client, firm_tok, ADJUSTED, 13.0, "Supplies again", debit="6800")
    await _two_way(client_company, credential)
    assert ("6600", 13.0, 0.0) in _lines(_from(await _journals(real_engine, client_company), again)[0])

    # The firm's side: a client bill to an account the firm archived.
    await _patch(real_client, firm_tok, "6300", is_active=False)
    bill = await _bill(real_client, tok, [{"name": "Electricity", "quantity": 1, "unit_price": 50.0,
                                           "line_total": 50.0, "account_code": "6300",
                                           "receive_as": "expense"}], "archived-bill")
    await _two_way(client_company, credential)
    [held] = [h for h in await _holds(real_client, firm_tok) if h["fix"]["kind"] == "archived"]
    assert "6300 Utilities" in held["reason"]
    await _fix(real_client, firm_tok, held, action="unarchive")
    await _two_way(client_company, credential)
    assert await _holds(real_client, firm_tok) == []
    from celerp.models.company_peer import CompanyPeerIdentity
    async with maker(real_engine)() as s:
        assert await s.scalar(select(CompanyPeerIdentity.target_entity_id).where(
            CompanyPeerIdentity.company_id == uuid.UUID(firm_company),
            CompanyPeerIdentity.entity_type == "doc", CompanyPeerIdentity.source_entity_id == bill))


# ---- AC35 posting roles ----------------------------------------------------------------

async def test_a_posting_role_line_crosses_as_the_role(real_client, real_engine, monkeypatch):
    """AC35 / A12: a line on a posting-role account posts to the receiver's account for
    that role; a receiver without the role holds the record naming it; no control account
    is created or relinked by sync."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    firm_company = _company_of(firm_tok)
    await _add(real_client, firm_tok, "6955", "General costs")
    r = await real_client.put("/accounting/posting-accounts/general_expense", json={"code": "6955"},
                              headers=auth(firm_tok))
    assert r.status_code == 200, r.text
    general = await _post_journal(real_client, tok, ADJUSTED, 14.0, "General", debit="6950")
    _route_to_app(monkeypatch, [])
    await _two_way(client_company, credential)
    assert ("6955", 14.0, 0.0) in _lines(_sent_journal(await _journals(real_engine, firm_company), general))
    links = {l.code: l for l in await _links(real_engine, firm_company)}
    assert links["6950"].other_code == "6950"  # the copy's link, never relinked by a role

    # A firm that keeps its own imported chart, with no posting accounts chosen yet.
    firm_user = await owner(real_engine, "other-firm@example.test", "Other Firm")
    books = await company(real_engine, firm_user, "Other Firm books", "other-books")
    books_tok = await token(real_engine, firm_user, books)
    r = await real_client.post("/accounting/accounts/import/batch", json={"records": [
        {"code": "1120", "name": "Debtors", "account_type": "asset"},
        {"code": "4000", "name": "Sales", "account_type": "revenue"}]}, headers=auth(books_tok))
    assert r.status_code == 200, r.text
    granted = await real_client.post("/company-peers/grants", json={
        "source_company_id": client_company, "label": "Demo Trading", "firm": "Firm Practice", "scopes": SCOPES},
        headers=auth(books_tok))
    other_key = granted.json()["credential"]
    receivable = await _post_journal(real_client, tok, ADJUSTED, 15.0, "Debtor reclass",
                                     debit="1120", credit="4100")
    chart = await _chart(real_client, books_tok)
    await _two_way(client_company, other_key)
    assert await _chart(real_client, books_tok) == chart
    [held] = [h for h in await _holds(real_client, books_tok) if h["label"] == "Debtor reclass"]
    assert held["fix"]["kind"] == "role" and "Accounts receivable" in held["reason"]
    assert held["fix"]["href"] == "/settings/accounting#posting-accounts"
    assert not [s for s in (await _journals(real_engine, str(books))).values()
                if (s.get("peer_source") or {}).get("journal") == receivable]


# ---- AC36 tax codes --------------------------------------------------------------------

async def test_a_tax_code_crosses_with_its_document(real_client, real_engine, monkeypatch):
    """AC36 / A13: a taxed line posts its own rate and amount; a missing code is added with
    a notice; a same-name code at another rate on the receiver is left unchanged."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    firm_company = _company_of(firm_tok)
    levy = {"name": "Import levy", "rate": 3.0, "tax_type": "purchase"}
    customs = {"name": "Customs", "rate": 4.0, "tax_type": "purchase"}
    r = await real_client.patch("/companies/me/purchasing-taxes", json={"taxes": [levy, customs]},
                                headers=auth(tok))
    assert r.status_code == 200, r.text
    r = await real_client.patch("/companies/me/purchasing-taxes", json={"taxes": [
        {"name": "Customs", "rate": 5.0, "tax_type": "purchase"}]}, headers=auth(firm_tok))
    assert r.status_code == 200, r.text
    bill = await _bill(real_client, tok, [
        {"name": "Crates", "quantity": 1, "unit_price": 100.0, "line_total": 100.0,
         "account_code": "6800", "receive_as": "expense",
         "taxes": [{"code": "Import levy", "rate": 3.0, "amount": 3.0}]},
        {"name": "Duty paperwork", "quantity": 1, "unit_price": 200.0, "line_total": 200.0,
         "account_code": "6800", "receive_as": "expense",
         "taxes": [{"code": "Customs", "rate": 4.0, "amount": 8.0}]}], "taxed-bill")
    _route_to_app(monkeypatch, [])
    results = await _two_way(client_company, credential)
    assert results["documents_out"].ok, results["documents_out"].errors

    taxes = {x["name"]: x for x in (await real_client.get("/companies/me/purchasing-taxes",
                                                          headers=auth(firm_tok))).json()}
    assert taxes["Import levy"]["rate"] == 3.0
    assert taxes["Customs"]["rate"] == 5.0  # the receiver's own code stays as it is
    told = [n for n in await _notices(real_engine, firm_company) if "Import levy" in n[1]]
    assert len(told) == 1 and "3" in told[0][1], told
    from celerp.models.company_peer import CompanyPeerIdentity
    async with maker(real_engine)() as s:
        doc = await s.scalar(select(CompanyPeerIdentity.target_entity_id).where(
            CompanyPeerIdentity.company_id == uuid.UUID(firm_company),
            CompanyPeerIdentity.entity_type == "doc", CompanyPeerIdentity.source_entity_id == bill))
    state = (await real_client.get(f"/docs/{doc}", headers=auth(firm_tok))).json()
    assert state["tax"] == 11.0
    from celerp.models.ledger import LedgerEntry
    async with maker(real_engine)() as s:
        posted = set((await s.scalars(select(LedgerEntry.entity_id).where(
            LedgerEntry.company_id == uuid.UUID(firm_company), LedgerEntry.entity_type == "journal_entry",
            LedgerEntry.metadata_["doc_id"].as_string() == doc))).all())
    tax_input = [s for eid, s in (await _journals(real_engine, firm_company)).items() if eid in posted]
    assert any(l["account"] == "1150" and l["debit"] == 11.0 for s in tax_input for l in s["entries"])


# ---- AC38 hierarchy --------------------------------------------------------------------

async def test_a_created_accounts_parent_crosses_first(real_client, real_engine, monkeypatch):
    """AC38 / A15: the parent is added first; a parent the receiver cannot use leaves the
    child at the top of its type and the notice says so."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    firm_company = _company_of(firm_tok)
    await _add(real_client, tok, "6990", "Professional services")
    await _add(real_client, tok, "6991", "Audit", parent="6990")
    await _patch(real_client, firm_tok, "6300", is_active=False)
    await _add(real_client, tok, "6310", "Water", parent="6300")
    audit = await _post_journal(real_client, tok, ADJUSTED, 16.0, "Audit", debit="6991")
    water = await _post_journal(real_client, tok, ADJUSTED, 17.0, "Water", debit="6310")
    _route_to_app(monkeypatch, [])
    await _two_way(client_company, credential)
    chart = await _chart(real_client, firm_tok)
    assert (chart["6990"]["parent_code"], chart["6991"]["parent_code"]) == ("6000", "6990")
    assert chart["6310"]["parent_code"] is None
    books = await _journals(real_engine, firm_company)
    assert ("6991", 16.0, 0.0) in _lines(_sent_journal(books, audit))
    assert ("6310", 17.0, 0.0) in _lines(_sent_journal(books, water))
    [told] = [n for n in await _notices(real_engine, firm_company) if "6310 Water" in n[1]]
    assert "top of the expense accounts" in told[1] and "6300" in told[1], told


# ---- AC40 one transaction --------------------------------------------------------------

async def test_a_held_record_writes_nothing(real_client, real_engine, monkeypatch):
    """AC40 / A18: a record and the accounts it would add commit together or not at all."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    firm_company = _company_of(firm_tok)
    await _add(real_client, tok, "6150", "Courier")
    await _patch(real_client, firm_tok, "6800", is_active=False)
    r = await real_client.post("/accounting/journal-entries", headers=auth(tok), json={
        "ts": ADJUSTED, "memo": "Mixed", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": "6150", "debit": 10.0, "credit": 0.0},
                    {"account": "6800", "debit": 5.0, "credit": 0.0},
                    {"account": "6200", "debit": 0.0, "credit": 15.0}]})
    assert r.status_code == 200, r.text
    _route_to_app(monkeypatch, [])
    await _two_way(client_company, credential)
    assert "6150" not in await _chart(real_client, firm_tok)
    assert not [l for l in await _links(real_engine, firm_company) if l.other_code == "6150"]

    await _patch(real_client, firm_tok, "6800", is_active=True)
    from celerp.services import peer_journals

    async def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(peer_journals, "emit_event", broken)
    await _two_way(client_company, credential)
    assert "6150" not in await _chart(real_client, firm_tok)
    assert not [l for l in await _links(real_engine, firm_company) if l.other_code == "6150"]
    assert not await _added_told(real_engine, firm_company, "6150 Courier")


# ---- AC39 an added account kept, archived or removed ----------------------------------

async def _added(client, tok: str) -> dict[str, dict]:
    r = await client.get("/accounting/peer/holds", headers=auth(tok))
    assert r.status_code == 200, r.text
    return {a["code"]: a for a in r.json()["added"]}


async def _settle(client, tok: str, item: dict, action: str):
    return await client.post(f"/accounting/peer/added/{item['id']}", json={"action": action},
                             headers=auth(tok))


async def test_an_added_account_is_kept_archived_or_removed(real_client, real_engine, monkeypatch):
    """AC39 / A17: Remove is offered only on an added account with no postings, children,
    posting role or bank account, and removes it with its link; any other is offered
    Archive with the reason. Keep closes the notice and leaves the account."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    firm_company = _company_of(firm_tok)
    await _add(real_client, tok, "6140", "Delivery")
    await _add(real_client, tok, "6150", "Courier", parent="6140")
    await _post_journal(real_client, tok, ADJUSTED, 21.0, "Courier run", debit="6150")
    await _add(real_client, firm_tok, "6160", "Audit adjustments")
    await _post_journal(real_client, firm_tok, ADJUSTED, 9.0, "Adjust", debit="6160")
    _route_to_app(monkeypatch, [])
    await _two_way(client_company, credential)

    added = await _added(real_client, firm_tok)
    assert set(added) == {"6140", "6150"}
    assert added["6150"]["removable"] is False and "entries" in added["6150"]["why"]
    assert added["6140"]["removable"] is False and "under it" in added["6140"]["why"]
    r = await _settle(real_client, firm_tok, added["6150"], "remove")
    assert r.status_code == 409 and "archived, not removed" in r.text
    r = await real_client.get("/auth/firm-view", headers=auth(firm_tok))
    [row] = [row for row in r.json()["items"] if row["company_id"] == firm_company]
    assert {i["href"] for i in row["attention"] if i["kind"] == "added"} == {"/accounting/sync-accounts"}

    await _patch(real_client, firm_tok, "6150", parent_code="")
    r = await _settle(real_client, firm_tok, added["6150"], "archive")
    assert r.status_code == 200, r.text
    assert (await _chart(real_client, firm_tok))["6150"]["is_active"] is False
    added = await _added(real_client, firm_tok)
    assert set(added) == {"6140"} and added["6140"]["removable"] is True and added["6140"]["why"] is None
    r = await _settle(real_client, firm_tok, added["6140"], "remove")
    assert r.status_code == 200, r.text
    assert "6140" not in await _chart(real_client, firm_tok)
    assert not [l for l in await _links(real_engine, firm_company) if l.code == "6140"]
    assert await _added(real_client, firm_tok) == {}

    kept = (await _added(real_client, tok))["6160"]
    r = await _settle(real_client, tok, kept, "keep")
    assert r.status_code == 200, r.text
    assert await _added(real_client, tok) == {}
    assert (await _chart(real_client, tok))["6160"]["is_active"] is True
    r = await _settle(real_client, tok, kept, "keep")
    assert r.status_code == 404  # a closed notice is gone, not kept twice


# ---- A8 the other side's name ----------------------------------------------------------

async def test_the_ledger_shows_the_other_sides_name_when_it_differs(real_client, real_engine, monkeypatch):
    """A8: names never travel after linking; the account page shows the other company's
    code and name for a linked account named differently, and nothing for one named alike."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    await _add(real_client, firm_tok, "6150", "Subscriptions")
    await _add(real_client, tok, "6150", "Courier")
    await _post_journal(real_client, tok, ADJUSTED, 5.0, "Courier", debit="6150")
    await _post_journal(real_client, tok, ADJUSTED, 6.0, "Rent", debit="6100")
    _route_to_app(monkeypatch, [])
    await _two_way(client_company, credential)
    [held] = await _holds(real_client, firm_tok)
    await _fix(real_client, firm_tok, held, action="use", account="6150")
    await _two_way(client_company, credential)

    r = await real_client.get("/accounting/ledger/6150", headers=auth(firm_tok))
    assert r.status_code == 200, r.text
    assert r.json()["elsewhere"] == [{"company": "Demo Trading", "code": "6150", "name": "Courier"}]
    r = await real_client.get("/accounting/ledger/6100", headers=auth(firm_tok))
    assert r.json()["elsewhere"] == []
    await _patch(real_client, tok, "6150", name="Couriers and post")
    await _post_journal(real_client, tok, ADJUSTED, 7.0, "Post", debit="6150")
    await _two_way(client_company, credential)
    r = await real_client.get("/accounting/ledger/6150", headers=auth(firm_tok))
    assert r.json()["elsewhere"][0]["name"] == "Couriers and post"
    assert (await _chart(real_client, firm_tok))["6150"]["name"] == "Subscriptions"  # names never travel
