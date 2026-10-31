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
