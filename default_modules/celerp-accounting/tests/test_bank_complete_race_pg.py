# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Completing a bank while a line is posted, matched or undone, on real PostgreSQL
connections against committed data in a database of the test's own.

The writer holds its locks with its change written but not committed; completion starts
and must wait for it, then judge the statement on the books the writer left. Reading the
entries before taking the lock would judge it on the books before the writer."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio

import bank_statement_helpers as B
from race_support import _until_waiting_or_done

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def own_client(committed_engine, monkeypatch):
    """An API client whose requests commit for real in the test's own database."""
    import celerp.db
    from httpx import ASGITransport, AsyncClient

    from celerp.db import get_session
    from celerp.main import app

    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(bind=committed_engine, expire_on_commit=False)

    async def _session():
        async with maker() as s:
            yield s

    @asynccontextmanager
    async def _session_ctx():
        async with maker() as s:
            yield s

    from celerp.ai import jobs

    monkeypatch.setattr(celerp.db, "engine", committed_engine)
    monkeypatch.setattr(jobs, "SessionLocal", maker)  # the statement reading job
    app.dependency_overrides[get_session] = _session
    try:
        with patch("celerp.gateway.client._client", MagicMock()), \
             patch("celerp.gateway.state.get_session_token", return_value="test-session-token"), \
             patch("celerp.middleware.get_session_ctx", _session_ctx):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                yield c
    finally:
        app.dependency_overrides.pop(get_session, None)


def _paused(monkeypatch, operation: str, entered: asyncio.Event, go: asyncio.Event) -> None:
    """The writer stops, its locks held and its change written, just before it commits."""
    from celerp_accounting import bank_statements

    real = bank_statements._recorded

    async def recorded(db, company_id, actor_id, key, request, op, *args, **kwargs):
        result = await real(db, company_id, actor_id, key, request, op, *args, **kwargs)
        if op == operation:
            entered.set()
            await go.wait()
        return result

    monkeypatch.setattr(bank_statements, "_recorded", recorded)


async def _expense(client, co, bk, amount, day):
    r = await client.post("/accounting/journal-entries", headers=co["h"], json={
        "ts": day, "memo": "ACME LTD", "idempotency_token": B.key(),
        "entries": [{"account": "5100", "debit": amount, "credit": 0.0},
                    {"account": bk["chart_account_code"], "debit": 0.0, "credit": amount}]})
    assert r.status_code == 200, r.text
    return r.json()["je_id"]


async def _setup(client, writer):
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    await B.statement(client, co, bk["id"], [("2026-01-05", "ACME LTD", "-40.00")], opening="0.00",
                      closing="-40.00", start="2026-01-01", end="2026-01-31", tick_all=True)
    line = B.all_lines(await B.groups(client, co, bk["id"]))[0]
    keyed = await _expense(client, co, bk, 40.0, "2026-01-05") if writer == "match" else None
    if writer == "unmatch":
        r = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [line["id"]], "kind": "post",
                                                              "account_code": "5100"}])
        assert r.status_code == 200 and r.json()["results"][0]["ok"], r.text
    return co, bk, line, keyed


def _write(client, co, bk, line, keyed, writer):
    if writer == "unmatch":
        return client.post(f"{B.API}/bank/{bk['id']}/unmatch", headers=co["h"],
                           json={"line_ids": [line["id"]], "idempotency_key": B.key()})
    decision = {"line_ids": [line["id"]], "kind": "post", "account_code": "5100"} if writer == "post" else \
        {"line_ids": [line["id"]], "kind": "match", "je_ids": [keyed]}
    return B.resolve(client, co, bk["id"], decisions=[decision])


# The writer, the operation it records just before its commit, and whether the statement
# balances once it has committed.
WRITERS = {"post": ("resolve_unit", True), "match": ("resolve_unit", True), "unmatch": ("unmatch", False)}


@pytest.mark.parametrize("writer", list(WRITERS))
async def test_complete_waits_for_a_line_writer_and_judges_its_result(committed_engine, own_client, monkeypatch,
                                                                       writer):
    operation, balances = WRITERS[writer]
    co, bk, line, keyed = await _setup(own_client, writer)
    entered, go = asyncio.Event(), asyncio.Event()
    _paused(monkeypatch, operation, entered, go)
    write = asyncio.create_task(_write(own_client, co, bk, line, keyed, writer))
    await asyncio.wait_for(entered.wait(), timeout=10)
    complete = asyncio.create_task(own_client.post(f"{B.API}/bank/{bk['id']}/complete", headers=co["h"],
                                                   json={"idempotency_key": B.key()}))
    await _until_waiting_or_done(committed_engine, complete)
    assert not complete.done(), "completion did not wait for the writer"
    go.set()
    w, c = await asyncio.wait_for(asyncio.gather(write, complete), timeout=30)
    assert w.status_code == 200, w.text
    assert c.status_code == 200, c.text
    done = c.json()
    assert bool(done["completed"]) is balances, (writer, done)
    assert (done["stopped_at"] is None) is balances, (writer, done)
