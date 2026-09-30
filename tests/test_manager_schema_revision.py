# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Manager file format revision: the adapter reads only the revisions it was built and
tested against, and refuses every other one on every entry into the file, with a
message that tells the user what to do."""

from __future__ import annotations

import shutil
import sqlite3

import pytest

from fixtures.manager_io import specs
from fixtures.manager_io.encoder import SCHEMA_GUID, write_manager_file
from fixtures.manager_io.support import BASIC, adapter, artifact, ref
from migration_support import (
    load_run,
    maker,
    migration_env,  # noqa: F401 - fixture
    real_client,  # noqa: F401 - fixture
    real_engine,  # noqa: F401 - fixture
    resume_run,
    scan_upload,
)

# The revision the synthetic fixtures are written at: the one the adapter was built against.
TESTED_REVISION = 419


def _book(path, revision: int):
    return write_manager_file(path, specs.basic_objects(), specs.basic_blobs(), schema_version=revision)


def _entries(path):
    """Every adapter entry that opens the file, as (name, call)."""
    from celerp.importers.adapters.base import MigrationDecisions
    from celerp.importers.schema import CIFMode

    manager, artifacts = adapter(), [artifact(path)]
    decisions = MigrationDecisions(mode=CIFMode.FULL_HISTORY)
    return [
        ("detect", lambda: manager.detect(artifacts)),
        ("inspect", lambda: manager.inspect(artifacts)),
        ("build_manifest", lambda: manager.build_manifest(artifacts, decisions)),
        ("source_expectations", lambda: manager.source_expectations(artifacts, decisions)),
        ("read_attachment", lambda: manager.read_attachment(artifacts, ref("ATT1"))),
    ]


def _refused_everywhere(path) -> list[str]:
    """The refusal message of every entry; each entry must refuse, none may read the file."""
    from celerp.importers.adapters.base import SourceRevisionError

    messages = []
    for name, call in _entries(path):
        with pytest.raises(SourceRevisionError) as refused:
            call()
        messages.append(str(refused.value))
        assert str(refused.value), name
    assert len(set(messages)) == 1, messages
    return messages


def _accepted_everywhere(path, revision: int) -> None:
    from celerp.importers.adapters.manager_io.sqlite_reader import ManagerReader

    for name, call in _entries(path):
        result = call()
        if name == "detect":
            assert result.matched is True
        elif name == "inspect":
            assert result.source_schema_version == str(revision)
        elif name == "build_manifest":
            assert result.source_schema_version == str(revision)
        elif name == "read_attachment":
            assert result == specs.PNG
    with ManagerReader(path) as reader:
        assert reader.schema_version == revision


async def _scan_refusals(client, path) -> set[str]:
    """The scan route's answer when the user names the source, and when Celerp detects it."""
    details = set()
    for source in ("manager_io", None):
        r = await scan_upload(client, path.read_bytes(), source=source, name=path.name)
        assert r.status_code == 422, r.text
        details.add(r.json()["detail"])
    return details


def test_manager_supported_revision_accepted(tmp_path):
    """The revision the adapter is built against is the declared supported one and reads on every entry."""
    from celerp.importers.adapters.manager_io import sqlite_reader

    assert sqlite_reader.SUPPORTED_SCHEMA_MIN <= TESTED_REVISION <= sqlite_reader.SUPPORTED_SCHEMA_MAX
    _accepted_everywhere(_book(tmp_path / "current.manager", TESTED_REVISION), TESTED_REVISION)
    _accepted_everywhere(BASIC, TESTED_REVISION)


def test_manager_oldest_supported_revision_accepted(tmp_path):
    """The oldest revision of the supported range reads on every entry."""
    from celerp.importers.adapters.manager_io import sqlite_reader

    oldest = sqlite_reader.SUPPORTED_SCHEMA_MIN
    _accepted_everywhere(_book(tmp_path / "oldest.manager", oldest), oldest)


async def test_manager_newer_revision_refused(client, migration_env, tmp_path):
    """A file saved by a newer Manager is refused on every entry, never optimistically decoded."""
    newer = _book(tmp_path / "newer.manager", TESTED_REVISION + 1)
    far_newer = _book(tmp_path / "far.manager", TESTED_REVISION + 40)
    for path in (newer, far_newer):
        message = _refused_everywhere(path)[0]
        assert "newer version of Manager" in message
        assert await _scan_refusals(client, path) == {message}


