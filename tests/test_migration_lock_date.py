# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A Manager accounting lock date moves with the company.

It is read from the source, carried on the scan, the manifest and the run, shown before
the user commits to anything, and installed through Celerp's own period lock only when
the migration finishes: the migration's historical writes are never blocked by it, the
company never becomes normal without it, and after finishing it refuses postings on or
before the date exactly as a lock set in Settings does."""

from __future__ import annotations

import re
import shutil
import uuid
from datetime import date

import pytest
from fasthtml.common import to_xml
from sqlalchemy import select

from fixtures.manager_io import specs
from fixtures.manager_io.encoder import Obj, write_manager_file
from fixtures.manager_io.support import BASIC, artifact
from migration_support import (
    OWNER_EMAIL,
    auth,
    count,
    creator_run,
    load_run,
    maker,
    real_client,  # noqa: F401 - fixture
    real_engine,  # noqa: F401 - fixture
    resume_run,
    upload_parts,
)
from test_helpers import make_authed_token

# Manager's LockDate singleton: field 1 the date, field 2 whether periods are locked.
LOCK_GUID = uuid.UUID("4c5dac8f-2d5e-4634-a51b-0bbdd021a499")
LOCKED_THROUGH = date(2026, 2, 28)


def _locked_book(path, lock: date = LOCKED_THROUGH, effective: bool = True):
    """The basic quarter of trading, with its books locked through *lock*."""
    return write_manager_file(path, [*specs.basic_objects(), Obj(LOCK_GUID, LOCK_GUID, {1: lock, 2: effective})],
                              specs.basic_blobs())


async def _migrate(engine, path, monkeypatch, tmp_path):
    from test_migration_e2e import migrate

    return await migrate(engine, path.read_bytes(), path.name, {"mode": "full_history"}, monkeypatch, tmp_path / "data")


async def _staged_migration(engine, path, entry: str, monkeypatch, tmp_path):
    """Scan, decide and run *path* into a staged company, started either by an owner adding a
    company alongside the one they already run, or by the first owner of a fresh install."""
    from celerp.config import settings
    from celerp.models.company import User
    from celerp.services import migration_scan_store as store
    from celerp.services import migrations, provisioning

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    async with maker(engine)() as s:
        if entry == "bootstrap":
            user = await provisioning.create_install_owner(s, name="Owner", email=OWNER_EMAIL, password="validpass1")
            owner = ("bootstrap", None)
        else:
            user = User(email=OWNER_EMAIL, name="Owner", is_install_owner=True)
            s.add(user)
            await s.flush()
            await provisioning.provision_additional_company(s, user=user, company_name="Existing Co")
            owner = ("user", user.id)
        scan = await store.create_scan(upload_parts((path.name, path.read_bytes())), owner=owner)
        chosen = migrations.validate_decisions(scan, {"mode": "full_history"})
        scan = store.save_decisions(scan.token, owner=owner, decisions=chosen)
        company = await provisioning.provision_migration_company(s, owner=user, company_name=scan.scan.company_name)
        run = await migrations.create_run(s, company=company, user=user, scan=scan, decisions=chosen)
        run_id = run.id
        await s.commit()
        await migrations.claim_source(s, run_id, token=scan.token, start=True)
    await migrations.run_migration(run_id)
    run = await load_run(engine, run_id)
    assert bool(run.source_summary.get("bootstrap")) is (entry == "bootstrap")
    return run


async def _company(engine, company_id):
    from celerp.models.company import Company

    async with maker(engine)() as s:
        return await s.get(Company, company_id)


async def _owner_headers(engine, run) -> dict:
    async with maker(engine)() as s:
        return auth(await make_authed_token(s, str(run.created_by_user_id), str(run.company_id), "owner"))


async def _finalize(client, engine, run):
    return await client.post(f"/migrations/{run.id}/finalize", headers=await _owner_headers(engine, run))


def _assert_unlocked_and_staged(company) -> None:
    assert company.is_migration_staged and not company.is_active
    assert "lock_date" not in (company.settings or {})


def _assert_locked_and_normal(company, run) -> None:
    assert company.is_active and not company.is_migration_staged
    settings = company.settings or {}
    assert settings["lock_date"] == LOCKED_THROUGH.isoformat()
    assert settings["lock_date_set_by"] == str(run.created_by_user_id)
    assert settings["lock_date_set_at"]


async def _post(client, headers, engine, company_id, day: str):
    """A balanced manual entry on *day* between two leaf accounts of the migrated chart."""
    from celerp_accounting.models import Account

    async with maker(engine)() as s:
        accounts = (await s.execute(select(Account).where(Account.company_id == company_id))).scalars().all()
    parents = {a.parent_code for a in accounts}
    leaves = [a for a in accounts if a.code not in parents and a.is_active]
    debit = next(a.code for a in leaves if a.account_type == "expense")
    credit = next(a.code for a in leaves if a.account_type == "revenue")
    return await client.post("/accounting/journal-entries", headers=headers, json={
        "ts": day, "memo": "Adjustment", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": debit, "debit": 5, "credit": 0}, {"account": credit, "debit": 0, "credit": 5}],
    })


async def _finalized(client, engine, monkeypatch, tmp_path):
    run, rejected = await _migrate(engine, _locked_book(tmp_path / "locked.manager"), monkeypatch, tmp_path)
    assert rejected == [] and run.status == "ready_to_finalize", run.error_summary
    r = await _finalize(client, engine, run)
    assert r.status_code == 200, r.text
    return run, await _owner_headers(engine, run)


def test_manager_lock_date_decoded_and_carried(tmp_path):
    """The source's lock date is read from the file, onto the scan and into the manifest;
    a lock that is switched off in the source, or absent, carries no date."""
    from celerp.importers.adapters.base import MigrationDecisions
    from celerp.importers.adapters.manager_io import ManagerIOAdapter
    from celerp.importers.schema import CIFMode

    locked = _locked_book(tmp_path / "locked.manager")
    switched_off = _locked_book(tmp_path / "off.manager", effective=False)
    adapter, decisions = ManagerIOAdapter(), MigrationDecisions(mode=CIFMode.FULL_HISTORY)
    for path, expected in ((locked, LOCKED_THROUGH), (switched_off, None), (BASIC, None)):
        assert adapter.inspect([artifact(path)]).lock_date == expected
        assert adapter.build_manifest([artifact(path)], decisions).lock_date == expected


def _lock_row(scan_or_manifest):
    (row,) = [r for r in scan_or_manifest.coverage if r.source_type.startswith("LockDate")]
    return row


def test_manager_lock_date_read_whatever_the_case_of_its_type(tmp_path):
    """RED before the change: the lock date was looked up by its type in lower case only,
    while every other object is classified case-blind, so a file storing the type in upper
    case showed the lock as carried yet installed none.

    Manager's type and key GUIDs name the same object in either case; the lock through
    2026-02-28 is carried on the scan and the manifest either way."""
    import sqlite3

    from celerp.importers.adapters.base import MigrationDecisions
    from celerp.importers.adapters.manager_io import ManagerIOAdapter
    from celerp.importers.schema import CIFMode

    path = _locked_book(tmp_path / "upper.manager")
    with sqlite3.connect(path) as conn:
        changed = conn.execute("UPDATE Objects SET ContentType = upper(ContentType), Key = upper(Key) "
                               "WHERE lower(ContentType) = ?", (str(LOCK_GUID),)).rowcount
    assert changed == 1
    adapter = ManagerIOAdapter()
    scan = adapter.inspect([artifact(path)])
    manifest = adapter.build_manifest([artifact(path)], MigrationDecisions(mode=CIFMode.FULL_HISTORY))
    for carried in (scan, manifest):
        assert carried.lock_date == date(2026, 2, 28)
        row = _lock_row(carried)
        assert (row.source_type, row.count, row.coverage_class.value) == ("LockDate", 1, "mapped")
        assert row.note == "Installed as the company lock date when the migration finishes."


def test_manager_lock_switched_off_previews_no_lock_date(tmp_path):
    """RED before the change: with locking switched off in Manager no lock date is carried,
    yet the preview still said one would be installed when the migration finishes.

    The preview says plainly that no lock date is installed, and promises no install."""
    from celerp.importers.adapters.base import MigrationDecisions
    from celerp.importers.adapters.manager_io import ManagerIOAdapter
    from celerp.importers.schema import CIFMode

    path = _locked_book(tmp_path / "off.manager", effective=False)
    adapter = ManagerIOAdapter()
    scan = adapter.inspect([artifact(path)])
    manifest = adapter.build_manifest([artifact(path)], MigrationDecisions(mode=CIFMode.FULL_HISTORY))
    for carried in (scan, manifest):
        assert carried.lock_date is None
        row = _lock_row(carried)
        assert row.source_type == "LockDate" and row.target is None
        assert "Installed" not in row.note
        assert "no lock date is installed" in row.note


def test_manager_unreadable_lock_date_blocks_the_scan(tmp_path):
    """RED before the change: a file with two lock dates stopped the scan outright, so the
    preview could not show which record was at fault.

    Two LockDate objects cannot say which lock the user set: the scan names LockDate as a
    blocker and the migration is refused rather than guessing or dropping the lock."""
    from celerp.importers.adapters.base import MigrationDecisions, ScanError
    from celerp.importers.adapters.manager_io import ManagerIOAdapter
    from celerp.importers.schema import CIFMode

    second = Obj(uuid.UUID("00000000-0000-4000-8000-00000000a499"), LOCK_GUID, {1: date(2026, 1, 31), 2: True})
    path = write_manager_file(tmp_path / "twice.manager", [
        *specs.basic_objects(), Obj(LOCK_GUID, LOCK_GUID, {1: LOCKED_THROUGH, 2: True}), second], specs.basic_blobs())
    adapter = ManagerIOAdapter()
    blockers = [r for r in adapter.inspect([artifact(path)]).coverage if r.coverage_class.value ==
                "unsupported_financial_blocker"]
    assert [(r.source_type, r.count) for r in blockers] == [("LockDate (unreadable)", 1)]
    with pytest.raises(ScanError, match=re.escape("LockDate (unreadable) (1)")):
        adapter.build_manifest([artifact(path)], MigrationDecisions(mode=CIFMode.FULL_HISTORY))


async def test_migration_preview_and_summary_show_lock_date(real_engine, monkeypatch, tmp_path):
    """The scan preview, the stored scan, the run, the page shown before finishing and the
    reconciliation pack all state the source's lock date."""
    from starlette.requests import Request

    from celerp.models.company import User
    from celerp.models.migration import MigrationRun
    from celerp.services import migration_scan_store as store
    from celerp.services import migrations
    from ui.routes import migrations as pages

    locked = _locked_book(tmp_path / "locked.manager")
    monkeypatch.setattr("celerp.config.settings.data_dir", tmp_path / "scans")
    async with maker(real_engine)() as s:
        user = User(email="preview@example.com", name="Owner")
        s.add(user)
        await s.commit()
    owner = ("user", user.id)
    scan = await store.create_scan(upload_parts((locked.name, locked.read_bytes())), owner=owner)
    assert scan.scan.lock_date == LOCKED_THROUGH
    assert store.load_scan(scan.token, owner=owner).scan.lock_date == LOCKED_THROUGH
    view = migrations.scan_view(scan)
    assert view["lock_date"] == LOCKED_THROUGH.isoformat()
    preview = to_xml(pages._summary(view))
    assert pages.t("migration.lock_date") in preview and LOCKED_THROUGH.isoformat() in preview

    run, _ = await _migrate(real_engine, locked, monkeypatch, tmp_path)
    assert run.source_lock_date == LOCKED_THROUGH
    async with maker(real_engine)() as s:
        run = await s.get(MigrationRun, run.id)
        run_view = await migrations.run_view(s, run)
    assert run_view["lock_date"] == LOCKED_THROUGH.isoformat()
    assert f"Lock date,{LOCKED_THROUGH.isoformat()}" in migrations.reconciliation_pack_csv(run)

    async def reconciliation(token, run_id):
        return run.reconciliation

    async def get_run(token, run_id):
        return run_view

    monkeypatch.setattr(pages.api, "migration_reconciliation", reconciliation)
    monkeypatch.setattr(pages.api, "get_migration_run", get_run)
    request = Request({"type": "http", "method": "GET", "path": f"/migrations/{run.id}/verify",
                       "query_string": b"", "headers": []})
    page = to_xml(await pages._verify_page(request, str(run.id)))
    assert pages.t("migration.lock_date_notice", date=LOCKED_THROUGH.isoformat()) in page


