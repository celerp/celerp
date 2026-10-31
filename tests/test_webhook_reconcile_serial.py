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
import contextlib
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
    """WooCommerce's webhook endpoints over an in-memory list. Like the real store it keeps
    each hook's secret but never returns it (the property is write-only). `pause_list` holds
    the next listing call until released, so a test can interleave a second writer;
    `fail_next` makes the next request of a method raise after or instead of acting."""

    def __init__(self) -> None:
        self.hooks: dict[int, dict] = {}
        self.writes: list[tuple[str, int, str]] = []
        self.next_id = 100
        self.pause_list: tuple[asyncio.Event, asyncio.Event] | None = None
        self.timeout_after_create = False
        self.disabled_on_read: set[int] = set()
        self.before_create = None

    @staticmethod
    def _shown(hook: dict) -> dict:
        return {k: v for k, v in hook.items() if k != "secret"}

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
        body = [self._shown(h) for h in self.hooks.values()] if page == 1 else []
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
        shown = self._shown(hook)
        if hook_id in self.disabled_on_read:
            shown["status"] = "disabled"
        return httpx.Response(200, json=shown)

    async def _create(self, request: httpx.Request) -> httpx.Response:
        if self.before_create is not None:
            await self.before_create()
        body = json.loads(request.content)
        self.next_id += 1
        self.add(self.next_id, body["topic"], body.get("secret", ""), body.get("status", "active"))
        self.writes.append(("create", self.next_id, body["topic"]))
        if self.timeout_after_create:
            self.timeout_after_create = False
            raise httpx.ReadTimeout("the store took the hook but the answer never came", request=request)
        return httpx.Response(201, json=self._shown(self.hooks[self.next_id]))

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
    monkeypatch.setattr(
        "celerp.connectors.woocommerce.validate_public_base_url",
        AsyncMock(side_effect=lambda value, **_k: value.rstrip("/")),
    )
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        yield fake, factory


async def _connected(factory, fake: FakeStore, directions: dict, *, recorded: bool = False) -> uuid.UUID:
    """A committed WooCommerce connection whose first sync is done and whose store holds
    exactly the hooks the directions call for; `recorded` when the connection also holds
    their identity, as the reconcile that created them left it."""
    from celerp import connectors
    from celerp.models.connector_config import ConnectorConfig
    from celerp.models.connector_first_sync import ConnectorFirstSync

    from celerp.connectors.webhook_reconcile import secret_fingerprint

    company_id, _user_id = await _seed_race_company(factory)
    for i, topic in enumerate(connectors.get("woocommerce").webhook_topics(directions)):
        fake.add(1 + i, topic)
    config = ConnectorConfig(company_id=str(company_id), connector="woocommerce",
                             entity_directions=directions, webhook_secret=_SECRET)
    if recorded:
        config.webhook_hooks = {str(hook_id): secret_fingerprint(_SECRET) for hook_id in fake.hooks}
    async with factory() as s:
        s.add(config)
        s.add(ConnectorFirstSync(company_id=str(company_id), connector="woocommerce", state="completed"))
        await s.commit()
    return company_id


async def _recorded(factory, company_id) -> dict:
    from celerp.models.connector_config import ConnectorConfig

    async with factory() as s:
        config = await s.scalar(sa.select(ConnectorConfig).where(ConnectorConfig.company_id == str(company_id)))
        return config.webhook_hooks


async def _rotate_secret(factory, company_id, secret: str) -> None:
    from celerp.connectors.webhook_reconcile import mark_webhooks_dirty
    from celerp.connectors.ownership import lock_connection_row

    async with factory() as s:
        config = await lock_connection_row(s, str(company_id), "woocommerce")
        config.webhook_secret = secret
        await mark_webhooks_dirty(s, config)
        await s.commit()


async def _set_orders_inbound(factory, company_id) -> None:
    from celerp_connectors.routes import DirectionRequest, set_connector_direction

    async with factory() as s:
        await set_connector_direction("woocommerce", DirectionRequest(entity="orders", direction="inbound"),
                                      str(company_id), None, s)


async def _set_orders_off(factory, company_id) -> None:
    from celerp_connectors.routes import DirectionRequest, set_connector_direction

    async with factory() as s:
        await set_connector_direction("woocommerce", DirectionRequest(entity="orders", direction="off"),
                                      str(company_id), None, s)


async def _webhooks_dirty(factory, company_id) -> bool:
    async with factory() as s:
        return bool((await s.execute(sa.text(
            "SELECT webhooks_dirty FROM connector_configs WHERE company_id = :c AND connector = 'woocommerce'"),
            {"c": str(company_id)})).scalar_one())


