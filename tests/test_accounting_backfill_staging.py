# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The boot-time chart backfill reaches every company that needs a chart, active or
deactivated, and skips only a company staged for a migration, which it recognises by
the migration service's own staging predicate, never by the company being inactive."""

from __future__ import annotations

import uuid

from sqlalchemy import select, text

from migration_support import (
    OWNER_EMAIL,
    auth,
    creator_run,
    maker,
    migration_env,  # noqa: F401 - fixture
    real_client,  # noqa: F401 - fixture
    real_engine,  # noqa: F401 - fixture
    staged_run,
)


async def _owner(s):
    from celerp.models.company import User

    user = User(email=OWNER_EMAIL, name="Owner", is_install_owner=True)
    s.add(user)
    await s.flush()
    return user


async def _plain_company(s, name: str, *, active: bool):
    """An ordinary company with no chart, as one created before accounting was enabled."""
    from celerp.models.company import Company

    company = Company(name=name, slug=f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}",
                      settings={}, is_active=active)
    s.add(company)
    await s.flush()
    return company


async def _backfill(engine) -> set:
    """Run the hook as startup does and return the companies that now hold a chart."""
    from celerp_accounting.models import Account
    from celerp_accounting.routes import backfill_chart_of_accounts_hook

    async with maker(engine)() as s:
        await backfill_chart_of_accounts_hook(session=s)
        await s.commit()
        return set((await s.execute(select(Account.company_id).distinct())).scalars())


async def _staged(engine, company_id) -> bool:
    from celerp.services import migrations

    async with maker(engine)() as s:
        return await migrations.is_company_migration_staged(s, company_id)


async def test_backfill_active_company_without_chart(real_engine):
    """An ordinary active company with no chart is seeded, and is not staged."""
    async with maker(real_engine)() as s:
        company = await _plain_company(s, "Active Co", active=True)
        await s.commit()
    assert await _staged(real_engine, company.id) is False
    assert company.id in await _backfill(real_engine)


async def test_backfill_inactive_company_without_chart(real_engine):
    """An ordinary deactivated company with no chart is seeded, so it works when reactivated."""
    async with maker(real_engine)() as s:
        company = await _plain_company(s, "Paused Co", active=False)
        await s.commit()
    assert await _staged(real_engine, company.id) is False
    assert company.id in await _backfill(real_engine)


async def test_backfill_skips_migration_staged_company(real_engine):
    """A company staged for a migration is never seeded: its chart comes from the imported books."""
    from celerp.services import provisioning

    async with maker(real_engine)() as s:
        staged = await provisioning.provision_migration_company(s, owner=await _owner(s), company_name="Staged Co")
        paused = await _plain_company(s, "Paused Co", active=False)
        await s.commit()
    assert await _staged(real_engine, staged.id) is True
    seeded = await _backfill(real_engine)
    assert staged.id not in seeded
    assert paused.id in seeded


async def test_inactive_company_without_staged_run_is_not_staged(real_engine):
    """Being inactive does not make a company staged; only the migration's own state does."""
    from celerp.services import provisioning

    async with maker(real_engine)() as s:
        paused = await _plain_company(s, "Paused Co", active=False)
        staged = await provisioning.provision_migration_company(s, owner=await _owner(s), company_name="Staged Co")
        await s.commit()
    assert await _staged(real_engine, paused.id) is False
    assert await _staged(real_engine, staged.id) is True


async def test_backfill_uses_the_migration_staging_predicate(real_engine, monkeypatch):
    """The hook asks the migration service's staging predicate about every chartless company
    and skips exactly the companies it names, whatever their active flag says."""
    from celerp.services import migrations

    async with maker(real_engine)() as s:
        active = await _plain_company(s, "Active Co", active=True)
        paused = await _plain_company(s, "Paused Co", active=False)
        held = await _plain_company(s, "Held Co", active=True)
        await s.commit()

    asked = []

    async def predicate(session, company_id):
        asked.append(company_id)
        return company_id == held.id

    monkeypatch.setattr(migrations, "is_company_migration_staged", predicate)
    seeded = await _backfill(real_engine)
    assert {active.id, paused.id, held.id} <= set(asked)
    assert active.id in seeded and paused.id in seeded
    assert held.id not in seeded


async def test_finalized_company_not_classified_staged(real_engine, migration_env):
    """A migration company is staged until its run is finalized, and normal afterwards."""
    from celerp.services import migrations

    run_id, company_id, _ = await staged_run(real_engine)
    assert await _staged(real_engine, company_id) is True
    await migrations.run_migration(run_id)
    assert await _staged(real_engine, company_id) is True
    async with maker(real_engine)() as s:
        await migrations.finalize(s, await creator_run(s, run_id))
    assert await _staged(real_engine, company_id) is False


async def test_backfilled_inactive_company_reactivated_accounting_works(real_client, real_engine):
    """A company deactivated before accounting reached it is backfilled at startup, and
    once reactivated its owner can post to the ledger."""
    r = await real_client.post("/auth/register", json={
        "company_name": "Paused Co", "email": "paused@example.com", "name": "Admin", "password": "validpass1"})
    assert r.status_code == 200, r.text
    from celerp.services.auth import decode_access_token

    token = r.json()["access_token"]
    headers = auth(token)
    company_id = uuid.UUID(str(decode_access_token(token)["company_id"]))

    # The company as it stood before accounting was enabled: no chart, no bank account.
    async with real_engine.begin() as conn:
        for table in ("bank_accounts", "accounts"):
            await conn.execute(text(f"DELETE FROM {table} WHERE company_id = :c"), {"c": str(company_id)})

    r = await real_client.delete("/companies/me", headers=headers)
    assert r.status_code == 200, r.text
    assert company_id in await _backfill(real_engine)

    r = await real_client.post("/companies/me/reactivate", headers=headers)
    assert r.status_code == 200, r.text
    r = await real_client.post("/accounting/journal-entries", headers=headers, json={
        "ts": "2026-01-15", "memo": "Adjustment", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": "1111", "debit": 80.0, "credit": 0},
                    {"account": "4100", "debit": 0, "credit": 80.0}],
    })
    assert r.status_code == 200, r.text

