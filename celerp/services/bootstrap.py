# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""First-owner bootstrap: the one-time setup code and the bootstrap lock.

Registration, native backup restore and company migration all bootstrap a
fresh install through these helpers, so the setup code is validated and
consumed one way and concurrent bootstraps serialize on one lock key.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ui.i18n import t

logger = logging.getLogger(__name__)

# Fixed advisory lock key that serializes first-owner bootstrap across workers.
# Distinct from the session-tracker advisory keys.
BOOTSTRAP_LOCK_KEY = 0x43454C4552500001


def setup_code_hash() -> str:
    """The one-time setup-code hash for a headless install, or '' if none required."""
    from celerp.config import read_config
    return read_config().get("auth", {}).get("setup_code_hash", "") or ""


def verify_setup_code(provided: str | None) -> bool:
    """Validate the one-time setup capability. Return whether one is configured."""
    required = setup_code_hash()
    if not required:
        return False
    value = (provided or "").strip()
    if not value or not hmac.compare_digest(hashlib.sha256(value.encode()).hexdigest(), required):
        raise HTTPException(status_code=403, detail=t("auth.setup_code_invalid"))
    return True


def clear_setup_code() -> None:
    """Best-effort cleanup after the first owner has already committed.

    The DB user row is the authoritative one-time bootstrap gate. Config-lock
    contention here must never turn a successful bootstrap into a 500 that
    tells the operator to retry an operation that already completed.
    """
    from celerp.config import _update_config, config_path
    try:
        _update_config(lambda cfg: cfg.get("auth", {}).pop("setup_code_hash", None))
    except Exception as exc:
        logger.warning("setup-code config cleanup deferred after bootstrap: %s", type(exc).__name__)
    try:
        (config_path().parent / "setup-code").unlink()
    except OSError:
        pass


async def lock_bootstrap(session: AsyncSession) -> None:
    """Take the transaction-scoped bootstrap lock; released on commit or rollback."""
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": BOOTSTRAP_LOCK_KEY})


_LOCAL_LOCK = asyncio.Lock()


@asynccontextmanager
async def bootstrap_restore_lock() -> AsyncIterator[None]:
    """Hold the bootstrap lock across a restore that spans several transactions."""
    from celerp.db import engine

    if engine.dialect.name != "postgresql":
        async with _LOCAL_LOCK:
            yield
        return

    async with engine.connect() as conn:
        await conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": BOOTSTRAP_LOCK_KEY})
        await conn.commit()
        try:
            yield
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": BOOTSTRAP_LOCK_KEY})
            await conn.commit()
