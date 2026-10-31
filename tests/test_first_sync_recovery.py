# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A first sync survives a stop at any point: what each step did is recorded with the step
itself, a first sync left starting or running is picked up again by the execution sweeper
with the choices a person made, a failed one can be retried keeping its import batch and
order cutoff, a retry runs only the phases that did not complete, and a phase that failed
stops only the phases that depend on it. Stopping after any commit and retrying ends
exactly as one uninterrupted run.

These run on the real database: each step commits on its own connection."""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
import sqlalchemy as sa

from connector_test_support import add_item, connect, item_state
from migration_support import real_engine  # noqa: F401 - fixture
from test_helpers import provision_company_books

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("connector_modules_running")]

STORE = r"https://store-[0-9a-f]+\.example\.com"


def _store(cid) -> str:
    """Each company's own store: one store is connected to one company."""
    return f"https://store-{uuid.UUID(str(cid)).hex}.example.com"
PRODUCTS = [
    {"id": 1, "sku": "MATCH-1", "name": "Store name", "regular_price": "99.00"},
    {"id": 2, "sku": "NEW-1", "name": "New in store", "regular_price": "5.00",
     "manage_stock": True, "stock_quantity": 3,
     "images": [{"id": 31, "src": "https://cdn.example.com/new.jpg", "name": "new"}]},
    {"id": 3, "sku": "NEW-2", "name": "Second new", "regular_price": "7.00",
     "manage_stock": True, "stock_quantity": 4},
]


class _Crash(BaseException):
    """The process stops: nothing below catches it, as nothing survives a stop."""


async def _company(name: str) -> uuid.UUID:
    from celerp.db import get_session_ctx
    from celerp.models.accounting import UserCompany
    from celerp.models.company import Company, User

    cid, uid = uuid.uuid4(), uuid.uuid4()
    async with get_session_ctx() as session:
        session.add(User(id=uid, email=f"{uid.hex[:12]}@example.test", name="Owner", auth_hash=None))
        await session.flush()
        session.add(Company(id=cid, name=name, slug=f"{name.lower()}-{cid.hex[:8]}", settings={"currency": "USD"}))
        await session.flush()
        session.add(UserCompany(user_id=uid, company_id=cid, role="owner", is_active=True))
        await provision_company_books(session, cid)
        await session.commit()
    return cid


async def _started(name: str, directions: dict, *, match: str = "take", reviewed: dict | None = None) -> uuid.UUID:
    """A company with an item matching a store product by SKU and a WooCommerce first sync
    started with ``directions``, as the start request saves it, reviewed for the store
    products of ``reviewed`` (by default every product of the store as it is now)."""
    from celerp.db import get_session_ctx
    from celerp.models.import_batch import ImportBatch

    cid = await _company(name)
    async with get_session_ctx() as session:
        await add_item(session, cid, "item:match", "MATCH-1", name="Celerp name", sale_price=10.0)
        config = await connect(session, cid, "woocommerce", directions, first_sync="starting")
        batch = ImportBatch(id=uuid.uuid4(), company_id=cid, entity_type="item", filename="WooCommerce first sync",
                            row_count=0, entity_ids=[], idempotency_keys=[], reversible=True)
        now = datetime.now(timezone.utc)
        config.orders_from = now - timedelta(days=30)
        config.first_sync.directions = dict(directions)
        config.first_sync.batch_id, config.first_sync.match = str(batch.id), match
        config.first_sync.operation_id = str(uuid.uuid4())
        config.first_sync.started_at = config.first_sync.watermark = now
        config.first_sync.undo = {"links": []}
        config.first_sync.progress = {}
        config.first_sync.reviewed = reviewed if reviewed is not None else {
            "1": "item:match", "2": None, "3": None}
        await session.flush()
        session.add(batch)
        await session.commit()
    return cid


