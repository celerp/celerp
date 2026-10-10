# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A run's claimed source file is checked against its recorded size and hash every time
the run reads it again: on resume and at the final verification."""

from __future__ import annotations

import pytest

from fixtures.manager_io.support import BASIC
from ui.i18n import t
from migration_support import (
    load_run,
    migration_env,  # noqa: F401 - fixture
    real_client,  # noqa: F401 - fixture
    real_engine,  # noqa: F401 - fixture
    resume_run,
)

pytestmark = pytest.mark.asyncio

CHANGED = t("migration.err_source_changed", "en")


def _stored(run):
    from celerp.services import migration_scan_store as store

    return store.run_dir(run.id) / run.source_summary["artifacts"][0]["name"]


def _alter_keeping_size(path) -> None:
    """Change the SQLite application id: same size, still a readable business file."""
    data = bytearray(path.read_bytes())
    data[68:72] = b"\x00\x00\x00\x01" if data[68:72] != b"\x00\x00\x00\x01" else b"\x00\x00\x00\x02"
    path.write_bytes(bytes(data))


async def test_resume_refuses_a_changed_source_before_writing(real_engine, monkeypatch, tmp_path):
    """RED before the change: the resume only checks that the file exists, so it goes on to
    import the attachment from a source that is no longer the one scanned."""
    from celerp.services import migration_core_sink
    from test_migration_e2e import migrate

    real_import = migration_core_sink._import_attachment
    calls: list[object] = []

    async def fail_once(context, record):
        calls.append(record)
        if len(calls) == 1:
            raise RuntimeError("storage unavailable")
        return await real_import(context, record)

    monkeypatch.setattr(migration_core_sink, "_import_attachment", fail_once)
    run, _ = await migrate(real_engine, BASIC.read_bytes(), "basic.manager", {"mode": "full_history"},
                           monkeypatch, tmp_path / "data")
    assert run.status == "failed" and run.error_summary["phase"] == "attachments", run.error_summary
    stored = _stored(run)
    size = stored.stat().st_size
    _alter_keeping_size(stored)
    assert stored.stat().st_size == size

    await resume_run(real_engine, run.id)
    resumed = await load_run(real_engine, run.id)
    assert resumed.status == "failed"
    assert resumed.error_summary["message"] == CHANGED
    assert len(calls) == 1
    assert resumed.phase_state["attachments"]["cursor"] == run.phase_state["attachments"]["cursor"]
    assert resumed.source_artifact_sha256 == run.source_artifact_sha256


async def test_finalize_refuses_a_changed_source(real_client, real_engine, monkeypatch, tmp_path):
    """RED before the change: finalize measures the changed file and still reports the
    hash of the one that was scanned."""
    from test_migration_e2e import migrate
    from test_migration_lock_date import _finalize

    run, rejected = await migrate(real_engine, BASIC.read_bytes(), "basic.manager", {"mode": "full_history"},
                                  monkeypatch, tmp_path / "data")
    assert rejected == [] and run.status == "ready_to_finalize", run.error_summary
    _alter_keeping_size(_stored(run))

    r = await _finalize(real_client, real_engine, run)
    assert r.status_code != 200 and CHANGED in r.text, r.text
    after = await load_run(real_engine, run.id)
    assert after.status != "completed"
    assert after.source_artifact_sha256 == run.source_artifact_sha256


async def test_source_is_hashed_off_the_event_loop(real_client, real_engine, monkeypatch, tmp_path):
    """RED before the change: the run and its finalization hashed the source, up to the
    upload limit in size, on the event loop, stalling every other request meanwhile."""
    import asyncio

    from celerp.services import migration_scan_store as store
    from test_migration_e2e import migrate
    from test_migration_lock_date import _finalize

    real_hash = store.file_sha256
    calls: list[tuple[object, bool]] = []

    def recording(path):
        try:
            asyncio.get_running_loop()
            calls.append((path, True))
        except RuntimeError:
            calls.append((path, False))
        return real_hash(path)

    monkeypatch.setattr(store, "file_sha256", recording)
    run, rejected = await migrate(real_engine, BASIC.read_bytes(), "basic.manager", {"mode": "full_history"},
                                  monkeypatch, tmp_path / "data")
    assert rejected == [] and run.status == "ready_to_finalize", run.error_summary
    assert (await _finalize(real_client, real_engine, run)).status_code == 200
    on_loop = [loop for path, loop in calls if store.run_dir(run.id) in path.parents]
    assert on_loop and not any(on_loop), calls
