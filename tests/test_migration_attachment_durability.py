# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Migrated attachment files stay consistent with the records that link them when a
batch, the storage backend or the connection fails: no file is left without its link,
no retry stores a second copy or writes a second link, a link names only a record that
exists, and the cleanup of a staged file is deterministic, retryable and scoped to the
one staged company. Every fault is injected against real Postgres."""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import select, text

from fixtures.manager_io import specs
from fixtures.manager_io.support import BASIC, ref
from migration_support import count, load_run, maker, real_engine, resume_run  # noqa: F401

ATTACHED = "doc.file_attached"


async def _migrate(engine, monkeypatch, tmp_path):
    from test_migration_e2e import migrate

    run, _ = await migrate(engine, BASIC.read_bytes(), "basic.manager", {"mode": "full_history"},
                           monkeypatch, tmp_path / "data")
    return run


def _stored(tmp_path, company_id) -> list[str]:
    """Every file in the company's attachment folder."""
    folder = tmp_path / "data" / "static" / "attachments" / str(company_id)
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


async def _links(engine, company_id) -> list[str]:
    """The stored file id of every file-attached event committed for the company."""
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT data FROM ledger WHERE company_id = :c AND event_type = :e"),
                                  {"c": str(company_id), "e": ATTACHED})
        return sorted(row.data["file_id"] for row in rows)


async def _target(engine, run_id, source_type: str, label: str) -> str | None:
    from celerp.models.migration import MigrationEntityMap

    async with maker(engine)() as s:
        return await s.scalar(select(MigrationEntityMap.target_entity_id).where(
            MigrationEntityMap.migration_run_id == run_id,
            MigrationEntityMap.source_type == source_type,
            MigrationEntityMap.source_external_id == ref(label)))


async def _invoice_files(engine, run) -> list[str]:
    from celerp.models.projections import Projection

    invoice = await _target(engine, run.id, "SalesInvoice", "INV1")
    async with maker(engine)() as s:
        row = await s.get(Projection, {"company_id": run.company_id, "entity_id": invoice})
        return [f["id"] for f in row.state.get("files", [])]


async def _tasks(engine, company_id) -> int:
    return await count(engine, "migration_cleanup_tasks", "company_id = :c", c=str(company_id))


def _fail_once(monkeypatch, owner, name: str, exc: Exception, *, after: bool = False):
    """Make ``owner.name`` raise ``exc`` on its first call: before it runs, or after it
    has done its work when ``after`` is set. Returns the list of calls seen."""
    real = getattr(owner, name)
    pending, calls = [exc], []

    async def flaky(*args, **kwargs):
        calls.append(args)
        if pending and not after:
            raise pending.pop()
        result = await real(*args, **kwargs)
        if pending:
            raise pending.pop()
        return result

    monkeypatch.setattr(owner, name, flaky)
    return calls


async def _neighbour(engine, tmp_path) -> uuid.UUID:
    """Another company's stored file and pending cleanup record, which nothing here may touch."""
    neighbour = uuid.uuid4()
    folder = tmp_path / "data" / "static" / "attachments" / str(neighbour)
    folder.mkdir(parents=True)
    (folder / "kept.png").write_bytes(specs.PNG)
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO migration_cleanup_tasks (id, company_id, run_ids, created_at) "
                                "VALUES (:i, :c, '[]', now())"), {"i": str(uuid.uuid4()), "c": str(neighbour)})
    return neighbour


async def _consistent(engine, run, tmp_path) -> str:
    """The resumed run's one attachment: one stored file, one link, both the same id."""
    assert run.status == "ready_to_finalize", run.error_summary
    stored_id = await _target(engine, run.id, "Attachment", "ATT1")
    assert _stored(tmp_path, run.company_id) == [f"{stored_id}.png"]
    assert await _links(engine, run.company_id) == [stored_id]
    assert await _invoice_files(engine, run) == [stored_id]
    assert await _tasks(engine, run.company_id) == 0
    return stored_id


async def _resume(engine, run_id):
    await resume_run(engine, run_id)
    return await load_run(engine, run_id)