@pytest.fixture
def store(signed_in_to_cloud, monkeypatch):
    """A WooCommerce store answering its products, one image and (when asked) its orders."""
    from celerp.connectors.base import ConnectorContext

    monkeypatch.setattr("celerp.connectors.woocommerce.validate_public_base_url",
                        AsyncMock(side_effect=lambda value, **_kw: value.rstrip("/")))
    monkeypatch.setattr("celerp.services.outbound_url.validate_public_base_url",
                        AsyncMock(side_effect=lambda value, **_kw: value))
    monkeypatch.setattr("celerp.connectors.relay_token.fetch_context", AsyncMock(
        side_effect=lambda cid, *_a, **_k: ConnectorContext(
            company_id=str(cid), access_token="ck:cs", store_handle=_store(cid))))
    monkeypatch.setattr("celerp.connectors.webhook_reconcile.reconcile_webhooks", AsyncMock())
    monkeypatch.setattr("celerp.services.attachments.store_upload", AsyncMock(side_effect=lambda cid, *_a, **_k: {
        "id": "pulled1", "filename": "new", "mime": "image/jpeg", "size": 3,
        "url": f"/static/attachments/{cid}/pulled1.jpg"}))
    with respx.mock(assert_all_called=False) as mock:
        products = mock.get(url__regex=STORE + "/wp-json/wc/v3/products").mock(
            return_value=httpx.Response(200, json=PRODUCTS))
        orders = mock.get(url__regex=STORE + "/wp-json/wc/v3/orders").mock(return_value=httpx.Response(200, json=[]))
        mock.get("https://cdn.example.com/new.jpg").mock(return_value=httpx.Response(
            200, content=b"img", headers={"content-type": "image/jpeg"}))
        yield SimpleNamespace(products=products, orders=orders)


async def _first_sync(cid):
    from celerp.db import get_session_ctx
    from celerp.models.connector_first_sync import ConnectorFirstSync

    async with get_session_ctx() as session:
        return await session.get(ConnectorFirstSync, (str(cid), "woocommerce"))


async def _batch(cid):
    from celerp.db import get_session_ctx
    from celerp.models.import_batch import ImportBatch

    row = await _first_sync(cid)
    if row.batch_id is None:
        return None
    async with get_session_ctx() as session:
        return await session.get(ImportBatch, uuid.UUID(row.batch_id))


async def _disconnect(cid) -> None:
    """One store connection per install: the next company connects once this one is gone."""
    from celerp.db import get_session_ctx

    async with get_session_ctx() as session:
        for table in ("connector_configs", "connector_first_syncs"):
            await session.execute(sa.text(f"DELETE FROM {table} WHERE company_id = :c"), {"c": str(cid)})
        await session.commit()


async def _lease_expired(cid) -> None:
    """A stopped process renews nothing: its leases run out."""
    from celerp.db import get_session_ctx

    async with get_session_ctx() as session:
        await session.execute(sa.text(
            "UPDATE connector_execution_heartbeats SET expires_at = :at "
            "WHERE company_id = :c AND holder IS NOT NULL"),
            {"c": str(cid), "at": datetime.now(timezone.utc) - timedelta(seconds=1)})
        await session.commit()


async def _retry(cid) -> None:
    from celerp.connectors.sync_runner import retry_first_sync
    from celerp.db import get_session_ctx

    async with get_session_ctx() as session:
        await retry_first_sync(session, cid, "woocommerce")
        await session.commit()


async def _execute(cid) -> None:
    from celerp.connectors.sync_runner import execute_first_sync

    await execute_first_sync(cid, "woocommerce")


@asynccontextmanager
async def _stopping_after_commit(monkeypatch, n: int):
    """The process stops right after the ``n``-th commit (1-based) inside the block; the
    commits a stop leaves to the lease's own release are not counted."""
    from sqlalchemy.ext.asyncio import AsyncSession

    commit = AsyncSession.commit
    seen = SimpleNamespace(count=0, armed=True)

    async def _commit(self):
        await commit(self)
        if seen.armed:
            seen.count += 1
            if seen.count == n:
                seen.armed = False
                raise _Crash(n)

    monkeypatch.setattr(AsyncSession, "commit", _commit)
    try:
        yield seen
    finally:
        monkeypatch.setattr(AsyncSession, "commit", commit)


