# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Require an active Celerp Connect session for cloud-gated routes."""
from __future__ import annotations

from fastapi import HTTPException, Request, status

from celerp.gateway.state import get_session_token


async def require_session_token(request: Request) -> None:
    """Reject requests when this installation has no active Connect session."""
    current = get_session_token()
    supplied = request.headers.get("X-Session-Token", "").strip()

    if supplied:
        if not current:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=(
                    "This instance is not connected to Celerp Connect. "
                    "Connect web access under Settings > Web Access and try again."
                ),
            )
        if supplied != current:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=(
                    "The Connect session is no longer valid. "
                    "Reconnect web access under Settings > Web Access."
                ),
            )
        return

    if current:
        return

    from celerp.config import settings
    if not settings.cloud_disconnected:
        from celerp.services.cloud_entitlement import sync_existing_entitlement
        await sync_existing_entitlement(require_persisted_key=True)
        if get_session_token():
            return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=(
            "No active Celerp Connect session. Connect or reconnect web access "
            "under Settings > Web Access, then try again."
        ),
    )