async def test_migration_writes_pre_lock_history(real_engine, monkeypatch, tmp_path):
    """Every historical record, most of them dated on or before the source's lock date,
    is written and verified while the company is staged, with no lock installed yet."""
    run, rejected = await _migrate(real_engine, _locked_book(tmp_path / "locked.manager"), monkeypatch, tmp_path)
    assert rejected == []
    assert run.status == "ready_to_finalize", run.error_summary
    assert run.reconciliation["blockers"] == 0
    assert run.source_lock_date == LOCKED_THROUGH
    _assert_unlocked_and_staged(await _company(real_engine, run.company_id))


async def test_finalize_installs_lock_date_after_reconciliation(real_client, real_engine, monkeypatch, tmp_path):
    """Finishing installs the source's lock date through the period lock, recorded as set by
    the user who ran the migration, in the same step that makes the company normal."""
    run, rejected = await _migrate(real_engine, _locked_book(tmp_path / "locked.manager"), monkeypatch, tmp_path)
    assert rejected == [] and run.status == "ready_to_finalize", run.error_summary
    _assert_unlocked_and_staged(await _company(real_engine, run.company_id))
    r = await _finalize(real_client, real_engine, run)
    assert r.status_code == 200, r.text
    run = await load_run(real_engine, run.id)
    assert run.status == "completed"
    _assert_locked_and_normal(await _company(real_engine, run.company_id), run)
    r = await real_client.get("/accounting/period-lock", headers=await _owner_headers(real_engine, run))
    assert r.status_code == 200 and r.json()["lock_date"] == LOCKED_THROUGH.isoformat()


