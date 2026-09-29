# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Moving a company into a fresh install: the pre-auth scan is read only, scan and
start share the first-owner authority, start is one atomic race-safe transaction,
and the staged company is created clean and inactive."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from fixtures.manager_io.support import BASIC
from migration_support import (
    OWNER_EMAIL,
    OWNER_PASSWORD,
    code_config,  # noqa: F401 - fixture
    count,
    fake_bytes,
    fake_spec,
    load_run,
    maker,
    migration_env,  # noqa: F401 - fixture
    real_engine,  # noqa: F401 - fixture
    save_decisions,
    scan_upload,
    staged_run,
    upload_parts,
)

BOOTSTRAPPED = "System already bootstrapped. Contact your admin."
EXPIRED = "This scan has expired. Upload the file again."


def _start_body(scan_token: str) -> dict:
    return {"scan_token": scan_token, "company_name": "Moved Co", "name": "Owner",
            "email": OWNER_EMAIL, "password": OWNER_PASSWORD}


@pytest.mark.asyncio
async def test_bootstrap_scan_is_read_only(client, session, migration_env):
    tables = ("users", "companies", "user_companies", "ledger", "projections", "locations", "migration_runs")

    async def counts():
        return {t: (await session.execute(text(f"SELECT count(*) FROM {t}"))).scalar_one() for t in tables}

    before = await counts()
    assert before["users"] == 0

    r = await scan_upload(client, BASIC.read_bytes(), name="Example Trading.manager")
    assert r.status_code == 200, r.text
    scan_token, scan = r.json()["scan_token"], r.json()["scan"]
    assert scan["source_system"] == "manager_io"
    assert scan["display_name"] == "Manager.io"
    assert scan["file_name"] == "Example Trading.manager"
    assert scan["source_schema_version"] == "419"
    assert scan["company_name"] == "Example Trading"
    assert (scan["period_start"], scan["period_end"]) == ("2026-01-02", "2026-03-10")
    assert scan["currencies"] == ["USD"]
    assert scan["object_counts"] and all(isinstance(v, int) for v in scan["object_counts"].values())
    assert {c["source_type"] for c in scan["coverage"]} >= {"SalesInvoice", "SalesQuote"}
    assert scan["blockers"] == []
    assert any("SalesQuote" in w for w in scan["warnings"])
    assert scan["decisions"] is None

    r = await save_decisions(client, scan_token, prepared_by="Example Accountant")
    assert r.status_code == 200, r.text
    assert r.json()["scan"]["decisions"]["prepared_by"] == "Example Accountant"
    r = await client.post("/migrations/bootstrap/scan/read", json={"scan_token": scan_token})
    assert r.status_code == 200, r.text
    assert r.json()["scan"]["decisions"]["mode"] == "full_history"

    assert await counts() == before


@pytest.mark.asyncio
async def test_bootstrap_scan_and_start_require_bootstrap_authority(client, session, migration_env, code_config):
    from celerp.routers.auth import limiter
    from celerp.services.bootstrap import setup_code_hash

    good = {"X-Setup-Code": code_config}

    # The pre-auth scan is rate limited per IP.
    limiter.enabled = True
    limiter._storage.reset()
    statuses = [(await scan_upload(client, fake_bytes())).status_code for _ in range(6)]
    assert statuses[:5] == [403] * 5 and statuses[5] == 429
    limiter.enabled = False
    limiter._storage.reset()

    for headers in ({}, {"X-Setup-Code": "wrong"}):
        r = await scan_upload(client, fake_bytes(), headers=headers)
        assert r.status_code == 403 and r.json()["detail"] == "Invalid or missing setup code."

    # One active bootstrap scan: a new scan replaces the previous token.
    first = (await scan_upload(client, fake_bytes(), headers=good)).json()["scan_token"]
    r = await scan_upload(client, fake_bytes(), headers=good)
    assert r.status_code == 200, r.text
    scan_token = r.json()["scan_token"]
    r = await client.post("/migrations/bootstrap/scan/read", json={"scan_token": first})
    assert r.status_code == 410 and r.json()["detail"] == EXPIRED
    assert len(list((migration_env["data_dir"] / "migration_scans").iterdir())) == 1

    assert (await save_decisions(client, scan_token)).status_code == 200
    for headers in ({}, {"X-Setup-Code": "wrong"}):
        r = await client.post("/migrations/bootstrap/start", json=_start_body(scan_token), headers=headers)
        assert r.status_code == 403, r.text
    assert (await session.execute(text("SELECT count(*) FROM users"))).scalar_one() == 0

    r = await client.post("/migrations/bootstrap/start", json=_start_body(scan_token), headers=good)
    assert r.status_code == 201, r.text
    assert setup_code_hash() == ""

    # Once an owner exists, every bootstrap migration route is closed.
    r = await scan_upload(client, fake_bytes(), headers=good)
    assert r.status_code == 409 and r.json()["detail"] == BOOTSTRAPPED
    r = await save_decisions(client, scan_token)
    assert r.status_code == 409 and r.json()["detail"] == BOOTSTRAPPED
    r = await client.post("/migrations/bootstrap/start", json=_start_body(scan_token), headers=good)
    assert r.status_code == 409 and r.json()["detail"] == BOOTSTRAPPED
    assert (await session.execute(text("SELECT count(*) FROM users"))).scalar_one() == 1


