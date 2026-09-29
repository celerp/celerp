# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Company migration lifecycle: decision validation, company isolation, the run
state machine and runner lock, resumable phases, discard, reconciliation and the
reconciliation pack."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from fixtures.manager_io.support import BASIC, FX, artifact
from migration_support import (
    auth,
    count,
    fake_bytes,
    fake_spec,
    load_run,
    maker,
    migrate_as_owner,
    migration_env,  # noqa: F401 - fixture
    real_client,  # noqa: F401 - fixture
    real_engine,  # noqa: F401 - fixture
    save_decisions,
    scan_upload,
    staged_run,
    upload_parts,
)
from test_helpers import make_authed_token, register_admin

OWNER_ONLY = "Only the company owner can move a company into Celerp."
NOT_FOUND = "Migration not found."

BLOCKED = [
    {"source_type": "Company", "count": 1, "coverage_class": "mapped", "target": "company"},
    {"source_type": "Location", "count": 3, "coverage_class": "mapped", "target": "location"},
    {"source_type": "Payslip", "count": 4, "coverage_class": "unsupported_financial_blocker", "target": None,
     "note": "Payroll is not supported yet."},
    {"source_type": "SalesQuote", "count": 2, "coverage_class": "unsupported_nonfinancial", "target": None},
]

QUESTIONS = [
    {"key": "acct:4000", "source_type": "Account", "source_external_id": "4000", "label": "Sales (4000)",
     "options": ["income", "other_income"], "suggested": "income"},
    {"key": "acct:4000:alt", "source_type": "Account", "source_external_id": "4000", "label": "Sales (4000) again",
     "options": ["income", "other_income"], "suggested": "income"},
    {"key": "tax:vat", "source_type": "TaxCode", "source_external_id": "vat", "label": "VAT",
     "options": ["standard", "exempt"], "suggested": None},
]


def _scan_json(env, scan_token):
    return env["data_dir"] / "migration_scans" / scan_token / "scan.json"


async def _scan_session(spec=None):
    from celerp.services import migration_scan_store as store
    return await store.create_scan(upload_parts(("books.fake", fake_bytes(spec))), owner=("user", uuid.uuid4()))


async def _manager_scan(path):
    from celerp.services import migration_scan_store as store
    return await store.create_scan(upload_parts((path.name, path.read_bytes())), owner=("user", uuid.uuid4()))


def _field_errors(call):
    from celerp.services.migrations import MigrationError
    with pytest.raises(MigrationError) as exc:
        call()
    assert exc.value.status_code == 422
    assert isinstance(exc.value.detail, dict)
    return exc.value.detail


async def _member_token(engine_or_session, company_id, role: str, email: str) -> str:
    """Add a user to a company with `role` and mint a DB-valid token for them."""
    from celerp.models.accounting import UserCompany
    from celerp.models.company import User

    async def _add(s):
        user = User(email=email, name=role.title())
        s.add(user)
        await s.flush()
        s.add(UserCompany(user_id=user.id, company_id=uuid.UUID(str(company_id)), role=role))
        await s.commit()
        return await make_authed_token(s, str(user.id), str(company_id), role)

    if hasattr(engine_or_session, "begin") and hasattr(engine_or_session, "dispose"):
        async with maker(engine_or_session)() as s:
            return await _add(s)
    return await _add(engine_or_session)


async def _finalize(engine, run_id):
    from celerp.services import migrations
    async with maker(engine)() as s:
        run = await migrations.get_run_for_company(s, run_id, (await load_run(engine, run_id)).company_id)
        return await migrations.finalize(s, run)


