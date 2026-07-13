# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Monthly online-payments reminder.

While the instance is cloud-connected but no Stripe account is connected, drop
one bell notification per company every REMINDER_DAYS pointing at the payments
setup page. Stops the moment payments are enabled; never fires for instances
without a relay session (they cannot take payments at all, and the Web Access
page carries that pitch instead).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

log = logging.getLogger(__name__)

REMINDER_DAYS = 30
_CHECK_INTERVAL_SECONDS = 6 * 60 * 60
_BOOT_DELAY_SECONDS = 120  # let the gateway handshake deliver feature flags first
_CATEGORY = "payments"


async def _remind_company(session, company_id) -> bool:
    """Create the reminder if this company has none newer than REMINDER_DAYS."""
    from celerp.models.notification import Notification
    from celerp.notifications import service as notif_service

    latest = (await session.execute(
        select(Notification.created_at)
        .where(Notification.company_id == company_id, Notification.category == _CATEGORY)
        .order_by(Notification.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    if latest is not None:
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        if latest > datetime.now(timezone.utc) - timedelta(days=REMINDER_DAYS):
            return False
    await notif_service.create(
        session, company_id, _CATEGORY,
        "Get paid by card",
        "Customers can pay your invoices online once you connect a Stripe account. "
        "Setup takes a few minutes.",
        action_url="/settings/payments", priority="low",
    )
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
                            await _remind_company(session, company.id)
                        except Exception as exc:
                            log.error("payments reminder failed for %s: %s", company.id, exc)
                    await session.commit()
        except Exception as exc:
            log.error("payments reminder loop error: %s", exc)
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)
