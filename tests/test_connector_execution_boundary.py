# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Every change a person makes to a store's products runs inside the connection's
execution of the products resource, as a sync does.

Publishing, unpublishing, stopping or resuming an item's sync, resolving a product
conflict and undoing the first sync's links are refused as busy while a products sync
runs, and a store client refuses to change anything with no execution running. Undo
and conflict resolution change only Celerp's records, so they need no store
credentials and work while the relay is unreachable."""
from __future__ import annotations

import asyncio
import contextvars
from unittest.mock import AsyncMock

import pytest

from celerp.connectors.base import ConnectorContext
from ui.i18n import t
from company_backup_support import company, owner, token
from connector_test_support import connected
from migration_support import real_client, real_engine  # noqa: F401

pytestmark = pytest.mark.asyncio

STORE = "https://shop.example.test"


def _auth(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


async def _outside(coro):
    """``coro`` in a task of its own, as another request runs: outside the execution of
    the task that awaits it."""
    return await asyncio.create_task(coro, context=contextvars.Context())


@pytest.fixture
def store_context(monkeypatch):
    fetch = AsyncMock(side_effect=lambda company_id, *_a, **_k: ConnectorContext(
        company_id=str(company_id), access_token="ck_x:cs_x"))
    monkeypatch.setattr("celerp.connectors.relay_token.fetch_context", fetch)
    return fetch


@pytest.fixture
async def shop(real_engine, real_client, connector_modules_running, signed_in_to_cloud, store_context):
    """A company connected to WooCommerce, its first sync completed, with one item."""
    user = await owner(real_engine)
    cid = str(await company(real_engine, user, "Boundary Trading", "boundary-marker"))
    tok = await token(real_engine, user, cid)
    await connected(cid, "woocommerce", {"products": "both"})
    r = await real_client.post("/items", headers=_auth(tok), json={
        "sku": "BOUND-1", "name": "Bound", "quantity": 2.0, "sell_by": "piece", "status": "available"})
    assert r.status_code == 200, r.text
    return cid, tok, r.json()["id"]


@pytest.mark.parametrize("action", ["publish", "unpublish", "stop"])
async def test_item_publish_waits_for_a_running_products_sync(shop, real_client, monkeypatch, action):
    """While a products sync runs, a person's publish, unpublish or stop is refused as
    busy and the store is not called; once the sync ends the same request runs."""
    from celerp.connectors.execution import connector_execution
    from celerp.connectors.woocommerce import WooCommerceConnector

    cid, tok, eid = shop
    publish = AsyncMock(return_value=eid)
    status = AsyncMock(return_value=None)
    monkeypatch.setattr(WooCommerceConnector, "ensure_product_link", publish)
    monkeypatch.setattr(WooCommerceConnector, "set_product_status", status)

    def _request():
        return real_client.post("/connector-items/woocommerce/sync", headers=_auth(tok),
                                json={"entity_ids": [eid], "action": action})

    async with connector_execution(cid, "woocommerce", "manual_sync", ["products"]):
        r = await _outside(_request())
        assert (r.status_code, r.json().get("detail")) == (409, t("connectors.fail_busy")), r.text
        assert publish.await_count == status.await_count == 0

    r = await _outside(_request())
    assert r.status_code == 200, r.text
    if action != "stop":
        assert publish.await_count + status.await_count == 1


async def test_conflict_resolution_and_link_undo_refuse_while_products_sync(shop, real_client):
    """Both write the items the products sync writes, so both wait for it."""
    from celerp.connectors.execution import connector_execution

    cid, tok, _ = shop
    async with connector_execution(cid, "woocommerce", "manual_sync", ["products"]):
        r = await _outside(real_client.post("/connectors/woocommerce/products/rec-1/resolve",
                                            headers=_auth(tok), json={"keep": "store"}))
        assert (r.status_code, r.json().get("detail")) == (409, t("connectors.fail_busy")), r.text
        r = await _outside(real_client.post("/connectors/woocommerce/first-sync/undo-links",
                                            headers=_auth(tok)))
        assert (r.status_code, r.json().get("detail")) == (409, t("connectors.fail_busy")), r.text


async def test_resolution_and_undo_need_no_store_credentials(shop, real_client, store_context):
    """Celerp-only work runs with the relay unreachable: it answers from Celerp's records."""
    cid, tok, _ = shop
    store_context.side_effect = None
    store_context.return_value = None
    r = await real_client.post("/connectors/woocommerce/products/rec-1/resolve",
                               headers=_auth(tok), json={"keep": "store"})
    assert (r.status_code, r.json().get("detail")) == (404, t("connectors.err_conflict_gone")), r.text
    r = await real_client.post("/connectors/woocommerce/first-sync/undo-links", headers=_auth(tok))
    assert (r.status_code, r.json().get("detail")) == (409, t("connectors.err_no_links_to_undo")), r.text
    assert store_context.await_count == 0