async def test_attachment_db_batch_fails_after_blob_staged(real_engine, monkeypatch, tmp_path):
    """The file is stored, then the batch's database write fails: the stored file is
    removed with the rolled-back batch, and the retry stores and links it once."""
    from celerp.services import migration_core_sink

    _fail_once(monkeypatch, migration_core_sink, "attach_file", RuntimeError("database write failed"))
    run = await _migrate(real_engine, monkeypatch, tmp_path)
    assert run.status == "failed" and run.error_summary["phase"] == "attachments", run.error_summary
    assert _stored(tmp_path, run.company_id) == []
    assert await _links(real_engine, run.company_id) == []
    assert await _target(real_engine, run.id, "Attachment", "ATT1") is None
    assert await _tasks(real_engine, run.company_id) == 0
    await _consistent(real_engine, await _resume(real_engine, run.id), tmp_path)


async def test_attachment_commit_response_lost_then_retry(real_engine, monkeypatch, tmp_path):
    """The attachment batch commits but the runner never hears so: the run treats the
    batch as not done, keeps the committed file and link, and the retry adds neither a
    second file nor a second link."""
    from sqlalchemy.ext.asyncio import AsyncSession

    import celerp.db
    from celerp.models.migration import MigrationPhase
    from celerp.services import migrations

    lost = [ConnectionResetError("connection closed before the commit was acknowledged")]

    class LostAck(AsyncSession):
        async def commit(self):
            await super().commit()
            if lost and self.info.get("attachments"):
                raise lost.pop()

    real_checkpoint = migrations._checkpoint

    async def mark(session, run_id, phase, state):
        if phase == MigrationPhase.ATTACHMENTS:
            session.info["attachments"] = True
        return await real_checkpoint(session, run_id, phase, state)

    monkeypatch.setattr(migrations, "_checkpoint", mark)
    monkeypatch.setattr(migrations, "_maker",
                        lambda: lambda: LostAck(bind=celerp.db.engine, expire_on_commit=False))
    run = await _migrate(real_engine, monkeypatch, tmp_path)
    assert run.status == "failed" and run.error_summary["phase"] == "attachments", run.error_summary
    assert run.error_summary["batch_cursor"] == 0
    assert run.phase_state["attachments"]["cursor"] == 0
    committed = await _target(real_engine, run.id, "Attachment", "ATT1")
    assert committed is not None
    assert _stored(tmp_path, run.company_id) == [f"{committed}.png"]
    assert await _links(real_engine, run.company_id) == [committed]
    assert await _consistent(real_engine, await _resume(real_engine, run.id), tmp_path) == committed


async def test_attachment_no_duplicates_cross_company_deletion_or_orphans(real_engine, monkeypatch, tmp_path):
    """After a failed batch, its cleanup and the retry, every stored file of the company
    is linked exactly once, and no other company's file or cleanup task is touched."""
    from celerp.services import migration_core_sink

    neighbour = await _neighbour(real_engine, tmp_path)
    _fail_once(monkeypatch, migration_core_sink, "attach_file", RuntimeError("database write failed"))
    run = await _migrate(real_engine, monkeypatch, tmp_path)
    assert run.status == "failed", run.error_summary
    run = await _resume(real_engine, run.id)
    await _consistent(real_engine, run, tmp_path)
    assert _stored(tmp_path, neighbour) == ["kept.png"]
    assert await _tasks(real_engine, neighbour) == 1


async def test_attachment_storage_write_fails_before_db_link(real_engine, monkeypatch, tmp_path):
    """The storage backend fails before the file is written: no link is written, the run
    stops at the attachment with the storage failure named, and the retry attaches it."""
    from celerp.services import attachments

    _fail_once(monkeypatch, attachments.LocalBackend, "store", OSError("No space left on device"))
    run = await _migrate(real_engine, monkeypatch, tmp_path)
    assert run.status == "failed" and run.error_summary["phase"] == "attachments", run.error_summary
    assert run.error_summary["error_class"] == "AttachmentStorageError"
    assert _stored(tmp_path, run.company_id) == []
    assert await _links(real_engine, run.company_id) == []
    assert await _target(real_engine, run.id, "Attachment", "ATT1") is None
    assert await _tasks(real_engine, run.company_id) == 0
    await _consistent(real_engine, await _resume(real_engine, run.id), tmp_path)


