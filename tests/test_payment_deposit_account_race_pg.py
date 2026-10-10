# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An online payment is deposited only into a chart account that can hold money when the
payment is recorded: active, an asset, and, for a bank, still behind an active bank
account. An archival of that account racing the payment either lands before it, and the
payment is kept among the unmatched, or waits for it, and the payment posts while the
account is still open. A payment is never posted into an archived account.

Each test holds the payment on a real PostgreSQL connection, lets another connection
archive the deposit account, then lets the payment finish."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from company_backup_support import token
from migration_support import auth, code_config, maker, real_client, real_engine  # noqa: F401
from test_company_reset_payments import (_OCTOBER_3, _Cloud, _harbor, _payment_journal, _payments_on,
                                         _references, _shared_invoice, _unmatched, _waiting_on_a_lock)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("docs_running")]


async def _paying_into_a_bank(monkeypatch, engine, client):
    """A company whose online payments are deposited to its bank account, and an invoice
    whose payment page is open: (owner, company, invoice, bank chart code, Cloud)."""
    _payments_on(monkeypatch)
    boss, a, _ = await _harbor(engine)
    headers = auth(await token(engine, boss, a))
    r = await client.post("/accounting/bank-accounts", json={
        "bank_name": "Harbor Bank", "account_number": "0001", "bank_type": "checking", "currency": "USD"},
        headers=headers)
    assert r.status_code == 200, r.text
    code = r.json()["chart_account_code"]
    r = await client.patch("/companies/me/books", json={"stripe_deposit_account": code}, headers=headers)
    assert r.status_code == 200, r.text
    cloud = _Cloud(monkeypatch, engine)
    eid, share = await _shared_invoice(client, engine, boss, a)
    assert (await client.get(f"/pay/{share}", follow_redirects=False)).status_code == 303
    assert cloud.opened[eid]["deposit_account"] == code
    return boss, a, eid, code, cloud


def _hold(monkeypatch, *, after_the_deposit_check: bool) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold the payment, until released, just before it takes the invoice's row lock, or
    just after it judged the deposit account, still inside its transaction."""
    from celerp_docs import routes, routes_payments
    reached, release = asyncio.Event(), asyncio.Event()

    async def wait() -> None:
        if not reached.is_set():
            reached.set()
            await release.wait()

    if after_the_deposit_check:
        real_check = routes_payments.require_online_deposit_account

        async def checked(session, company_id, code):
            await real_check(session, company_id, code)
            await wait()
        monkeypatch.setattr(routes_payments, "require_online_deposit_account", checked)
    else:
        real_doc = routes._get_doc

        async def held(session, company_id, entity_id, *, for_update=False):
            if for_update:
                await wait()
            return await real_doc(session, company_id, entity_id, for_update=for_update)
        monkeypatch.setattr(routes, "_get_doc", held)
    return reached, release


async def _account(engine, a, code) -> tuple[bool, str]:
    async with maker(engine)() as s:
        return tuple((await s.execute(text(
            "SELECT is_active, account_type FROM accounts WHERE company_id = :c AND code = :k"),
            {"c": a, "k": code})).one())


async def _posted_to(engine, eid) -> list[str]:
    """Every account the invoice's payment journal entries debit."""
    async with maker(engine)() as s:
        states = (await s.scalars(text("SELECT state FROM projections WHERE entity_id LIKE :j"),
                                  {"j": f"je:auto:{eid}:pay:%"})).all()
    return sorted({e["account"] for st in states for e in st.get("entries", []) if float(e.get("debit") or 0)})


async def _kept_unmatched(engine, cloud, a, eid) -> None:
    assert all(d["acked"] for d in cloud.deliveries)
    assert await _references(engine, eid) == []
    assert await _payment_journal(engine, eid) == []
    assert await _unmatched(engine) == [("pi_paid", 50000, "USD", str(a), eid)]


