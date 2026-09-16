# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Regression for the post-commit nonce-cache revocation race."""
from __future__ import annotations

import pytest
from jose import jwt

from celerp.config import settings


@pytest.mark.asyncio
async def test_stale_process_cache_cannot_revive_revoked_access(client, session):
    from celerp.services.session_tracker import (
        _nonce_cache_set,
        get_nonce,
        invalidate_sessions,
    )

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

    _nonce_cache_set(user_id, n0)

    assert await get_nonce(session, user_id) == n1
    r = await client.get(
        "/auth/my-companies",
        headers={"Authorization": f"Bearer {access}"},
    )
    assert r.status_code == 401