async def _snapshot(cid) -> dict:
    """Everything a first sync leaves behind, comparable between companies: items (with
    their stock, files and store link), the import batch, the undo record, the item and
    file events and the first sync's own outcome."""
    from celerp.connectors.product_links import external_link_for_state
    from celerp.db import get_session_ctx

    async with get_session_ctx() as session:
        rows = (await session.execute(sa.text(
            "SELECT entity_id FROM projections WHERE company_id = :c AND entity_type = 'item' ORDER BY entity_id"),
            {"c": cid})).scalars().all()
        items = {}
        for entity_id in rows:
            state = await item_state(session, cid, entity_id)
            link = external_link_for_state(state, "woocommerce") or {}
            items[entity_id] = (state.get("sku"), state.get("name"), state.get("sale_price"), state.get("quantity"),
                                sorted(f["id"] for f in state.get("files") or []), link.get("product_id"))
        events = (await session.execute(sa.text(
            "SELECT entity_id, event_type, count(*) FROM ledger WHERE company_id = :c "
            "AND entity_type = 'item' GROUP BY entity_id, event_type ORDER BY 1, 2"), {"c": cid})).all()
        docs = await session.scalar(sa.text(
            "SELECT count(*) FROM projections WHERE company_id = :c AND entity_type = 'doc'"), {"c": cid})
    first_sync, batch = await _first_sync(cid), await _batch(cid)
    return {
        "items": items,
        "events": [tuple(e) for e in events],
        "docs": docs,
        "batch": None if batch is None else (sorted(batch.entity_ids), len(batch.idempotency_keys),
                                             len(set(batch.idempotency_keys)), batch.row_count),
        "links": sorted(link["entity_id"] for link in (first_sync.undo or {}).get("links", [])),
        "state": first_sync.state,
        "progress": first_sync.progress,
        "choices": (first_sync.directions, first_sync.match, first_sync.batch_id is not None),
    }


DIRECTIONS = {"products": "inbound", "stock": "off", "orders": "inbound", "customers": "off"}


async def test_an_item_created_before_a_crash_stays_in_the_first_syncs_batch(real_engine, store, monkeypatch):
    """The first created item commits; the process stops before the first sync ends. The
    item is already in the first sync's import batch, so Import History's undo removes it."""
    cid = await _started("CrashCreate", DIRECTIONS)
    pulled = []
    original = __import__("celerp.connectors.woocommerce", fromlist=["x"]).WooCommerceConnector._pull_product_files

    async def _stop_after_first_file(self, ctx, product, entity_id, *args, **kwargs):
        await original(self, ctx, product, entity_id, *args, **kwargs)
        pulled.append(entity_id)
        if entity_id == "item:woocommerce:2":
            raise _Crash("after the first created item's files")

    monkeypatch.setattr("celerp.connectors.woocommerce.WooCommerceConnector._pull_product_files",
                        _stop_after_first_file)
    with pytest.raises(_Crash):
        await _execute(cid)
    assert pulled == ["item:match", "item:woocommerce:2"]
    batch = await _batch(cid)
    assert batch is not None and batch.entity_ids == ["item:woocommerce:2"], batch and batch.entity_ids
    assert len(batch.idempotency_keys) == 2 and batch.row_count == 1  # the creation and its file


async def test_a_link_made_before_a_crash_stays_undoable(real_engine, store, monkeypatch):
    """The SKU match links (taking the store's values) and commits; the process stops before
    the first sync ends. The link is already on the first sync's undo record, with the
    values it replaced."""
    from celerp_inventory import services

    cid = await _started("CrashLink", DIRECTIONS)
    original = services.upsert_external_product

    async def _stop_after_link(company_id, **kwargs):
        outcome = await original(company_id, **kwargs)
        if kwargs["sku"] == "MATCH-1":
            raise _Crash("after the link")
        return outcome

    monkeypatch.setattr(services, "upsert_external_product", _stop_after_link)
    with pytest.raises(_Crash):
        await _execute(cid)
    links = (await _first_sync(cid)).undo["links"]
    assert [link["entity_id"] for link in links] == ["item:match"], links
    assert (links[0]["restore"]["name"], links[0]["restore"]["sale_price"]) == ("Celerp name", 10.0)


