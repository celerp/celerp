# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An online payment is recorded on the books its payment page opened with only while
they still describe the ledger, judged at the moment it is recorded. A change of the
company's currency or of the invoice's rate that commits while the payment is on its
way, or that is still committing when the payment reaches the invoice, keeps the
payment among the unmatched instead of posting it on books the company no longer keeps.

Each test holds the payment on a real PostgreSQL connection just before it takes the
invoice, lets another connection change the books, then lets the payment finish."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from company_backup_support import token
from migration_support import auth, code_config, maker, real_client, real_engine  # noqa: F401
from test_company_reset_payments import (_OCTOBER_3, _Cloud, _company_settings, _harbor, _payment_journal,
                                         _payments_on, _rate_changed, _references, _shared_invoice, _unmatched,
                                         _waiting_on_a_lock)

pytestmark = pytest.mark.asyncio


def _hold_the_payment(monkeypatch) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold the payment just before it takes the invoice's row lock, until released."""
    from celerp_docs import routes
    reached, release = asyncio.Event(), asyncio.Event()
    real = routes._get_doc

    async def held(session, company_id, entity_id, *, for_update=False):
        if for_update and not reached.is_set():
            reached.set()
            await release.wait()
        return await real(session, company_id, entity_id, for_update=for_update)
    monkeypatch.setattr(routes, "_get_doc", held)
    return reached, release


async def _books_balance(engine, company_id) -> tuple[float, float]:
    """Total debits and credits across the company's journal entries."""
    async with maker(engine)() as s:
        journal = (await s.scalars(text(
            "SELECT state FROM projections WHERE company_id = :c AND entity_type = 'journal_entry'"),
            {"c": company_id})).all()
    lines = [e for je in journal if je.get("status") != "void" for e in je.get("entries", [])]
    return (round(sum(float(e.get("debit") or 0) for e in lines), 2),
            round(sum(float(e.get("credit") or 0) for e in lines), 2))


async def _opened(monkeypatch, engine, client, **company):
    _payments_on(monkeypatch)
    boss, a, b = await _harbor(engine)
    if company:
        await _company_settings(engine, a, **company)
    cloud = _Cloud(monkeypatch, engine)
    invoice = {"conversion_rate": 35.125} if company.get("currency") == "THB" else {}
    eid, share = await _shared_invoice(client, engine, boss, a, **invoice)
    assert (await client.get(f"/pay/{share}", follow_redirects=False)).status_code == 303
    return boss, a, eid, cloud


async def _kept_unmatched(engine, cloud, a, eid, before: tuple[float, float]) -> None:
    assert all(d["acked"] for d in cloud.deliveries)
    assert await _references(engine, eid) == []
    assert await _payment_journal(engine, eid) == []
    assert await _unmatched(engine) == [("pi_paid", 50000, "USD", str(a), eid)]
    debits, credits = await _books_balance(engine, a)
    assert debits == credits and (debits, credits) == before


@pytest.mark.parametrize("change", ["currency", "rate"])
async def test_a_change_committed_while_the_payment_is_on_its_way_keeps_it_among_the_unmatched(
        monkeypatch, real_engine, real_client, change):
    """The company moves its books from dollars to baht, or the owner gives the invoice
    another rate, and that commits after the payment arrived but before it reaches the
    invoice."""
    boss, a, eid, cloud = await _opened(monkeypatch, real_engine, real_client,
                                        **({"currency": "THB"} if change == "rate" else {}))
    reached, release = _hold_the_payment(monkeypatch)
    cloud.pay(a, eid, "pi_paid", amount_minor=50000, paid_at=_OCTOBER_3)
    paying = asyncio.create_task(cloud.deliver())
    await asyncio.wait_for(reached.wait(), 10)
    try:
        if change == "currency":
            r = await real_client.patch("/companies/me", json={"settings": {"currency": "THB"}},
                                        headers=auth(await token(real_engine, boss, a)))
            assert r.status_code == 200, r.text
        else:
            await _rate_changed(real_client, real_engine, boss, a, eid, 36.5)
        before = await _books_balance(real_engine, a)
    finally:
        release.set()
    await paying

    await _kept_unmatched(real_engine, cloud, a, eid, before)


async def test_a_currency_change_still_committing_when_the_payment_reaches_the_invoice_keeps_it_among_the_unmatched(
        monkeypatch, real_engine, real_client):
    """The company's settings save holds the company when the payment reaches the
    invoice: the payment waits for it, then judges its books against what it saved."""
    boss, a, eid, cloud = await _opened(monkeypatch, real_engine, real_client)
    reached, release = _hold_the_payment(monkeypatch)
    cloud.pay(a, eid, "pi_paid", amount_minor=50000, paid_at=_OCTOBER_3)
    paying = asyncio.create_task(cloud.deliver())
    await asyncio.wait_for(reached.wait(), 10)
    before = await _books_balance(real_engine, a)
    async with real_engine.connect() as saving:
        try:
            # As a settings save does: the company lock, then the new settings.
            await saving.execute(text("SELECT id FROM companies WHERE id = :c FOR NO KEY UPDATE"), {"c": a})
            await saving.execute(text(
                "UPDATE companies SET settings = CAST((settings::jsonb || '{\"currency\": \"THB\"}') AS json) "
                "WHERE id = :c"), {"c": a})
            release.set()
            for _ in range(500):
                if await _waiting_on_a_lock(real_engine):
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("the payment did not wait for the settings save")
            await saving.commit()
        finally:
            release.set()
    await paying

    await _kept_unmatched(real_engine, cloud, a, eid, before)