@pytest.mark.asyncio
async def test_full_history_blocker_is_server_validated_not_hidden(client, session, migration_env):
    r = await scan_upload(client, fake_bytes(fake_spec(coverage=BLOCKED)))
    assert r.status_code == 200, r.text
    scan_token, scan = r.json()["scan_token"], r.json()["scan"]
    assert scan["blockers"] == [{"source_type": "Payslip", "count": 4, "reason": "Payroll is not supported yet."}]
    assert len(scan["warnings"]) == 1 and "SalesQuote" in scan["warnings"][0]

    r = await save_decisions(client, scan_token, mode="full_history")
    assert r.status_code == 422
    assert "Payslip (4)" in r.json()["detail"]["mode"]
    r = await save_decisions(client, scan_token, mode="everything")
    assert r.status_code == 422 and "mode" in r.json()["detail"]
    r = await save_decisions(client, scan_token, mode="cutover", cutover_date="2026-02-01")
    assert r.status_code == 200, r.text
    assert r.json()["scan"]["decisions"]["mode"] == "cutover"

    # The server re-validates at start: a Full history decision on a blocked source is refused.
    path = _scan_json(migration_env, scan_token)
    stored = json.loads(path.read_text())
    stored["decisions"]["mode"] = "full_history"
    stored["decisions"]["cutover_date"] = None
    path.write_text(json.dumps(stored))
    r = await client.post("/migrations/bootstrap/start", json={
        "scan_token": scan_token, "company_name": "Moved Co", "name": "Owner",
        "email": "owner@example.com", "password": "ownerpw123"})
    assert r.status_code == 422, r.text
    assert "mode" in r.json()["detail"]
    assert (await session.execute(text("SELECT count(*) FROM companies"))).scalar_one() == 0

    # An unclassified source type blocks the scan from being used at all.
    mystery = BLOCKED[:2] + [{"source_type": "Mystery", "count": 1, "coverage_class": "unclassified"}]
    r = await scan_upload(client, fake_bytes(fake_spec(coverage=mystery)))
    scan_token = r.json()["scan_token"]
    assert [b["source_type"] for b in r.json()["scan"]["blockers"]] == ["Mystery"]
    for mode, cutover in (("full_history", None), ("cutover", "2026-02-01")):
        r = await save_decisions(client, scan_token, mode=mode, cutover_date=cutover)
        assert r.status_code == 422 and "Mystery (1)" in r.json()["detail"]["mode"]


@pytest.mark.asyncio
async def test_cutover_date_and_opening_scope_validation(migration_env):
    from celerp.importers.adapters.manager_io.adapter import ManagerIOAdapter
    from celerp.services.migrations import validate_decisions

    scan = await _scan_session()
    for value, needle in (
        (None, "Choose a cutover date."),
        ("2026-02-30", "YYYY-MM-DD"),
        ("01/02/2026", "YYYY-MM-DD"),
        ("2025-12-31", "2026-01-01"),
        ("2026-04-01", "2026-03-31"),
        ("2026-01-02", "before the first transaction"),
    ):
        errors = _field_errors(lambda: validate_decisions(scan, {"mode": "cutover", "cutover_date": value}))
        assert needle in errors["cutover_date"], (value, errors)
    chosen = validate_decisions(scan, {"mode": "cutover", "cutover_date": "2026-02-01"})
    assert (chosen.mode, chosen.cutover_date) == ("cutover", date(2026, 2, 1))
    assert validate_decisions(scan, {"mode": "full_history", "cutover_date": "2026-02-01"}).cutover_date is None

    # An open-document snapshot the source cannot carry at cutover is refused with its reason.
    fx = await _manager_scan(FX)
    errors = _field_errors(lambda: validate_decisions(fx, {"mode": "cutover", "cutover_date": "2026-02-01"}))
    assert "foreign currency" in errors["cutover_date"]

    # Historical effects are carried once: before the cutover only open documents and their
    # settlements survive, and balances arrive as openings dated at the cutover.
    basic = await _manager_scan(BASIC)
    chosen = validate_decisions(basic, {"mode": "cutover", "cutover_date": "2026-02-15"})
    manifest = ManagerIOAdapter().build_manifest([artifact(BASIC)], chosen)
    assert manifest.bundle.bank_transfers == []
    assert {(j.source_type, j.entry_date) for j in manifest.bundle.journals} == {("OpeningBalances", date(2026, 2, 15))}
    openings = [a for a in manifest.bundle.inventory_adjustments if a.kind == "opening"]
    assert openings and all(a.adjustment_date == date(2026, 2, 15) for a in openings)


@pytest.mark.asyncio
async def test_mapping_decisions_are_validated_at_function_boundary(client, migration_env):
    from celerp.services.migrations import validate_decisions

    spec = fake_spec(questions=QUESTIONS)
    scan = await _scan_session(spec)
    valid = {"acct:4000": "income", "acct:4000:alt": "income", "tax:vat": "exempt"}

    def errors_for(mappings):
        return _field_errors(lambda: validate_decisions(scan, {"mode": "full_history", "mappings": mappings}))

    assert "acct:9999" in errors_for({**valid, "acct:9999": "income"})
    assert "tax:vat" in errors_for({k: v for k, v in valid.items() if k != "tax:vat"})
    assert "tax:vat" in errors_for({**valid, "tax:vat": 5})
    assert "tax:vat" in errors_for({**valid, "tax:vat": "zero_rated"})
    assert "acct:4000:alt" in errors_for({**valid, "acct:4000:alt": "other_income"})
    assert "mappings" in _field_errors(lambda: validate_decisions(scan, {"mode": "full_history", "mappings": ["x"]}))
    assert validate_decisions(scan, {"mode": "full_history", "mappings": valid}).mappings == valid

    r = await scan_upload(client, fake_bytes(spec))
    scan_token = r.json()["scan_token"]
    r = await save_decisions(client, scan_token, mappings={**valid, "tax:vat": "zero_rated"})
    assert r.status_code == 422 and "tax:vat" in r.json()["detail"]
    r = await save_decisions(client, scan_token, mappings=valid, prepared_by="Example Accountant")
    assert r.status_code == 200, r.text
    saved = r.json()["scan"]["decisions"]
    for _ in range(2):  # Back to Coverage and forward again
        r = await client.post("/migrations/bootstrap/scan/read", json={"scan_token": scan_token})
        assert r.status_code == 200 and r.json()["scan"]["decisions"] == saved
    assert saved["mappings"] == valid


