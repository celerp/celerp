# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A Manager accounting lock date moves with the company.

It is read from the source, carried on the scan, the manifest and the run, shown before
the user commits to anything, and installed through Celerp's own period lock only when
the migration finishes: the migration's historical writes are never blocked by it, the
company never becomes normal without it, and after finishing it refuses postings on or
before the date exactly as a lock set in Settings does."""

from __future__ import annotations

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
    auth,
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
    from celerp.importers.adapters.manager_io.lock_date import read_lock_date
    from celerp.importers.adapters.manager_io.sqlite_reader import ManagerReader
    from celerp.importers.schema import CIFMode

    locked = _locked_book(tmp_path / "locked.manager")
    switched_off = _locked_book(tmp_path / "off.manager", effective=False)
    adapter, decisions = ManagerIOAdapter(), MigrationDecisions(mode=CIFMode.FULL_HISTORY)
    for path, expected in ((locked, LOCKED_THROUGH), (switched_off, None), (BASIC, None)):
        with ManagerReader(path) as reader:
            assert read_lock_date(reader) == expected
        assert adapter.inspect([artifact(path)]).lock_date == expected
        assert adapter.build_manifest([artifact(path)], decisions).lock_date == expected


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