async def test_an_abandoned_first_sync_is_resumed_with_its_original_choices(real_engine, store, monkeypatch):
    """A first sync left running by a stopped process, with no live lease and no change for
    longer than a lease lasts, is run again by the execution sweeper with the batch,
    directions, match and order cutoff saved when it started."""
    from celerp.connectors.execution import TTL, sweep_reruns
    from celerp.db import get_session_ctx

    cid = await _started("Abandoned", DIRECTIONS)
    before = await _first_sync(cid)
    async with get_session_ctx() as session:
        await session.execute(sa.text(
            "UPDATE connector_first_syncs SET state = 'running', updated_at = :at WHERE company_id = :c"),
            {"c": str(cid), "at": datetime.now(timezone.utc) - TTL * 2})
        await session.commit()
    await sweep_reruns()
    after = await _first_sync(cid)
    assert after.state == "completed", (after.state, after.error)
    assert (after.batch_id, after.directions, after.match, after.watermark) == (
        before.batch_id, before.directions, before.match, before.watermark)
    assert sorted((await _batch(cid)).entity_ids) == ["item:woocommerce:2", "item:woocommerce:3"]
    assert store.orders.calls and all(
        c.request.url.params.get("after", "").startswith(str((before.watermark - timedelta(days=30)).date()))
        for c in store.orders.calls)


async def test_a_first_sync_still_starting_or_recently_changed_is_left_alone(real_engine, store):
    """The sweeper takes over only work no process holds: a first sync changed within a
    lease's lifetime is left to the process running it."""
    from celerp.connectors.execution import sweep_reruns

    cid = await _started("Recent", DIRECTIONS)
    await sweep_reruns()
    assert (await _first_sync(cid)).state == "starting"
    assert not store.products.calls


async def test_a_failed_first_sync_retries_with_its_original_batch_and_cutoff(real_engine, store):
    """The store refuses the orders; the first sync needs attention, its empty import
    batch kept. Retry starts it again with the same batch, choices and order cutoff."""
    cid = await _started("Retry", DIRECTIONS)
    batch_id = (await _first_sync(cid)).batch_id
    store.products.mock(return_value=httpx.Response(200, json=PRODUCTS[:1]))  # nothing new to create
    store.orders.mock(return_value=httpx.Response(500, json={"message": "store down"}))
    await _execute(cid)
    failed = await _first_sync(cid)
    assert failed.state == "failed" and failed.batch_id == batch_id, (failed.state, failed.batch_id)
    assert await _batch(cid) is not None
    first_cutoff = store.orders.calls.last.request.url.params["after"]

    store.orders.mock(return_value=httpx.Response(200, json=[]))
    await _retry(cid)
    retried = await _first_sync(cid)
    assert (retried.state, retried.error, retried.batch_id, retried.watermark, retried.directions) == (
        "starting", None, batch_id, failed.watermark, failed.directions)
    await _execute(cid)
    done = await _first_sync(cid)
    assert done.state == "completed" and done.error is None
    assert store.orders.calls.last.request.url.params["after"] == first_cutoff
    # Completed with nothing created: only now is the empty import batch removed.
    assert done.batch_id is None


async def test_a_retry_runs_only_the_phases_that_did_not_complete(real_engine, store):
    """Products completed and orders failed: the retry reads the orders again and never the
    products, and products stay completed throughout."""
    cid = await _started("Phases", DIRECTIONS)
    store.orders.mock(return_value=httpx.Response(500, json={"message": "store down"}))
    await _execute(cid)
    failed = await _first_sync(cid)
    assert failed.state == "failed"
    assert failed.progress["products"]["state"] == "completed", failed.progress
    product_reads = store.products.call_count

    store.orders.mock(return_value=httpx.Response(200, json=[]))
    await _retry(cid)
    assert (await _first_sync(cid)).progress["products"]["state"] == "completed"
    await _execute(cid)
    assert (await _first_sync(cid)).state == "completed"
    assert store.products.call_count == product_reads