@pytest.mark.asyncio
async def test_migration_run_company_isolation(client, session, migration_env):
    from celerp.models.company import Company
    from celerp.models.migration import MigrationRun

    admin_token = await register_admin(client)
    admin_company_id = (await session.execute(select(Company.id).where(Company.name == "Perm Co"))).scalar_one()
    moved_token, run_id = await migrate_as_owner(client, admin_token)

    r = await client.get("/migrations/not-a-uuid", headers=auth(moved_token))
    assert r.status_code == 422

    # The first company's session cannot see or touch the moved company's run.
    for method, path in (("get", ""), ("get", "/reconciliation"), ("get", "/reconciliation/pack"),
                         ("post", "/start"), ("post", "/cancel"), ("post", "/finalize"), ("post", "/discard")):
        r = await getattr(client, method)(f"/migrations/{run_id}{path}", headers=auth(admin_token))
        assert r.status_code == 404 and r.json() == {"detail": NOT_FOUND}, (path, r.text)
    run = await session.get(MigrationRun, uuid.UUID(run_id))
    await session.refresh(run)
    assert run.status == "running" and run.cancel_requested_at is None

    # A non-owner cannot scan or start a migration.
    manager_token = await _member_token(session, admin_company_id, "admin", "manager@example.com")
    r = await scan_upload(client, fake_bytes(), token=manager_token)
    assert r.status_code == 403 and r.json()["detail"] == OWNER_ONLY
    r = await client.post("/migrations/start-from-scan", headers=auth(manager_token),
                          json={"scan_token": "x" * 43, "company_name": "Nope"})
    assert r.status_code == 403 and r.json()["detail"] == OWNER_ONLY

    # Inside the run's company a non-owner member reads status and verification, never acts.
    company = await session.get(Company, run.company_id)
    company.is_active = True
    run.status = "completed"
    run.reconciliation = {"generated_at": datetime.now(timezone.utc).isoformat(), "rows": [], "blockers": 0}
    await session.commit()
    member_token = await _member_token(session, run.company_id, "admin", "member@example.com")
    assert (await client.get(f"/migrations/{run_id}", headers=auth(member_token))).status_code == 200
    assert (await client.get(f"/migrations/{run_id}/reconciliation", headers=auth(member_token))).status_code == 200
    for action in ("start", "cancel", "finalize", "discard"):
        r = await client.post(f"/migrations/{run_id}/{action}", headers=auth(member_token))
        assert r.status_code == 403 and r.json()["detail"] == OWNER_ONLY, (action, r.text)