@pytest.mark.parametrize("method", ["post", "put", "delete"])
async def test_a_store_write_outside_an_execution_is_refused(method):
    """A store client changes nothing unless an execution runs; reads stay open (previews)."""
    from celerp.connectors.execution import RemoteCallOutsideExecution, before_remote_call
    from celerp.connectors.woocommerce import _http_client

    async with _http_client() as client:
        with pytest.raises(RemoteCallOutsideExecution):
            await getattr(client, method)(f"{STORE}/wp-json/wc/v3/products/1")
    before_remote_call("GET", require_execution=True)


async def test_a_local_execution_refuses_any_store_call(shop):
    """Undo and resolution hold no store credentials, and a store call inside them is refused."""
    from celerp.connectors.execution import RemoteCallOutsideExecution, connector_execution
    from celerp.connectors.woocommerce import _http_client

    cid, _, _ = shop
    async with connector_execution(cid, "woocommerce", "resolve", ["products"]) as ex:
        assert ex.ctx is None
        async with _http_client() as client:
            with pytest.raises(RemoteCallOutsideExecution):
                await client.get(f"{STORE}/wp-json/wc/v3/products/1")


async def test_every_connector_store_client_requires_an_execution():
    """Census: a connector's HTTP client is built requiring an execution, so a new
    store-changing path cannot skip the boundary. The exemptions are clients that never
    change a store."""
    import ast
    from pathlib import Path

    exempt = {"celerp_key.py": "verifies a peer key with a GET at connect"}
    root = Path(__file__).resolve().parents[1] / "celerp" / "connectors"
    missing = []
    for path in sorted(root.glob("*.py")):
        if path.name in exempt or path.name == "http.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "RateLimitedClient":
                kw = {k.arg: k.value for k in node.keywords}
                if not (isinstance(kw.get("require_execution"), ast.Constant)
                        and kw["require_execution"].value is True):
                    missing.append(f"{path.name}:{node.lineno}")
    assert missing == []


_ORDER_DOC = {"woocommerce_order_id": "7", "total": 25.0, "currency": "USD", "line_items": [{"name": "Blue mug"}]}


@pytest.fixture
async def unbound_shop(real_engine, connector_modules_running, signed_in_to_cloud, monkeypatch):
    """A connected company holding an imported WooCommerce order, whose store the
    connection has not been checked against yet."""
    from datetime import datetime, timezone

    from celerp.db import get_session_ctx
    from celerp.models.projections import Projection

    user = await owner(real_engine)
    cid = str(await company(real_engine, user, "Proof Trading", "proof-marker"))
    tok = await token(real_engine, user, cid)
    await connected(cid, "woocommerce", {"products": "both", "orders": "inbound"})
    async with get_session_ctx() as session:
        now = datetime.now(timezone.utc)
        session.add(Projection(company_id=cid, entity_id="doc:woocommerce:order:7", entity_type="doc",
                               state=_ORDER_DOC, version=1, created_at=now, updated_at=now))
        await session.commit()
    monkeypatch.setattr("celerp.connectors.relay_token.fetch_context", AsyncMock(
        side_effect=lambda company_id, *_a, **_k: ConnectorContext(
            company_id=str(company_id), access_token="ck_x:cs_x", store_handle=STORE)))
    return cid, tok