async def test_failed_finalize_installs_no_lock_date(real_client, real_engine, monkeypatch, tmp_path):
    """A finish refused by verification, or one whose commit fails, leaves the company
    staged with no lock date, so the company is never normal without its lock."""
    from celerp.services import migrations
    from celerp.services.migrations import MigrationError

    run, rejected = await _migrate(real_engine, _locked_book(tmp_path / "locked.manager"), monkeypatch, tmp_path)
    assert rejected == [] and run.status == "ready_to_finalize", run.error_summary

    real_verification = migrations._verification

    async def mismatch(session, run_):
        report = await real_verification(session, run_)
        return {**report, "blockers": 1}

    monkeypatch.setattr(migrations, "_verification", mismatch)
    r = await _finalize(real_client, real_engine, run)
    assert r.status_code == 409, r.text
    _assert_unlocked_and_staged(await _company(real_engine, run.company_id))
    monkeypatch.setattr(migrations, "_verification", real_verification)

    async with maker(real_engine)() as s:
        async def lost_commit():
            raise ConnectionError("connection lost before the commit")

        s.commit = lost_commit
        with pytest.raises(MigrationError) as failed:
            await migrations.finalize(s, await creator_run(s, run.id))
    assert failed.value.status_code == 500
    assert (await load_run(real_engine, run.id)).status == "ready_to_finalize"
    _assert_unlocked_and_staged(await _company(real_engine, run.company_id))

    # Only a finish that succeeds installs the lock, together with making the company normal.
    r = await _finalize(real_client, real_engine, run)
    assert r.status_code == 200, r.text
    _assert_locked_and_normal(await _company(real_engine, run.company_id), run)