@pytest.mark.asyncio
async def test_migration_state_machine_rejects_illegal_transitions(real_client, real_engine, migration_env):
    from celerp.services import migrations

    sink = migration_env["sink"]
    admin_token = await register_admin(real_client)

    # An empty source package is refused before any company or run exists.
    companies = await count(real_engine, "companies")
    r = await scan_upload(real_client, fake_bytes(fake_spec(locations=0, journal=False, coverage=[])), token=admin_token)
    scan_token = r.json()["scan_token"]
    assert (await save_decisions(real_client, scan_token, token=admin_token)).status_code == 200
    r = await real_client.post("/migrations/start-from-scan", headers=auth(admin_token),
                               json={"scan_token": scan_token, "company_name": "Empty Co"})
    assert r.status_code == 422 and r.json()["detail"] == "The source file contains no records to migrate."
    assert await count(real_engine, "companies") == companies
    assert await count(real_engine, "migration_runs") == 0

    token, run_id = await migrate_as_owner(real_client, admin_token)
    headers = auth(token)
    assert (await load_run(real_engine, uuid.UUID(run_id))).status == "running"
    r = await real_client.post(f"/migrations/{run_id}/start", headers=headers)
    assert r.status_code == 409 and r.json()["detail"] == "Cannot start a migration that is running."
    r = await real_client.post(f"/migrations/{run_id}/finalize", headers=headers)
    assert r.status_code == 409 and r.json()["detail"] == "Cannot finalize a migration that is running."

    # Cancel is persisted at once; the runner stops between batches at cancelled.
    cancel_responses = []

    async def cancel_during_first_batch(context, records):
        if not cancel_responses:
            cancel_responses.append(await real_client.post(f"/migrations/{run_id}/cancel", headers=headers))

    sink.on_batch = cancel_during_first_batch
    await migrations.run_migration(uuid.UUID(run_id))
    sink.on_batch = None
    assert cancel_responses[0].status_code == 202
    assert cancel_responses[0].json()["status"] == "cancel_requested"
    run = await load_run(real_engine, uuid.UUID(run_id))
    assert run.status == "cancelled" and run.cancel_requested_at is not None
    assert run.phase_state["company_settings"]["status"] == "done"
    assert run.phase_state.get("contacts_locations", {}).get("status") != "done"
    r = await real_client.post(f"/migrations/{run_id}/cancel", headers=headers)
    assert r.status_code == 409 and r.json()["detail"] == "Cannot cancel a migration that is cancelled."

    # Resume: 202 after the intent is persisted, then the runner finishes.
    scheduled = len(migration_env["scheduled"])
    r = await real_client.post(f"/migrations/{run_id}/start", headers=headers)
    assert r.status_code == 202 and r.json()["status"] == "running"
    assert (await load_run(real_engine, uuid.UUID(run_id))).status == "running"
    assert migration_env["scheduled"][scheduled:] == [uuid.UUID(run_id)]
    await migrations.run_migration(uuid.UUID(run_id))
    assert (await load_run(real_engine, uuid.UUID(run_id))).status == "ready_to_finalize"
    assert await count(real_engine, "locations", "company_id = :c", c=run.company_id) == 3

    # Two concurrent finalize requests activate the company once.
    results = await asyncio.gather(*[real_client.post(f"/migrations/{run_id}/finalize", headers=headers)
                                     for _ in range(2)])
    assert sorted(r.status_code for r in results) == [200, 409]
    assert [r.json()["detail"] for r in results if r.status_code == 409] == [
        "Cannot finalize a migration that is completed."]
    assert await count(real_engine, "companies", "id = :c AND is_active", c=run.company_id) == 1

    # A finished run cannot be restarted, cancelled or discarded, even once the company is deactivated.
    async with real_engine.begin() as conn:
        await conn.execute(text("UPDATE companies SET is_active = false WHERE id = :c"), {"c": run.company_id})
    for action, detail in (("start", "Cannot start a migration that is completed."),
                           ("cancel", "Cannot cancel a migration that is completed."),
                           ("finalize", "Cannot finalize a migration that is completed."),
                           ("discard", "This company has no unfinished migration to discard.")):
        r = await real_client.post(f"/migrations/{run_id}/{action}", headers=headers)
        assert r.status_code == 409 and r.json()["detail"] == detail, (action, r.text)
    assert await count(real_engine, "companies", "id = :c", c=run.company_id) == 1


@pytest.mark.asyncio
async def test_migration_advisory_lock_prevents_double_runner(real_engine, migration_env, monkeypatch):
    from celerp.services import migrations
    from celerp.services.migrations import MigrationError

    sink = migration_env["sink"]
    run_id, company_id, _ = await staged_run(real_engine)
    before = await load_run(real_engine, run_id)

    async with real_engine.connect() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(hashtext('migration:' || :r))"), {"r": str(run_id)})
        await holder.commit()
        await migrations.run_migration(run_id)
        after = await load_run(real_engine, run_id)
        assert (after.status, after.phase_state, after.heartbeat_at) == (before.status, before.phase_state,
                                                                          before.heartbeat_at)
        assert await count(real_engine, "locations", "company_id = :c", c=company_id) == 0
        assert await count(real_engine, "migration_entity_maps") == 0
        assert sink.events == []

        async with real_engine.begin() as conn:
            await conn.execute(text("UPDATE migration_runs SET status = 'failed' WHERE id = :r"), {"r": run_id})
        async with maker(real_engine)() as s:
            run = await migrations.get_run_for_company(s, run_id, company_id)
            with pytest.raises(MigrationError) as exc:
                await migrations.request_start(s, run)
            assert (exc.value.status_code, exc.value.detail) == (409, "Migration is already running.")
        assert (await load_run(real_engine, run_id)).status == "failed"
        await holder.execute(text("SELECT pg_advisory_unlock(hashtext('migration:' || :r))"), {"r": str(run_id)})
        await holder.commit()

    async with real_engine.begin() as conn:
        await conn.execute(text("UPDATE migration_runs SET status = 'running' WHERE id = :r"), {"r": run_id})

    # Every batch transaction takes the company lock before the sink writes anything.
    real_lock = migrations.lock_company

    async def recording_lock(session, cid):
        sink.events.append(("lock_company", cid))
        return await real_lock(session, cid)

    monkeypatch.setattr(migrations, "lock_company", recording_lock)

    async def slow(context, records):
        await asyncio.sleep(0.1)

    sink.on_batch = slow
    await asyncio.gather(migrations.run_migration(run_id), migrations.run_migration(run_id))
    assert (await load_run(real_engine, run_id)).status == "ready_to_finalize"
    assert await count(real_engine, "locations", "company_id = :c", c=company_id) == 3
    imported = [i for kind, ids in sink.events if kind == "import_batch" for i in ids]
    assert len(imported) == len(set(imported)) == 5
    for i, event in enumerate(sink.events):
        if event[0] == "import_batch":
            assert i > 0 and sink.events[i - 1] == ("lock_company", company_id)


