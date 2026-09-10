# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Route tests for the in-app commercial checkout mint route.

/commercial/checkout is the click-time handoff every subscribe/top-up CTA points
at. For a direct celerp_direct checkout it mints a single-use handoff token on the
relay and 302-bounces the browser to the checkout URL with the token appended; for
a partner-managed or Enterprise destination it bounces token-free. A mint failure
degrades to a neutral in-app error page with no redirect and no fabricated URL. The
relay bearer JWT never rides the redirect URL.
"""

from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

import celerp.gateway.state as gw_state
from test_helpers import authed_cookies


@pytest.fixture(autouse=True)
def _reset_commercial_context():
    saved = gw_state._commercial_context
    gw_state._commercial_context = {"commercial_mode": "celerp_direct"}
    yield
    gw_state._commercial_context = saved


@pytest_asyncio.fixture
async def client():
    """UI-app client with follow_redirects off so the 302 Location is asserted
    directly rather than followed."""
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app),
                           base_url="http://ui", follow_redirects=False) as c:
        yield c


@pytest.mark.asyncio
async def test_commercial_checkout_handoff_route_mints_and_redirects(client):
    """A direct celerp_direct destination mints a handoff token and 302-redirects
    to celerp.com with handoff_token appended; a partner-managed/Enterprise
    destination 302s with no token minted and no relay call. The relay bearer JWT
    never appears in the redirect URL."""
    # Direct destination: token minted and appended.
    with patch("ui.routes.commercial._mint_handoff_token",
               AsyncMock(return_value="ht_opaque_value")) as mint:
        r = await client.get("/commercial/checkout?intent=subscribe&sku=cloud",
                             cookies=authed_cookies())
    assert r.status_code == 302
    location = r.headers["location"]
    assert "celerp.com/subscribe" in location
    assert "plan=cloud" in location
    assert "handoff_token=ht_opaque_value" in location
    assert "Bearer" not in location
    assert "access_token" not in location
    mint.assert_awaited_once()
    assert mint.await_args.args[0] == "subscribe"

    # Partner-managed destination: no token minted, no relay call.
    gw_state._commercial_context = {"commercial_mode": "partner_managed",
                                    "implementation": {"display_name": "Partner Co"}}
    with patch("ui.routes.commercial._mint_handoff_token",
               AsyncMock(return_value="ht_should_not_be_used")) as mint2:
        r2 = await client.get("/commercial/checkout?intent=subscribe&sku=team",
                              cookies=authed_cookies())
    assert r2.status_code == 302
    loc2 = r2.headers["location"]
    assert "/enterprise" in loc2
    assert "handoff_token=" not in loc2
    mint2.assert_not_awaited()


@pytest.mark.asyncio
async def test_commercial_checkout_handoff_route_topup_purpose(client):
    """An intent=topup click mints a topup-purpose token for the direct
    /subscribe/topup destination."""
    with patch("ui.routes.commercial._mint_handoff_token",
               AsyncMock(return_value="ht_topup")) as mint:
        r = await client.get("/commercial/checkout?intent=topup&sku=ai",
                             cookies=authed_cookies())
    assert r.status_code == 302
    location = r.headers["location"]
    assert "celerp.com/subscribe/topup" in location
    assert "handoff_token=ht_topup" in location
    mint.assert_awaited_once()
    assert mint.await_args.args[0] == "topup"


@pytest.mark.asyncio
async def test_commercial_checkout_handoff_mint_failure_degrades(client):
    """A relay-down mint degrades to a neutral in-app error page: 200, no redirect,
    no fabricated checkout URL."""
    with patch("ui.routes.commercial._mint_handoff_token",
               AsyncMock(side_effect=RuntimeError("relay down"))):
        r = await client.get("/commercial/checkout?intent=subscribe&sku=cloud",
                             cookies=authed_cookies())
    assert r.status_code == 200
    assert "location" not in r.headers
    body = r.text
    assert "celerp.com/subscribe" not in body
    assert "handoff_token" not in body


@pytest.mark.asyncio
async def test_commercial_checkout_partner_support_url_with_subscribe_in_path(client):
    """A partner support URL that happens to contain "/subscribe" in its path
    (e.g. a help article) is not a Celerp direct checkout: no handoff token is
    minted and no relay call is made, even though the substring "/subscribe"
    appears in the destination."""
    gw_state._commercial_context = {
        "commercial_mode": "partner_managed",
        "implementation": {
            "display_name": "Partner Co",
            "support_url": "https://partner.example/subscribe/help",
        },
    }
    with patch("ui.routes.commercial._mint_handoff_token",
               AsyncMock(return_value="ht_should_not_be_used")) as mint:
        r = await client.get("/commercial/checkout?intent=subscribe&sku=cloud",
                             cookies=authed_cookies())
    assert r.status_code == 302
    location = r.headers["location"]
    assert location == "https://partner.example/subscribe/help"
    assert "handoff_token=" not in location
    mint.assert_not_awaited()


@pytest.mark.asyncio
async def test_commercial_checkout_requires_auth(client):
    """No app session bounces to /login, never minting a token."""
    with patch("ui.routes.commercial._mint_handoff_token",
               AsyncMock(return_value="ht_should_not_mint")) as mint:
        r = await client.get("/commercial/checkout?intent=subscribe&sku=cloud")
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login")
    mint.assert_not_awaited()