@pytest.mark.asyncio
async def test_bootstrap_migration_start_is_atomic_and_race_safe(real_engine, migration_env, code_config, monkeypatch):
    from celerp.routers.migrations import BootstrapStartIn, bootstrap_start
    from celerp.services import migration_scan_store as store
    from celerp.services import migrations
    from celerp.services.bootstrap import setup_code_hash

    bootstrap = ("bootstrap", None)
    scan = await store.create_scan(upload_parts(("books.fake", fake_bytes())), owner=bootstrap)
    store.save_decisions(scan.token, owner=bootstrap,
                         decisions=migrations.validate_decisions(scan, {"mode": "full_history"}))
    payload = BootstrapStartIn(**_start_body(scan.token))

    async def empty_install():
        for table in ("users", "companies", "user_companies", "migration_runs", "locations", "ledger"):
            assert await count(real_engine, table) == 0, table

    # A start that fails before commit leaves nothing behind and keeps the setup code valid.
    real_create_run = migrations.create_run
    monkeypatch.setattr(migrations, "create_run", AsyncMock(side_effect=RuntimeError("boom")))
    async with maker(real_engine)() as s:
        with pytest.raises(HTTPException) as exc:
            await bootstrap_start(payload, session=s, x_setup_code=code_config)
    assert exc.value.status_code == 500
    await empty_install()
    assert setup_code_hash() != ""
    assert migration_env["scheduled"] == []
    monkeypatch.setattr(migrations, "create_run", real_create_run)

    # Two concurrent starts: exactly one wins; the other loses cleanly after the lock.
    async def attempt():
        async with maker(real_engine)() as s:
            try:
                return await bootstrap_start(payload, session=s, x_setup_code=code_config)
            except HTTPException as e:
                return e

    results = await asyncio.gather(attempt(), attempt())
    wins = [r for r in results if not isinstance(r, HTTPException)]
    losses = [r for r in results if isinstance(r, HTTPException)]
    assert len(wins) == 1 and len(losses) == 1
    assert losses[0].status_code == 409
    win = wins[0]
    assert set(win) >= {"access_token", "refresh_token", "run_id"}

    assert await count(real_engine, "users") == 1
    assert await count(real_engine, "companies") == 1
    assert await count(real_engine, "companies", "is_active = false") == 1
    assert await count(real_engine, "user_companies", "role = 'owner'") == 1
    assert await count(real_engine, "migration_runs") == 1
    run = await load_run(real_engine, uuid.UUID(win["run_id"]))
    assert run.status == "running"
    assert migration_env["scheduled"] == [run.id]
    run_dir = migration_env["data_dir"] / "migration_runs" / str(run.id)
    assert any(p.is_file() for p in run_dir.iterdir())
    assert not (migration_env["data_dir"] / "migration_scans" / scan.token).exists()
    assert setup_code_hash() == ""


@pytest.mark.asyncio
async def test_migration_start_creates_clean_inactive_company_without_seed_hooks(real_engine, migration_env, monkeypatch):
    import celerp.modules.slots as slots
    from celerp.models.company import Company
    from celerp.services import migrations

    hooks = AsyncMock()
    monkeypatch.setattr(slots, "fire_lifecycle", hooks)

    run_id, company_id, _ = await staged_run(real_engine)
    async with maker(real_engine)() as s:
        company = await s.get(Company, company_id)
        assert company.is_active is False
        assert not {"price_lists", "self_contact_id", "payment_terms"} & set(company.settings or {})
    for table in ("locations", "ledger", "projections"):
        assert await count(real_engine, table, "company_id = :c", c=company_id) == 0, table
    assert await count(real_engine, "user_companies", "company_id = :c AND role = 'owner'", c=company_id) == 1

    await migrations.run_migration(run_id)
    assert (await load_run(real_engine, run_id)).status == "ready_to_finalize"
    async with maker(real_engine)() as s:
        await migrations.finalize(s, await migrations.get_run_for_company(s, run_id, company_id))
    async with maker(real_engine)() as s:
        company = await s.get(Company, company_id)
        assert company.is_active is True
        assert (company.settings or {}).get("fiscal_year_start") == "01-01"
    # Only what is still missing is added: the imported locations stay, one becomes the default.
    assert await count(real_engine, "locations", "company_id = :c", c=company_id) == 3
    assert await count(real_engine, "locations", "company_id = :c AND is_default", c=company_id) == 1
    assert await count(real_engine, "locations", "company_id = :c AND name = 'Head Office'", c=company_id) == 0
    assert await count(real_engine, "ledger", "company_id = :c", c=company_id) == 0

    # A source with no locations gets the one default location Celerp needs.
    run_id, company_id, _ = await staged_run(
        real_engine, email="second@example.com",
        spec=fake_spec(locations=0, coverage=[
            {"source_type": "Payment", "count": 1, "coverage_class": "mapped", "target": "settlement"}]))
    await migrations.run_migration(run_id)
    async with maker(real_engine)() as s:
        await migrations.finalize(s, await migrations.get_run_for_company(s, run_id, company_id))
    assert await count(real_engine, "locations", "company_id = :c AND is_default AND name = 'Head Office'",
                       c=company_id) == 1
    assert await count(real_engine, "locations", "company_id = :c", c=company_id) == 1
    hooks.assert_not_awaited()
