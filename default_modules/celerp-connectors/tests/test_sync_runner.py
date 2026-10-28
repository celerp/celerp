# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""When Celerp Connect ends, a store connection pauses: no run starts, the company is
told once why and what resumes it, and renewing Connect picks up where it left off."""
from __future__ import annotations

import os
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import pytest
import respx
import sqlalchemy as sa

from connector_test_support import connect, connector_session, seed_company  # noqa: F401
from ui.i18n import t

RELAY = "https://relay.test"
STORE = "https://store.example.com"
WOO = {"products": "outbound", "stock": "outbound", "orders": "inbound", "customers": "off"}


@pytest.fixture
def relay():
    with patch("celerp.gateway.state.relay_http_url", return_value=RELAY), \
            patch("celerp.gateway.state.relay_session_headers",
                  return_value={"X-Session-Token": "s", "X-Instance-ID": "i"}), \
            patch("celerp.gateway.state.get_session_token", return_value="s"), \
            respx.mock:
        yield


def _token(status: int):
    route = respx.get(f"{RELAY}/tokens/woocommerce/access-token")
    route.mock(return_value=httpx.Response(status, json={"access_token": "k:s", "store_handle": STORE}
                                           if status == 200 else {"detail": "subscription required"}))


async def _notices(session, cid) -> list:
    from celerp.models.notification import Notification

    return list((await session.execute(sa.select(Notification.title).where(
        Notification.company_id == cid))).scalars().all())


@pytest.mark.asyncio
async def test_connect_lapse_pauses_with_notice(connector_session, relay):
    from celerp.connectors.daily_scheduler import check_and_run_daily_syncs
    from celerp.connectors.relay_token import ConnectRequired, fetch_context
    from celerp.models.sync_run import SyncRun

    session = connector_session
    cid = await seed_company(session, "Lapsed")
    config = await connect(session, cid, "woocommerce", WOO, first_sync="completed")
    _token(402)

    assert await check_and_run_daily_syncs(str(cid)) == []
    with pytest.raises(ConnectRequired) as paused:
        await fetch_context(str(cid), "woocommerce")
    assert str(paused.value) == t("connectors.connect_lapsed", service="WooCommerce")
    await session.refresh(config)
    assert config.connect_lapsed_at is not None
    assert config.last_daily_sync_at is None  # still due once Connect is renewed
    assert await session.scalar(sa.select(sa.func.count()).select_from(SyncRun).where(
        SyncRun.company_id == str(cid))) == 0
    # Told once per lapse, however many runs are refused.
    assert await _notices(session, cid) == [t("connectors.connect_lapsed_title", service="WooCommerce")]

    _token(200)
    ctx = await fetch_context(str(cid), "woocommerce")
    assert ctx is not None and ctx.store_handle == STORE
    await session.refresh(config)
    assert config.connect_lapsed_at is None
    assert await _notices(session, cid) == []


@pytest.mark.asyncio
async def test_a_paused_connection_says_so_on_its_card():
    from types import SimpleNamespace

    from fasthtml.common import to_xml
    from ui.routes.settings_connectors import _connector_card

    config = SimpleNamespace(entity_directions=WOO, first_sync_state="completed",
                             sync_frequency="realtime", connect_lapsed_at=datetime.now(timezone.utc))
    html = to_xml(_connector_card({"id": "woocommerce", "name": "WooCommerce", "connected": True,
                                   "auth_type": "api_key"}, None, RELAY, "c1", config=config))
    assert t("connectors.connect_lapsed", service="WooCommerce") in html