async def test_finalize_retry_keeps_lock_date(real_client, real_engine, monkeypatch, tmp_path):
    """A finish that failed is retried and installs the same date, not a shifted or missing one."""
    from celerp.services import migrations

    run, rejected = await _migrate(real_engine, _locked_book(tmp_path / "locked.manager"), monkeypatch, tmp_path)
    assert rejected == [] and run.status == "ready_to_finalize", run.error_summary
    real_defaults = migrations.add_missing_required_defaults
    failures = [RuntimeError("defaults could not be written")]

    async def fail_once(session, company_id):
        if failures:
            raise failures.pop()
        return await real_defaults(session, company_id)

    monkeypatch.setattr(migrations, "add_missing_required_defaults", fail_once)
    r = await _finalize(real_client, real_engine, run)
    assert r.status_code == 500, r.text
    _assert_unlocked_and_staged(await _company(real_engine, run.company_id))
    assert (await load_run(real_engine, run.id)).source_lock_date == LOCKED_THROUGH

    r = await _finalize(real_client, real_engine, run)
    assert r.status_code == 200, r.text
    run = await load_run(real_engine, run.id)
    _assert_locked_and_normal(await _company(real_engine, run.company_id), run)


async def test_resume_keeps_lock_date(real_client, real_engine, monkeypatch, tmp_path):
    """A run stopped part way keeps its lock date; a resume against a source whose lock date
    has changed is refused rather than shifting it; the resumed run installs the original."""
    from celerp.services import migration_core_sink
    from celerp.services import migration_scan_store as store

    real_import = migration_core_sink._import_attachment
    failures = [RuntimeError("storage unavailable")]

    async def fail_once(context, record):
        if failures:
            raise failures.pop()
        return await real_import(context, record)

    monkeypatch.setattr(migration_core_sink, "_import_attachment", fail_once)
    run, _ = await _migrate(real_engine, _locked_book(tmp_path / "locked.manager"), monkeypatch, tmp_path)
    assert run.status == "failed" and run.error_summary["phase"] == "attachments", run.error_summary
    assert run.source_lock_date == LOCKED_THROUGH
    _assert_unlocked_and_staged(await _company(real_engine, run.company_id))

    stored = store.run_dir(run.id) / run.source_summary["artifacts"][0]["name"]
    original = shutil.copyfile(stored, tmp_path / "original.manager")
    _locked_book(stored, lock=date(2026, 3, 31))
    await resume_run(real_engine, run.id)
    shifted = await load_run(real_engine, run.id)
    assert shifted.status == "failed"
    assert shifted.source_lock_date == LOCKED_THROUGH
    assert shifted.phase_state["attachments"]["cursor"] == run.phase_state["attachments"]["cursor"]

    shutil.copyfile(original, stored)
    await resume_run(real_engine, run.id)
    run = await load_run(real_engine, run.id)
    assert run.status == "ready_to_finalize", run.error_summary
    assert run.source_lock_date == LOCKED_THROUGH
    r = await _finalize(real_client, real_engine, run)
    assert r.status_code == 200, r.text
    _assert_locked_and_normal(await _company(real_engine, run.company_id), await load_run(real_engine, run.id))


