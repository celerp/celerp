# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""The store's webhooks are changed by one writer at a time, from the configuration it reads itself.

Webhook state is an execution resource ("webhooks", one per company and connector). Only the
execution holding it talks to the store about webhooks; a direction change only marks the
webhooks dirty and records a rerun. So a reconciler that read the directions before a change
can never remove a hook the change needs, two reconcilers never both delete each other's
duplicate, and nothing creates hooks on a store that was disconnected meanwhile."""

from __future__ import annotations

import asyncio
import json
import re
import uuid

import httpx
import pytest
import respx
import sqlalchemy as sa

from test_import_preflight_invariants import _factory, _seed_race_company

pytestmark = pytest.mark.asyncio

_STORE = "https://shop.test"
_DELIVERY = "https://relay.test/wh"
_SECRET = "s3"
_NO_ORDERS = {"products": "outbound", "stock": "outbound", "orders": "off", "customers": "off"}
_ORDER_TOPICS = {"order.created", "order.updated", "order.deleted"}


class FakeStore:
    """WooCommerce's webhook endpoints over an in-memory list. `pause_list` holds the next
    listing call until released, so a test can interleave a second writer."""

    def __init__(self) -> None:
        self.hooks: dict[int, dict] = {}
        self.writes: list[tuple[str, int, str]] = []
        self.next_id = 100
        self.pause_list: tuple[asyncio.Event, asyncio.Event] | None = None

    def add(self, hook_id: int, topic: str, secret: str = _SECRET, status: str = "active") -> None:
        self.hooks[hook_id] = {"id": hook_id, "topic": topic, "delivery_url": _DELIVERY,
                               "secret": secret, "status": status, "name": f"Celerp {topic}"}

    def topics(self) -> dict[str, list[int]]:
        out: dict[str, list[int]] = {}
        for hook_id, hook in sorted(self.hooks.items()):
            if hook["status"] == "active":
                out.setdefault(hook["topic"], []).append(hook_id)
        return out

    async def _list(self, request: httpx.Request) -> httpx.Response:
        if self.pause_list is not None:
            entered, go = self.pause_list
            self.pause_list = None
            entered.set()
            await go.wait()
        page = int(request.url.params.get("page", "1"))
        body = [dict(h) for h in self.hooks.values()] if page == 1 else []
        return httpx.Response(200, json=body)

    def _one(self, request: httpx.Request) -> httpx.Response:
        hook_id = int(request.url.path.rsplit("/", 1)[1])
        if request.method == "DELETE":
            self.writes.append(("delete", hook_id, ""))
            return httpx.Response(200 if self.hooks.pop(hook_id, None) else 404, json={})
        hook = self.hooks.get(hook_id)
        if hook is None:
            return httpx.Response(404, json={})
        if request.method == "PUT":
            hook.update(json.loads(request.content))
            self.writes.append(("update", hook_id, hook["topic"]))
        return httpx.Response(200, json=dict(hook))

    def _create(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.next_id += 1
        self.add(self.next_id, body["topic"], body.get("secret", ""), body.get("status", "active"))
        self.writes.append(("create", self.next_id, body["topic"]))
        return httpx.Response(201, json=dict(self.hooks[self.next_id]))

    def mount(self, router: respx.Router) -> None:
        router.get(f"{_STORE}/wp-json/wc/v3/webhooks").mock(side_effect=self._list)
        router.post(f"{_STORE}/wp-json/wc/v3/webhooks").mock(side_effect=self._create)
        router.route(url__regex=re.escape(_STORE) + r"/wp-json/wc/v3/webhooks/\d+").mock(side_effect=self._one)


@pytest.fixture
def store(committed_engine, monkeypatch):
    from unittest.mock import AsyncMock

    from celerp.connectors.base import ConnectorContext

    fake = FakeStore()
    factory = _factory(committed_engine)
    monkeypatch.setattr("celerp.db.SessionLocal", factory)
    monkeypatch.setattr("celerp.db.LifecycleSessionLocal", factory)

    async def _context(company_id, *_a, **_k):
        return ConnectorContext(company_id=str(company_id), access_token="k:s", store_handle=_STORE)

    monkeypatch.setattr("celerp.connectors.relay_token.fetch_context", AsyncMock(side_effect=_context))
    monkeypatch.setattr("celerp.connectors.woocommerce.webhook_delivery_url", lambda: _DELIVERY)
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        yield fake, factory


async def _connected(factory, fake: FakeStore, directions: dict) -> uuid.UUID:
    """A committed WooCommerce connection whose first sync is done and whose store holds
    exactly the hooks the directions call for."""
    from celerp import connectors
    from celerp.models.connector_config import ConnectorConfig
    from celerp.models.connector_first_sync import ConnectorFirstSync

    company_id, _user_id = await _seed_race_company(factory)
    async with factory() as s:
        s.add(ConnectorConfig(company_id=str(company_id), connector="woocommerce",
                              entity_directions=directions, webhook_secret=_SECRET))
        s.add(ConnectorFirstSync(company_id=str(company_id), connector="woocommerce", state="completed"))
        await s.commit()
    for i, topic in enumerate(connectors.get("woocommerce").webhook_topics(directions)):
        fake.add(1 + i, topic)
    return company_id


async def _set_orders_inbound(factory, company_id) -> None:
    from celerp_connectors.routes import DirectionRequest, set_connector_direction

    async with factory() as s:
        await set_connector_direction("woocommerce", DirectionRequest(entity="orders", direction="inbound"),
                                      str(company_id), None, s)


async def _webhook_state(factory, company_id) -> tuple[bool, list[str]]:
    async with factory() as s:
        dirty = (await s.execute(sa.text(
            "SELECT webhooks_dirty FROM connector_configs WHERE company_id = :c AND connector = 'woocommerce'"),
            {"c": str(company_id)})).scalar_one()
        reruns = (await s.execute(sa.text(
            "SELECT rerun_requested FROM connector_execution_heartbeats "
            "WHERE company_id = :c AND connector = 'woocommerce' AND resource = 'webhooks'"),
            {"c": str(company_id)})).scalar_one_or_none()
    return bool(dirty), list(reruns or [])


async def test_a_reconcile_from_a_stale_read_never_removes_a_hook_a_newer_direction_needs(store):
    """A reconcile that read the directions with orders off meets a person turning orders on.
    Its result is not recorded as current (dirty stays set), and the rerun the change recorded
    leaves the order hooks active."""
    from celerp.connectors.webhook_reconcile import run_webhook_reconcile

    fake, factory = store
    company_id = await _connected(factory, fake, _NO_ORDERS)
    entered, go = asyncio.Event(), asyncio.Event()
    fake.pause_list = (entered, go)

    stale = asyncio.create_task(run_webhook_reconcile(str(company_id), "woocommerce"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    await _set_orders_inbound(factory, company_id)
    go.set()
    await asyncio.wait_for(stale, timeout=10)

    dirty, _reruns = await _webhook_state(factory, company_id)
    assert dirty, "a reconcile that read the directions before a change cleared the dirty mark"

    await asyncio.wait_for(run_webhook_reconcile(str(company_id), "woocommerce"), timeout=10)
    assert _ORDER_TOPICS <= set(fake.topics()), f"order hooks missing after the rerun: {fake.topics()}"
    assert not any(kind == "delete" and fake.hooks.get(i, {}).get("topic") in _ORDER_TOPICS
                   for kind, i, _t in fake.writes)
    assert (await _webhook_state(factory, company_id))[0] is False


async def test_two_reconcilers_over_duplicates_leave_exactly_the_lowest_hook_per_topic(store):
    """The store holds every topic twice (left by an earlier failed run). Two reconciles start
    together; afterwards each topic has exactly one active hook, and it is the lowest id."""
    from celerp.connectors.webhook_reconcile import run_webhook_reconcile

    fake, factory = store
    company_id = await _connected(factory, fake, _NO_ORDERS)
    lowest = {hook["topic"]: hook_id for hook_id, hook in sorted(fake.hooks.items(), reverse=True)}
    for hook_id, hook in list(fake.hooks.items()):
        fake.add(hook_id + 50, hook["topic"])
    entered, go = asyncio.Event(), asyncio.Event()
    fake.pause_list = (entered, go)

    first = asyncio.create_task(run_webhook_reconcile(str(company_id), "woocommerce"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    second = asyncio.create_task(run_webhook_reconcile(str(company_id), "woocommerce"))
    await asyncio.sleep(0.3)
    go.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=10)
    # The run that found the resource busy recorded a rerun; running it changes nothing.
    await asyncio.wait_for(run_webhook_reconcile(str(company_id), "woocommerce"), timeout=10)

    assert fake.topics() == {topic: [hook_id] for topic, hook_id in sorted(lowest.items())}


async def test_a_direction_change_never_writes_to_the_store_and_a_disconnect_leaves_nothing_to_create(store):
    """The direction route changes no webhook itself: it marks them dirty and records a
    rerun for the webhooks holder. A disconnect right after it leaves that rerun with no
    connection, so nothing is ever created on the disconnected store."""
    from celerp_connectors.routes import revoke_credentials

    fake, factory = store
    company_id = await _connected(factory, fake, _NO_ORDERS)

    await _set_orders_inbound(factory, company_id)
    assert fake.writes == [], f"the direction route wrote to the store itself: {fake.writes}"
    dirty, reruns = await _webhook_state(factory, company_id)
    assert dirty and "webhooks" in reruns

    async with factory() as s:
        await revoke_credentials("woocommerce", str(company_id), None, s, force=False)
    after_disconnect = len(fake.writes)

    from celerp.connectors.webhook_reconcile import run_webhook_reconcile
    await asyncio.wait_for(run_webhook_reconcile(str(company_id), "woocommerce"), timeout=10)
    created = [w for w in fake.writes[after_disconnect:] if w[0] in ("create", "update")]
    assert created == [], f"hooks written to a disconnected store: {created}"