@pytest.mark.asyncio
async def test_migration_resume_is_idempotent_after_phase_failure(real_engine, migration_env):
    from celerp.models.migration import MigrationEntityMap
    from celerp.services import migrations

    sink = migration_env["sink"]
    spec = fake_spec(locations=5, coverage=fake_spec()["coverage"][:1] + [
        {"source_type": "Location", "count": 5, "coverage_class": "mapped", "target": "location"},
        {"source_type": "Payment", "count": 1, "coverage_class": "mapped", "target": "settlement"}])

    # A failing batch rolls back entirely and the failure is recorded before any retry.
    sink.fail_on = {"loc-4": 1}
    run_id, company_id, _ = await staged_run(real_engine, spec=spec)
    await migrations.run_migration(run_id)
    run = await load_run(real_engine, run_id)
    assert run.status == "failed"
    assert run.error_summary == {"phase": "contacts_locations", "batch_cursor": 2,
                                 "error_class": "RuntimeError", "message": "Sink failed on loc-4."}
    assert run.phase_state["contacts_locations"]["status"] == "failed"
    assert run.phase_state["contacts_locations"]["cursor"] == 2
    assert await count(real_engine, "locations", "company_id = :c", c=company_id) == 2

    async def resume(rid):
        async with maker(real_engine)() as s:
            run = await migrations.get_run_for_company(s, rid, (await load_run(real_engine, rid)).company_id)
            await migrations.request_start(s, run)
            await s.commit()
        await migrations.run_migration(rid)

    await resume(run_id)
    run = await load_run(real_engine, run_id)
    assert run.status == "ready_to_finalize" and run.error_summary == {}
    assert await count(real_engine, "locations", "company_id = :c", c=company_id) == 5
    async with maker(real_engine)() as s:
        maps = (await s.execute(select(MigrationEntityMap).where(
            MigrationEntityMap.migration_run_id == run_id))).scalars().all()
    by_id = {m.source_external_id: m for m in maps}
    assert len(maps) == len(by_id) == 7
    assert by_id["company"].status == "skipped"
    assert by_id["loc-1"].meta == {"group": "locations", "representation": "native"}
    assert by_id["pay-1:journal"].meta == {"group": "journals", "representation": "journal_fallback"}
    assert f"migration:{run_id}:Location:loc-1:create" in sink.keys

    # A crash after committed batches resumes without duplicates.
    class Crash(BaseException):
        pass

    async def crash_on_last(context, records):
        if any(r.source_external_id == "loc-5" for r in records):
            raise Crash()

    sink.fail_on = {}
    sink.on_batch = crash_on_last
    crashed, crashed_company, _ = await staged_run(real_engine, spec=spec, email="crash@example.com")
    with pytest.raises(Crash):
        await migrations.run_migration(crashed)
    sink.on_batch = None
    assert (await load_run(real_engine, crashed)).status == "running"
    assert await count(real_engine, "locations", "company_id = :c", c=crashed_company) == 4
    async with real_engine.begin() as conn:
        await conn.execute(text("UPDATE migration_runs SET heartbeat_at = :t WHERE id = :r"),
                           {"t": datetime.now(timezone.utc) - timedelta(minutes=10), "r": crashed})
    async with maker(real_engine)() as s:
        assert await migrations.mark_stale_runs_interrupted(s) == 1
    assert (await load_run(real_engine, crashed)).status == "interrupted"
    await resume(crashed)
    assert (await load_run(real_engine, crashed)).status == "ready_to_finalize"
    assert await count(real_engine, "locations", "company_id = :c", c=crashed_company) == 5
    assert await count(real_engine, "migration_entity_maps", "migration_run_id = :r", r=crashed) == 7


