# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Postgres is the only nonce authority: a revoked access token stays revoked."""
from __future__ import annotations

import pytest
from jose import jwt

from celerp.config import settings


@pytest.mark.asyncio
async def test_revoked_access_stays_revoked(client, session):
    from celerp.services.session_tracker import get_nonce, invalidate_sessions

    reg = await client.post(
        "/auth/register",
        json={
            "company_name": "Nonce Authority Co",
            "email": "nonce-authority@example.com",
            "name": "Owner",
            "password": "pw123456",
        },
    )
    assert reg.status_code == 200, reg.text
    access = reg.json()["access_token"]
    claims = jwt.decode(access, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    user_id = claims["sub"]
    n0 = claims["snonce"]

    await invalidate_sessions(session, user_id)
    n1 = await get_nonce(session, user_id)
    assert n1 != n0
    r = await client.get(
        "/auth/my-companies",
        headers={"Authorization": f"Bearer {access}"},
    )
    assert r.status_code == 401


def test_no_process_local_nonce_cache_remains():
    """Postgres is the only nonce authority: no cache helpers survive to be called."""
    import celerp.services.session_tracker as tracker

    left = [name for name in ("_nonce_cache_set", "_nonce_cache_get", "_nonce_cache_bust",
                              "_nonce_cache_bust_all", "get_nonce_from_cache") if hasattr(tracker, name)]
    assert left == []


@pytest.mark.asyncio
async def test_debug_cache_stats_answers():
    from celerp.routers import debug

    stats = await debug.cache_stats()
    assert set(stats) == {"drain_cache"}