async def _bound_store(cid) -> str | None:
    import sqlalchemy as sa

    from celerp.db import get_session_ctx
    from celerp.models.connector_source import ConnectorSource

    async with get_session_ctx() as session:
        return await session.scalar(sa.select(ConnectorSource.store_handle).where(
            ConnectorSource.company_id == cid, ConnectorSource.connector == "woocommerce"))


@pytest.mark.parametrize("purpose, resource", [
    ("manual_sync", "products"), ("schedule", "orders"), ("webhook", "orders"), ("outbound_queue", "products"),
    ("item_publish", "products"), ("rerun", "products"), ("webhooks", "webhooks"),
])
async def test_every_execution_proves_the_store_first(unbound_shop, monkeypatch, purpose, resource):
    """Every execution that reaches the store first checks it is the store the company's
    records came from: a store without them refuses the work before it runs, in the
    check's own words; the store that has them is bound once and not asked again."""
    from celerp.connectors.execution import ExecutionRefused, connector_execution
    from celerp.connectors.woocommerce import WooCommerceConnector

    cid, _ = unbound_shop
    same_store = AsyncMock(return_value=False)
    monkeypatch.setattr(WooCommerceConnector, "same_store", same_store)
    ran = []
    with pytest.raises(ExecutionRefused) as refused:
        async with connector_execution(cid, "woocommerce", purpose, [resource]):
            ran.append(purpose)
    assert (refused.value.reason, ran) == ("store_changed", [])
    assert "does not have them" in refused.value.detail
    assert await _bound_store(cid) is None

    same_store.return_value = True
    for _ in range(2):
        async with connector_execution(cid, "woocommerce", purpose, [resource]):
            ran.append(purpose)
    assert ran == [purpose, purpose] and same_store.await_count == 2
    assert await _bound_store(cid) == STORE


async def test_a_first_sync_on_another_store_registers_no_webhook_and_says_why(unbound_shop, monkeypatch):
    """The first sync's webhook registration runs inside its execution, so a store without
    the company's records gets no webhook and the first sync needs attention with the reason."""
    from celerp.connectors import webhook_reconcile
    from celerp.connectors.execution import ExecutionRefused
    from celerp.connectors.sync_runner import execute_first_sync
    from celerp.connectors.woocommerce import WooCommerceConnector
    from celerp.db import get_session_ctx
    from celerp.models.connector_first_sync import ConnectorFirstSync
    from connector_test_support import record_first_sync
    from celerp.models.connector_config import ConnectorConfig
    import sqlalchemy as sa

    cid, _ = unbound_shop
    async with get_session_ctx() as session:
        config = await session.scalar(sa.select(ConnectorConfig).where(
            ConnectorConfig.company_id == cid, ConnectorConfig.connector == "woocommerce"))
        await session.execute(sa.delete(ConnectorFirstSync).where(ConnectorFirstSync.company_id == cid))
        await record_first_sync(session, config, "starting")
        await session.commit()
    monkeypatch.setattr(WooCommerceConnector, "same_store", AsyncMock(return_value=False))
    reconcile = AsyncMock()
    monkeypatch.setattr(webhook_reconcile, "reconcile_webhooks", reconcile)

    with pytest.raises(ExecutionRefused):
        await execute_first_sync(cid, "woocommerce")
    reconcile.assert_not_awaited()
    async with get_session_ctx() as session:
        row = await session.get(ConnectorFirstSync, (cid, "woocommerce"))
        assert row.state == "failed" and "does not have them" in row.error["detail"]


