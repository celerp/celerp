# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Starting a migration survives a crash at every step.

A start first commits the staged company and a ``preparing`` run that holds a
durable, hashed claim on the scan, then moves the scan's files into run storage,
then starts the run. Whatever point a start dies at, a retry with the same scan or
startup recovery ends with one company, one run, the source in one place and at
most one runner, and the scan can never start a second run."""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import pytest

from migration_support import (
    OWNER_EMAIL,
    OWNER_PASSWORD,
    auth,
    count,
    fake_bytes,
    load_run,
    maker,
    migration_env,  # noqa: F401 - fixture
    real_client,  # noqa: F401 - fixture
    real_engine,  # noqa: F401 - fixture
    save_decisions,
    scan_upload,
)
from test_helpers import register_admin

STAGED = "This company is still being moved into Celerp. Finish or discard the migration first."


class Crash(Exception):
    """A process dying at the injected point."""


async def _scan(client, token: str | None = None) -> str:
    r = await scan_upload(client, fake_bytes(), token=token)
    scan_token = r.json()["scan_token"]
    assert (await save_decisions(client, scan_token, token=token)).status_code == 200
    return scan_token


async def _start(client, token: str, scan_token: str, name: str = "Moved Co"):
    """POST start-from-scan; a crash inside the request returns None."""
    try:
        return await client.post("/migrations/start-from-scan", headers=auth(token),
                                 json={"scan_token": scan_token, "company_name": name})
    except Crash:
        return None


def _scan_dir(env, scan_token: str):
    return env["data_dir"] / "migration_scans" / scan_token


def _run_dir(env, run_id):
    return env["data_dir"] / "migration_runs" / str(run_id)


async def _only_run(engine):
    from celerp.models.migration import MigrationRun
    from sqlalchemy import select
    async with maker(engine)() as s:
        runs = (await s.scalars(select(MigrationRun))).all()
    assert len(runs) == 1, runs
    return runs[0]


async def _recover(engine):
    from celerp.services import migrations
    async with maker(engine)() as s:
        await migrations.housekeeping(s)


async def _assert_one_start(client, engine, env, token, scan_token, companies_before, *, status):
    """One staged company, one run in *status*, its source in run storage only, no
    runner scheduled twice, and the scan maps back to that run instead of a new one."""
    run = await _only_run(engine)
    assert run.status == status
    assert run.scan_claim_sha256 == hashlib.sha256(scan_token.encode()).hexdigest()
    assert await count(engine, "companies") == companies_before + 1
    assert await count(engine, "companies", "is_migration_staged") == 1
    assert sorted(p.name for p in _run_dir(env, run.id).iterdir()) == ["artifact-1"]
    assert not _scan_dir(env, scan_token).exists()
    assert env["scheduled"].count(run.id) <= 1
    scheduled = list(env["scheduled"])

    again = await _start(client, token, scan_token, name="Another Name")
    assert again.status_code == 200 and again.json() == {"run_id": str(run.id), "preparing": False}, again.text
    assert await count(engine, "migration_runs") == 1
    assert await count(engine, "companies") == companies_before + 1
    assert env["scheduled"] == scheduled
    return run


@pytest.mark.asyncio
async def test_crash_before_commit_leaves_nothing_and_the_scan_reusable(real_client, real_engine, migration_env,
                                                                        monkeypatch):
    from celerp.services import migrations

    admin_token = await register_admin(real_client)
    companies = await count(real_engine, "companies")
    scan_token = await _scan(real_client, admin_token)

    real_create = migrations.create_run

    async def create_then_crash(*args, **kwargs):
        await real_create(*args, **kwargs)
        raise Crash()

    monkeypatch.setattr(migrations, "create_run", create_then_crash)
    r = await _start(real_client, admin_token, scan_token)
    assert r is None or r.status_code == 500
    assert await count(real_engine, "companies") == companies
    assert await count(real_engine, "migration_runs") == 0
    assert (_scan_dir(migration_env, scan_token) / "artifact-1").is_file()
    assert not (migration_env["data_dir"] / "migration_runs").exists() or \
        not any((migration_env["data_dir"] / "migration_runs").iterdir())

    monkeypatch.setattr(migrations, "create_run", real_create)
    r = await _start(real_client, admin_token, scan_token)
    assert r.status_code == 201, r.text
    run = await _assert_one_start(real_client, real_engine, migration_env, admin_token, scan_token, companies,
                                  status="running")
    assert migration_env["scheduled"] == [run.id]


@pytest.mark.asyncio
async def test_crash_after_commit_before_move_is_finished_by_a_retry(real_client, real_engine, migration_env,
                                                                     monkeypatch):
    from celerp.services import migration_scan_store as store

    admin_token = await register_admin(real_client)
    companies = await count(real_engine, "companies")
    scan_token = await _scan(real_client, admin_token)

    real_claim = store.claim_for_run
    monkeypatch.setattr(store, "claim_for_run", lambda *a, **k: (_ for _ in ()).throw(Crash()))
    r = await _start(real_client, admin_token, scan_token)
    assert r is None or r.status_code == 500
    run = await _only_run(real_engine)
    assert run.status == "preparing"
    assert (_scan_dir(migration_env, scan_token) / "artifact-1").is_file()
    assert not _run_dir(migration_env, run.id).exists()
    assert migration_env["scheduled"] == []

    # The repeated start finds the claimed run and finishes starting it; no second company.
    monkeypatch.setattr(store, "claim_for_run", real_claim)
    r = await _start(real_client, admin_token, scan_token)
    assert r.status_code == 200 and r.json() == {"run_id": str(run.id), "preparing": False}, r.text
    assert migration_env["scheduled"] == [run.id]
    await _assert_one_start(real_client, real_engine, migration_env, admin_token, scan_token, companies,
                            status="running")


@pytest.mark.asyncio
async def test_crash_after_commit_before_move_is_recovered_at_startup(real_client, real_engine, migration_env,
                                                                      monkeypatch):
    """Recovery runs before expired scans are purged, so an old scan still reaches its run."""
    import time

    from celerp.services import migration_scan_store as store

    admin_token = await register_admin(real_client)
    companies = await count(real_engine, "companies")
    scan_token = await _scan(real_client, admin_token)

    real_claim = store.claim_for_run
    monkeypatch.setattr(store, "claim_for_run", lambda *a, **k: (_ for _ in ()).throw(Crash()))
    await _start(real_client, admin_token, scan_token)
    monkeypatch.setattr(store, "claim_for_run", real_claim)
    later = time.time() + store.SCAN_TTL_SECONDS + 60
    monkeypatch.setattr(store, "_now", lambda: later)

    await _recover(real_engine)
    monkeypatch.setattr(store, "_now", time.time)
    run = await _assert_one_start(real_client, real_engine, migration_env, admin_token, scan_token, companies,
                                  status="ready")
    assert migration_env["scheduled"] == []  # the owner starts it from the progress page
    assert run.error_summary == {}


@pytest.mark.asyncio
async def test_crash_after_move_before_transition_is_recovered(real_client, real_engine, migration_env,
                                                               monkeypatch):
    from celerp.services import migrations

    admin_token = await register_admin(real_client)
    companies = await count(real_engine, "companies")
    scan_token = await _scan(real_client, admin_token)

    real_request_start = migrations.request_start

    async def crash(*args, **kwargs):
        raise Crash()

    monkeypatch.setattr(migrations, "request_start", crash)
    r = await _start(real_client, admin_token, scan_token)
    assert r is None or r.status_code == 500
    run = await _only_run(real_engine)
    assert run.status == "preparing"
    assert (_run_dir(migration_env, run.id) / "artifact-1").is_file()
    assert not _scan_dir(migration_env, scan_token).exists()

    monkeypatch.setattr(migrations, "request_start", real_request_start)
    await _recover(real_engine)
    await _assert_one_start(real_client, real_engine, migration_env, admin_token, scan_token, companies,
                            status="ready")
    assert migration_env["scheduled"] == []


@pytest.mark.asyncio
async def test_crash_after_transition_before_response_is_not_started_twice(real_client, real_engine,
                                                                           migration_env, monkeypatch):
    from celerp.services import migrations

    admin_token = await register_admin(real_client)
    companies = await count(real_engine, "companies")
    scan_token = await _scan(real_client, admin_token)

    recorder = migrations.schedule_run
    monkeypatch.setattr(migrations, "schedule_run", lambda run_id: (_ for _ in ()).throw(Crash()))
    r = await _start(real_client, admin_token, scan_token)
    assert r is None or r.status_code == 500
    monkeypatch.setattr(migrations, "schedule_run", recorder)

    await _recover(real_engine)
    await _assert_one_start(real_client, real_engine, migration_env, admin_token, scan_token, companies,
                            status="running")
    assert migration_env["scheduled"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("names", [("Raced Co", "Raced Co"), ("Raced Co", "Other Co")])
async def test_concurrent_start_from_scan_starts_one_migration(real_client, real_engine, migration_env, names):
    """Two simultaneous starts from one scan both answer with the one run they created."""
    admin_token = await register_admin(real_client)
    companies = await count(real_engine, "companies")
    scan_token = await _scan(real_client, admin_token)

    results = await asyncio.gather(*(_start(real_client, admin_token, scan_token, name) for name in names))
    assert sorted(r.status_code for r in results) == [200, 201], [r.text for r in results]
    assert results[0].json() == results[1].json()
    run = await _assert_one_start(real_client, real_engine, migration_env, admin_token, scan_token, companies,
                                  status="running")
    assert migration_env["scheduled"] == [run.id]


@pytest.mark.asyncio
async def test_a_scan_claimed_by_another_user_cannot_start_a_run(real_client, real_engine, migration_env):
    from celerp.models.migration import MigrationRun
    from sqlalchemy import update

    admin_token = await register_admin(real_client)
    scan_token = await _scan(real_client, admin_token)
    r = await _start(real_client, admin_token, scan_token)
    assert r.status_code == 201
    run_id = uuid.UUID(r.json()["run_id"])
    async with maker(real_engine)() as s:
        # Reassign the run's creator: the claim alone never hands a run to someone else.
        from celerp.models.company import User
        other = User(email="other@example.com", name="Other")
        s.add(other)
        await s.flush()
        await s.execute(update(MigrationRun).where(MigrationRun.id == run_id).values(created_by_user_id=other.id))
        await s.commit()
    r = await _start(real_client, admin_token, scan_token)
    assert r.status_code == 409 and r.json()["detail"] == \
        "This upload was already used to start a migration, or it was replaced."
    assert await count(real_engine, "migration_runs") == 1


@pytest.mark.asyncio
async def test_preparing_run_with_no_source_fails_explicitly(real_client, real_engine, migration_env, monkeypatch):
    from celerp.services import migration_scan_store as store

    admin_token = await register_admin(real_client)
    scan_token = await _scan(real_client, admin_token)
    real_claim = store.claim_for_run
    monkeypatch.setattr(store, "claim_for_run", lambda *a, **k: (_ for _ in ()).throw(Crash()))
    await _start(real_client, admin_token, scan_token)
    monkeypatch.setattr(store, "claim_for_run", real_claim)
    store.delete_scan(scan_token)

    await _recover(real_engine)
    run = await _only_run(real_engine)
    assert run.status == "failed"
    assert run.error_summary == {"message": "The uploaded file for this migration is missing. "
                                            "Discard it and upload the file again."}
    assert not _run_dir(migration_env, run.id).exists()
    assert migration_env["scheduled"] == []
    # Recovery is idempotent.
    await _recover(real_engine)
    assert (await _only_run(real_engine)).status == "failed"


@pytest.mark.asyncio
async def test_bootstrap_lost_response_recovers_through_login(real_client, real_engine, migration_env,
                                                              monkeypatch):
    """The first owner's start commits and the process dies before it answers: signing
    in with the submitted credentials leads back to the same run, ERP stays closed,
    and discard returns the install to first-run setup."""
    from celerp.services import migration_scan_store as store

    scan_token = await _scan(real_client)
    real_claim = store.claim_for_run
    monkeypatch.setattr(store, "claim_for_run", lambda *a, **k: (_ for _ in ()).throw(Crash()))
    try:
        r = await real_client.post("/migrations/bootstrap/start", json={
            "scan_token": scan_token, "company_name": "Moved Co", "name": "Owner",
            "email": OWNER_EMAIL, "password": OWNER_PASSWORD})
        assert r.status_code == 500
    except Crash:
        pass
    monkeypatch.setattr(store, "claim_for_run", real_claim)
    await _recover(real_engine)
    run = await _only_run(real_engine)
    assert run.status == "ready"
    run_id = str(run.id)

    assert (await real_client.get("/auth/bootstrap-status")).json()["bootstrapped"] is True
    r = await real_client.post("/migrations/bootstrap/start", json={
        "scan_token": scan_token, "company_name": "Moved Co", "name": "Owner",
        "email": OWNER_EMAIL, "password": OWNER_PASSWORD})
    assert r.status_code == 409 and r.json()["detail"] == "System already bootstrapped. Contact your admin."

    r = await real_client.post("/auth/login", json={"email": OWNER_EMAIL, "password": OWNER_PASSWORD})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    r = await real_client.get("/companies/me", headers=auth(token))
    assert r.status_code == 403 and r.json()["detail"] == STAGED
    r = await real_client.get("/migrations/staged", headers=auth(token))
    assert r.status_code == 200 and r.json()["id"] == run_id

    r = await real_client.post(f"/migrations/{run_id}/discard", headers=auth(token))
    assert r.status_code == 200 and r.json() == {"redirect": "/setup"}
    assert (await real_client.get("/auth/bootstrap-status")).json()["bootstrapped"] is False
    assert await count(real_engine, "users") == 0
    assert await count(real_engine, "companies") == 0
    assert (await load_run(real_engine, uuid.UUID(run_id))) is None


@pytest.mark.asyncio
async def test_reading_a_started_scan_names_its_run_to_its_creator_only(real_client, real_engine, migration_env):
    """After a lost start response the wizard reads its scan again and is sent to the run."""
    from sqlalchemy import update

    from celerp.models.company import User
    from celerp.models.migration import MigrationRun

    admin_token = await register_admin(real_client)
    scan_token = await _scan(real_client, admin_token)
    r = await _start(real_client, admin_token, scan_token)
    run_id = r.json()["run_id"]
    read = {"scan_token": scan_token}
    r = await real_client.post("/migrations/scan/read", headers=auth(admin_token), json=read)
    assert r.status_code == 200 and r.json() == {"run_id": run_id}, r.text

    async with maker(real_engine)() as s:
        other = User(email="other@example.com", name="Other")
        s.add(other)
        await s.flush()
        await s.execute(update(MigrationRun).where(MigrationRun.id == uuid.UUID(run_id))
                        .values(created_by_user_id=other.id))
        await s.commit()
    r = await real_client.post("/migrations/scan/read", headers=auth(admin_token), json=read)
    assert r.status_code == 410, r.text
