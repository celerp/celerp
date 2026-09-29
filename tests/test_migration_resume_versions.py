# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Resume compatibility: a stopped run resumes only under the adapter and migration
CIF versions it was created with. After an upgrade that changes either, resume is
refused before any sink call, and the run stays readable and discardable."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from migration_support import (
    OWNER_EMAIL,
    OWNER_PASSWORD,
    auth,
    code_config,  # noqa: F401 - fixture
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
    staged_run,
)
from test_helpers import register_admin

PHASE = "contacts_locations"
RESTART = "This migration was created by an older importer version and must be restarted."


async def _stop_midway(engine, sink, run_id) -> None:
    """Fail the run after the first location batch, leaving cursor 2 and its mappings."""
    from celerp.services import migrations

    sink.fail_on = {"loc-3": 1}
    await migrations.run_migration(run_id)
    run = await load_run(engine, run_id)
    assert run.status == "failed" and run.phase_state[PHASE]["cursor"] == 2


async def _resume(engine, run_id) -> None:
    from celerp.services import migrations

    async with maker(engine)() as s:
        run = await migrations.get_run_for_company(s, run_id, (await load_run(engine, run_id)).company_id)
        await migrations.request_start(s, run)
        await s.commit()
    await migrations.run_migration(run_id)


async def _maps(engine, run_id) -> list:
    async with engine.connect() as conn:
        return (await conn.execute(text(
            "SELECT source_type, source_external_id, target_entity_id FROM migration_entity_maps "
            "WHERE migration_run_id = :r ORDER BY 1, 2"), {"r": run_id})).all()


async def _cif_version(engine, run_id, version: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE migration_runs SET cif_version = :v WHERE id = :r"),
                           {"v": version, "r": run_id})


@pytest.mark.asyncio
async def test_resume_under_the_same_versions_continues(real_engine, migration_env):
    sink = migration_env["sink"]
    run_id, company_id, _ = await staged_run(real_engine)
    await _stop_midway(real_engine, sink, run_id)

    await _resume(real_engine, run_id)

    run = await load_run(real_engine, run_id)
    assert run.status == "ready_to_finalize", run.error_summary
    assert await count(real_engine, "locations", "company_id = :c", c=company_id) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("upgrade", ["adapter", "cif"])
async def test_resume_after_an_importer_upgrade_is_refused_before_any_sink_call(real_engine, migration_env, upgrade):
    sink = migration_env["sink"]
    run_id, company_id, _ = await staged_run(real_engine)
    await _stop_midway(real_engine, sink, run_id)
    before = await load_run(real_engine, run_id)
    maps = await _maps(real_engine, run_id)
    calls = len(sink.events)
    if upgrade == "adapter":
        migration_env["adapter"].adapter_version = "2"
    else:
        await _cif_version(real_engine, run_id, "1")

    await _resume(real_engine, run_id)

    run = await load_run(real_engine, run_id)
    assert len(sink.events) == calls
    assert run.status == "failed"
    assert run.error_summary["error_class"] == "IncompatibleImporterVersion"
    assert run.error_summary["message"] == RESTART
    assert run.phase_state[PHASE]["cursor"] == before.phase_state[PHASE]["cursor"] == 2
    assert {k: v["cursor"] for k, v in run.phase_state.items()} == \
        {k: v["cursor"] for k, v in before.phase_state.items()}
    assert await _maps(real_engine, run_id) == maps
    assert await count(real_engine, "locations", "company_id = :c", c=company_id) == 2


@pytest.mark.asyncio
async def test_journey_upgrade_during_a_stopped_migration(real_client, real_engine, migration_env):
    """Journey 9, additional company: the refused run stays readable and discardable
    and the owner's live company keeps working throughout."""
    from celerp.services import migrations

    sink = migration_env["sink"]
    owner_token = await register_admin(real_client)
    token, run_id = await migrate_as_owner(real_client, owner_token)
    await _stop_midway(real_engine, sink, uuid.UUID(run_id))
    calls = len(sink.events)
    migration_env["adapter"].adapter_version = "2"

    r = await real_client.post(f"/migrations/{run_id}/start", headers=auth(token))
    assert r.status_code == 202, r.text
    await migrations.run_migration(uuid.UUID(run_id))
    assert len(sink.events) == calls

    r = await real_client.get(f"/migrations/{run_id}", headers=auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "failed"
    assert r.json()["error_summary"]["message"] == RESTART

    r = await real_client.post("/companies/me/locations", headers=auth(owner_token),
                               json={"name": "Back room", "type": "warehouse"})
    assert r.status_code == 200, r.text

    r = await real_client.post(f"/migrations/{run_id}/discard", headers=auth(token))
    assert r.status_code == 200, r.text
    assert await count(real_engine, "migration_runs", "id = :r", r=uuid.UUID(run_id)) == 0
    r = await real_client.get("/companies/me", headers=auth(owner_token))
    assert r.status_code == 200 and r.json()["name"] == "Perm Co"


@pytest.mark.asyncio
async def test_journey_upgrade_during_a_stopped_bootstrap_migration(real_client, real_engine, migration_env,
                                                                    code_config):
    """Journey 9, first install: a bootstrap run refused after an upgrade can still be discarded."""
    from celerp.services import migrations

    sink = migration_env["sink"]
    r = await scan_upload(real_client, fake_bytes(), headers={"X-Setup-Code": code_config})
    assert r.status_code == 200, r.text
    scan_token = r.json()["scan_token"]
    assert (await save_decisions(real_client, scan_token)).status_code == 200
    r = await real_client.post("/migrations/bootstrap/start", headers={"X-Setup-Code": code_config}, json={
        "scan_token": scan_token, "company_name": "Moved Co", "name": "Owner",
        "email": OWNER_EMAIL, "password": OWNER_PASSWORD})
    assert r.status_code == 201, r.text
    token, run_id = r.json()["access_token"], r.json()["run_id"]
    await _stop_midway(real_engine, sink, uuid.UUID(run_id))
    calls = len(sink.events)
    migration_env["adapter"].adapter_version = "2"

    r = await real_client.post(f"/migrations/{run_id}/start", headers=auth(token))
    assert r.status_code == 202, r.text
    await migrations.run_migration(uuid.UUID(run_id))
    assert len(sink.events) == calls

    r = await real_client.get(f"/migrations/{run_id}", headers=auth(token))
    assert r.status_code == 200 and r.json()["error_summary"]["message"] == RESTART
    r = await real_client.post(f"/migrations/{run_id}/discard", headers=auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["redirect"]
    assert await count(real_engine, "migration_runs", "id = :r", r=uuid.UUID(run_id)) == 0
