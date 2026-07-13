# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Online-payments reminder with backoff.

While the instance is cloud-connected but no Stripe account is connected, drop
a bell notification per company on a widening schedule - 4, 6, 9, then 14
weeks after the previous one - and stop for good after the last. A finite,
spaced series keeps the bell trustworthy (endless nagging teaches users to
ignore every notification); permanent discovery stays with the passive
surfaces (send-modal line, invoice hint, Web Access card), which appear at the
moment of need. Never fires for instances without a relay session: they cannot
take payments at all, and the Web Access page carries that pitch instead.

Schedule state lives in company.settings (not in past notifications, which the
bell prunes per company and would quietly reset the series).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

log = logging.getLogger(__name__)

# Gap (in weeks) before nudge N+1; after the last gap is served, no more nudges.
BACKOFF_WEEKS = (4, 6, 9, 14)
_CHECK_INTERVAL_SECONDS = 6 * 60 * 60
_BOOT_DELAY_SECONDS = 120  # let the gateway handshake deliver feature flags first
_CATEGORY = "payments"
_STATE_KEY = "pay_reminder"  # {"count": int, "last_at": iso8601}


async def _remind_company(session, company) -> bool:
    """Create the next reminder when its backoff gap has elapsed.

    Returns True when a notification was created. The first call fires
    immediately; call N+1 fires BACKOFF_WEEKS[N-1] weeks after call N; after
    len(BACKOFF_WEEKS)+1 total nudges the series is over, permanently.
    """
    from celerp.notifications import service as notif_service

    state = dict((company.settings or {}).get(_STATE_KEY) or {})
    count = int(state.get("count") or 0)
    if count > len(BACKOFF_WEEKS):
        return False
    now = datetime.now(timezone.utc)
    if count > 0:
        last_at = datetime.fromisoformat(state["last_at"])
        if last_at.tzinfo is None:
            last_at = last_at.replace(tzinfo=timezone.utc)
        if now - last_at < timedelta(weeks=BACKOFF_WEEKS[count - 1]):
            return False

    await notif_service.create(
        session, company.id, _CATEGORY,
        "Get paid by card",
        "Customers can pay your invoices online once you connect a Stripe account. "
        "Setup takes a few minutes.",
        action_url="/settings/payments", priority="low",
    )
    settings = dict(company.settings or {})
    settings[_STATE_KEY] = {"count": count + 1, "last_at": now.isoformat()}
    company.settings = settings
    session.add(company)
    return True


async def payments_reminder_loop() -> None:
    """Background loop started from the API lifespan."""
    from celerp.db import get_session_ctx
    from celerp.gateway.state import get_session_token
    from celerp.models.company import Company
    from celerp.services.payments import payments_enabled

    await asyncio.sleep(_BOOT_DELAY_SECONDS)
    while True:
        try:
            if get_session_token() and not payments_enabled():
                async with get_session_ctx() as session:
                    companies = (await session.execute(
                        select(Company).where(Company.is_active.is_(True))
                    )).scalars().all()
                    for company in companies:
                        try:
                            await _remind_company(session, company)
                        except Exception as exc:
                            log.error("payments reminder failed for %s: %s", company.id, exc)
                    await session.commit()
        except Exception as exc:
            log.error("payments reminder loop error: %s", exc)
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)
