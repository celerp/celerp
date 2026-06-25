# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Backup scheduler - one daily cloud snapshot (database + files) as an asyncio
background task.

Usage:
    from celerp.services import backup_scheduler
    backup_scheduler.start()   # called in main.py lifespan
    backup_scheduler.stop()    # called on shutdown

Last-run status is exposed via last_db_result for the UI.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

_db_task: asyncio.Task | None = None


@dataclass
class SchedulerStatus:
    """Snapshot of the scheduler's last result."""
    last_run: datetime | None = None
    ok: bool | None = None
    error: str | None = None
    size_bytes: int = 0


_last_db = SchedulerStatus()


def last_db_result() -> SchedulerStatus:
    return _last_db


def record_db_result(ok: bool, error: str | None = None, size_bytes: int = 0) -> None:
    """Update last snapshot status (called by manual triggers and the scheduler)."""
    global _last_db
    _last_db = SchedulerStatus(last_run=datetime.now(timezone.utc), ok=ok, error=error, size_bytes=size_bytes)


def _seconds_until(target_hour: int, target_minute: int = 0) -> float:
    """Seconds from now until the next occurrence of target_hour:target_minute UTC."""
    now = datetime.now(timezone.utc)
    target = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def next_db_run_utc() -> datetime | None:
    """Return the next scheduled snapshot time (UTC), or None if not running."""
    if _db_task is None or _db_task.done():
        return None
    from celerp.config import settings
    secs = _seconds_until(settings.backup_hour)
    return datetime.now(timezone.utc) + timedelta(seconds=secs)


async def _db_backup_loop() -> None:
    """Daily cloud snapshot at the configured hour, with a 1-hour retry on failure."""
    from celerp.config import settings
    while True:
        wait = _seconds_until(settings.backup_hour)
        log.debug("Next snapshot in %.0f seconds", wait)
        await asyncio.sleep(wait)

        from celerp.services.backup_repo import run_snapshot
        result = await run_snapshot(label="daily")
        record_db_result(result.ok, result.error, result.size_bytes or 0)
        if result.ok:
            log.info("Daily snapshot succeeded (%d bytes uploaded)", result.size_bytes)
        elif result.error and "Relay not connected" in result.error:
            log.info("Daily snapshot skipped: %s", result.error)
        else:
            log.error("Daily snapshot failed: %s - retrying in 1 hour", result.error)
            await asyncio.sleep(3600)
            result = await run_snapshot(label="daily-retry")
            record_db_result(result.ok, result.error, result.size_bytes or 0)
            if result.ok:
                log.info("Daily snapshot retry succeeded (%d bytes uploaded)", result.size_bytes)
            else:
                log.error("Daily snapshot retry also failed: %s", result.error)


def start() -> None:
    """Start the backup scheduler task. Idempotent."""
    global _db_task
    if _db_task is None or _db_task.done():
        _db_task = asyncio.create_task(_db_backup_loop())
        log.debug("Backup scheduler started")


def stop() -> None:
    """Stop the backup scheduler task."""
    global _db_task
    if _db_task and not _db_task.done():
        _db_task.cancel()
    _db_task = None
    log.debug("Backup scheduler stopped")