async def test_attachment_retry_after_ambiguous_outcome(real_engine, monkeypatch, tmp_path):
    """The backend writes the file but reports a failure: the unlinked file is removed,
    and the retry writes the same identity once rather than a second copy."""
    from celerp.services import attachments

    calls = _fail_once(monkeypatch, attachments.LocalBackend, "store", TimeoutError("storage timed out"),
                       after=True)
    run = await _migrate(real_engine, monkeypatch, tmp_path)
    assert run.status == "failed" and run.error_summary["phase"] == "attachments", run.error_summary
    assert _stored(tmp_path, run.company_id) == []
    assert await _links(real_engine, run.company_id) == []
    first_id = calls[0][2]
    stored_id = await _consistent(real_engine, await _resume(real_engine, run.id), tmp_path)
    assert stored_id == first_id
    assert [c[2] for c in calls] == [first_id, first_id]


async def test_attachment_link_written_only_for_existing_target(real_engine, monkeypatch, tmp_path):
    """A file whose mapped target record no longer exists is neither stored nor linked;
    the run stops with the reason instead."""
    from celerp.services import migration_core_sink

    _fail_once(monkeypatch, migration_core_sink, "_import_attachment", RuntimeError("storage unavailable"))
    run = await _migrate(real_engine, monkeypatch, tmp_path)
    assert run.status == "failed" and run.error_summary["phase"] == "attachments", run.error_summary
    invoice = await _target(real_engine, run.id, "SalesInvoice", "INV1")
    async with real_engine.begin() as conn:
        await conn.execute(text("DELETE FROM projections WHERE company_id = :c AND entity_id = :e"),
                           {"c": str(run.company_id), "e": invoice})

    run = await _resume(real_engine, run.id)
    assert run.status == "failed" and run.error_summary["phase"] == "attachments", run.error_summary
    assert run.error_summary["error_class"] == "MigrationBatchError"
    assert "no longer exists" in run.error_summary["message"]
    assert _stored(tmp_path, run.company_id) == []
    assert await _links(real_engine, run.company_id) == []
    assert await _target(real_engine, run.id, "Attachment", "ATT1") is None
    assert await count(real_engine, "projections", "company_id = :c AND entity_id = :e",
                       c=str(run.company_id), e=invoice) == 0


async def test_attachment_identity_is_content_addressed(real_engine, monkeypatch, tmp_path):
    """A migrated file's stored id is derived from its run, its source identity and its
    content hash: the same file always gets the same id, and any other file another."""
    from celerp.services.migration_core_sink import attachment_file_id

    run = await _migrate(real_engine, monkeypatch, tmp_path)
    digest = hashlib.sha256(specs.PNG).hexdigest()
    stored_id = await _consistent(real_engine, run, tmp_path)
    assert stored_id == attachment_file_id(run.id, ref("ATT1"), digest)
    assert stored_id == attachment_file_id(run.id, ref("ATT1"), digest)
    assert str(uuid.UUID(stored_id)) == stored_id
    others = {
        attachment_file_id(run.id, ref("ATT1"), hashlib.sha256(b"other content").hexdigest()),
        attachment_file_id(run.id, ref("INV1"), digest),
        attachment_file_id(uuid.uuid4(), ref("ATT1"), digest),
    }
    assert stored_id not in others and len(others) == 3