async def test_a_failed_phase_stops_only_the_phases_that_depend_on_it(monkeypatch):
    """Contacts fail: journals and documents (which need contacts) do not run, invoices
    (which do not) still do."""
    from celerp import connectors
    from celerp.connectors import execution, sync_runner
    from celerp.connectors.base import SyncDirection, SyncResult

    connector = connectors.get("celerp")
    plan = ["products_out", "contacts_out", "journals_out", "invoices_out", "documents_out", "inventory_out"]
    ran = []

    async def _run_sync(_connector, _cid, entity, **_kw):
        ran.append(entity)
        return SyncResult(entity=entity, direction=SyncDirection.OUTBOUND,
                          errors=["refused"] if entity == "contacts_out" else [])

    @asynccontextmanager
    async def _execution(*_a, **_k):
        yield SimpleNamespace(ctx=None)

    monkeypatch.setattr(sync_runner, "_directions", AsyncMock(return_value={}))
    monkeypatch.setattr(sync_runner, "sync_plan", lambda *_a: list(plan))
    monkeypatch.setattr(execution, "plan_resources", lambda *_a: [])
    monkeypatch.setattr(execution, "connector_execution", _execution)
    monkeypatch.setattr(sync_runner, "run_sync", _run_sync)
    monkeypatch.setattr(type(connector), "before_outbound", AsyncMock())
    await sync_runner.run_connector_sync(connector, uuid.uuid4(), purpose="manual_sync")
    assert ran == ["products_out", "contacts_out", "invoices_out"]


async def test_crash_at_every_commit_then_retry_equals_one_run(real_engine, store, monkeypatch):
    """For every commit the first sync makes: stop right after it, let the sweeper's runner
    pick the first sync up again (as often as it takes), and compare everything it left
    with one run that never stopped."""
    with_choices = DIRECTIONS
    clean = await _started("Clean", with_choices)
    async with _stopping_after_commit(monkeypatch, 10 ** 6) as seen:
        await _execute(clean)
    expected = await _snapshot(clean)
    assert expected["state"] == "completed", expected
    commits = seen.count
    assert commits > 5, commits
    await _disconnect(clean)

    for n in range(1, commits + 1):
        cid = await _started(f"Stop{n}", with_choices)
        async with _stopping_after_commit(monkeypatch, n):
            try:
                await _execute(cid)
            except _Crash:
                pass
        await _lease_expired(cid)
        for _ in range(3):
            if (await _first_sync(cid)).state == "completed":
                break
            await _execute(cid)
        got = await _snapshot(cid)
        await _disconnect(cid)
        assert got == expected, (n, got, expected)


async def test_execution_links_only_reviewed_pairs(real_engine, store):
    """The first sync does only what its preview showed. A store product the preview showed
    as new that now matches an item made since, and one that appeared in the store after the
    preview, are left alone and wait on a person, who brings them in with a normal sync; the
    reviewed match links as shown."""
    from celerp.db import get_session_ctx
    from celerp.models.sync_run import SyncRun
    from ui.i18n import t

    cid = await _started("Reviewed", DIRECTIONS, reviewed={"1": "item:match", "2": None})
    async with get_session_ctx() as session:
        await add_item(session, cid, "item:late", "NEW-1", name="Made after the preview")
    await _execute(cid)

    async with get_session_ctx() as session:
        assert (await item_state(session, cid, "item:match"))["name"] == "Store name"  # linked, taking the store's
        late = await item_state(session, cid, "item:late")
        assert late["name"] == "Made after the preview" and "woocommerce" not in str(late.get("external_links"))
        for created in ("item:woocommerce:2", "item:woocommerce:3"):
            assert await item_state(session, cid, created) == {}
        run = (await session.scalars(sa.select(SyncRun).where(
            SyncRun.company_id == str(cid), SyncRun.entity == "products").order_by(SyncRun.started_at.desc()))).first()
    waiting = {e["id"]: e for e in run.attention}
    assert {k: waiting[k]["kind"] for k in ("2", "3")} == {"2": "unreviewed", "3": "unreviewed"}
    assert waiting["2"]["reason"] == t("connectors.attention_unreviewed", service="WooCommerce")
    assert (await _first_sync(cid)).state == "completed"