async def test_manager_older_revision_refused_with_upgrade_instruction(client, migration_env, tmp_path):
    """The revision just below the supported range is refused with the steps that upgrade it."""
    from celerp.importers.adapters.manager_io import sqlite_reader

    older = _book(tmp_path / "older.manager", sqlite_reader.SUPPORTED_SCHEMA_MIN - 1)
    message = _refused_everywhere(older)[0]
    assert "older version of Manager" in message
    assert "Open it in the latest version of Manager" in message
    assert "upload" in message
    assert await _scan_refusals(client, older) == {message}


async def test_manager_missing_or_unknown_revision_refused(client, migration_env, tmp_path):
    """A file with no revision is not read as Manager; one whose revision cannot be read is refused."""
    from celerp.importers.adapters.base import ScanError
    from celerp.importers.adapters.manager_io.sqlite_reader import NOT_MANAGER

    missing = _book(tmp_path / "missing.manager", TESTED_REVISION)
    with sqlite3.connect(missing) as conn:
        conn.execute('DELETE FROM "Objects" WHERE "Key" = ?', (str(SCHEMA_GUID),))
    for name, call in _entries(missing):
        if name == "detect":
            assert call().matched is False
        else:
            with pytest.raises(ScanError, match=NOT_MANAGER):
                call()

    messages = set()
    for label, content in (("garbled", b"\xff\xff\xff"), ("zero", b""), ("text", b"\x0a\x03abc")):
        unknown = shutil.copyfile(missing, tmp_path / f"{label}.manager")
        with sqlite3.connect(unknown) as conn:
            conn.execute('INSERT INTO "Objects" VALUES (?, ?, ?, 0)', (str(SCHEMA_GUID), str(SCHEMA_GUID), content))
        messages.add(_refused_everywhere(unknown)[0])
        assert await _scan_refusals(client, unknown) == messages
    assert len(messages) == 1
    assert "format version" in messages.pop()


async def test_manager_revision_enforced_on_every_adapter_entry(real_client, real_engine, monkeypatch, tmp_path):
    """Every entry refuses an unsupported revision: the adapter entries, both scan routes,
    and a runner resuming a stopped run whose unchanged source is at a revision this Celerp
    no longer supports."""
    from celerp.importers.adapters.manager_io import sqlite_reader
    from celerp.services import migration_core_sink, migrations
    from celerp.services import migration_scan_store as store
    from test_migration_e2e import migrate

    for revision in (sqlite_reader.SUPPORTED_SCHEMA_MIN - 1, sqlite_reader.SUPPORTED_SCHEMA_MAX + 1):
        path = _book(tmp_path / f"r{revision}.manager", revision)
        message = _refused_everywhere(path)[0]
        assert await _scan_refusals(real_client, path) == {message}

    # Stop a real Manager run at its attachments, then resume it after the supported revisions move on.
    real_import = migration_core_sink._import_attachment
    failures = [RuntimeError("storage unavailable")]

    async def fail_once(context, record):
        if failures:
            raise failures.pop()
        return await real_import(context, record)

    monkeypatch.setattr(migration_core_sink, "_import_attachment", fail_once)
    run, _ = await migrate(real_engine, BASIC.read_bytes(), "basic.manager", {"mode": "full_history"},
                           monkeypatch, tmp_path / "data")
    assert run.status == "failed" and run.error_summary["phase"] == "attachments", run.error_summary
    stored = store.run_dir(run.id) / run.source_summary["artifacts"][0]["name"]
    monkeypatch.setattr(sqlite_reader, "SUPPORTED_SCHEMA_MIN", TESTED_REVISION + 1)
    monkeypatch.setattr(sqlite_reader, "SUPPORTED_SCHEMA_MAX", TESTED_REVISION + 1)
    message = _refused_everywhere(stored)[0]

    await resume_run(real_engine, run.id)
    resumed = await load_run(real_engine, run.id)
    assert resumed.status == "failed"
    assert resumed.error_summary["message"] == message
    assert resumed.phase_state["attachments"]["cursor"] == run.phase_state["attachments"]["cursor"]
    async with maker(real_engine)() as s:
        assert await migrations._unmapped(s, run.id, [("Attachment", ref("ATT1"))]) == [("Attachment", ref("ATT1"))]
