# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Connector routes reach the shared connector services.

POST /connectors/{name}/sync-plan writes nothing itself: it reads the company's
connector row, fetches the channel context and starts run_connector_sync in the
background with the stored direction. Every record the sync writes goes through
that runner.

POST /connectors/{name}/credentials claims the connector for the company through
claim_connector_ownership, stores the credential on the relay and saves the
webhook secret on the same connector row.
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

pytestmark = pytest.mark.asyncio

SESSION_TOKEN = "test-session-token-connector-routes"
RELAY = "https://relay.test"
STORE = "https://store.example.test"


async def _register(client) -> tuple[dict, str]:
    from celerp.services.auth import decode_access_token
    r = await client.post("/auth/register", json={
        "company_name": "Plan Co", "email": f"plan-{uuid.uuid4().hex[:8]}@example.test",
        "name": "Owner", "password": "pwvalid12"})
    assert r.status_code == 200, r.text
    tok = r.json()["access_token"]
    return ({"Authorization": f"Bearer {tok}", "X-Session-Token": SESSION_TOKEN},
            decode_access_token(tok)["company_id"])


@pytest.fixture
def connect_session(monkeypatch):
    from celerp.gateway import state
    monkeypatch.setattr(state, "_session_token", SESSION_TOKEN)


async def test_sync_plan_runs_the_shared_sync_runner_with_the_stored_direction(client, session, connect_session):
    from celerp.connectors.base import SyncDirection
    from celerp.models.connector_config import ConnectorConfig

    h, cid = await _register(client)
    session.add(ConnectorConfig(company_id=cid, connector="woocommerce",
                                direction=SyncDirection.INBOUND.value))
    await session.commit()
    ctx = object()
    runner = AsyncMock()
    with patch("celerp_connectors.routes._connector_context", AsyncMock(return_value=ctx)), \
         patch("celerp.connectors.sync_runner.run_connector_sync", runner):
        r = await client.post("/connectors/woocommerce/sync-plan", headers=h)
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True}
        for _ in range(50):
            if runner.await_count:
                break
            await asyncio.sleep(0.01)
    assert runner.await_count == 1
    connector, passed_ctx = runner.await_args.args
    assert connector.name == "woocommerce" and passed_ctx is ctx
    assert runner.await_args.kwargs == {"direction": SyncDirection.INBOUND}


async def test_sync_plan_refuses_a_channel_that_is_not_connected(client, connect_session):
    h, _ = await _register(client)
    runner = AsyncMock()
    with patch("celerp.connectors.sync_runner.run_connector_sync", runner):
        r = await client.post("/connectors/woocommerce/sync-plan", headers=h)
        unknown = await client.post("/connectors/no-such-channel/sync-plan", headers=h)
    assert r.status_code == 409
    assert unknown.status_code == 404
    assert runner.await_count == 0


async def test_store_credentials_claims_the_connector_and_keeps_its_webhook_secret(client, session, connect_session):
    from sqlalchemy import select

    from celerp.models.connector_config import ConnectorConfig

    h, cid = await _register(client)
    body = {"consumer_key": "ck_x", "consumer_secret": "cs_y", "store_url": STORE + "/"}
    with patch("celerp.gateway.state.relay_http_url", return_value=RELAY), \
         patch("celerp.gateway.state.relay_session_headers", return_value={"X-Session-Token": "s"}), \
         patch("celerp.services.outbound_url.validate_public_base_url", AsyncMock(return_value=STORE)), \
         patch("celerp.services.outbound_url.fetch_public_bytes",
               AsyncMock(return_value=SimpleNamespace(status_code=200))), \
         patch("celerp.connectors.remote_state.revoke_connector_remote_state", AsyncMock()), \
         patch("celerp.connectors.woocommerce.WooCommerceConnector.register_webhooks",
               AsyncMock(return_value=["11"])), \
         patch("celerp.connectors.woocommerce.WooCommerceConnector.deregister_webhooks", AsyncMock()), \
         respx.mock(assert_all_called=False) as relay:
        stored = relay.post(f"{RELAY}/tokens/woocommerce").mock(
            return_value=httpx.Response(200, json={"stored": True}))
        r = await client.post("/connectors/woocommerce/credentials", headers=h, json=body)
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True}
        assert stored.call_count == 1
        again = await client.post("/connectors/woocommerce/credentials", headers=h, json=body)
    assert again.json()["error"] == "already_connected"
    assert stored.call_count == 1

    rows = (await session.execute(select(ConnectorConfig).where(
        ConnectorConfig.connector == "woocommerce"))).scalars().all()
    assert [str(row.company_id) for row in rows] == [cid]
    assert rows[0].webhook_ids == ["11"] and rows[0].webhook_secret
