# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Accounting routes refuse malformed input cleanly and date what they post by the
company's business day.

A year-end that is not a YYYY-MM-DD date is refused before anything posts or locks.
A malformed bank account id is a 422, never a 500. A bank account's opening balance is
dated the company's today, never the server's UTC day.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import func, select

from celerp.models.projections import Projection
from celerp.services.company_lock import locked_company
from test_posting_roles_older_stock import _clock

pytestmark = pytest.mark.asyncio


async def _je_count(session, cid) -> int:
    return (await session.execute(select(func.count()).select_from(Projection).where(
        Projection.company_id == cid, Projection.entity_type == "journal_entry"))).scalar_one()


async def _settings(session, cid) -> dict:
    session.expire_all()
    return dict((await locked_company(session, cid)).settings or {})


async def _revenue(client, auth, ts="2025-06-01"):
    r = await client.post("/accounting/journal-entries", headers=auth["headers"], json={
        "ts": ts, "memo": "Sale", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": "1111", "debit": 70.0, "credit": 0.0},
                    {"account": "4100", "debit": 0.0, "credit": 70.0}]})
    assert r.status_code == 200, r.text


# --- Year-end close --------------------------------------------------------------


@pytest.mark.parametrize("bad", ["x", "2026-13-45", "31/12/2025", "20251231", ""])
async def test_close_year_refuses_a_year_end_that_is_not_a_date(client, session, auth, bad):
    await _revenue(client, auth)
    cid = auth["company_id"]
    before, lock0 = await _je_count(session, cid), (await _settings(session, cid)).get("lock_date")
    r = await client.post("/accounting/close-year", headers=auth["headers"], json={"fiscal_year_end": bad})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message_key"] == "accounting.year_end_invalid"
    assert await _je_count(session, cid) == before
    assert (await _settings(session, cid)).get("lock_date") == lock0


async def test_close_year_with_a_valid_year_end_closes_and_locks_through_it(client, session, auth):
    """Neighbour: a proper year-end still posts the closing entry and locks through it."""
    await _revenue(client, auth)
    cid = auth["company_id"]
    before = await _je_count(session, cid)
    r = await client.post("/accounting/close-year", headers=auth["headers"], json={"fiscal_year_end": "2025-12-31"})
    assert r.status_code == 200, r.text
    assert await _je_count(session, cid) == before + 1
    assert (await _settings(session, cid))["lock_date"] == "2025-12-31"


# --- Malformed bank ids ----------------------------------------------------------


@pytest.mark.parametrize("path,body", [
    ("/accounting/transfers", {"from_bank_id": "not-a-uuid", "to_bank_id": "not-a-uuid", "amount": 1,
                               "date": "2026-10-01"}),
    ("/accounting/reconciliation/start", {"bank_account_id": "not-a-uuid", "statement_date": "2026-10-01",
                                          "statement_balance": 1}),
    ("/accounting/rules", {"bank_account_id": "not-a-uuid", "match_pattern": "x",
                           "target_account_code": "6000"}),
])
async def test_a_malformed_bank_id_is_refused_not_a_server_error(client, auth, path, body):
    r = await client.post(path, headers=auth["headers"], json=body)
    assert r.status_code == 422, r.text


async def test_a_malformed_bank_id_filter_on_rules_is_refused(client, auth):
    r = await client.get("/accounting/rules", headers=auth["headers"], params={"bank_account_id": "nope"})
    assert r.status_code == 422, r.text


async def test_a_rule_for_a_bank_account_of_another_company_is_refused(client, auth):
    """A well-formed id that is not one of this company's bank accounts is not found."""
    r = await client.post("/accounting/rules", headers=auth["headers"], json={
        "bank_account_id": str(uuid.uuid4()), "match_pattern": "x", "target_account_code": "6000"})
    assert r.status_code == 404, r.text
    # The refusal keeps its message key, so the UI can say what to do next.
    assert r.json()["detail"]["message_key"] == "accounting.bank_not_found"


async def test_well_formed_bank_ids_still_work(client, auth):
    """Neighbour: transfers, reconciliation and rules accept the company's own bank accounts."""
    h = auth["headers"]
    banks = []
    for name in ("One", "Two"):
        r = await client.post("/accounting/bank-accounts", headers=h, json={
            "bank_name": name, "account_number": name, "bank_type": "checking", "currency": "USD"})
        assert r.status_code == 200, r.text
        banks.append(r.json()["id"])
    r = await client.post("/accounting/transfers", headers=h, json={
        "from_bank_id": banks[0], "to_bank_id": banks[1], "amount": 5, "date": "2026-10-01"})
    assert r.status_code == 200, r.text
    r = await client.post("/accounting/reconciliation/start", headers=h, json={
        "bank_account_id": banks[0], "statement_date": "2026-10-01", "statement_balance": 1})
    assert r.status_code == 200, r.text
    r = await client.post("/accounting/rules", headers=h, json={
        "bank_account_id": banks[0], "match_pattern": "x", "target_account_code": "6000"})
    assert r.status_code == 200, r.text
    r = await client.get("/accounting/rules", headers=h, params={"bank_account_id": banks[0]})
    assert r.status_code == 200 and r.json()["total"] == 1, r.text


# --- Bank opening balance date ---------------------------------------------------


@pytest.mark.parametrize("tz, instant, day", [
    # 20:00 UTC on Oct 1 is already Oct 2 in Kiritimati (UTC+14).
    ("Pacific/Kiritimati", datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc), "2026-10-02"),
    # 05:00 UTC on Oct 2 is still Oct 1 at UTC-12.
    ("Etc/GMT+12", datetime(2026, 10, 2, 5, 0, tzinfo=timezone.utc), "2026-10-01"),
    # No timezone: the UTC day.
    (None, datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc), "2026-10-01"),
])
async def test_a_bank_opening_balance_is_dated_the_business_day(client, session, auth, monkeypatch, tz, instant, day):
    import celerp_accounting.routes as acc_routes
    import celerp.services.business_time as business_time

    company = await locked_company(session, auth["company_id"])
    company.settings = {k: v for k, v in company.settings.items() if k != "timezone"} | ({"timezone": tz} if tz else {})
    await session.commit()
    _clock(monkeypatch, instant, instant.date())
    monkeypatch.setattr(acc_routes, "datetime", business_time.datetime)
    r = await client.post("/accounting/bank-accounts", headers=auth["headers"], json={
        "bank_name": "Opening", "account_number": "1", "bank_type": "checking", "currency": "USD",
        "opening_balance": 250})
    assert r.status_code == 200, r.text
    row = await session.get(Projection, {"company_id": auth["company_id"],
                                         "entity_id": f"je:opening:{r.json()['id']}"})
    assert row is not None
    assert row.state["ts"][:10] == day
