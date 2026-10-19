# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Tell the sending company in time when its key to the receiving company stops working.

Before each outbound run the connector asks the receiving installation how long its key
still works. Only the receiving company's owner can renew a key, so within RENEW_WITHIN
of expiry, once it has expired, and when it is no longer accepted, the sending company
gets one unmissable notice naming the company to ask. The notice clears once the key
works with more than RENEW_WITHIN left."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

from celerp.connectors.base import ConnectorContext
from celerp.connectors.http import RateLimitedClient
from celerp.services.peer_access import RENEW_WITHIN

log = logging.getLogger(__name__)

ACTION_URL = "/settings/cloud?tab=accounting"
NOTICES = {"expiring": "notice.peer_key_expiring_here", "expired": "notice.peer_key_expired",
           "rejected": "notice.peer_key_rejected"}


async def check_key(ctx: ConnectorContext, *, now: datetime | None = None) -> None:
    """Never raises: the run that follows reports a key that does not work."""
    try:
        await _check(ctx, now or datetime.now(timezone.utc))
    except Exception:
        log.exception("company key check failed")


async def _check(ctx: ConnectorContext, when: datetime) -> None:
    base = str(ctx.store_handle or "").rstrip("/")
    async with RateLimitedClient(public_only=True) as client:
        verified = await client.get(f"{base}/company-peers/verify",
                                    headers={"Authorization": "Peer " + ctx.access_token},
                                    params={"source_company_id": ctx.company_id})
    if verified.status_code == 200:
        info = verified.json()
        expires = _instant(info["expires_at"])
        if expires - when > RENEW_WITHIN:
            await _tell(ctx.company_id, "current")
        else:
            await _tell(ctx.company_id, "expiring", firm=info["company_name"],
                        date=expires.date().isoformat())
        return
    if verified.status_code != 401:
        return
    detail = verified.json().get("detail")
    if isinstance(detail, dict) and detail.get("code") == "key_expired":
        await _tell(ctx.company_id, "expired", firm=detail["company_name"])
    else:
        # A refused key carries no company name; the address is what the company entered.
        await _tell(ctx.company_id, "rejected", firm=urlsplit(base).hostname or base)


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def _tell(company_id: str, kind: str, **params) -> None:
    """Show the notice for *kind* and clear the others ("current" clears all)."""
    from celerp.db import get_session_ctx
    from celerp.notifications import service as notification_service
    from ui.i18n import t

    cid = uuid.UUID(company_id)
    async with get_session_ctx() as session:
        for other, key in NOTICES.items():
            if other != kind:
                await notification_service.clear_standing(
                    session, cid, "integrations", t(f"{key}.title", "en"))
        if kind in NOTICES:
            key = NOTICES[kind]
            await notification_service.notify_standing(
                session, cid, "integrations", t(f"{key}.title", "en"),
                t(f"{key}.body", "en", **params), action_url=ACTION_URL,
                i18n={"title": f"{key}.title", "body": f"{key}.body", "params": params})
        await session.commit()
