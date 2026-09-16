# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Browser logout must revoke refresh-only sessions and clear relay cookies."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_logout_uses_refresh_fallback_and_clears_external_domain(method):
    from ui.app import app

    logout = AsyncMock()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://bup053.celerp.com",
        follow_redirects=False,
    ) as client:
        with patch("ui.routes.auth.api_logout", new=logout):
            r = await client.request(
                method,
                "/logout",
                cookies={"celerp_refresh": "refresh-only"},
            )

    assert r.status_code == 302
    logout.assert_awaited_once_with(None, "refresh-only")

    cookies = "\n".join(r.headers.get_list("set-cookie")).lower()
    assert "celerp_token=" in cookies
    assert "celerp_refresh=" in cookies
    assert "domain=bup053.celerp.com" in cookies
    assert "max-age=0" in cookies