@pytest.mark.asyncio
async def test_discard_staged_company_is_complete_or_noop(real_client, real_engine, migration_env):
    from celerp.services import migrations

    admin_token = await register_admin(real_client)
    admin_companies = await count(real_engine, "companies")

    async def discard(token, run_id):
        return await real_client.post(f"/migrations/{run_id}/discard", headers=auth(token))

    # A staged company with nothing imported yet is removed completely.
    token, run_id = await migrate_as_owner(real_client, admin_token, company_name="First Move")
    company_id = (await load_run(real_engine, uuid.UUID(run_id))).company_id
    r = await discard(token, run_id)
    assert r.status_code == 200 and r.json() == {"redirect": "/"}
    assert await count(real_engine, "companies", "id = :c", c=company_id) == 0
    assert await count(real_engine, "migration_runs") == 0
    assert not (migration_env["data_dir"] / "migration_runs" / run_id).exists()
    assert await count(real_engine, "users", "email = 'admin@perm.example'") == 1
    assert await count(real_engine, "companies") == admin_companies

    # Unsafe discards fail closed and leave the staged company intact and inactive.
    token, run_id = await migrate_as_owner(real_client, admin_token, company_name="Second Move")
    rid = uuid.UUID(run_id)
    await migrations.run_migration(rid)
    company_id = (await load_run(real_engine, rid)).company_id

    async def intact():
        assert await count(real_engine, "companies", "id = :c AND NOT is_active", c=company_id) == 1
        assert await count(real_engine, "locations", "company_id = :c", c=company_id) == 3
        assert await count(real_engine, "migration_runs", "id = :r", r=rid) == 1

    async with real_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE mystery_rows (company_id uuid)"))
        await conn.execute(text("INSERT INTO mystery_rows VALUES (:c)"), {"c": company_id})
    try:
        r = await discard(token, run_id)
        assert r.status_code == 409 and "mystery_rows" in r.json()["detail"]
        await intact()
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE mystery_rows"))

    async with real_engine.connect() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(hashtext('migration:' || :r))"), {"r": run_id})
        await holder.commit()
        r = await discard(token, run_id)
        assert r.status_code == 409 and r.json()["detail"] == "Migration is already running."
        await intact()
        await holder.execute(text("SELECT pg_advisory_unlock(hashtext('migration:' || :r))"), {"r": run_id})
        await holder.commit()

    r = await discard(token, run_id)
    assert r.status_code == 200 and r.json() == {"redirect": "/"}
    for table in ("companies", "locations", "user_companies", "ledger", "projections"):
        column = "id" if table == "companies" else "company_id"
        assert await count(real_engine, table, f"{column} = :c", c=company_id) == 0, table
    assert await count(real_engine, "migration_entity_maps") == 0

    # An active or finalized company is never discarded.
    token, run_id = await migrate_as_owner(real_client, admin_token, company_name="Third Move")
    rid = uuid.UUID(run_id)
    await migrations.run_migration(rid)
    await _finalize(real_engine, rid)
    company_id = (await load_run(real_engine, rid)).company_id
    r = await discard(token, run_id)
    assert r.status_code == 409 and r.json()["detail"] == "This company has no unfinished migration to discard."
    assert await count(real_engine, "companies", "id = :c AND is_active", c=company_id) == 1

    # A first-run bootstrap discard returns the install to setup with no owner left.
    async with real_engine.begin() as conn:
        await conn.execute(text("TRUNCATE users, companies RESTART IDENTITY CASCADE"))
    r = await scan_upload(real_client, fake_bytes())
    scan_token = r.json()["scan_token"]
    assert (await save_decisions(real_client, scan_token)).status_code == 200
    r = await real_client.post("/migrations/bootstrap/start", json={
        "scan_token": scan_token, "company_name": "Moved Co", "name": "Owner",
        "email": "owner@example.com", "password": "ownerpw123"})
    assert r.status_code == 201, r.text
    r = await discard(r.json()["access_token"], r.json()["run_id"])
    assert r.status_code == 200 and r.json() == {"redirect": "/setup"}
    assert await count(real_engine, "users") == 0
    assert await count(real_engine, "companies") == 0


