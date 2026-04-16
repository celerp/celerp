# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Backup scheduler - daily DB + weekly file backups as asyncio background tasks.

Usage:
    from celerp.services import backup_scheduler
    backup_scheduler.start()   # called in main.py lifespan
    backup_scheduler.stop()    # called on shutdown

Last-run status is exposed via last_db_result / last_file_result for the UI.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

_db_task: asyncio.Task | None = None
_file_task: asyncio.Task | None = None


@dataclass
class SchedulerStatus:
    """Snapshot of a scheduler task's last result."""
    last_run: datetime | None = None
    ok: bool | None = None
    error: str | None = None
    size_bytes: int = 0


_last_db = SchedulerStatus()
_last_file = SchedulerStatus()


def last_db_result() -> SchedulerStatus:
    return _last_db


def last_file_result() -> SchedulerStatus:
    return _last_file


def _seconds_until(target_hour: int, target_minute: int = 0) -> float:
    """Seconds from now until the next occurrence of target_hour:target_minute UTC."""
    now = datetime.now(timezone.utc)
    target = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _seconds_until_weekday(weekday: int, hour: int) -> float:
    """Seconds until next occurrence of weekday (0=Mon) at hour UTC."""
    now = datetime.now(timezone.utc)
    days_ahead = weekday - now.weekday()
    if days_ahead < 0 or (days_ahead == 0 and now.hour >= hour):
        days_ahead += 7
    target = (now + timedelta(days=days_ahead)).replace(
        hour=hour, minute=0, second=0, microsecond=0,
    )
    return (target - now).total_seconds()


def next_db_run_utc() -> datetime | None:
    """Return the next scheduled DB backup time (UTC), or None if not running."""
    if _db_task is None or _db_task.done():
        return None
    from celerp.config import settings
    secs = _seconds_until(settings.backup_hour)
    return datetime.now(timezone.utc) + timedelta(seconds=secs)


def next_file_run_utc() -> datetime | None:
    """Return the next scheduled file backup time (UTC), or None if not running."""
    if _file_task is None or _file_task.done():
        return None
    from celerp.config import settings
    secs = _seconds_until_weekday(6, settings.backup_hour + 1)
    return datetime.now(timezone.utc) + timedelta(seconds=secs)


async def _db_backup_loop() -> None:
    """Daily DB backup at configured hour."""
    global _last_db
    from celerp.config import settings
    while True:
        wait = _seconds_until(settings.backup_hour)
        log.info("Next DB backup in %.0f seconds", wait)
        await asyncio.sleep(wait)

        from celerp.services.backup import run_backup
        result = await run_backup(label="daily")
        _last_db = SchedulerStatus(
            last_run=datetime.now(timezone.utc),
            ok=result.ok,
            error=result.error,
            size_bytes=result.size_bytes,
        )
        if result.ok:
            log.info("Daily DB backup succeeded (%d bytes)", result.size_bytes)
        else:
            log.error("Daily DB backup failed: %s - retrying in 1 hour", result.error)
            await asyncio.sleep(3600)
            result = await run_backup(label="daily-retry")
            _last_db = SchedulerStatus(
                last_run=datetime.now(timezone.utc),
                ok=result.ok,
                error=result.error,
                size_bytes=result.size_bytes,
            )
            if result.ok:
                log.info("Daily DB backup retry succeeded (%d bytes)", result.size_bytes)
            else:
                log.error("Daily DB backup retry also failed: %s", result.error)


async def _file_backup_loop() -> None:
    """Weekly file backup on Sunday at backup_hour + 1."""
    global _last_file
    from celerp.config import settings
    while True:
        wait = _seconds_until_weekday(6, settings.backup_hour + 1)  # Sunday
        log.info("Next file backup in %.0f seconds", wait)
        await asyncio.sleep(wait)

        from celerp.services.backup_files import run_file_backup
        result = await run_file_backup(label="weekly")
        _last_file = SchedulerStatus(
            last_run=datetime.now(timezone.utc),
            ok=result.ok,
            error=result.error,
            size_bytes=result.size_bytes,
        )
        if result.ok:
            log.info("Weekly file backup succeeded (%d bytes)", result.size_bytes)
        else:
            log.error("Weekly file backup failed: %s - retrying in 1 hour", result.error)
            await asyncio.sleep(3600)
            result = await run_file_backup(label="weekly-retry")
            _last_file = SchedulerStatus(
                last_run=datetime.now(timezone.utc),
                ok=result.ok,
                error=result.error,
                size_bytes=result.size_bytes,
            )
            if result.ok:
                log.info("Weekly file backup retry succeeded (%d bytes)", result.size_bytes)
            else:
                log.error("Weekly file backup retry also failed: %s", result.error)


def start() -> None:
    """Start backup scheduler tasks. Idempotent."""
    global _db_task, _file_task
    if _db_task is None or _db_task.done():
        _db_task = asyncio.create_task(_db_backup_loop())
        log.info("DB backup scheduler started")
    if _file_task is None or _file_task.done():
        _file_task = asyncio.create_task(_file_backup_loop())
        log.info("File backup scheduler started")


def stop() -> None:
    """Stop backup scheduler tasks."""
    global _db_task, _file_task
    if _db_task and not _db_task.done():
        _db_task.cancel()
    if _file_task and not _file_task.done():
        _file_task.cancel()
    _db_task = None
    _file_task = None
    log.info("Backup scheduler stopped")
