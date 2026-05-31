# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""System administration endpoints — restart, factory-reset."""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.services.auth import get_current_company_id, require_admin, require_min_role

_ATTACHMENT_ROOT = Path("static/attachments")

router = APIRouter(dependencies=[Depends(require_admin)])


# ── Graceful restart ──────────────────────────────────────────────────────────


def _restart_sentinel_path() -> Path:
    from celerp.config import config_path
    return config_path().parent / ".restart_requested"


def _send_sigterm() -> None:
    """Write restart sentinel then SIGTERM self.

    The sentinel tells the `celerp start` process manager to respawn rather
    than exit when it detects the subprocess death.
    """
    import os
    import signal
    import time
    _restart_sentinel_path().touch()
    time.sleep(0.2)  # let the response flush
    os.kill(os.getpid(), signal.SIGTERM)


@router.post("/restart")
async def restart_server(
    background_tasks: BackgroundTasks,
    _=Depends(require_admin),
) -> dict:
    """Gracefully restart the server process (SIGTERM → process manager respawns).

    Used by the setup wizard after applying a preset so new modules are loaded.
    Returns immediately; the restart happens ~200ms later in a background task.
    """
    background_tasks.add_task(_send_sigterm)
    return {"ok": True, "restarting": True}


# ── Factory reset ─────────────────────────────────────────────────────────────

_TRUNCATE_TABLES = [
    "ledger", "projections", "notifications", "import_batches", "sync_runs",
    "doc_share_tokens", "ai_conversations", "ai_messages", "ai_batch_jobs",
    "outbound_queue", "connector_configs",
    "accounts", "bank_accounts", "bank_statement_lines",
    "label_templates", "reconciliation_rules", "reconciliation_sessions",
    "marketplace_configs", "session_registry", "user_auth_state",
]


@router.post("/factory-reset")
async def factory_reset(
    _: None = require_min_role("owner"),
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Wipe all company data and return the system to a fresh-install state."""
    from sqlalchemy import text

    async with session.begin():
        for table in _TRUNCATE_TABLES:
            await session.execute(text(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE"))
        await session.execute(
            text("DELETE FROM user_companies WHERE company_id = :cid"),
            {"cid": str(company_id)},
        )
        await session.execute(
            text("DELETE FROM locations WHERE company_id = :cid"),
            {"cid": str(company_id)},
        )
        await session.execute(text("DELETE FROM users"))
        await session.execute(
            text("DELETE FROM companies WHERE id = :cid"),
            {"cid": str(company_id)},
        )

    # Bust in-process nonce cache — all users deleted, stale tokens must not auto-create rows
    from celerp.services.session_tracker import _nonce_cache_bust_all
    _nonce_cache_bust_all()

    att_dir = _ATTACHMENT_ROOT / str(company_id)
    if att_dir.exists():
        import shutil
        shutil.rmtree(att_dir, ignore_errors=True)

    return {"ok": True}
