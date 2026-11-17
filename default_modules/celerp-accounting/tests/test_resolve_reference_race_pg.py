# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A resolve reads its lines' near entries once, then decides each line in a transaction of its
own. A statement another request imports between that read and a decision, carrying the same
reference, makes the reference no transaction identifier: the decision judges it on the
statement lines as they stand under its lock, on real PostgreSQL connections, and stops with the
payment as a candidate instead of matching it. So does a second payment of the document whose
number the reference is: the entries' identifiers are read under the lock too."""
from __future__ import annotations

import asyncio

import pytest

import bank_statement_helpers as B
from test_bank_complete_race_pg import own_client  # noqa: F401  (a fixture)
from test_twin_evidence import _lines

pytestmark = pytest.mark.asyncio


async def test_a_reference_imported_before_a_decision_is_counted(committed_engine, own_client, monkeypatch):
    import celerp_accounting.bank_statements as BS
    co = await B.company(own_client)
    bk = await B.bank(own_client, co["h"])
    other = await B.bank(own_client, co["h"], name="Second account")
    _doc, number = await B.paid_invoice(own_client, co, bk, 80.0, "2026-01-08")
    line = (await _lines(own_client, co, bk, [("2026-01-10", "RECEIVED", "80.00", number)]))[0]
    real, between, go = BS._resolve_unit, asyncio.Event(), asyncio.Event()

    async def held(*a, **kw):  # after the request's read, holding no lock
        between.set()
        await go.wait()
        return await real(*a, **kw)
    monkeypatch.setattr(BS, "_resolve_unit", held)
    first = asyncio.create_task(B.resolve(own_client, co, bk["id"], decisions=[
        {"line_ids": [line["id"]], "kind": "post", "account_code": "4100"}]))
    await asyncio.wait_for(between.wait(), timeout=20)
    await _lines(own_client, co, other, [("2026-01-11", "RECEIVED", "-40.00", number)])
    go.set()
    r = await asyncio.wait_for(first, timeout=30)
    assert r.status_code == 200, r.text
    row = r.json()["results"][0]
    assert not row["ok"] and not row["matched_instead"] and len(row["candidates"]) == 1, row


async def test_a_second_payment_recorded_before_a_decision_is_counted(committed_engine, own_client, monkeypatch):
    import celerp_accounting.bank_statements as BS
    co = await B.company(own_client)
    bk = await B.bank(own_client, co["h"])
    doc, number = await B.paid_invoice(own_client, co, bk, 160.0, "2026-01-08", payments=2, paid=1)
    line = (await _lines(own_client, co, bk, [("2026-01-10", "RECEIVED", "80.00", number)]))[0]
    real, between, go = BS._resolve_unit, asyncio.Event(), asyncio.Event()

    async def held(*a, **kw):  # after the request's read, holding no lock
        between.set()
        await go.wait()
        return await real(*a, **kw)
    monkeypatch.setattr(BS, "_resolve_unit", held)
    first = asyncio.create_task(B.resolve(own_client, co, bk["id"], decisions=[
        {"line_ids": [line["id"]], "kind": "post", "account_code": "4100"}]))
    await asyncio.wait_for(between.wait(), timeout=20)
    r = await own_client.post(f"/docs/{doc}/payment", headers=co["h"], json={
        "amount": 80.0, "payment_date": "2026-01-09", "bank_account": bk["chart_account_code"]})
    assert r.status_code == 200, r.text
    go.set()
    r = await asyncio.wait_for(first, timeout=30)
    assert r.status_code == 200, r.text
    row = r.json()["results"][0]
    assert not row["ok"] and not row["matched_instead"] and len(row["candidates"]) == 2, row