@pytest.mark.asyncio
async def test_reconciliation_provider_failure_blocks_finalize(real_engine, migration_env, monkeypatch):
    from celerp.models.company import Company
    from celerp.services import migrations
    from celerp.services.migrations import MigrationError

    sink = migration_env["sink"]

    async def finalize_error(run_id):
        with pytest.raises(MigrationError) as exc:
            await _finalize(real_engine, run_id)
        return exc.value

    async def inactive(company_id):
        async with maker(real_engine)() as s:
            return (await s.get(Company, company_id)).is_active is False

    # A provider error fails closed: the error is shown and no figure is invented.
    sink.reconcile_error = RuntimeError("Ledger measurement failed.")
    run_id, company_id, _ = await staged_run(real_engine)
    await migrations.run_migration(run_id)
    run = await load_run(real_engine, run_id)
    assert run.status == "failed"
    assert run.error_summary["phase"] == "reconciliation"
    assert "Ledger measurement failed." in run.error_summary["message"]
    assert run.reconciliation == {}
    error = await finalize_error(run_id)
    assert (error.status_code, error.detail) == (409, "Cannot finalize a migration that is failed.")
    assert await inactive(company_id)
    sink.reconcile_error = None

    # An unexplained difference fails and shows the differing row.
    run_id, company_id, _ = await staged_run(real_engine, spec=fake_spec(expected_locations=4),
                                             email="diff@example.com")
    await migrations.run_migration(run_id)
    run = await load_run(real_engine, run_id)
    assert run.status == "failed" and run.reconciliation["blockers"] == 1
    row = next(r for r in run.reconciliation["rows"] if r["key"] == "Location")
    assert (row["source"], row["celerp"], row["difference"], row["result"]) == ("4", "3", "-1", "fail")
    assert await inactive(company_id)

    # A category with no data on either side is n-a by an explicit rule; one Celerp cannot
    # measure while the source has a figure fails instead of reading as zero.
    spec = fake_spec(extra_expectations=[{"measure": "bank_cash", "key": "1000", "expected": "0"}])
    run_id, company_id, _ = await staged_run(real_engine, spec=spec, email="na@example.com")
    await migrations.run_migration(run_id)
    run = await load_run(real_engine, run_id)
    assert run.status == "ready_to_finalize", run.error_summary
    na = next(r for r in run.reconciliation["rows"] if r["check"] == "bank_cash")
    assert (na["celerp"], na["result"]) == ("0", "n-a") and na["rule"]
    unmeasured = fake_spec(extra_expectations=[{"measure": "bank_cash", "key": "1000", "expected": "12.50"}])
    other, _, _ = await staged_run(real_engine, spec=unmeasured, email="unmeasured@example.com")
    await migrations.run_migration(other)
    row = next(r for r in (await load_run(real_engine, other)).reconciliation["rows"] if r["check"] == "bank_cash")
    assert (row["celerp"], row["result"]) == (None, "fail")

    # Finalize re-checks verification under lock.
    async with real_engine.begin() as conn:
        await conn.execute(text(
            "DELETE FROM locations WHERE id = (SELECT id FROM locations WHERE company_id = :c LIMIT 1)"),
            {"c": company_id})
    error = await finalize_error(run_id)
    assert error.status_code == 409
    assert error.detail == "Verification no longer matches the source. Resume the migration to re-run it."
    run = await load_run(real_engine, run_id)
    assert run.status == "ready_to_finalize" and run.reconciliation["blockers"] == 1
    assert await inactive(company_id)

    # A database failure during finalize leaves the company inactive and the run staged.
    run_id, company_id, _ = await staged_run(real_engine, email="dbfail@example.com")
    await migrations.run_migration(run_id)

    async def broken(session, cid):
        raise RuntimeError("database went away")

    monkeypatch.setattr(migrations, "add_missing_required_defaults", broken)
    error = await finalize_error(run_id)
    assert error.status_code == 500
    assert (await load_run(real_engine, run_id)).status == "ready_to_finalize"
    assert await inactive(company_id)


@pytest.mark.asyncio
async def test_reconciliation_pack_matches_stored_verification(client, session, migration_env, monkeypatch):
    from celerp.models.migration import MigrationRun
    from celerp.services import migrations

    admin_token = await register_admin(client)
    token, run_id = await migrate_as_owner(client, admin_token, prepared_by="Example Accountant")
    pack = f"/migrations/{run_id}/reconciliation/pack"

    r = await client.get(pack, headers=auth(token))
    assert r.status_code == 409 and r.json()["detail"] == "Verification has not run yet."
    assert (await client.get("/migrations/nope/reconciliation/pack", headers=auth(token))).status_code == 422
    r = await client.get(pack, headers=auth(admin_token))
    assert r.status_code == 404 and r.json()["detail"] == NOT_FOUND

    generated = "2026-04-01T10:00:00+00:00"
    rows = [
        {"check": "document_count", "key": "Location", "currency": None, "source": "3", "celerp": "3",
         "difference": "0", "rule": "exact", "result": "pass"},
        {"check": "ar_by_customer", "key": "=HYPERLINK(\"http://example.invalid\")", "currency": "USD",
         "source": "10.00", "celerp": "10.00", "difference": "0.00", "rule": "exact", "result": "pass"},
    ]
    run = await session.get(MigrationRun, uuid.UUID(run_id))
    run.reconciliation = {"generated_at": generated, "rows": rows, "blockers": 0}
    run.status = "running"  # a verification re-run is in progress
    await session.commit()

    r = await client.get(pack, headers=auth(token))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"] == f'attachment; filename="reconciliation-{run_id}.csv"'
    lines = list(csv.reader(io.StringIO(r.text)))
    header = {line[0]: line[1] for line in lines[:7]}
    assert header == {"Run id": run_id, "Source": "Fake source", "Mode": "Full history", "Cutover date": "--",
                      "Source hash": run.source_artifact_sha256, "Prepared by": "Example Accountant",
                      "Generated at": generated}
    table = lines[lines.index(["Check", "Key", "Currency", "Source", "Celerp", "Difference", "Rule", "Result"]) + 1:]
    assert len(table) == len(rows)
    for line, row in zip(table, rows):
        assert line[0] == row["check"] and line[3:] == [row["source"], row["celerp"], row["difference"],
                                                        row["rule"], row["result"]]
    assert table[1][1].startswith("'=")

    def unreadable(run):
        raise ValueError("stored verification is corrupt")

    monkeypatch.setattr(migrations, "reconciliation_pack_csv", unreadable)
    r = await client.get(pack, headers=auth(token))
    assert r.status_code == 500
    assert r.json() == {"detail": "Could not build the reconciliation pack."}


