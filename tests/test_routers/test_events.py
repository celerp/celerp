# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Muxed /events/stream API route.

Characterization guard for the current muxed SSE route: it decodes the bearer
token manually and rejects an invalid or expired credential with 401 before the
stream is opened, so a bad token never yields event data.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from celerp.main import app


@pytest.mark.asyncio
async def test_events_stream_invalid_bearer_is_401():
    """An invalid bearer token to /events/stream is rejected with 401 and no event data.

    get_token_claims returns None for an undecodable token, so the route raises 401
    before subscribing or streaming.
    """
    app.state.limiter.enabled = False
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            "/events/stream",
            headers={"Authorization": "Bearer not-a-real-token"},
        )
    assert r.status_code == 401
    assert "event:" not in r.text
    assert "data:" not in r.text
