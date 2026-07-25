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
async def test_an_accounts_section_is_derived_from_its_type_and_code(client):
    """Which section a figure lands in is read off the account every run.

    6200 is an expense, so rent paid in cash is operating and only operating.
    Nothing is stored against the account to say otherwise, which is what keeps
    two companies on the same chart reading the same statement.
    """
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
