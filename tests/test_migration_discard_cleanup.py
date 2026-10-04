# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Discard never waits on file storage, and never loses track of the files.

The discard transaction records a cleanup task (company id and run ids only) with
the deletes; after the commit the run sources and the company's attachment files
are removed and the task is deleted. A storage failure keeps the task for the
startup sweep, while the user carries on as if the discard were complete."""

from __future__ import annotations

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
    migrate_as_owner,
    migration_env,  # noqa: F401 - fixture
    real_client,  # noqa: F401 - fixture
    real_engine,  # noqa: F401 - fixture
    save_decisions,
    scan_upload,
)
from test_helpers import create_item, default_location_id, register_admin

TASKS = "migration_cleanup_tasks"


def _source(env, run_id):
    return env["data_dir"] / "migration_runs" / str(run_id)


def _attachments(env, company_id):
    return env["data_dir"] / "static" / "attachments" / str(company_id)


async def _staged(client, engine, env) -> tuple[str, str, str]:
    """A company owner's staged migration with a source and a stored attachment file.
    Returns (owner token, run id, company id)."""
    token = await register_admin(client)
    run_id = await migrate_as_owner(client, token)
    run = await load_run(engine, uuid.UUID(run_id))
    stored = _attachments(env, run.company_id)
    stored.mkdir(parents=True)
    (stored / "receipt.bin").write_bytes(b"x")
    assert _source(env, run_id).is_dir()
    return token, run_id, str(run.company_id)


async def _tasks(engine) -> list[tuple]:
    from sqlalchemy import text
    async with maker(engine)() as s:
        return [tuple(r) for r in (await s.execute(text(f"SELECT company_id, run_ids FROM {TASKS}"))).all()]


async def _sweep(engine):
    from celerp.services import migrations
    async with maker(engine)() as s:
        await migrations.housekeeping(s)


def _fail_source_delete(monkeypatch):
    from celerp.services import migration_scan_store as store

    def refuse(path):
        raise OSError("device busy")
    monkeypatch.setattr(store, "_remove_tree", refuse)


def _fail_attachment_delete(monkeypatch):
    from celerp.services import attachments

    async def refuse(self, company_id):
        raise OSError("device busy")
    monkeypatch.setattr(attachments.LocalBackend, "delete_company", refuse)


@pytest.mark.asyncio
async def test_discard_removes_the_files_and_its_cleanup_task(real_client, real_engine, migration_env):
    token, run_id, company_id = await _staged(real_client, real_engine, migration_env)
    r = await real_client.post(f"/migrations/{run_id}/discard", headers=auth(token))
    assert r.status_code == 200 and r.json() == {"redirect": "/"}, r.text
    assert await count(real_engine, "companies", "id = :c", c=company_id) == 0
    assert not _source(migration_env, run_id).exists()
    assert not _attachments(migration_env, company_id).exists()
    assert await _tasks(real_engine) == []


@pytest.mark.asyncio
async def test_discard_removes_the_notices_the_staged_company_was_told(real_client, real_engine, migration_env):
    """RED before the change: a notice told to every company (a start that held its updates
    back, say) reached the staged company too, and discard refused it as data it could not
    remove."""
    from celerp.notifications.service import create

    token, run_id, company_id = await _staged(real_client, real_engine, migration_env)
    async with maker(real_engine)() as s:
        await create(s, uuid.UUID(company_id), "system", "Held back", "Updates were held back.")
        await s.commit()
    assert await count(real_engine, "notifications", "company_id = :c", c=company_id) == 1

    r = await real_client.post(f"/migrations/{run_id}/discard", headers=auth(token))
    assert r.status_code == 200, r.text
    assert await count(real_engine, "companies", "id = :c", c=company_id) == 0
    assert await count(real_engine, "notifications", "company_id = :c", c=company_id) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [_fail_source_delete, _fail_attachment_delete])
async def test_a_storage_failure_keeps_the_task_and_startup_finishes_it(real_client, real_engine, migration_env,
                                                                       monkeypatch, caplog, fail):
    import logging

    token, run_id, company_id = await _staged(real_client, real_engine, migration_env)
    with monkeypatch.context() as m:
        fail(m)
        caplog.set_level(logging.WARNING, logger="celerp.services.migrations")
        r = await real_client.post(f"/migrations/{run_id}/discard", headers=auth(token))
        assert r.status_code == 200 and r.json() == {"redirect": "/"}, r.text
        assert await count(real_engine, "companies", "id = :c", c=company_id) == 0
        assert await count(real_engine, "migration_runs") == 0
        assert await _tasks(real_engine) == [(uuid.UUID(company_id), [run_id])]
        warnings = [rec.getMessage() for rec in caplog.records if rec.name == "celerp.services.migrations"]
        assert warnings and all("receipt" not in w and "artifact" not in w for w in warnings), warnings

        # A sweep while storage is still failing keeps the task.
        await _sweep(real_engine)
        assert len(await _tasks(real_engine)) == 1

    # The next startup retries and clears the task; a repeat finds nothing to do.
    await _sweep(real_engine)
    assert await _tasks(real_engine) == []
    assert not _source(migration_env, run_id).exists()
    assert not _attachments(migration_env, company_id).exists()
    await _sweep(real_engine)
    assert await _tasks(real_engine) == []


@pytest.mark.asyncio
async def test_cleanup_is_idempotent_when_files_are_already_gone(real_engine, migration_env):
    """Missing files count as deleted, so a task whose files are gone just completes."""
    from celerp.models.migration import MigrationCleanupTask
    from celerp.services import migrations

    async with maker(real_engine)() as s:
        task = MigrationCleanupTask(company_id=uuid.uuid4(), run_ids=[str(uuid.uuid4())])
        s.add(task)
        await s.commit()
        assert await migrations.run_cleanup_task(s, task.id) is True
        assert await migrations.run_cleanup_task(s, task.id) is True
    assert await _tasks(real_engine) == []


@pytest.mark.asyncio
async def test_bootstrap_discard_with_a_storage_failure_returns_to_setup(real_client, real_engine, migration_env,
                                                                        monkeypatch):
    r = await scan_upload(real_client, fake_bytes())
    scan_token = r.json()["scan_token"]
    assert (await save_decisions(real_client, scan_token)).status_code == 200
    r = await real_client.post("/migrations/bootstrap/start", json={
        "scan_token": scan_token, "company_name": "Moved Co", "name": "Owner",
        "email": OWNER_EMAIL, "password": OWNER_PASSWORD})
    assert r.status_code == 201, r.text
    token, run_id = r.json()["access_token"], r.json()["run_id"]

    _fail_source_delete(monkeypatch)
    _fail_attachment_delete(monkeypatch)
    r = await real_client.post(f"/migrations/{run_id}/discard", headers=auth(token))
    assert r.status_code == 200 and r.json() == {"redirect": "/setup"}, r.text
    assert (await real_client.get("/auth/bootstrap-status")).json()["bootstrapped"] is False
    assert len(await _tasks(real_engine)) == 1
    # First-run setup is open again: a new scan is accepted.
    assert (await scan_upload(real_client, fake_bytes())).status_code == 200


@pytest.mark.asyncio
async def test_additional_company_discard_with_a_storage_failure_keeps_the_active_company(
        real_client, real_engine, migration_env, monkeypatch):
    token, run_id, _ = await _staged(real_client, real_engine, migration_env)
    _fail_source_delete(monkeypatch)
    _fail_attachment_delete(monkeypatch)
    r = await real_client.post(f"/migrations/{run_id}/discard", headers=auth(token))
    assert r.status_code == 200 and r.json() == {"redirect": "/"}, r.text
    assert len(await _tasks(real_engine)) == 1

    headers = auth(token)
    me = await real_client.get("/companies/me", headers=headers)
    assert me.status_code == 200 and me.json()["name"] == "Perm Co"
    location_id = await default_location_id(real_client, headers)
    await create_item(real_client, headers, location_id, sku="SKU-AFTER-DISCARD")
    # Another migration can start straight away.
    assert await migrate_as_owner(real_client, token)
