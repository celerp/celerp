# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from celerp import __version__
from celerp.db import get_session
from celerp.services.system_health import get_system_health

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": __version__}


@router.get("/health/ready")
async def readiness(session: AsyncSession = Depends(get_session)) -> dict:
    try:
        await session.execute(text("SELECT 1"))
        return {"status": "ok", "db": "ok"}
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(503, detail=f"DB not reachable: {e}")


@router.get("/health/system")
async def system_health() -> dict:
    return get_system_health()


@router.get("/settings/cloud-status")
async def cloud_status() -> dict:
    """Return cloud connection status, tier, last backup date, and email quota."""
    from celerp.config import settings
    from celerp.gateway.state import get_session_token
    connected = bool(settings.gateway_token)
    if not connected:
        return {"connected": False, "tier": None, "last_backup": None, "email_quota": 0, "email_used": 0}

    # Try to fetch relay status from cloud
    tier: str | None = None
    last_backup: str | None = None
    email_quota: int = 0
    email_used: int = 0
    try:
        import httpx
        http_url = settings.gateway_http_url or settings.gateway_url.replace("wss://", "https://").replace("ws://", "http://").replace("/ws/connect", "")
        instance_id = settings.gateway_instance_id
        session_token = get_session_token()
        if instance_id and session_token:
            async with httpx.AsyncClient(base_url=http_url, timeout=3.0) as c:
                r = await c.get(
                    "/billing/status",
                    params={"instance_id": instance_id, "session_token": session_token},
                )
                if r.status_code == 200:
                    data = r.json()
                    tier = data.get("tier")
                    last_backup = data.get("last_backup")
                    email_quota = int(data.get("email_quota", 0))
                    email_used = int(data.get("email_used", 0))
    except Exception:
        pass

    return {"connected": True, "tier": tier, "last_backup": last_backup, "email_quota": email_quota, "email_used": email_used}


@router.get("/settings/email-status")
async def email_status() -> dict:
    """Return whether SMTP and/or gateway are configured for email sending."""
    from celerp.config import settings
    return {
        "smtp_configured": bool(settings.smtp_host),
        "gateway_connected": bool(settings.gateway_token),
    }
