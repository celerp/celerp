# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Everything Celerp keeps for one company outside its database, deleted in one place.

A deleted company (reset, or a discarded migration) takes all of it: its attachment
files on every storage backend, its migration source files, its assistant uploads, its
staged import files and its generated backups. Each writer of these files holds the
company while it writes (``company_lock.hold_company``), so nothing is written for a
company after its deletion has committed."""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from pathlib import Path

from celerp.ai import files as ai_files
from celerp.config import settings
from celerp.services import attachments, import_stage
from celerp.services import migration_scan_store as store

logger = logging.getLogger(__name__)


def company_backups_dir() -> Path:
    """Where generated company backups and uploaded backups waiting to be restored are kept."""
    return settings.data_dir / "company_backups"


def _remove_backups(company_id) -> None:
    if not attachments.is_plain_name(str(company_id)):
        raise ValueError(f"Invalid company id: {company_id!r}")
    path = company_backups_dir() / str(company_id)
    if path.exists():
        shutil.rmtree(path)


async def delete_company_data(company_id, run_ids: list[str]) -> None:
    """Delete every file kept for the company; files already gone count as deleted.

    Every kind is attempted even when another fails; then raises if any failed, so the
    caller's cleanup task stays for a retry that repeats only what is left."""
    steps = [
        *((f"migration run {run_id}", lambda r=run_id: asyncio.to_thread(store.remove_run_dir, uuid.UUID(r)))
          for run_id in run_ids),
        ("attachments", lambda: attachments.delete_company_files(str(company_id))),
        ("assistant uploads", lambda: asyncio.to_thread(ai_files.delete_company_uploads, company_id)),
        ("staged imports", lambda: asyncio.to_thread(import_stage.delete_company_stages, company_id)),
        ("company backups", lambda: asyncio.to_thread(_remove_backups, company_id)),
    ]
    failed = []
    for name, step in steps:
        try:
            await step()
        except Exception as exc:
            logger.warning("Deleting the %s of a deleted company failed: %s", name, type(exc).__name__)
            failed.append(name)
    if failed:
        raise OSError(f"Could not delete: {', '.join(failed)}")
