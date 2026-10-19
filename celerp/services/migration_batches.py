# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Move several companies: each file of an upload is kept as its own scan, waiting its
turn, and listed with the step it has reached. Each company is opened, reviewed and
started by the person through the one-company wizard; nothing starts on its own."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterable
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.importers.adapters.registry import get_adapter
from celerp.models.company import Company
from celerp.models.migration import MigrationBatchFile, MigrationRun
from celerp.services import migration_scan_store as store
from celerp.services import migrations
from ui.i18n import t

GONE = "migration.several_gone"
EXPORT_SET = "migration.several_export_set"


def _owner(user_id: uuid.UUID) -> store.ScanOwner:
    return store.SEVERAL, user_id


async def add(session: AsyncSession, user_id: uuid.UUID, parts: AsyncIterable[store.UploadPart]) -> None:
    """Keep each file of an upload, readable or not, in the person's list. A file that is
    one part of an export set is refused: that client is moved on its own, as a set."""
    upload = await store.create_several(parts, owner=_owner(user_id))
    try:
        prepared_by = migrations.validate_prepared_by(upload.prepared_by) or ""
    except migrations.MigrationError:
        for _, result in upload.files:
            if not isinstance(result, store.ScanStoreError):
                store.delete_scan(result.token)
        raise
    now = datetime.now(timezone.utc)
    for n, (file_name, result) in enumerate(upload.files):
        row = MigrationBatchFile(user_id=user_id, file_name=file_name, prepared_by=prepared_by,
                                 created_at=now + timedelta(microseconds=n))  # newest first is the upload order
        if isinstance(result, store.ScanStoreError):
            row.error = str(result.detail)
        elif isinstance(result, store.DraftSession):
            store.delete_scan(result.token)
            row.source_system = result.adapter_key
            row.error = t(EXPORT_SET, source=get_adapter(result.adapter_key).display_name)
        else:
            row.source_system = result.adapter_key
            row.scan_claim_sha256 = store.scan_claim(result.token)
        session.add(row)
    await session.commit()


def _ready_token(row: MigrationBatchFile) -> str | None:
    """The token of the file's scan while it is still kept, else None."""
    token = store.find_scan_token(row.scan_claim_sha256)
    if token is None:
        return None
    try:
        store.load_scan(token, owner=_owner(row.user_id))
    except store.ScanStoreError:
        return None
    return token


async def _runs(session: AsyncSession, user_id: uuid.UUID, rows: list[MigrationBatchFile]) -> dict:
    claims = [r.scan_claim_sha256 for r in rows if r.scan_claim_sha256]
    if not claims:
        return {}
    found = await session.execute(
        select(MigrationRun, Company.name).join(Company, Company.id == MigrationRun.company_id)
        .where(MigrationRun.scan_claim_sha256.in_(claims), MigrationRun.created_by_user_id == user_id))
    return {run.scan_claim_sha256: (run, name) for run, name in found}


async def items(session: AsyncSession, user_id: uuid.UUID) -> list[dict]:
    """The person's files, newest first, each with its step: ready to continue, started
    (with its run and company), gone (kept past its day, or its run discarded), or
    unreadable with the reason."""
    rows = list(await session.scalars(select(MigrationBatchFile).where(MigrationBatchFile.user_id == user_id)
                                      .order_by(MigrationBatchFile.created_at.desc())))
    runs = await _runs(session, user_id, rows)
    out = []
    for row in rows:
        run, company_name = runs.get(row.scan_claim_sha256, (None, None))
        if row.error is not None:
            state = "unreadable"
        elif run is not None:
            state = "started"
        else:
            state = "ready" if _ready_token(row) else "gone"
        out.append({
            "id": str(row.id), "file_name": row.file_name, "uploaded_at": row.created_at.isoformat(),
            "state": state, "error": row.error, "source_system": row.source_system,
            "run_id": str(run.id) if run else None, "run_status": run.status if run else None,
            "company_name": company_name,
        })
    return out


async def _owned(session: AsyncSession, user_id: uuid.UUID, file_id: uuid.UUID) -> MigrationBatchFile:
    row = await session.get(MigrationBatchFile, file_id)
    if row is None or row.user_id != user_id:
        raise migrations.MigrationError(404, t(migrations.NOT_FOUND))
    return row


async def open_file(session: AsyncSession, user_id: uuid.UUID, file_id: uuid.UUID) -> dict:
    """Where Continue goes: the run already started from the file, or its scan for the
    wizard's next step. A file that is gone or unreadable says why."""
    row = await _owned(session, user_id, file_id)
    if row.error is not None:
        raise migrations.MigrationError(422, row.error)
    run_id = await migrations.started_run_id(session, row.scan_claim_sha256, user_id)
    if run_id is not None:
        return {"run_id": str(run_id)}
    token = _ready_token(row)
    if token is None:
        raise migrations.MigrationError(410, t(GONE))
    return {"scan_token": token, "prepared_by": row.prepared_by}


async def remove(session: AsyncSession, user_id: uuid.UUID, file_id: uuid.UUID) -> None:
    """Take a file off the list and delete its upload from this server. A company already
    started from it is not touched."""
    row = await _owned(session, user_id, file_id)
    if row.scan_claim_sha256 and (token := store.find_scan_token(row.scan_claim_sha256)):
        store.delete_scan(token)
    await session.delete(row)
    await session.commit()