async def test_post_finalize_posting_on_or_before_lock_refused(real_client, real_engine, monkeypatch, tmp_path):
    """After finishing, a posting dated on or before the source's lock date is refused under
    Celerp's own period-lock rule."""
    run, headers = await _finalized(real_client, real_engine, monkeypatch, tmp_path)
    for day in (LOCKED_THROUGH.isoformat(), "2026-02-01"):
        r = await _post(real_client, headers, real_engine, run.company_id, day)
        assert r.status_code == 422, r.text
        assert f"Period is locked through {LOCKED_THROUGH.isoformat()}" in r.json()["detail"]


async def test_post_finalize_posting_after_lock_allowed(real_client, real_engine, monkeypatch, tmp_path):
    """After finishing, with the lock installed, a posting dated the day after the lock date is accepted."""
    run, headers = await _finalized(real_client, real_engine, monkeypatch, tmp_path)
    _assert_locked_and_normal(await _company(real_engine, run.company_id), run)
    r = await _post(real_client, headers, real_engine, run.company_id, "2026-03-01")
    assert r.status_code == 200, r.text


ENTRIES = pytest.mark.parametrize("entry", ["additional", "bootstrap"])


@ENTRIES
async def test_finalize_failure_between_activation_and_lock_install_leaves_company_staged(
        entry, real_client, real_engine, monkeypatch, tmp_path):
    """A finish that fails while installing the lock date leaves the company staged, inactive
    and unlocked, never normal without its lock; retrying it installs the lock and activates."""
    from celerp.services import migrations

    run = await _staged_migration(real_engine, _locked_book(tmp_path / "locked.manager"), entry, monkeypatch, tmp_path)
    assert run.status == "ready_to_finalize", run.error_summary
    real_install = migrations.write_period_lock

    def failing_install(company, lock_date, user_id):
        raise RuntimeError("the lock date could not be written")

    monkeypatch.setattr(migrations, "write_period_lock", failing_install)
    r = await _finalize(real_client, real_engine, run)
    assert r.status_code == 500, r.text
    assert (await load_run(real_engine, run.id)).status == "ready_to_finalize"
    _assert_unlocked_and_staged(await _company(real_engine, run.company_id))

    monkeypatch.setattr(migrations, "write_period_lock", real_install)
    r = await _finalize(real_client, real_engine, run)
    assert r.status_code == 200, r.text
    run = await load_run(real_engine, run.id)
    assert run.status == "completed"
    _assert_locked_and_normal(await _company(real_engine, run.company_id), run)


