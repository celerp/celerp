# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Start runs the first sync the person reviewed, or none. The preview carries a version
of everything the first sync acts on (the connection, the store, the directions, the
order date, each store product and the item it links to); Start hands that version back,
reads the preview again outside every lock with the submitted choices, and refuses when
anything changed since: the store's products, Celerp's catalog, the directions or the
connection itself. A started first sync records the version and the store products it
was reviewed for.

These run on the real database (each request commits), as the start route commits its
short read before reading the store."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import sqlalchemy as sa

from company_backup_support import company, owner, token
from connector_test_support import add_item, connected
from migration_support import maker, real_client, real_engine  # noqa: F401

pytestmark = pytest.mark.asyncio

PLATFORM = "woocommerce"
DIRECTIONS = {"products": "inbound"}


def _product(key: str, sku: str, name: str | None = None):
    from celerp.connectors.base import RemoteProduct

    return RemoteProduct(key=key, product_id=key, variation_id=None, sku=sku, name=name or sku,
                         description=None, price=10.0, modified=None)


@pytest_asyncio.fixture
async def store(real_engine, real_client, connector_modules_running, signed_in_to_cloud, monkeypatch):
    """A company with a pending first sync to a store whose products the test sets, and
    the API signed in as its owner. The background first sync is not run: these tests are
    about what Start accepts and records."""
    from celerp import connectors
    from celerp.connectors.base import ConnectorContext
    import celerp_connectors.routes as routes

    user = await owner(real_engine)
    cid = await company(real_engine, user, "Receipt Trading", "receipt-marker")
    tok = await token(real_engine, user, cid)
    await connected(cid, PLATFORM, DIRECTIONS, first_sync="pending")

    calls: list[str] = []
    products = [_product("1", "STORE-1"), _product("2", "STORE-2")]

    async def _fetch_context(company_id, *_a, **_k):
        calls.append("store:context")
        return ConnectorContext(company_id=str(company_id), access_token="t",
                                store_handle="https://store.example.com")

    async def _preview_products(self, ctx):
        calls.append("store:products")
        return list(products)

    monkeypatch.setattr("celerp.connectors.relay_token.fetch_context", _fetch_context)
    monkeypatch.setattr(type(connectors.get(PLATFORM)), "preview_products", _preview_products)
    monkeypatch.setattr("celerp.connectors.webhook_reconcile.reconcile_webhooks", AsyncMock())
    monkeypatch.setattr(routes, "_spawn_first_sync", lambda *_a: calls.append("spawn"))
    headers = {"Authorization": f"Bearer {tok}"}

    async def preview() -> dict:
        r = await real_client.get(f"/connectors/{PLATFORM}/first-sync/preview", headers=headers)
        assert r.status_code == 200, r.text
        return r.json()

    async def start(version: str, directions: dict = DIRECTIONS):
        return await real_client.post(f"/connectors/{PLATFORM}/first-sync", headers=headers, json={
            "directions": directions, "match": "keep", "preview_version": version})

    async def record():
        from celerp.models.connector_first_sync import ConnectorFirstSync

        async with maker(real_engine)() as s:
            return await s.get(ConnectorFirstSync, (str(cid), PLATFORM))

    yield SimpleNamespace(cid=cid, products=products, calls=calls, preview=preview, start=start,
                          record=record, engine=real_engine)


async def test_start_refuses_a_preview_that_changed(store):
    from celerp.db import get_session_ctx
    from ui.i18n import t

    refused = t("connectors.err_preview_changed", service="WooCommerce")

    # The store swaps a product for another.
    seen = await store.preview()
    store.products[1] = _product("3", "STORE-3")
    r = await store.start(seen["version"])
    assert r.status_code == 409 and r.json()["detail"] == refused, r.text

    # Celerp's catalog gains an item with a store product's SKU: it now links instead of
    # being created.
    seen = await store.preview()
    async with get_session_ctx() as session:
        await add_item(session, store.cid, "item:store-1", "STORE-1")
    r = await store.start(seen["version"])
    assert r.status_code == 409 and r.json()["detail"] == refused, r.text

    # The person changed the directions after the preview was read.
    seen = await store.preview()
    r = await store.start(seen["version"], {"products": "both"})
    assert r.status_code == 409 and r.json()["detail"] == refused, r.text

    record = await store.record()
    assert record.state == "pending" and record.preview_version is None and record.reviewed is None
    assert "spawn" not in store.calls


async def test_start_refuses_after_a_reconnect(store, monkeypatch):
    """The connection is replaced while Start reads the store: the version it read was
    for the old connection, so the first sync is not started on the new one."""
    import mutation_observer
    from celerp import connectors
    from celerp.models.connector_config import ConnectorConfig

    seen = await store.preview()
    read = type(connectors.get(PLATFORM)).preview_products

    async def _reconnecting(self, ctx):
        with mutation_observer.another_client():
            await _reconnect()
        return await read(self, ctx)

    async def _reconnect():
        async with maker(store.engine)() as s:
            old = (await s.scalars(sa.select(ConnectorConfig).where(
                ConnectorConfig.company_id == str(store.cid), ConnectorConfig.connector == PLATFORM))).one()
            fields = {c.key: getattr(old, c.key) for c in sa.inspect(ConnectorConfig).column_attrs if c.key != "id"}
            await s.delete(old)
            await s.flush()
            s.add(ConnectorConfig(**fields))
            await s.commit()

    monkeypatch.setattr(type(connectors.get(PLATFORM)), "preview_products", _reconnecting)
    r = await store.start(seen["version"])
    assert r.status_code == 409, r.text
    record = await store.record()
    assert record.state == "pending" and record.reviewed is None


async def test_start_makes_no_store_call_under_the_operation_lock(store, monkeypatch):
    from celerp.connectors import ownership

    lock = ownership.lock_connector_operation

    async def _locking(*a, **k):
        store.calls.append(f"lock:{'exclusive' if k.get('exclusive') else 'shared'}")
        return await lock(*a, **k)

    seen = await store.preview()
    store.calls.clear()
    monkeypatch.setattr(ownership, "lock_connector_operation", _locking)
    r = await store.start(seen["version"])
    assert r.status_code == 200, r.text
    # The short read of the connection commits before the store is read; the exclusive
    # lock is taken only after.
    assert store.calls == ["lock:shared", "store:context", "store:products", "lock:exclusive", "spawn"]


async def test_start_records_the_reviewed_version_and_products(store):
    from celerp.db import get_session_ctx

    async with get_session_ctx() as session:
        await add_item(session, store.cid, "item:store-2", "STORE-2")
    seen = await store.preview()
    r = await store.start(seen["version"])
    assert r.status_code == 200, r.text

    record = await store.record()
    assert record.state == "starting"
    assert record.preview_version == seen["version"]
    assert record.reviewed == {"1": None, "2": "item:store-2"}


async def test_start_without_a_preview_version_is_refused(store):
    r = await store.start("")
    assert r.status_code == 422, r.text
    assert (await store.record()).state == "pending"
