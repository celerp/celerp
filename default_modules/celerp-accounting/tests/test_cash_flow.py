# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""GET /accounting/cash-flow: both methods, and the equality that binds them.

The direct and indirect methods read the same entries from opposite ends, so they
must land on the same number. Because every posting is stored in base currency at
its own date, nothing restates a cash balance after the fact and there is no
reconciling item: the check below is exact equality, not a tolerance. Any drift
means an entry whose sides do not sum to zero, or a classification that lost cents.
"""

from __future__ import annotations

import uuid

import pytest


async def _reg(client) -> str:
    addr = f"cf-{uuid.uuid4().hex[:8]}@cf.test"
    r = await client.post("/auth/register", json={
        "company_name": "CfCo", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


async def _je(client, tok, entries, ts="2026-02-10", memo="Entry"):
    r = await client.post("/accounting/journal-entries", json={
        "ts": ts, "memo": memo, "entries": entries,
        "idempotency_token": uuid.uuid4().hex,
    }, headers=_h(tok))
    assert r.status_code == 200, r.text
    return r.json()


async def _cash_flow(client, tok, **params):
    r = await client.get("/accounting/cash-flow", params=params, headers=_h(tok))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_direct_and_indirect_agree_exactly(client):
    """The load-bearing assertion: both methods and the movement in the cash
    accounts are one number, to the cent."""
    tok = await _reg(client)
    # Cash sale, rent paid in cash, equipment bought for cash.
    await _je(client, tok, [{"account": "1111", "debit": 1000.0, "credit": 0.0},
                            {"account": "4100", "debit": 0.0, "credit": 1000.0}])
    await _je(client, tok, [{"account": "6200", "debit": 300.0, "credit": 0.0},
                            {"account": "1111", "debit": 0.0, "credit": 300.0}])
    await _je(client, tok, [{"account": "1210", "debit": 500.0, "credit": 0.0},
                            {"account": "1111", "debit": 0.0, "credit": 500.0}])

    data = await _cash_flow(client, tok)
    assert data["net_change"] == pytest.approx(200.0)
    assert data["direct"]["total"] == data["net_change"]
    assert data["indirect"]["total"] == data["net_change"]
    assert data["balanced"] is True


@pytest.mark.asyncio
async def test_movements_land_in_the_right_section(client):
    tok = await _reg(client)
    await _je(client, tok, [{"account": "1111", "debit": 1000.0, "credit": 0.0},
                            {"account": "4100", "debit": 0.0, "credit": 1000.0}])
    await _je(client, tok, [{"account": "1210", "debit": 500.0, "credit": 0.0},
                            {"account": "1111", "debit": 0.0, "credit": 500.0}])
    await _je(client, tok, [{"account": "1111", "debit": 800.0, "credit": 0.0},
                            {"account": "2210", "debit": 0.0, "credit": 800.0}])

    data = await _cash_flow(client, tok)
    assert data["direct"]["operating"]["total"] == pytest.approx(1000.0)
    assert data["direct"]["investing"]["total"] == pytest.approx(-500.0)
    assert data["direct"]["financing"]["total"] == pytest.approx(800.0)


@pytest.mark.asyncio
async def test_a_multi_leg_entry_splits_without_losing_cents(client):
    """One payment covering several accounts has no single category, so the
    movement is apportioned; the parts must still sum to the whole."""
    tok = await _reg(client)
    await _je(client, tok, [{"account": "6200", "debit": 3.33, "credit": 0.0},
                            {"account": "6300", "debit": 3.33, "credit": 0.0},
                            {"account": "6400", "debit": 3.34, "credit": 0.0},
                            {"account": "1111", "debit": 0.0, "credit": 10.0}])
    data = await _cash_flow(client, tok)
    assert data["net_change"] == pytest.approx(-10.0)
    assert data["direct"]["total"] == pytest.approx(-10.0)
    assert data["indirect"]["total"] == pytest.approx(-10.0)


@pytest.mark.asyncio
async def test_the_same_entries_always_split_the_same_way(client):
    """Apportioning introduces rounding, so the residual has to land in a fixed
    place or two runs of the same books would disagree."""
    tok = await _reg(client)
    await _je(client, tok, [{"account": "6200", "debit": 1.0, "credit": 0.0},
                            {"account": "6300", "debit": 1.0, "credit": 0.0},
                            {"account": "6400", "debit": 1.0, "credit": 0.0},
                            {"account": "1111", "debit": 0.0, "credit": 3.0}])
    first = await _cash_flow(client, tok)
    second = await _cash_flow(client, tok)
    assert first["direct"] == second["direct"]
    assert first["indirect"] == second["indirect"]


@pytest.mark.asyncio
async def test_opening_cash_excludes_the_period_and_closing_includes_it(client):
    tok = await _reg(client)
    await _je(client, tok, [{"account": "1111", "debit": 100.0, "credit": 0.0},
                            {"account": "4100", "debit": 0.0, "credit": 100.0}],
              ts="2026-01-05")
    await _je(client, tok, [{"account": "1111", "debit": 50.0, "credit": 0.0},
                            {"account": "4100", "debit": 0.0, "credit": 50.0}],
              ts="2026-02-05")

    data = await _cash_flow(client, tok, date_from="2026-02-01", date_to="2026-02-28")
    assert data["opening_cash"] == pytest.approx(100.0)
    assert data["net_change"] == pytest.approx(50.0)
    assert data["closing_cash"] == pytest.approx(150.0)
    assert data["direct"]["total"] == data["net_change"]
    assert data["indirect"]["total"] == data["net_change"]


@pytest.mark.asyncio
async def test_an_entry_that_moves_no_cash_changes_nothing(client):
    tok = await _reg(client)
    await _je(client, tok, [{"account": "1120", "debit": 400.0, "credit": 0.0},
                            {"account": "4100", "debit": 0.0, "credit": 400.0}])
    data = await _cash_flow(client, tok)
    assert data["net_change"] == pytest.approx(0.0)
    assert data["direct"]["total"] == pytest.approx(0.0)
    # Profit earned but not collected still reconciles: the receivable offsets it.
    assert data["indirect"]["net_profit"] == pytest.approx(400.0)
    assert data["indirect"]["total"] == pytest.approx(0.0)
    assert data["balanced"] is True


@pytest.mark.asyncio
async def test_an_account_with_no_override_is_classified_by_type_and_code(client):
    """The default: 6200 is an expense, so rent paid in cash is operating."""
    tok = await _reg(client)
    await _je(client, tok, [{"account": "6200", "debit": 100.0, "credit": 0.0},
                            {"account": "1111", "debit": 0.0, "credit": 100.0}])
    data = await _cash_flow(client, tok)
    assert data["direct"]["operating"]["total"] == pytest.approx(-100.0)
    assert [line["code"] for line in data["direct"]["operating"]["lines"]] == ["6200"]
    assert data["direct"]["investing"]["total"] == pytest.approx(0.0)
    assert data["direct"]["financing"]["total"] == pytest.approx(0.0)
    assert data["balanced"] is True


@pytest.mark.asyncio
async def test_an_account_override_beats_the_derived_default(client):
    """A company whose own chart disagrees with the default says so per account,
    through the same API the chart of accounts screen uses. Nothing here touches
    the database directly: an override a user cannot set is not an override."""
    tok = await _reg(client)
    await _je(client, tok, [{"account": "6200", "debit": 100.0, "credit": 0.0},
                            {"account": "1111", "debit": 0.0, "credit": 100.0}])
    before = await _cash_flow(client, tok)
    assert before["direct"]["operating"]["total"] == pytest.approx(-100.0)

    r = await client.patch("/accounting/accounts/6200",
                           json={"cash_flow_category": "financing"}, headers=_h(tok))
    assert r.status_code == 200, r.text
    assert r.json()["cash_flow_category"] == "financing"

    after = await _cash_flow(client, tok)
    assert after["direct"]["operating"]["total"] == pytest.approx(0.0)
    assert after["direct"]["financing"]["total"] == pytest.approx(-100.0)
    assert [line["code"] for line in after["direct"]["financing"]["lines"]] == ["6200"]
    # Moving a figure between sections must not break the tie-out.
    assert after["net_change"] == pytest.approx(before["net_change"])
    assert after["balanced"] is True


@pytest.mark.asyncio
async def test_clearing_the_override_returns_the_account_to_its_default(client):
    """An empty value clears the override rather than storing a blank category,
    so a company can undo the change it just made."""
    tok = await _reg(client)
    await _je(client, tok, [{"account": "6200", "debit": 100.0, "credit": 0.0},
                            {"account": "1111", "debit": 0.0, "credit": 100.0}])
    await client.patch("/accounting/accounts/6200",
                       json={"cash_flow_category": "financing"}, headers=_h(tok))

    r = await client.patch("/accounting/accounts/6200",
                           json={"cash_flow_category": ""}, headers=_h(tok))
    assert r.status_code == 200, r.text
    assert r.json()["cash_flow_category"] is None

    data = await _cash_flow(client, tok)
    assert data["direct"]["operating"]["total"] == pytest.approx(-100.0)
    assert data["direct"]["financing"]["total"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_an_unknown_category_is_rejected_and_the_valid_ones_named(client):
    """Validation sits on the endpoint, and the message says what was allowed
    instead of failing silently or storing a value the statement cannot use."""
    tok = await _reg(client)
    r = await client.patch("/accounting/accounts/6200",
                           json={"cash_flow_category": "bogus"}, headers=_h(tok))
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    for name in ("operating", "investing", "financing"):
        assert name in detail

    c = await client.post("/accounting/accounts", json={
        "code": "6299", "name": "Odd", "account_type": "expense",
        "cash_flow_category": "bogus"}, headers=_h(tok))
    assert c.status_code == 422, c.text


@pytest.mark.asyncio
async def test_a_new_account_can_carry_its_category_from_the_start(client):
    """The override is settable when the account is created, not only afterwards."""
    tok = await _reg(client)
    r = await client.post("/accounting/accounts", json={
        "code": "6298", "name": "Levy", "account_type": "expense",
        "cash_flow_category": "financing"}, headers=_h(tok))
    assert r.status_code == 200, r.text
    assert r.json()["cash_flow_category"] == "financing"

    await _je(client, tok, [{"account": "6298", "debit": 40.0, "credit": 0.0},
                            {"account": "1111", "debit": 0.0, "credit": 40.0}])
    data = await _cash_flow(client, tok)
    assert data["direct"]["financing"]["total"] == pytest.approx(-40.0)


@pytest.mark.asyncio
async def test_the_chart_reports_the_override_so_a_screen_can_show_it(client):
    """GET /accounting/chart carries the field, which is what lets the chart of
    accounts screen show the current setting rather than guess it."""
    tok = await _reg(client)
    await client.patch("/accounting/accounts/6200",
                       json={"cash_flow_category": "investing"}, headers=_h(tok))
    r = await client.get("/accounting/chart", headers=_h(tok))
    assert r.status_code == 200, r.text
    by_code = {a["code"]: a for a in r.json()["items"]}
    assert by_code["6200"]["cash_flow_category"] == "investing"
    assert by_code["1111"]["cash_flow_category"] is None


def test_the_screen_offers_exactly_the_categories_the_api_accepts():
    """The chart of accounts screen and the endpoint hold the same list, and the
    endpoint is the authoritative side. They live in separate processes so the
    screen cannot import it; this is what keeps them in lockstep instead."""
    from celerp_accounting.routes import CASH_FLOW_CATEGORIES
    from ui.routes.settings_accounting import CASH_FLOW_CATEGORIES as OFFERED

    assert set(OFFERED) == set(CASH_FLOW_CATEGORIES)


@pytest.mark.asyncio
async def test_reports_permission_can_read_it(client, session):
    """Cash flow is a report, so it answers to the report permission and to nothing
    else. The subject holds view_financial_reports and not manage_accounting, which
    is the only pairing that tells the two grants apart: anyone holding both would
    read the statement either way and prove nothing about which grant let them in.
    """
    from test_helpers import grant_permission
    from test_journal_gl_soa import _user_with_role
    admin = await _reg(client)
    reader = await _user_with_role(client, session, admin, "operator")
    await grant_permission(client, _h(admin), "view_financial_reports", "operator")

    r = await client.get("/accounting/cash-flow", headers=_h(reader))
    assert r.status_code == 200, r.text

    # manage_accounting stays at manager, so reading the books never became writing them.
    w = await client.post("/accounting/journal-entries", json={
        "ts": "2026-02-10", "memo": "Entry", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": "1111", "debit": 10.0, "credit": 0.0},
                    {"account": "4100", "debit": 0.0, "credit": 10.0}],
    }, headers=_h(reader))
    assert w.status_code == 403, w.text
    assert "manage_accounting" in w.json()["detail"]


# ---------------------------------------------------------------------------
# Bank opening balances: which source the statement believes
# ---------------------------------------------------------------------------


async def _bank(client, tok, name: str, opening: float) -> dict:
    r = await client.post("/accounting/bank-accounts", json={
        "bank_name": name, "account_number": "0001", "bank_type": "checking",
        "currency": "USD", "opening_balance": opening,
    }, headers=_h(tok))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_an_opening_balance_is_counted_once_not_twice(client):
    """A bank account's opening balance reaches the books one way only.

    Creating the account posts the opening balance as a journal entry, so the
    ledger already carries it. Adding the column on top of the entries counted the
    same money twice and made the bank screen disagree with every report that
    reads the ledger.
    """
    tok = await _reg(client)
    bank = await _bank(client, tok, "Opening Bank", 1000.0)
    code = bank["chart_account_code"]

    assert bank["balance"] == pytest.approx(1000.0)

    listed = await client.get("/accounting/bank-accounts", headers=_h(tok))
    shown = {b["chart_account_code"]: b for b in listed.json()["items"]}[code]
    assert shown["balance"] == pytest.approx(1000.0)

    fetched = await client.get(f"/accounting/bank-accounts/{bank['id']}", headers=_h(tok))
    assert fetched.json()["balance"] == pytest.approx(1000.0)

    ledger = await client.get(f"/accounting/ledger/{code}", headers=_h(tok))
    assert ledger.json()["closing_balance"] == pytest.approx(1000.0)

    data = await _cash_flow(client, tok)
    assert data["closing_cash"] == pytest.approx(1000.0)
    assert data["unbacked_bank_openings"] == []


@pytest.mark.asyncio
async def test_an_opening_balance_with_no_entry_behind_it_is_reported_not_absorbed(client, session):
    """The ledger is authoritative, and the gap is named rather than papered over.

    A bank row can hold an opening balance that no entry carries. Folding it back
    into the balance would put the bank screen back out of step with the books and
    give the reader a figure the ledger cannot support, so the account is reported
    instead and the balance stays the ledger's.
    """
    from sqlalchemy import select

    from celerp_accounting.models import BankAccount

    tok = await _reg(client)
    bank = await _bank(client, tok, "Legacy Bank", 0.0)
    code = bank["chart_account_code"]

    row = (await session.execute(
        select(BankAccount).where(BankAccount.id == uuid.UUID(bank["id"]))
    )).scalar_one()
    row.opening_balance = 750.0
    await session.commit()

    fetched = await client.get(f"/accounting/bank-accounts/{bank['id']}", headers=_h(tok))
    assert fetched.json()["balance"] == pytest.approx(0.0), "the ledger says nothing was ever posted"
    assert fetched.json()["opening_unbacked"] is True

    data = await _cash_flow(client, tok)
    assert data["closing_cash"] == pytest.approx(0.0)
    assert [u["chart_account_code"] for u in data["unbacked_bank_openings"]] == [code]
    reported = data["unbacked_bank_openings"][0]
    assert reported["bank_name"] == "Legacy Bank"
    assert reported["opening_balance"] == pytest.approx(750.0)


def test_the_statement_names_the_accounts_whose_opening_balance_is_missing():
    """A count would tell the reader there is a problem without telling them where.

    The warning sits on the statement because that is where the figure it explains
    is read, and it names each account so the fix is one entry away rather than a
    hunt through the bank list.
    """
    from fasthtml.common import to_xml

    from ui.routes.financial_reports import _cash_flow_view

    clean = {"opening_cash": 0.0, "closing_cash": 0.0, "net_change": 0.0,
             "direct": {}, "indirect": {}, "unbacked_bank_openings": []}
    assert "flash--warning" not in to_xml(_cash_flow_view(clean, "THB"))

    flagged = dict(clean, unbacked_bank_openings=[
        {"bank_account_id": "b1", "bank_name": "Legacy Bank",
         "chart_account_code": "1112", "opening_balance": 750.0},
    ])
    markup = to_xml(_cash_flow_view(flagged, "THB"))
    assert "flash--warning" in markup
    assert "Legacy Bank" in markup and "1112" in markup and "750" in markup


def test_the_bank_screen_says_why_its_figure_leaves_the_opening_balance_out():
    """The balance shown there is the ledger's, and a reader who typed an opening
    balance into that same screen needs to be told why it is not in the number."""
    from fasthtml.common import to_xml

    from ui.routes.settings_accounting import _bank_account_row

    backed = {"id": "b1", "bank_name": "Bank", "chart_account_code": "1112",
              "bank_type": "checking", "account_number": "1", "currency": "USD",
              "balance": 1000.0, "is_active": True, "opening_unbacked": False}
    assert "flash--warning" not in to_xml(_bank_account_row(backed))
    assert "flash--warning" in to_xml(_bank_account_row(dict(backed, opening_unbacked=True)))
