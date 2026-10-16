# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Keep the sending company's key to its receiving company current.

Before each outbound run the connector asks the receiving installation how long its key
still works. Within RENEW_WITHIN of expiry it exchanges the key for a new one and stores
that on the relay, so synchronization never stops on an expired key. The company is told
when its key was renewed, when renewing failed, and when the receiving company no longer
accepts the key, which only its owner can fix."""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone

from celerp.connectors.base import ConnectorContext
from celerp.connectors.http import RateLimitedClient
from celerp.connectors.relay_token import fetch_context, store_api_key
from celerp.services.peer_access import RENEW_WITHIN

log = logging.getLogger(__name__)

CONNECTOR = "celerp"
ACTION_URL = "/settings/cloud?tab=accounting"
STANDING = ("notice.peer_key_renew_failed", "notice.peer_key_rejected")


async def keep_key_current(ctx: ConnectorContext, *, now: datetime | None = None) -> None:
    """Renew the key when it expires soon. Never raises: the run that follows reports
    a key that does not work, and the company is told what happened here."""
    try:
        await _keep_current(ctx, now or datetime.now(timezone.utc))
    except Exception:
        log.exception("company key renewal check failed")


async def _keep_current(ctx: ConnectorContext, when: datetime) -> None:
    from celerp.db import get_session_ctx

    async with get_session_ctx() as session:
        # One renewal per company at a time: a second one would retire the first's key.
        await _lock(session, ctx.company_id)
        latest = await _current_context(ctx)
        ctx.access_token = latest.access_token
        base = str(ctx.store_handle or "").rstrip("/")
        headers = {"Authorization": "Peer " + ctx.access_token}
        async with RateLimitedClient(public_only=True) as client:
            verified = await client.get(f"{base}/company-peers/verify", headers=headers,
                                        params={"source_company_id": ctx.company_id})
            if verified.status_code in (401, 403):
                await _tell(ctx.company_id, "rejected")
                return
            if verified.status_code != 200:
                return
            expires = _instant(verified.json()["expires_at"])
            if expires - when > RENEW_WITHIN:
                await _tell(ctx.company_id, "current")
                return
            renewed = await client.post(f"{base}/company-peers/renew", headers=headers,
                                        json={"source_company_id": ctx.company_id})
        failed = {"date": expires.date().isoformat()}
        if renewed.status_code != 200:
            await _tell(ctx.company_id, "renew_failed", **failed)
            return
        reply = renewed.json()
        try:
            await store_api_key(CONNECTOR, reply["credential"], "", ctx.store_handle)
        except Exception:
            log.exception("renewed company key could not be stored")
            await _tell(ctx.company_id, "renew_failed", **failed)
            return
        await session.commit()
    ctx.access_token = reply["credential"]
    await _tell(ctx.company_id, "renewed", date=_instant(reply["expires_at"]).date().isoformat())


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def _lock(session, company_id: str) -> None:
    from sqlalchemy import text

    if session.get_bind().dialect.name != "postgresql":
        return
    key = int.from_bytes(hashlib.sha256(b"peer-key:" + company_id.encode()).digest()[:8],
                         "big", signed=True)
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


async def _current_context(ctx: ConnectorContext) -> ConnectorContext:
    """The key the relay holds now; another run may have renewed it since ctx was read."""
    return await fetch_context(ctx.company_id, CONNECTOR) or ctx


async def _tell(company_id: str, kind: str, **params) -> None:
    """Tell the company what happened to its key (see the module docstring)."""
    from celerp.db import get_session_ctx
    from celerp.notifications import service as notification_service
    from ui.i18n import t

    cid = uuid.UUID(company_id)
    async with get_session_ctx() as session:
        for key in STANDING:
            if key != f"notice.peer_key_{kind}":
                await notification_service.clear_standing(
                    session, cid, "integrations", t(f"{key}.title", "en"))
        if kind == "renewed":
            await notification_service.create_keyed(
                session, cid, "integrations", "notice.peer_key_renewed", params,
                action_url=ACTION_URL)
        elif kind in ("renew_failed", "rejected"):
            key = f"notice.peer_key_{kind}"
            await notification_service.notify_standing(
                session, cid, "integrations", t(f"{key}.title", "en"),
                t(f"{key}.body", "en", **params), action_url=ACTION_URL,
                i18n={"title": f"{key}.title", "body": f"{key}.body", "params": params})
        await session.commit()