@ENTRIES
async def test_finalize_installs_lock_date_via_canonical_settings_operation(
        entry, real_client, real_engine, monkeypatch, tmp_path):
    """Finishing installs the lock through the same operation the Settings period-lock route
    uses, and leaves exactly what that route leaves for the same date and user."""
    from celerp.events import engine as events
    from celerp.services import migrations
    from celerp_accounting import routes as accounting

    installs = []

    def spy(company, lock_date, user_id):
        installs.append((company.id, lock_date, str(user_id)))
        return events.write_period_lock(company, lock_date, user_id)

    monkeypatch.setattr(migrations, "write_period_lock", spy)
    monkeypatch.setattr(accounting, "write_period_lock", spy)

    run = await _staged_migration(real_engine, _locked_book(tmp_path / "locked.manager"), entry, monkeypatch, tmp_path)
    assert run.status == "ready_to_finalize", run.error_summary
    assert installs == []
    r = await _finalize(real_client, real_engine, run)
    assert r.status_code == 200, r.text
    run = await load_run(real_engine, run.id)
    expected = (run.company_id, LOCKED_THROUGH.isoformat(), str(run.created_by_user_id))
    assert installs == [expected]
    _assert_locked_and_normal(await _company(real_engine, run.company_id), run)
    headers = await _owner_headers(real_engine, run)
    finished = (await real_client.get("/accounting/period-lock", headers=headers)).json()

    # The Settings route, given the same date by the same user, records the same fields and
    # writes nothing to the ledger, so the finish has no other effect to reproduce.
    ledger = await count(real_engine, "ledger", "company_id = :c", c=str(run.company_id))
    r = await real_client.post("/accounting/period-lock", headers=headers,
                               json={"lock_date": LOCKED_THROUGH.isoformat()})
    assert r.status_code == 200, r.text
    assert installs == [expected, expected]
    assert await count(real_engine, "ledger", "company_id = :c", c=str(run.company_id)) == ledger
    by_route = (await real_client.get("/accounting/period-lock", headers=headers)).json()
    assert set(finished) == set(by_route)
    assert {k: v for k, v in finished.items() if k != "lock_date_set_at"} == \
        {k: v for k, v in by_route.items() if k != "lock_date_set_at"}
    assert finished["lock_date_set_at"] and by_route["lock_date_set_at"]


@ENTRIES
async def test_reconciliation_failure_installs_no_lock_date(entry, real_client, real_engine, monkeypatch, tmp_path):
    """A run whose reconciliation does not match the source stops with the company staged and
    unlocked, and cannot be finished; once a resumed run reconciles, finishing installs the lock."""
    from celerp.services import migrations

    real_verification = migrations._verification

    async def mismatch(session, run_):
        report = await real_verification(session, run_)
        return {**report, "blockers": 1}

    monkeypatch.setattr(migrations, "_verification", mismatch)
    run = await _staged_migration(real_engine, _locked_book(tmp_path / "locked.manager"), entry, monkeypatch, tmp_path)
    assert run.status == "failed" and run.error_summary["phase"] == "reconciliation", run.error_summary
    assert run.source_lock_date == LOCKED_THROUGH
    _assert_unlocked_and_staged(await _company(real_engine, run.company_id))
    r = await _finalize(real_client, real_engine, run)
    assert r.status_code == 409, r.text
    _assert_unlocked_and_staged(await _company(real_engine, run.company_id))

    monkeypatch.setattr(migrations, "_verification", real_verification)
    await resume_run(real_engine, run.id)
    run = await load_run(real_engine, run.id)
    assert run.status == "ready_to_finalize", run.error_summary
    _assert_unlocked_and_staged(await _company(real_engine, run.company_id))
    r = await _finalize(real_client, real_engine, run)
    assert r.status_code == 200, r.text
    _assert_locked_and_normal(await _company(real_engine, run.company_id), await load_run(real_engine, run.id))