async def test_a_reconcile_from_a_stale_read_never_removes_a_hook_a_newer_direction_needs(store, monkeypatch):
    """A reconcile that read the directions with orders off meets a person turning orders on.
    Its result is not recorded as current (dirty stays set while the holder's rerun is held
    back), and the holder's rerun before it releases leaves the order hooks active."""
    from unittest.mock import AsyncMock

    from celerp.connectors import execution
    from celerp.connectors.webhook_reconcile import run_webhook_reconcile

    fake, factory = store
    company_id = await _connected(factory, fake, _NO_ORDERS)

    async def _stale_run(rerun: bool) -> None:
        entered, go = asyncio.Event(), asyncio.Event()
        fake.pause_list = (entered, go)
        stale = asyncio.create_task(run_webhook_reconcile(str(company_id), "woocommerce"))
        await asyncio.wait_for(entered.wait(), timeout=5)
        await _set_orders_inbound(factory, company_id)
        with monkeypatch.context() as m:
            if not rerun:
                m.setattr(execution, "_run_reruns", AsyncMock())
            go.set()
            await asyncio.wait_for(stale, timeout=10)

    await _stale_run(rerun=False)
    assert await _webhooks_dirty(factory, company_id), "a reconcile that read the directions before a change cleared the dirty mark"

    await _set_orders_off(factory, company_id)
    await asyncio.wait_for(run_webhook_reconcile(str(company_id), "woocommerce"), timeout=10)
    await _stale_run(rerun=True)
    assert _ORDER_TOPICS <= set(fake.topics()), f"order hooks missing after the holder's rerun: {fake.topics()}"
    assert await _webhooks_dirty(factory, company_id) is False


async def test_a_direction_change_is_reconciled_within_one_sweeper_tick(store):
    """No execution holds the webhooks when a person turns orders on: the dirty mark the
    change committed is the request, and the next sweeper tick adds the order hooks. The
    change and the reconcile each write the connection row under its company's lock."""
    from connector_test_support import ungated_connector_takes
    from celerp.connectors.execution import sweep_reruns

    writers = ("celerp/connectors/webhook_reconcile.py", "celerp_connectors/routes.py")
    with ungated_connector_takes(*writers) as ungated:
        fake, factory = store
        company_id = await _connected(factory, fake, _NO_ORDERS)
        await _set_orders_inbound(factory, company_id)

        await asyncio.wait_for(sweep_reruns(), timeout=10)
        assert _ORDER_TOPICS <= set(fake.topics()), f"order hooks missing after the sweep: {fake.topics()}"
        assert await _webhooks_dirty(factory, company_id) is False
    assert ungated == set()


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
    """The direction route changes no webhook itself: it marks them dirty, which the
    webhooks holder or the sweeper reconciles. A disconnect right after it leaves that
    mark with no connection, so nothing is ever created on the disconnected store."""
    from celerp_connectors.routes import revoke_credentials

    fake, factory = store
    company_id = await _connected(factory, fake, _NO_ORDERS)

    await _set_orders_inbound(factory, company_id)
    assert fake.writes == [], f"the direction route wrote to the store itself: {fake.writes}"
    assert await _webhooks_dirty(factory, company_id)

    from unittest.mock import AsyncMock, patch
    # The relay's own copy of the key is not under test: the local disconnect is.
    with patch("celerp.connectors.remote_state.revoke_connector_remote_state", AsyncMock()):
        async with factory() as s:
            revoked = await revoke_credentials("woocommerce", str(company_id), None, s, force=False)
    assert revoked == {"ok": True, "kept_store_values": 0}, revoked
    after_disconnect = len(fake.writes)

    from celerp.connectors.webhook_reconcile import run_webhook_reconcile
    await asyncio.wait_for(run_webhook_reconcile(str(company_id), "woocommerce"), timeout=10)
    created = [w for w in fake.writes[after_disconnect:] if w[0] in ("create", "update")]
    assert created == [], f"hooks written to a disconnected store: {created}"


async def _reconcile(company_id) -> None:
    from celerp.connectors.webhook_reconcile import run_webhook_reconcile

    await asyncio.wait_for(run_webhook_reconcile(str(company_id), "woocommerce"), timeout=10)


async def test_hooks_recorded_with_the_current_secret_are_left_alone_when_the_store_hides_it(store):
    """WooCommerce never returns a hook's secret. Hooks the connection recorded with its
    current secret are kept as they are: no hook is created, updated or deleted."""
    fake, factory = store
    company_id = await _connected(factory, fake, _NO_ORDERS, recorded=True)
    before = dict(fake.hooks)

    await _reconcile(company_id)

    assert fake.writes == [], f"a hidden secret was read as a different one: {fake.writes}"
    assert fake.hooks.keys() == before.keys()