@pytest.mark.asyncio
async def test_prepared_by_is_validated_and_persisted(client, session, migration_env):
    from celerp.models.migration import MigrationRun
    from celerp.services.migrations import validate_prepared_by

    assert validate_prepared_by("  Example Accountant  ") == "Example Accountant"
    assert validate_prepared_by("   ") is None
    assert validate_prepared_by(None) is None
    for bad in ("x" * 201, "Example\x07Accountant", "Example\nAccountant"):
        assert "prepared_by" in _field_errors(lambda: validate_prepared_by(bad))

    r = await scan_upload(client, fake_bytes())
    scan_token = r.json()["scan_token"]
    r = await save_decisions(client, scan_token, prepared_by="x" * 201)
    assert r.status_code == 422 and "prepared_by" in r.json()["detail"]
    start = {"scan_token": scan_token, "company_name": "Moved Co", "name": "Owner",
             "email": "owner@example.com", "password": "ownerpw123"}
    r = await client.post("/migrations/bootstrap/start", json=start)
    assert r.status_code == 422, r.text
    assert (await session.execute(select(MigrationRun))).scalars().all() == []

    r = await save_decisions(client, scan_token, prepared_by="  Example Accountant ")
    assert r.status_code == 200 and r.json()["scan"]["decisions"]["prepared_by"] == "Example Accountant"
    r = await client.post("/migrations/bootstrap/start", json=start)
    assert r.status_code == 201, r.text
    owner_token = r.json()["access_token"]
    run = await session.get(MigrationRun, uuid.UUID(r.json()["run_id"]))
    assert run.prepared_by == "Example Accountant"
    assert (await client.get(f"/migrations/{run.id}", headers=auth(owner_token))).json()["prepared_by"] == \
        "Example Accountant"

    # The owner is on an inactive staged company; move another company from it with no preparer.
    _, second_run = await migrate_as_owner(client, owner_token, prepared_by="")
    assert (await session.get(MigrationRun, uuid.UUID(second_run))).prepared_by is None


@pytest.mark.asyncio
@pytest.mark.parametrize("names", [("Raced Co", "Raced Co"), ("Raced Co", "Other Co")])
async def test_concurrent_start_from_scan_starts_one_migration(real_client, real_engine, migration_env, names):
    """Two simultaneous starts from one scan create one company and one run; the other is refused."""
    from celerp.services import migrations

    admin_token = await register_admin(real_client)
    companies = await count(real_engine, "companies")
    r = await scan_upload(real_client, fake_bytes(), token=admin_token)
    scan_token = r.json()["scan_token"]
    assert (await save_decisions(real_client, scan_token, token=admin_token)).status_code == 200

    async def start(name):
        return await real_client.post("/migrations/start-from-scan", headers=auth(admin_token),
                                      json={"scan_token": scan_token, "company_name": name})

    results = await asyncio.gather(*(start(name) for name in names))
    assert sorted(r.status_code for r in results) == [201, 409], [r.text for r in results]
    refused = next(r for r in results if r.status_code == 409)
    assert refused.json()["detail"] == migrations.SCAN_ALREADY_STARTED
    assert await count(real_engine, "companies") == companies + 1
    assert await count(real_engine, "migration_runs") == 1


@pytest.mark.asyncio
async def test_concurrent_bootstrap_start_starts_one_migration(real_client, real_engine, migration_env):
    """Two simultaneous first-run starts from one scan create one owner, one company and one run."""
    r = await scan_upload(real_client, fake_bytes())
    scan_token = r.json()["scan_token"]
    assert (await save_decisions(real_client, scan_token)).status_code == 200

    async def start():
        return await real_client.post("/migrations/bootstrap/start", json={
            "scan_token": scan_token, "company_name": "Moved Co", "name": "Owner",
            "email": "owner@example.com", "password": "ownerpw123"})

    results = await asyncio.gather(start(), start())
    assert sorted(r.status_code for r in results) == [201, 409], [r.text for r in results]
    assert await count(real_engine, "users") == 1
    assert await count(real_engine, "companies") == 1
    assert await count(real_engine, "migration_runs") == 1