async def test_archiving_the_deposit_account_while_its_bank_is_active_is_refused_and_the_payment_posts_there(
        monkeypatch, real_engine, real_client):
    """The owner archives the chart account behind the bank account online payments go
    to while a payment is on its way. The bank account is still active, so the chart
    refuses, and the payment posts into the open account."""
    boss, a, eid, code, cloud = await _paying_into_a_bank(monkeypatch, real_engine, real_client)
    reached, release = _hold(monkeypatch, after_the_deposit_check=False)
    cloud.pay(a, eid, "pi_paid", amount_minor=50000, paid_at=_OCTOBER_3)
    paying = asyncio.create_task(cloud.deliver())
    await asyncio.wait_for(reached.wait(), 10)
    try:
        r = await real_client.patch(f"/accounting/accounts/{code}", json={"is_active": False},
                                    headers=auth(await token(real_engine, boss, a)))
    finally:
        release.set()
    await paying

    assert r.status_code == 422, r.text
    assert "Archive the bank account" in r.json()["detail"]
    assert await _account(real_engine, a, code) == (True, "asset")
    assert await _references(real_engine, eid) == ["pi_paid"]
    assert await _posted_to(real_engine, eid) == [code]


async def test_a_bank_and_its_account_archived_while_the_payment_is_on_its_way_keep_it_among_the_unmatched(
        monkeypatch, real_engine, real_client):
    """Archived the way the chart allows: the bank account first, then its chart account."""
    boss, a, eid, code, cloud = await _paying_into_a_bank(monkeypatch, real_engine, real_client)
    headers = auth(await token(real_engine, boss, a))
    bank_id = (await real_client.get("/accounting/bank-accounts", headers=headers)).json()["items"]
    bank_id = next(b["id"] for b in bank_id if b["chart_account_code"] == code)
    reached, release = _hold(monkeypatch, after_the_deposit_check=False)
    cloud.pay(a, eid, "pi_paid", amount_minor=50000, paid_at=_OCTOBER_3)
    paying = asyncio.create_task(cloud.deliver())
    await asyncio.wait_for(reached.wait(), 10)
    try:
        r = await real_client.patch(f"/accounting/bank-accounts/{bank_id}", json={"is_active": False},
                                    headers=headers)
        assert r.status_code == 200, r.text
        r = await real_client.patch(f"/accounting/accounts/{code}", json={"is_active": False}, headers=headers)
        assert r.status_code == 200, r.text
    finally:
        release.set()
    await paying

    await _kept_unmatched(real_engine, cloud, a, eid)


async def test_a_chart_account_archived_under_an_active_bank_while_the_payment_is_on_its_way_keeps_it_unmatched(
        monkeypatch, real_engine, real_client):
    """Any writer of the chart that leaves the bank account active, committed before the
    payment reaches the invoice."""
    boss, a, eid, code, cloud = await _paying_into_a_bank(monkeypatch, real_engine, real_client)
    reached, release = _hold(monkeypatch, after_the_deposit_check=False)
    cloud.pay(a, eid, "pi_paid", amount_minor=50000, paid_at=_OCTOBER_3)
    paying = asyncio.create_task(cloud.deliver())
    await asyncio.wait_for(reached.wait(), 10)
    try:
        async with real_engine.begin() as archiving:
            await archiving.execute(text(
                "UPDATE accounts SET is_active = false WHERE company_id = :c AND code = :k"), {"c": a, "k": code})
    finally:
        release.set()
    await paying

    await _kept_unmatched(real_engine, cloud, a, eid)


async def test_an_archival_still_committing_when_the_payment_reaches_the_account_keeps_it_unmatched(
        monkeypatch, real_engine, real_client):
    """The archival holds the chart account when the payment reaches it: the payment waits
    for it, then finds the account archived."""
    boss, a, eid, code, cloud = await _paying_into_a_bank(monkeypatch, real_engine, real_client)
    reached, release = _hold(monkeypatch, after_the_deposit_check=False)
    cloud.pay(a, eid, "pi_paid", amount_minor=50000, paid_at=_OCTOBER_3)
    paying = asyncio.create_task(cloud.deliver())
    await asyncio.wait_for(reached.wait(), 10)
    async with real_engine.connect() as archiving:
        try:
            # As the chart's own edit does: the account's row lock, then the change.
            await archiving.execute(text(
                "SELECT id FROM accounts WHERE company_id = :c AND code = :k FOR UPDATE"), {"c": a, "k": code})
            await archiving.execute(text(
                "UPDATE accounts SET is_active = false WHERE company_id = :c AND code = :k"), {"c": a, "k": code})
            release.set()
            for _ in range(500):
                if await _waiting_on_a_lock(real_engine):
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("the payment did not wait for the archival")
            await archiving.commit()
        finally:
            release.set()
    await paying

    await _kept_unmatched(real_engine, cloud, a, eid)