async def test_a_rotated_secret_updates_each_hook_in_place(store):
    """A new secret is sent to the hooks the connection already has: each is updated where
    it is, nothing is created or deleted, and the record names the new secret."""
    from celerp.connectors.webhook_reconcile import secret_fingerprint

    fake, factory = store
    company_id = await _connected(factory, fake, _NO_ORDERS, recorded=True)
    ids = set(fake.hooks)
    await _rotate_secret(factory, company_id, "s4")

    await _reconcile(company_id)

    assert {w[0] for w in fake.writes} == {"update"}, fake.writes
    assert set(fake.hooks) == ids and all(h["secret"] == "s4" for h in fake.hooks.values())
    assert await _recorded(factory, company_id) == {str(i): secret_fingerprint("s4") for i in ids}
    assert await _webhooks_dirty(factory, company_id) is False


async def test_a_disabled_hook_is_reactivated_in_place(store):
    """The store disables a hook after failed deliveries. The reconcile turns the same hook
    back on with the current secret instead of replacing it."""
    fake, factory = store
    company_id = await _connected(factory, fake, _NO_ORDERS, recorded=True)
    fake.hooks[1]["status"] = "disabled"

    await _reconcile(company_id)

    assert fake.writes == [("update", 1, fake.hooks[1]["topic"])], fake.writes
    assert fake.hooks[1]["status"] == "active" and fake.hooks[1]["secret"] == _SECRET


async def test_a_create_that_timed_out_is_adopted_not_duplicated(store):
    """The store created a hook but its answer timed out. Nothing is deleted on that
    ambiguous outcome; the next reconcile finds the hook, gives it the secret and records
    it, so the topic ends with exactly one hook and no second create."""
    fake, factory = store
    company_id = await _connected(factory, fake, _NO_ORDERS, recorded=True)
    await _set_orders_inbound(factory, company_id)
    fake.timeout_after_create = True

    with contextlib.suppress(Exception):
        await _reconcile(company_id)
    assert [w[0] for w in fake.writes] == ["create"] and await _webhooks_dirty(factory, company_id)
    await _reconcile(company_id)

    creates = [w for w in fake.writes if w[0] == "create"]
    assert len(creates) == len(_ORDER_TOPICS), f"a topic was created twice: {fake.writes}"
    assert not [w for w in fake.writes if w[0] == "delete"], fake.writes
    assert all(len(ids) == 1 for ids in fake.topics().values()), fake.topics()
    assert set(await _recorded(factory, company_id)) == {str(i) for i in fake.hooks}


async def test_a_hook_created_before_a_crash_is_adopted_by_the_next_run(store, monkeypatch):
    """The worker stops after the store acknowledged a create and before it was recorded.
    The next run adopts that hook: no duplicate, and no hook that works is removed."""
    from celerp.connectors import webhook_reconcile

    fake, factory = store
    company_id = await _connected(factory, fake, _NO_ORDERS, recorded=True)
    await _set_orders_inbound(factory, company_id)

    async def _crash(*_a, **_k):
        raise RuntimeError("worker stopped")

    with monkeypatch.context() as m:
        m.setattr(webhook_reconcile, "_record_hook", _crash)
        with contextlib.suppress(Exception):
            await _reconcile(company_id)
    assert [w[0] for w in fake.writes] == ["create"]
    await _reconcile(company_id)

    assert len([w for w in fake.writes if w[0] == "create"]) == len(_ORDER_TOPICS), fake.writes
    assert not [w for w in fake.writes if w[0] == "delete"], fake.writes
    assert all(len(ids) == 1 for ids in fake.topics().values()), fake.topics()


async def test_each_acknowledged_hook_is_recorded_before_the_next_store_call(store):
    """A create the store acknowledged is in the connection's record before the reconcile
    makes its next store request, so an interruption never loses a hook's identity."""
    fake, factory = store
    company_id = await _connected(factory, fake, _NO_ORDERS, recorded=True)
    await _set_orders_inbound(factory, company_id)
    missing: list[str] = []

    async def _check() -> None:
        recorded = await _recorded(factory, company_id)
        missing.extend(str(w[1]) for w in fake.writes if w[0] == "create" and str(w[1]) not in recorded)

    fake.before_create = _check
    await _reconcile(company_id)

    assert len([w for w in fake.writes if w[0] == "create"]) == len(_ORDER_TOPICS)
    assert missing == [], f"hooks the store acknowledged were not yet recorded: {missing}"
    assert set(await _recorded(factory, company_id)) == {str(i) for i in fake.hooks}


async def test_nothing_is_deleted_while_a_kept_hook_fails_verification(store):
    """A hook the reconcile keeps reads back disabled. The run stops before deleting the
    hooks it would retire, so the store is never left without a working hook."""
    fake, factory = store
    company_id = await _connected(factory, fake, _NO_ORDERS, recorded=True)
    for hook_id, hook in list(fake.hooks.items()):
        fake.add(hook_id + 50, hook["topic"])
    fake.disabled_on_read.add(1)

    with pytest.raises(RuntimeError, match="is not active"):
        await _reconcile(company_id)

    assert not [w for w in fake.writes if w[0] == "delete"], fake.writes
