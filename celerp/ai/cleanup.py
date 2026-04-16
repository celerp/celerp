# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""AI file cleanup - periodic removal of old upload files.

All files older than 30 days are deleted unconditionally.
Runs every 6 hours via background task.

Does NOT require a database session - operates purely on filesystem.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from celerp.ai.files import upload_dir

log = logging.getLogger(__name__)

CLEANUP_INTERVAL_SECONDS = 6 * 3600  # 6 hours
MAX_AGE_DAYS = 30


def _delete_file_pair(meta_path: Path) -> None:
    """Delete both .meta and .bin files."""
    bin_path = meta_path.with_suffix(".bin")
    try:
        if bin_path.exists():
            bin_path.unlink()
        if meta_path.exists():
            meta_path.unlink()
    except OSError:
        log.warning("Failed to delete %s", meta_path.stem, exc_info=True)


def cleanup_uploads() -> int:
    """Remove AI upload files older than 30 days. Returns count deleted."""
    try:
        ud = upload_dir()
    except (PermissionError, OSError) as exc:
        log.debug("AI upload dir unavailable, skipping cleanup: %s", exc)
        return 0
    if not ud.exists():
        return 0

    now = time.time()
    deleted = 0

    for meta_path in list(ud.glob("*.meta")):
        try:
            age_days = (now - meta_path.stat().st_mtime) / 86400
        except OSError:
            continue

        if age_days > MAX_AGE_DAYS:
            _delete_file_pair(meta_path)
            deleted += 1
            log.debug("Deleted old file %s (%.0f days)", meta_path.stem, age_days)

    # Clean up orphaned .bin files (no matching .meta)
    for bin_path in list(ud.glob("*.bin")):
        if not bin_path.with_suffix(".meta").exists():
            try:
                bin_path.unlink()
                deleted += 1
            except OSError:
                pass

    if deleted:
        log.info("AI file cleanup: removed %d files", deleted)
    return deleted


async def run_cleanup_loop() -> None:
    """Background task: run cleanup every CLEANUP_INTERVAL_SECONDS."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        try:
            cleanup_uploads()
        except Exception:
            log.warning("AI file cleanup failed", exc_info=True)
