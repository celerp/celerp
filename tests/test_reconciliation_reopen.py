# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A completed bank reconciliation can be reopened by whoever can complete one. Reopening
puts it back in progress and changes nothing in the books: every match stays, and no
journal entry is written. The reopening is recorded as an event. A reconciliation that
is completed tells the person trying to change it that it can be reopened, and where."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from celerp.main import app
from celerp.models.ledger import LedgerEntry
from company_backup_support import token
from migration_support import auth, code_config, maker, real_client, real_engine  # noqa: F401
from test_company_reset_payments import _harbor, _waiting_on_a_lock
from test_helpers import invite_user, register_admin
from ui.app import app as ui_app

pytestmark = pytest.mark.asyncio

_OPENING = 1000.0


async def _completed(client, headers) -> str:
    """A completed reconciliation whose statement matches the opening balance."""
    r = await client.post("/accounting/bank-accounts", json={
        "bank_name": "Harbor Bank", "account_number": "0001", "bank_type": "checking",
        "currency": "USD", "opening_balance": _OPENING}, headers=headers)
    assert r.status_code == 200, r.text
    r = await client.post("/accounting/reconciliation/start", json={
        "bank_account_id": r.json()["id"], "statement_date": "2026-09-30",
        "statement_balance": _OPENING}, headers=headers)
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    r = await client.post(f"/accounting/reconciliation/{sid}/complete", headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "completed", r.text
    return sid


async def _journal_count(session) -> int:
    return (await session.execute(text(
        "SELECT count(*) FROM ledger WHERE event_type LIKE 'acc.journal_entry.%'"))).scalar()


async def _reopen_events(session, sid) -> list[LedgerEntry]:
    return list((await session.execute(select(LedgerEntry).where(
        LedgerEntry.event_type == "acc.reconciliation.reopened",
        LedgerEntry.entity_id == f"recon:{sid}"))).scalars().all())


async def test_reopen_puts_it_back_in_progress_and_leaves_the_books_alone(client, session):
    h = {"Authorization": f"Bearer {await register_admin(client)}"}
    sid = await _completed(client, h)
    before = (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()
    journals = await _journal_count(session)

    r = await client.post(f"/accounting/reconciliation/{sid}/reopen", headers=h)
    assert r.status_code == 200, r.text
    after = (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()
    assert after["status"] == "open" and after["completed_at"] is None
    assert after["reconciled_je_ids"] == before["reconciled_je_ids"]
    assert await _journal_count(session) == journals
    events = await _reopen_events(session, sid)
    assert len(events) == 1
    assert events[0].data["statement_date"] == "2026-09-30"
    assert events[0].actor_id is not None

    # Reopening again changes nothing and records nothing more.
    r = await client.post(f"/accounting/reconciliation/{sid}/reopen", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "open", r.text
    assert len(await _reopen_events(session, sid)) == 1


async def test_a_reopened_reconciliation_can_be_changed_and_completed_again(client, session):
    h = {"Authorization": f"Bearer {await register_admin(client)}"}
    sid = await _completed(client, h)
    assert (await client.post(f"/accounting/reconciliation/{sid}/reopen", headers=h)).status_code == 200
    r = await client.post(f"/accounting/reconciliation/{sid}/match", json={"je_ids": []}, headers=h)
    assert r.status_code == 200, r.text
    r = await client.post(f"/accounting/reconciliation/{sid}/complete", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "completed", r.text
    assert r.json()["completed_at"]
    # A second cycle is recorded as a second reopening.
    assert (await client.post(f"/accounting/reconciliation/{sid}/reopen", headers=h)).status_code == 200
    assert len(await _reopen_events(session, sid)) == 2


async def test_reopen_needs_the_permission_completing_needs(client, session):
    h = {"Authorization": f"Bearer {await register_admin(client)}"}
    sid = await _completed(client, h)
    viewer = await invite_user(client, session, h, "viewer@example.test", "viewer")
    r = await client.post(f"/accounting/reconciliation/{sid}/reopen",
                          headers={"Authorization": f"Bearer {viewer}"})
    assert r.status_code == 403, r.text
    assert (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()["status"] == "completed"
    assert await _reopen_events(session, sid) == []


async def test_reopen_refused_while_another_reconciliation_of_that_statement_is_open(client, session):
    """One open reconciliation per statement: reopening would make a second."""
    h = {"Authorization": f"Bearer {await register_admin(client)}"}
    sid = await _completed(client, h)
    bank_id = (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()["bank_account_id"]
    r = await client.post("/accounting/reconciliation/start", json={
        "bank_account_id": bank_id, "statement_date": "2026-09-30", "statement_balance": _OPENING}, headers=h)
    other = r.json()["id"]
    assert other != sid
    r = await client.post(f"/accounting/reconciliation/{sid}/reopen", headers=h)
    assert r.status_code == 409, r.text
    assert "already open" in r.json()["detail"]
    assert (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()["status"] == "completed"


@pytest.mark.parametrize("path, body", [
    ("match", {"je_ids": []}),
    ("auto-match", None),
    ("bulk-confirm", None),
    ("write-off", {}),
])
async def test_changing_a_completed_reconciliation_says_it_can_be_reopened_and_where(client, session, path, body):
    h = {"Authorization": f"Bearer {await register_admin(client)}"}
    sid = await _completed(client, h)
    r = await client.post(f"/accounting/reconciliation/{sid}/{path}", json=body, headers=h)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "completed" in detail and "Reopen" in detail
    assert "top of this reconciliation" in detail


@asynccontextmanager
async def _ui_as(tok: str):
    def _bridged(t, timeout=10.0):
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Authorization": f"Bearer {t}"}, follow_redirects=True)

    with patch("ui.api_client._client", _bridged):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                               follow_redirects=False, cookies={"celerp_token": tok}) as ui:
            yield ui


async def test_completed_reconciliation_page_offers_reopen_and_reopening_returns_to_work(client, session):
    tok = await register_admin(client)
    h = {"Authorization": f"Bearer {tok}"}
    sid = await _completed(client, h)
    async with _ui_as(tok) as ui:
        # The page a completion lands on leads back to the reconciliation.
        r = await ui.post(f"/accounting/reconcile/{sid}/complete")
        assert f'href="/accounting/reconcile/{sid}"' in r.text
        page = (await ui.get(f"/accounting/reconcile/{sid}")).text
        assert f'hx-post="/accounting/reconcile/{sid}/reopen"' in page
        assert "reopening changes nothing in your books" in page
        r = await ui.post(f"/accounting/reconcile/{sid}/reopen")
        assert r.status_code == 200, r.text
        assert f'hx-post="/accounting/reconcile/{sid}/reopen"' not in r.text
    assert (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()["status"] == "open"


async def test_a_reopen_that_arrives_while_completing_waits_and_reopens_the_completed_one(
        monkeypatch, real_engine, real_client):
    """Completing holds the reconciliation; a reopen arriving meanwhile waits for it, then
    finds it completed and reopens it. Neither is lost, and the event matches the end state."""
    from celerp_accounting import routes
    boss, a, _ = await _harbor(real_engine)
    h = auth(await token(real_engine, boss, a))
    r = await real_client.post("/accounting/bank-accounts", json={
        "bank_name": "Harbor Bank", "account_number": "0001", "bank_type": "checking",
        "currency": "USD", "opening_balance": _OPENING}, headers=h)
    r = await real_client.post("/accounting/reconciliation/start", json={
        "bank_account_id": r.json()["id"], "statement_date": "2026-09-30",
        "statement_balance": _OPENING}, headers=h)
    sid = r.json()["id"]

    reached, release = asyncio.Event(), asyncio.Event()
    real_entries = routes._recon_bank_and_entries

    async def held_entries(*args, **kw):
        out = await real_entries(*args, **kw)
        if not release.is_set():
            reached.set()
            await release.wait()
        return out
    monkeypatch.setattr(routes, "_recon_bank_and_entries", held_entries)

    completing = asyncio.create_task(real_client.post(f"/accounting/reconciliation/{sid}/complete", headers=h))
    await asyncio.wait_for(reached.wait(), 10)
    reopening = asyncio.create_task(real_client.post(f"/accounting/reconciliation/{sid}/reopen", headers=h))
    try:
        for _ in range(500):
            if await _waiting_on_a_lock(real_engine):
                break
            if reopening.done():
                raise AssertionError("the reopen did not wait for the completion")
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("the reopen did not wait for the completion")
    finally:
        release.set()
    done, reopened = await completing, await reopening
    assert done.status_code == 200 and done.json()["status"] == "completed", done.text
    assert reopened.status_code == 200 and reopened.json()["status"] == "open", reopened.text
    async with maker(real_engine)() as s:
        status = (await s.execute(text("SELECT status FROM reconciliation_sessions WHERE id = :i"),
                                  {"i": sid})).scalar()
        events = (await s.execute(text(
            "SELECT count(*) FROM ledger WHERE event_type = 'acc.reconciliation.reopened' AND entity_id = :e"),
            {"e": f"recon:{sid}"})).scalar()
    assert status == "open" and events == 1


async def _bank(client, headers) -> str:
    r = await client.post("/accounting/bank-accounts", json={
        "bank_name": "Harbor Bank", "account_number": "0001", "bank_type": "checking",
        "currency": "USD", "opening_balance": _OPENING}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _start(client, headers, bank):
    return await client.post("/accounting/reconciliation/start", json={
        "bank_account_id": bank, "statement_date": "2026-09-30", "statement_balance": _OPENING},
        headers=headers)


async def _complete_one(client, headers, bank) -> str:
    sid = (await _start(client, headers, bank)).json()["id"]
    r = await client.post(f"/accounting/reconciliation/{sid}/complete", headers=headers)
    assert r.json()["status"] == "completed", r.text
    return sid


async def _open_ids(engine) -> list[str]:
    async with maker(engine)() as s:
        return [str(i) for i in (await s.execute(text(
            "SELECT id FROM reconciliation_sessions WHERE status = 'open' AND statement_date = '2026-09-30'"
        ))).scalars()]


def _hold_reopen(monkeypatch):
    """Hold a reopen after its check for another open reconciliation, before it commits."""
    from celerp_accounting import routes
    reached, release = asyncio.Event(), asyncio.Event()
    real = routes.emit_event

    async def held(*args, **kw):
        if kw.get("event_type") == "acc.reconciliation.reopened" and not release.is_set():
            reached.set()
            await release.wait()
        return await real(*args, **kw)
    monkeypatch.setattr(routes, "emit_event", held)
    return reached, release


async def _let_it_reach_a_lock(engine, task) -> None:
    for _ in range(500):
        if task.done() or await _waiting_on_a_lock(engine):
            return
        await asyncio.sleep(0.01)


async def test_two_reopens_of_the_same_statement_leave_one_open(monkeypatch, real_engine, real_client):
    boss, a, _ = await _harbor(real_engine)
    h = auth(await token(real_engine, boss, a))
    bank = await _bank(real_client, h)
    s1 = await _complete_one(real_client, h, bank)
    s2 = await _complete_one(real_client, h, bank)
    reached, release = _hold_reopen(monkeypatch)
    first = asyncio.create_task(real_client.post(f"/accounting/reconciliation/{s1}/reopen", headers=h))
    await asyncio.wait_for(reached.wait(), 10)
    second = asyncio.create_task(real_client.post(f"/accounting/reconciliation/{s2}/reopen", headers=h))
    try:
        await _let_it_reach_a_lock(real_engine, second)
    finally:
        release.set()
    r1, r2 = await first, await second
    assert r1.status_code == 200 and r1.json()["status"] == "open", r1.text
    assert r2.status_code == 409, r2.text
    assert "already open" in r2.json()["detail"]
    assert await _open_ids(real_engine) == [s1]


async def test_start_during_a_reopen_of_the_same_statement_continues_the_reopened_one(
        monkeypatch, real_engine, real_client):
    boss, a, _ = await _harbor(real_engine)
    h = auth(await token(real_engine, boss, a))
    bank = await _bank(real_client, h)
    s1 = await _complete_one(real_client, h, bank)
    reached, release = _hold_reopen(monkeypatch)
    reopening = asyncio.create_task(real_client.post(f"/accounting/reconciliation/{s1}/reopen", headers=h))
    await asyncio.wait_for(reached.wait(), 10)
    starting = asyncio.create_task(_start(real_client, h, bank))
    try:
        await _let_it_reach_a_lock(real_engine, starting)
    finally:
        release.set()
    r1, r2 = await reopening, await starting
    assert r1.status_code == 200 and r1.json()["status"] == "open", r1.text
    assert r2.status_code == 200 and r2.json()["id"] == s1, r2.text
    assert await _open_ids(real_engine) == [s1]
