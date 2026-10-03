# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A payment recorded by hand and an archival of the account it goes to, racing on a real
PostgreSQL. The archival either lands first, and the payment is refused, or waits for the
payment, which posts while the account is still open. A payment never posts into an
archived account."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from company_backup_support import token
from migration_support import auth, code_config, maker, real_client, real_engine  # noqa: F401
from test_company_reset_payments import _harbor, _invoice, _waiting_on_a_lock
from test_payment_deposit_account_race_pg import _account, _posted_to

pytestmark = pytest.mark.asyncio

CODE = "1190"


async def _invoice_and_account(engine, client):
    boss, a, _ = await _harbor(engine)
    headers = auth(await token(engine, boss, a))
    r = await client.post("/accounting/accounts", headers=headers, json={
        "code": CODE, "name": "Petty cash", "account_type": "asset", "parent_code": None})
    assert r.status_code == 200, r.text
    return a, headers, await _invoice(client, engine, boss, a)


def _hold(monkeypatch, *, after_the_account_check: bool) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold the payment, until released, just before it locks the invoice, or just after
    it judged the account, still inside its transaction."""
    from celerp_accounting import ledger_accounts
    from celerp_docs import routes
    reached, release = asyncio.Event(), asyncio.Event()

    async def wait() -> None:
        if not reached.is_set():
            reached.set()
            await release.wait()

    if after_the_account_check:
        real_check = ledger_accounts.require_money_account

        async def checked(session, company_id, code):
            acc = await real_check(session, company_id, code)
            await wait()
            return acc
        monkeypatch.setattr(ledger_accounts, "require_money_account", checked)
    else:
        real_doc = routes._get_doc

        async def held(session, company_id, entity_id, *, for_update=False):
            if for_update:
                await wait()
            return await real_doc(session, company_id, entity_id, for_update=for_update)
        monkeypatch.setattr(routes, "_get_doc", held)
    return reached, release


def _pay(client, headers, eid):
    return asyncio.create_task(client.post(f"/docs/{eid}/payment", headers=headers, json={
        "amount": 1070.0, "payment_date": "2026-03-02", "bank_account": CODE}))


async def test_an_account_archived_before_the_payment_reaches_it_refuses_the_payment(
        monkeypatch, real_engine, real_client):
    a, headers, eid = await _invoice_and_account(real_engine, real_client)
    reached, release = _hold(monkeypatch, after_the_account_check=False)
    paying = _pay(real_client, headers, eid)
    await asyncio.wait_for(reached.wait(), 10)
    try:
        r = await real_client.patch(f"/accounting/accounts/{CODE}", json={"is_active": False}, headers=headers)
        assert r.status_code == 200, r.text
    finally:
        release.set()
    r = await paying

    assert r.status_code == 422, r.text
    assert f"Account {CODE} is inactive." in r.json()["detail"]
    assert await _posted_to(real_engine, eid) == []


async def test_an_archival_that_reaches_the_account_while_the_payment_posts_waits_for_it(
        monkeypatch, real_engine, real_client):
    a, headers, eid = await _invoice_and_account(real_engine, real_client)
    reached, release = _hold(monkeypatch, after_the_account_check=True)
    paying = _pay(real_client, headers, eid)
    await asyncio.wait_for(reached.wait(), 10)

    async def archive() -> None:
        async with real_engine.begin() as archiving:
            await archiving.execute(text("SET LOCAL lock_timeout = '20s'"))
            await archiving.execute(text(
                "UPDATE accounts SET is_active = false WHERE company_id = :c AND code = :k"), {"c": a, "k": CODE})
    archival = asyncio.create_task(archive())
    try:
        for _ in range(500):
            if await _waiting_on_a_lock(real_engine) or archival.done():
                break
            await asyncio.sleep(0.01)
        assert not archival.done(), "the archival did not wait for the payment"
    finally:
        release.set()
    r = await paying
    await archival

    assert r.status_code == 200, r.text
    assert await _posted_to(real_engine, eid) == [CODE]
    assert await _account(real_engine, a, CODE) == (False, "asset")
