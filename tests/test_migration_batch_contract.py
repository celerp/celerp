# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The runner's batch contract: a batch commits only when every record in it
imported, a sink must report exactly one outcome per record, and a run reaches
reconciliation only when every manifest record has a durable entity mapping."""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest
from sqlalchemy import text

from celerp.importers.sinks import SinkBatchResult, SinkEntityMapping, SinkError
from migration_support import (
    auth,
    count,
    fake_spec,
    load_run,
    maker,
    migrate_as_owner,
    migration_env,  # noqa: F401 - fixture
    real_client,  # noqa: F401 - fixture
    real_engine,  # noqa: F401 - fixture
    staged_run,
)
from test_helpers import register_admin

PHASE = "contacts_locations"


def _spec(locations: int) -> dict:
    base = fake_spec()["coverage"]
    return fake_spec(locations=locations, coverage=[
        base[0], {"source_type": "Location", "count": locations, "coverage_class": "mapped", "target": "location"},
        base[2]])


def _tamper(sink, change) -> None:
    """Rewrite what the sink reports for location batches, after it has written them."""
    real = type(sink).import_batch

    async def import_batch(context, records):
        result = await real(sink, context, records)
        if any(r.source_type == "Location" for r in records):
            result = change(result, records)
        return result

    sink.import_batch = import_batch


def _heal(sink) -> None:
    sink.__dict__.pop("import_batch", None)


def _reject(external_id: str):
    def change(result: SinkBatchResult, records) -> SinkBatchResult:
        kept = [m for m in result.mappings if m.source_external_id != external_id]
        return SinkBatchResult(created=len(kept), skipped=result.skipped, mappings=kept, errors=[
            *result.errors, SinkError("Location", external_id, "A location with this name already exists.")])
    return change


async def _resume(engine, run_id) -> None:
    from celerp.services import migrations

    async with maker(engine)() as s:
        run = await migrations.get_run_for_company(s, run_id, (await load_run(engine, run_id)).company_id)
        await migrations.request_start(s, run)
        await s.commit()
    await migrations.run_migration(run_id)


def _location_batches(sink) -> list[list[str]]:
    return [ids for kind, ids in sink.events if kind == "import_batch" and ids[0].startswith("loc-")]


async def _location_maps(engine, run_id) -> int:
    return await count(engine, "migration_entity_maps", "migration_run_id = :r AND source_type = 'Location'",
                       r=run_id)


@pytest.mark.asyncio
async def test_one_rejected_record_rolls_back_its_whole_batch_and_resume_imports_each_once(
        real_engine, migration_env):
    from celerp.services import migrations

    sink = migration_env["sink"]
    sink.batch_size = 10
    _tamper(sink, _reject("loc-5"))
    run_id, company_id, _ = await staged_run(real_engine, spec=_spec(10))

    await migrations.run_migration(run_id)

    run = await load_run(real_engine, run_id)
    assert run.status == "failed"
    assert run.current_phase == PHASE
    assert run.phase_state[PHASE]["status"] == "failed"
    assert run.phase_state[PHASE]["cursor"] == 0
    assert run.phase_state[PHASE]["created"] == 0
    assert run.phase_state[PHASE]["errors"] == 1
    assert run.error_summary["error_class"] == "MigrationBatchError"
    assert "loc-5" in run.error_summary["message"]
    assert await count(real_engine, "locations", "company_id = :c", c=company_id) == 0
    assert await _location_maps(real_engine, run_id) == 0
    assert run.phase_state["company_settings"]["status"] == "done"

    _heal(sink)
    await _resume(real_engine, run_id)

    run = await load_run(real_engine, run_id)
    assert run.status == "ready_to_finalize", run.error_summary
    assert run.phase_state[PHASE]["cursor"] == 10
    assert run.phase_state[PHASE]["errors"] == 0
    assert await count(real_engine, "locations", "company_id = :c", c=company_id) == 10
    assert await _location_maps(real_engine, run_id) == 10
    batches = _location_batches(sink)
    assert len(batches) == 2 and batches[0] == batches[1] == [f"loc-{i}" for i in range(1, 11)]


def _drop(result, records):
    return replace(result, mappings=[m for m in result.mappings if m.source_external_id != "loc-2"])


def _duplicate(result, records):
    return replace(result, mappings=[*result.mappings, result.mappings[0]])


def _unexpected(result, records):
    extra = SinkEntityMapping("Location", "loc-99", "location", str(uuid.uuid4()), "created")
    return replace(result, mappings=[*result.mappings, extra])


def _conflicting(result, records):
    return replace(result, errors=[SinkError("Location", "loc-1", "Also failed.")])


def _empty_skip(result, records):
    first = replace(result.mappings[0], status="skipped", target_entity_id="")
    return replace(result, mappings=[first, *result.mappings[1:]])


@pytest.mark.asyncio
@pytest.mark.parametrize("change, needle", [
    (_drop, "no outcome for Location loc-2"),
    (_duplicate, "more than one outcome for Location loc-1"),
    (_unexpected, "an outcome for Location loc-99, which was not in the batch"),
    (_conflicting, "more than one outcome for Location loc-1"),
    (_empty_skip, "no Celerp record for Location loc-1"),
], ids=["missing", "duplicate", "unexpected", "conflicting", "skipped-without-target"])
async def test_sink_outcomes_must_match_the_batch_one_to_one(real_engine, migration_env, change, needle):
    from celerp.services import migrations

    sink = migration_env["sink"]
    sink.batch_size = 10
    _tamper(sink, change)
    run_id, company_id, _ = await staged_run(real_engine, spec=_spec(3))

    await migrations.run_migration(run_id)

    run = await load_run(real_engine, run_id)
    assert run.status == "failed"
    assert run.error_summary["error_class"] == "SinkContractError"
    assert needle in run.error_summary["message"]
    assert run.phase_state[PHASE]["cursor"] == 0
    assert await count(real_engine, "locations", "company_id = :c", c=company_id) == 0
    assert await _location_maps(real_engine, run_id) == 0


@pytest.mark.asyncio
async def test_a_batch_of_idempotent_replays_succeeds(real_engine, migration_env):
    from celerp.services import migrations

    run_id, company_id, _ = await staged_run(real_engine)
    await migrations.run_migration(run_id)
    assert (await load_run(real_engine, run_id)).status == "ready_to_finalize"

    await _resume(real_engine, run_id)  # a re-run replays every phase; every record already exists

    run = await load_run(real_engine, run_id)
    assert run.status == "ready_to_finalize", run.error_summary
    assert (run.phase_state[PHASE]["created"], run.phase_state[PHASE]["skipped"]) == (0, 3)
    assert await count(real_engine, "locations", "company_id = :c", c=company_id) == 3
    assert await count(real_engine, "migration_entity_maps", "migration_run_id = :r", r=run_id) == 5


@pytest.mark.asyncio
async def test_a_missing_mapping_fails_the_run_before_reconciliation(real_engine, migration_env, monkeypatch):
    """Financial reconciliation passes here (every location is written); only the
    completeness gate can see that one source record has no durable mapping."""
    from celerp.services import migrations

    real_record = migrations._record_mappings

    async def lose_one(session, run_id, group, mappings, targets):
        await real_record(session, run_id, group, [m for m in mappings if m.source_external_id != "loc-2"],
                          targets)

    monkeypatch.setattr(migrations, "_record_mappings", lose_one)
    sink = migration_env["sink"]
    reconciled = []
    real_reconcile = type(sink).reconcile

    async def reconcile(context, expectations):
        reconciled.append(True)
        return await real_reconcile(sink, context, expectations)

    sink.reconcile = reconcile
    run_id, company_id, _ = await staged_run(real_engine)

    await migrations.run_migration(run_id)

    run = await load_run(real_engine, run_id)
    assert run.status == "failed"
    assert run.current_phase == "reconciliation"
    assert run.phase_state["reconciliation"]["status"] == "failed"
    assert run.phase_state.get("ready_to_finalize", {}).get("status") != "done"
    assert run.error_summary["error_class"] == "IncompleteMigrationError"
    assert run.error_summary["message"] == "1 source record has no imported Celerp record: Location (1)."
    assert run.error_summary["missing"] == "Location loc-2"
    assert "Store 2" not in str(run.error_summary)
    assert reconciled == []
    assert run.reconciliation == {}
    assert await count(real_engine, "locations", "company_id = :c", c=company_id) == 3


@pytest.mark.asyncio
async def test_journey_interrupted_migration_is_retry_safe(real_client, real_engine, migration_env):
    """Journey 6: a sink failure mid-batch fails the run at its previous cursor, the
    owner's live company keeps working, and a resume imports everything once."""
    from celerp.services import migrations

    sink = migration_env["sink"]
    owner_token = await register_admin(real_client)
    token, run_id = await migrate_as_owner(real_client, owner_token)
    rid = uuid.UUID(run_id)
    company_id = (await load_run(real_engine, rid)).company_id
    _tamper(sink, _reject("loc-2"))

    await migrations.run_migration(rid)

    run = await load_run(real_engine, rid)
    assert run.status == "failed" and run.phase_state[PHASE]["cursor"] == 0
    assert await count(real_engine, "locations", "company_id = :c", c=company_id) == 0

    r = await real_client.get("/companies/me", headers=auth(owner_token))
    assert r.status_code == 200 and r.json()["name"] == "Perm Co"
    r = await real_client.post("/companies/me/locations", headers=auth(owner_token),
                               json={"name": "Back room", "type": "warehouse"})
    assert r.status_code == 200, r.text

    _heal(sink)
    r = await real_client.post(f"/migrations/{run_id}/start", headers=auth(token))
    assert r.status_code == 202, r.text
    await migrations.run_migration(rid)

    run = await load_run(real_engine, rid)
    assert run.status == "ready_to_finalize", run.error_summary
    assert [row["result"] for row in run.reconciliation["rows"]] == ["pass"]
    assert await count(real_engine, "locations", "company_id = :c", c=company_id) == 3
    async with real_engine.connect() as conn:
        dupes = (await conn.execute(text(
            "SELECT count(*) FROM (SELECT source_type, source_external_id FROM migration_entity_maps "
            "WHERE migration_run_id = :r GROUP BY 1, 2 HAVING count(*) > 1) d"), {"r": rid})).scalar_one()
    assert dupes == 0
    assert await count(real_engine, "migration_entity_maps", "migration_run_id = :r", r=rid) == 5


def test_import_outcomes_live_in_a_neutral_module():
    """Every importer, standalone or migration, reports through one results module;
    the migration sink helpers neither define nor re-export it."""
    import ast
    import importlib
    import pathlib

    names = {"RecordOutcome", "ImportOutcome", "OutcomeStatus", "ROUTE_ERROR_LIMIT"}
    results = importlib.import_module("celerp.importers.results")
    assert results.RecordOutcome.__module__ == results.ImportOutcome.__module__ == "celerp.importers.results"
    root = pathlib.Path(__file__).resolve().parents[1]
    stale = []
    for path in [*root.glob("celerp/**/*.py"), *root.glob("default_modules/*/*/*.py"), *root.glob("tests/*.py")]:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.ImportFrom) and node.module == "celerp.services.migration_core_sink"
                    and names & {a.name for a in node.names}):
                stale.append(str(path.relative_to(root)))
            if path.name == "migration_core_sink.py" and isinstance(node, (ast.ClassDef, ast.Assign)) and (
                    getattr(node, "name", None) in names
                    or any(getattr(t, "id", None) in names for t in getattr(node, "targets", []))):
                stale.append(f"{path.relative_to(root)} defines {getattr(node, 'name', 'a result name')}")
    assert stale == []
