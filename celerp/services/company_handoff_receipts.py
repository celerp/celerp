# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Report an authenticated company restore after it has committed locally."""
from __future__ import annotations

import logging
from pathlib import Path

import httpx

from celerp.services import company_backup_files as files

log = logging.getLogger(__name__)


async def report_completed_handoff(stage: Path) -> bool:
    """Best-effort remote acknowledgment; retain a private receipt for retry."""
    receipt = files._receipt(stage)
    if receipt is None:
        return False
    if receipt.get("reported"):
        return True
    try:
        from celerp.gateway.state import relay_http_url
        async with httpx.AsyncClient(timeout=httpx.Timeout(5, connect=3), follow_redirects=False) as client:
            response = await client.post(
                f"{relay_http_url()}/handoffs/{receipt['copy_id']}/imported",
                json={"proof": receipt["proof"]},
            )
        if response.status_code != 200 or response.json().get("imported") is not True:
            return False
        files.mark_handoff_reported(stage)
        return True
    except Exception as exc:
        log.debug("Company handoff acknowledgment pending retry (%s)", type(exc).__name__)
        return False


async def retry_completed_handoffs() -> None:
    """Retry confirmed local restores, never uploads waiting for the user to restore."""
    for stage, _ in files.pending_handoffs(reported=False)[:1]:
        await report_completed_handoff(stage)