async def test_attachment_cleanup_after_rollback_is_deterministic_and_retryable(real_engine, monkeypatch, tmp_path):
    """When the cleanup after a rolled-back batch cannot delete the staged file, the file
    stays recorded for a retry; the startup sweep then removes exactly that file, and a
    second sweep finds nothing left to do."""
    from celerp.services import attachments, migration_core_sink, migrations

    _fail_once(monkeypatch, migration_core_sink, "attach_file", RuntimeError("database write failed"))
    _fail_once(monkeypatch, attachments.LocalBackend, "delete", OSError("device busy"))
    run = await _migrate(real_engine, monkeypatch, tmp_path)
    assert run.status == "failed", run.error_summary
    staged = _stored(tmp_path, run.company_id)
    assert len(staged) == 1
    assert await _links(real_engine, run.company_id) == []
    assert await _tasks(real_engine, run.company_id) == 1

    async with maker(real_engine)() as s:
        assert await migrations.sweep_cleanup_tasks(s) == 0
    assert _stored(tmp_path, run.company_id) == []
    assert await _tasks(real_engine, run.company_id) == 0
    async with maker(real_engine)() as s:
        assert await migrations.sweep_cleanup_tasks(s) == 0
    assert _stored(tmp_path, run.company_id) == []

    stored_id = await _consistent(real_engine, await _resume(real_engine, run.id), tmp_path)
    assert [f"{stored_id}.png"] == staged


async def test_attachment_failure_reported_distinctly_accounting_intact(real_engine, monkeypatch, tmp_path):
    """A storage failure is reported as an attachment storage failure, separately from a
    record rejection, and leaves every record and posting imported before it in place."""
    from celerp.services import attachments, migrations

    _fail_once(monkeypatch, attachments.LocalBackend, "store", OSError("No space left on device"))
    run = await _migrate(real_engine, monkeypatch, tmp_path)
    assert run.status == "failed", run.error_summary
    assert run.error_summary["phase"] == "attachments"
    assert run.error_summary["error_class"] == "AttachmentStorageError"
    assert "receipt-scan.png" in run.error_summary["message"]
    assert "could not be stored" in run.error_summary["message"]
    assert "No space left on device" not in run.error_summary["message"]
    for phase, entry in run.phase_state.items():
        if phase != "attachments":
            assert entry["status"] == "done", (phase, entry)
    async with maker(real_engine)() as s:
        assert await migrations._unmapped(s, run.id, [("SalesInvoice", ref("INV1"))]) == []
    postings = await count(real_engine, "ledger", "company_id = :c AND event_type <> :e",
                           c=str(run.company_id), e=ATTACHED)
    assert postings > 0

    run = await _resume(real_engine, run.id)
    await _consistent(real_engine, run, tmp_path)
    assert run.reconciliation["blockers"] == 0
    assert await count(real_engine, "ledger", "company_id = :c AND event_type <> :e",
                       c=str(run.company_id), e=ATTACHED) == postings


async def test_attachment_discard_after_failed_or_interrupted_run(real_engine, monkeypatch, tmp_path):
    """Discarding a staged company whose run stopped with a staged file still awaiting
    cleanup removes that company's files and cleanup records, and nothing of another
    company's."""
    from celerp.models.migration import MigrationRun
    from celerp.services import attachments, migration_core_sink, migrations

    neighbour = await _neighbour(real_engine, tmp_path)
    _fail_once(monkeypatch, migration_core_sink, "attach_file", RuntimeError("database write failed"))
    _fail_once(monkeypatch, attachments.LocalBackend, "delete", OSError("device busy"))
    run = await _migrate(real_engine, monkeypatch, tmp_path)
    assert run.status == "failed", run.error_summary
    assert len(_stored(tmp_path, run.company_id)) == 1
    assert await _tasks(real_engine, run.company_id) == 1
    async with real_engine.begin() as conn:
        await conn.execute(text("UPDATE migration_runs SET status = 'interrupted' WHERE id = :r"),
                           {"r": str(run.id)})

    async with maker(real_engine)() as s:
        # The owner has no other company, so the login starts over.
        assert await migrations.discard(s, await s.get(MigrationRun, run.id)) == migrations.START_COMPANY_PAGE
    assert _stored(tmp_path, run.company_id) == []
    assert not (tmp_path / "data" / "static" / "attachments" / str(run.company_id)).exists()
    assert await _tasks(real_engine, run.company_id) == 0
    assert await count(real_engine, "companies", "id = :c", c=str(run.company_id)) == 0
    assert _stored(tmp_path, neighbour) == ["kept.png"]
    assert await _tasks(real_engine, neighbour) == 1