async def test_an_archival_that_reaches_the_account_while_the_payment_posts_waits_for_it(
        monkeypatch, real_engine, real_client):
    """The payment has judged the deposit account and is still posting when an archival
    arrives: the archival waits until the payment is recorded, so the payment posts into
    an account that was open, and is the last thing that account took."""
    boss, a, eid, code, cloud = await _paying_into_a_bank(monkeypatch, real_engine, real_client)
    reached, release = _hold(monkeypatch, after_the_deposit_check=True)
    cloud.pay(a, eid, "pi_paid", amount_minor=50000, paid_at=_OCTOBER_3)
    paying = asyncio.create_task(cloud.deliver())
    await asyncio.wait_for(reached.wait(), 10)

    async def archive() -> None:
        async with real_engine.begin() as archiving:
            await archiving.execute(text("SET LOCAL lock_timeout = '20s'"))
            await archiving.execute(text(
                "UPDATE accounts SET is_active = false WHERE company_id = :c AND code = :k"), {"c": a, "k": code})
    archival = asyncio.create_task(archive())
    try:
        for _ in range(500):
            if await _waiting_on_a_lock(real_engine):
                break
            if archival.done():
                raise AssertionError("the archival did not wait for the payment")
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("the archival did not wait for the payment")
        assert not archival.done()
    finally:
        release.set()
    await paying
    await archival

    assert all(d["acked"] for d in cloud.deliveries)
    assert await _references(real_engine, eid) == ["pi_paid"]
    assert await _posted_to(real_engine, eid) == [code]
    assert await _account(real_engine, a, code) == (False, "asset")


@pytest.mark.parametrize("change", [{"is_active": False}, {"account_type": "liability"}])
async def test_cash_archived_or_retyped_while_a_payment_is_on_its_way_keeps_it_among_the_unmatched(
        monkeypatch, real_engine, real_client, change):
    """Online payments go to the default deposit account, a cash account with no bank
    account behind it, when the payment page opens. The owner points the default elsewhere
    and archives or retypes that cash account before the payment is recorded: the payment
    never posts to it and is kept among the unmatched."""
    _payments_on(monkeypatch)
    boss, a, _ = await _harbor(real_engine)
    headers = auth(await token(real_engine, boss, a))
    r = await real_client.post("/accounting/accounts", headers=headers, json={
        "code": "1119", "name": "Cash", "account_type": "asset", "parent_code": "1110"})
    assert r.status_code == 200, r.text
    r = await real_client.put("/accounting/posting-accounts/default_deposit", json={"code": "1119"}, headers=headers)
    assert r.status_code == 200, r.text
    cloud = _Cloud(monkeypatch, real_engine)
    eid, share = await _shared_invoice(real_client, real_engine, boss, a)
    assert (await real_client.get(f"/pay/{share}", follow_redirects=False)).status_code == 303
    assert cloud.opened[eid]["deposit_account"] == "1119"
    reached, release = _hold(monkeypatch, after_the_deposit_check=False)
    cloud.pay(a, eid, "pi_paid", amount_minor=50000, paid_at=_OCTOBER_3)
    paying = asyncio.create_task(cloud.deliver())
    await asyncio.wait_for(reached.wait(), 10)
    try:
        moved = await real_client.put("/accounting/posting-accounts/default_deposit", json={"code": "1111"},
                                      headers=headers)
        r = await real_client.patch("/accounting/accounts/1119", json=change, headers=headers)
    finally:
        release.set()
    await paying

    assert moved.status_code == 200, moved.text
    assert r.status_code == 200, r.text
    await _kept_unmatched(real_engine, cloud, a, eid)
    assert "1119" not in await _posted_to(real_engine, eid)
