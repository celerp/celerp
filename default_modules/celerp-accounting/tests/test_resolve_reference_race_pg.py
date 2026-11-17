# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A resolve reads its lines' near entries once, then decides each line in a transaction of its
own. A statement another request imports between that read and a decision, carrying the same
reference, makes the reference no transaction identifier: the decision judges it on the
statement lines as they stand under its lock, on real PostgreSQL connections, and stops with the
keyed entry as a candidate instead of matching it."""
from __future__ import annotations

import asyncio

import pytest

import bank_statement_helpers as B
from test_bank_complete_race_pg import own_client  # noqa: F401  (a fixture)
from test_twin_evidence import _keyed, _lines

pytestmark = pytest.mark.asyncio


async def test_a_reference_imported_before_a_decision_is_counted(committed_engine, own_client, monkeypatch):
    import celerp_accounting.bank_statements as BS
    co = await B.company(own_client)
    bk = await B.bank(own_client, co["h"])
    other = await B.bank(own_client, co["h"], name="Second account")
    keyed = await _keyed(own_client, co, bk, 80.0, "2026-01-08", account="5300", memo="Paid PO-55120")
    line = (await _lines(own_client, co, bk, [("2026-01-10", "PO 55120", "-80.00", "PO-55120")]))[0]
    real, between, go = BS._resolve_unit, asyncio.Event(), asyncio.Event()

    async def held(*a, **kw):  # after the request's read, holding no lock
        between.set()
        await go.wait()
        return await real(*a, **kw)
    monkeypatch.setattr(BS, "_resolve_unit", held)
    first = asyncio.create_task(B.resolve(own_client, co, bk["id"], decisions=[
        {"line_ids": [line["id"]], "kind": "post", "account_code": "5300"}]))
    await asyncio.wait_for(between.wait(), timeout=20)
    await _lines(own_client, co, other, [("2026-01-11", "PO 55120 REFUND", "40.00", "PO-55120")])
    go.set()
    r = await asyncio.wait_for(first, timeout=30)
    assert r.status_code == 200, r.text
    row = r.json()["results"][0]
    assert not row["ok"] and not row["matched_instead"] and row["candidates"] == [keyed], row