async def test_a_sync_refused_for_another_store_is_on_the_sync_history(unbound_shop, monkeypatch):
    """A person's sync on a store without the company's records shows why on the sync history."""
    import sqlalchemy as sa

    from celerp import connectors
    from celerp.connectors.execution import ExecutionRefused
    from celerp.connectors.sync_runner import run_connector_sync
    from celerp.connectors.woocommerce import WooCommerceConnector
    from celerp.db import get_session_ctx
    from celerp.models.sync_run import SyncRun

    cid, _ = unbound_shop
    monkeypatch.setattr(WooCommerceConnector, "same_store", AsyncMock(return_value=False))
    with pytest.raises(ExecutionRefused):
        await run_connector_sync(connectors.get("woocommerce"), cid, purpose="manual_sync")
    async with get_session_ctx() as session:
        runs = (await session.execute(sa.select(SyncRun.entity, SyncRun.status, SyncRun.errors_json)
                                      .where(SyncRun.company_id == cid))).all()
    assert {entity for entity, _, _ in runs} >= {"products", "orders"}
    assert all(status == "failed" and "does not have them" in errors for _, status, errors in runs)


async def test_item_publish_on_another_store_is_refused_with_the_reason(unbound_shop, real_client, monkeypatch):
    from celerp.connectors.woocommerce import WooCommerceConnector

    _, tok = unbound_shop
    r = await real_client.post("/items", headers=_auth(tok), json={
        "sku": "PROOF-1", "name": "Proof", "quantity": 1.0, "sell_by": "piece", "status": "available"})
    assert r.status_code == 200, r.text
    eid = r.json()["id"]
    monkeypatch.setattr(WooCommerceConnector, "same_store", AsyncMock(return_value=False))
    publish = AsyncMock()
    monkeypatch.setattr(WooCommerceConnector, "ensure_product_link", publish)
    r = await real_client.post("/connector-items/woocommerce/sync", headers=_auth(tok),
                               json={"entity_ids": [eid], "action": "publish"})
    assert r.status_code == 409 and "does not have them" in r.json()["detail"], r.text
    publish.assert_not_awaited()


async def test_store_proof_makes_no_network_call_under_the_company_lock(unbound_shop, monkeypatch):
    """While the store is asked, the company row is free: another request takes its lock
    without waiting. A sample that changed before the binding is written refuses it."""
    import sqlalchemy as sa

    from celerp.connectors.execution import ExecutionRefused, connector_execution
    from celerp.connectors.woocommerce import WooCommerceConnector
    from celerp.db import get_session_ctx
    from celerp.models.projections import Projection

    cid, _ = unbound_shop

    async def lock_free() -> None:
        async with get_session_ctx() as session:
            await session.execute(sa.text("SET LOCAL lock_timeout = '100ms'"))
            await session.execute(sa.text("SELECT 1 FROM companies WHERE id = :c FOR UPDATE NOWAIT"), {"c": cid})
            await session.rollback()

    async def asks_with_no_lock(self, ctx, records):
        await _outside(lock_free())
        return True

    monkeypatch.setattr(WooCommerceConnector, "same_store", asks_with_no_lock)
    async with connector_execution(cid, "woocommerce", "manual_sync", ["orders"]):
        pass
    assert await _bound_store(cid) == STORE

    # A new connection is checked again; a record imported while the store was asked
    # changes the sample, so the binding is refused rather than written on a stale proof.
    async with get_session_ctx() as session:
        await session.execute(sa.text(
            "UPDATE connector_sources SET config_id = NULL WHERE company_id = :c"), {"c": cid})
        await session.commit()

    async def import_while_asked(self, ctx, records):
        from datetime import datetime, timezone

        async with get_session_ctx() as session:
            now = datetime.now(timezone.utc)
            session.add(Projection(company_id=cid, entity_id="doc:woocommerce:order:8", entity_type="doc",
                                   state={**_ORDER_DOC, "woocommerce_order_id": "8"}, version=1,
                                   created_at=now, updated_at=now))
            await session.commit()
        return True

    monkeypatch.setattr(WooCommerceConnector, "same_store", import_while_asked)
    with pytest.raises(ExecutionRefused) as refused:
        async with connector_execution(cid, "woocommerce", "manual_sync", ["orders"]):
            pass
    assert refused.value.reason == "store_changed"
