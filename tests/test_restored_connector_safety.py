# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A company restored from a backup is an independent copy: it pushes nothing to an
external system until its owner turns outbound sync on for a connector, its historical
external ids are not live channel links, and reconnecting keeps the same-store check."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
import respx
import sqlalchemy as sa
from sqlalchemy import text

from company_backup_support import company, download, member, owner, restore, token
from migration_support import auth, count, maker, real_client, real_engine  # noqa: F401
from test_helpers import merge_items

pytestmark = pytest.mark.asyncio

RELAY = "https://relay.test"
STORE = "https://store.example.test"
LIVE_SHOPIFY = {"product_id": "9001", "variant_id": "9002", "sync_enabled": True}
LIVE_WOO = {"product_id": "77", "sync_enabled": True}
NOTICE = "Integrations are disconnected and outbound sync is off until you turn it on again."


def _local(monkeypatch, tmp_path) -> None:
    from celerp.config import settings
    from celerp.services import attachments
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(attachments, "_backend", attachments.LocalBackend())


async def _sql(engine, sql: str, **params):
    async with engine.begin() as conn:
        return await conn.execute(text(sql), params)


async def _item(client, tok: str, sku: str) -> str:
    r = await client.post("/items", headers=auth(tok), json={
        "sku": sku, "name": sku, "quantity": 5.0, "sell_by": "piece", "category": "Raw", "status": "available"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _links(engine, cid, eid: str, links: dict) -> None:
    async with engine.begin() as conn:
        state = (await conn.execute(text(
            "SELECT state FROM projections WHERE company_id = :c AND entity_id = :e"),
            {"c": uuid.UUID(str(cid)), "e": eid})).scalar_one()
        await conn.execute(text(
            "UPDATE projections SET state = CAST(:s AS json) WHERE company_id = :c AND entity_id = :e"),
            {"s": json.dumps({**state, "external_links": links}), "c": uuid.UUID(str(cid)), "e": eid})


async def _by_sku(engine, cid, sku: str) -> str:
    async with engine.connect() as conn:
        return (await conn.execute(text(
            "SELECT entity_id FROM projections WHERE company_id = :c AND state ->> 'sku' = :s"),
            {"c": uuid.UUID(str(cid)), "s": sku})).scalar_one()


async def _projection(engine, cid, eid: str) -> tuple[dict, bool | None]:
    async with engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT state, is_sync_to_shopify FROM projections WHERE company_id = :c AND entity_id = :e"),
            {"c": uuid.UUID(str(cid)), "e": eid})).one()
    return dict(row[0]), row[1]


async def _doc(engine, cid, entity_id: str, **markers) -> None:
    await _sql(engine,
               "INSERT INTO projections (company_id, entity_id, entity_type, state, version, updated_at) "
               "VALUES (:c, :e, 'doc', CAST(:s AS json), 1, now())",
               c=cid, e=entity_id, s=json.dumps({"doc_type": "invoice", "ref_id": entity_id, "status": "final",
                                                 "line_items": [], **markers}))


@pytest_asyncio.fixture
async def restored(real_engine, real_client, tmp_path, monkeypatch):
    """A source company with channel-linked, Shopify-opted-in items and imported or pushed
    invoices, restored as a new company. Yields a namespace of both companies."""
    _local(monkeypatch, tmp_path)
    user = await owner(real_engine)
    source = await company(real_engine, user, "Source Trading", "source-marker")
    tok = await token(real_engine, user, source)
    a = await _item(real_client, tok, "RST-A")
    await _item(real_client, tok, "RST-B")
    await _links(real_engine, source, a, {"shopify": dict(LIVE_SHOPIFY), "woocommerce": dict(LIVE_WOO)})
    r = await real_client.post("/items/bulk/shopify-sync", headers=auth(tok), json={"entity_ids": [a], "enable": True})
    assert r.status_code == 200, r.text
    assert (await _projection(real_engine, source, a))[1] is True
    await _doc(real_engine, source, "doc:imported", shopify_order_id="5001")
    await _doc(real_engine, source, "doc:pushed", xero_invoice_id="X-1")
    await _doc(real_engine, source, "doc:native")
    data = await download(real_client, tok)
    r = await restore(real_client, tok, data, mode="new_company")
    assert r.status_code == 201, r.text
    body = r.json()
    new = body["company_id"]
    yield SimpleNamespace(user=user, source=source, source_tok=tok, data=data, cid=new, tok=body["access_token"],
                          a=await _by_sku(real_engine, new, "RST-A"), b=await _by_sku(real_engine, new, "RST-B"))


async def _claim(engine, cid, connector: str):
    from celerp.connectors.ownership import claim_connector_ownership
    async with maker(engine)() as s:
        config = await claim_connector_ownership(s, cid, connector)
        direction = config.direction
        await s.commit()
    return direction


async def _direction(engine, cid, connector: str) -> str | None:
    async with engine.connect() as conn:
        return (await conn.execute(text(
            "SELECT direction FROM connector_configs WHERE company_id = :c AND connector = :n"),
            {"c": str(cid), "n": connector})).scalar_one_or_none()


async def _queued(engine, cid) -> int:
    return await count(engine, "outbound_queue", "company_id = :c", c=str(cid))


async def _patch_qty(client, tok: str, eid: str, old: float, new: float) -> None:
    r = await client.patch(f"/items/{eid}", headers=auth(tok),
                           json={"fields_changed": {"quantity": {"old": old, "new": new}}})
    assert r.status_code == 200, r.text


# ── Restore ──────────────────────────────────────────────────────────────────

async def test_restored_shopify_opt_ins_off_after_restore_and_rebuild(restored, real_engine):
    from celerp.projections.engine import ProjectionEngine
    state, column = await _projection(real_engine, restored.cid, restored.a)
    assert state.get("is_sync_to_shopify") is False and column is False
    assert await count(real_engine, "ledger", "company_id = :c AND entity_id = :e AND event_type = 'shop.sync.disabled'",
                       c=uuid.UUID(restored.cid), e=restored.a) == 1
    async with maker(real_engine)() as s:
        await ProjectionEngine.rebuild(s, uuid.UUID(restored.cid))
        await s.commit()
    state, column = await _projection(real_engine, restored.cid, restored.a)
    assert state.get("is_sync_to_shopify") is False and column is False
    assert (await _projection(real_engine, restored.source, await _by_sku(real_engine, restored.source, "RST-A")))[1] is True


async def test_portable_restore_historical_ids_not_active(restored, real_engine, real_client):
    """Restored external ids stay on the item as history, show no live channel and do not
    block a merge while no connector is connected."""
    state, _ = await _projection(real_engine, restored.cid, restored.a)
    assert state["external_links"]["shopify"]["product_id"] == "9001"
    r = await real_client.get("/items", headers=auth(restored.tok), params={"q": "RST"})
    assert r.status_code == 200, r.text
    channel = next(i for i in r.json()["items"] if i["id"] == restored.a)["_channel_state"]
    assert channel == {"shopify": {"linked": False, "enabled": False, "historical": True},
                       "woocommerce": {"linked": False, "enabled": False, "historical": True}}
    r = await merge_items(real_client, headers=auth(restored.tok),
                          json={"source_entity_ids": [restored.a, restored.b], "target_sku_from": restored.a})
    assert r.status_code == 200, r.text
    assert await _queued(real_engine, restored.cid) == 0


async def test_restored_doc_markers_prevent_duplicate_invoice_push(restored):
    from celerp_docs.doc_service import list_unsynced_invoices
    pushed = {i["entity_id"] for i in await list_unsynced_invoices(restored.cid, "xero")}
    assert "doc:native" in pushed
    assert "doc:imported" not in pushed and "doc:pushed" not in pushed


async def test_restored_company_new_connector_is_inbound(restored, real_engine):
    assert await _claim(real_engine, restored.cid, "woocommerce") == "inbound"
    assert await _direction(real_engine, restored.cid, "woocommerce") == "inbound"
    assert await _claim(real_engine, restored.source, "xero") == "both"


async def test_reconnect_does_not_reset_direction(restored, real_engine):
    """Reconnecting or refreshing a connector keeps the direction its owner chose."""
    assert await _claim(real_engine, restored.cid, "woocommerce") == "inbound"
    await _sql(real_engine, "UPDATE connector_configs SET direction = 'both' WHERE company_id = :c", c=restored.cid)
    assert await _claim(real_engine, restored.cid, "woocommerce") == "both"
    assert await _direction(real_engine, restored.cid, "woocommerce") == "both"


async def test_restored_woocommerce_pushes_nothing_until_enabled(restored, real_engine, real_client):
    from celerp.connectors.outbound_queue import enqueue_outbound
    await _claim(real_engine, restored.cid, "woocommerce")
    await _patch_qty(real_client, restored.tok, restored.a, 5.0, 4.0)
    assert await _queued(real_engine, restored.cid) == 0
    await enqueue_outbound(restored.cid, "woocommerce", "inventory", "77")
    assert await _queued(real_engine, restored.cid) == 0
    await _sql(real_engine, "UPDATE connector_configs SET direction = 'both' WHERE company_id = :c", c=restored.cid)
    await _patch_qty(real_client, restored.tok, restored.a, 4.0, 3.0)
    assert await _queued(real_engine, restored.cid) == 1


# ── Owner turns outbound on ──────────────────────────────────────────────────

def _factory(transport):
    def make(token=None, *, timeout=10.0, follow_redirects=True, bulk=False, headers=None):
        merged = dict(headers or {})
        if token is not None:
            merged["Authorization"] = f"Bearer {token}"
        return httpx.AsyncClient(base_url="http://api", headers=merged, transport=transport,
                                 follow_redirects=follow_redirects, timeout=timeout)
    return make


@pytest_asyncio.fixture
async def ui(real_client, monkeypatch):
    """The UI app, its API calls routed to the real API app on `real_engine`."""
    import celerp.main
    import ui.api_client as api
    import ui.routes.settings_connectors as settings_connectors
    monkeypatch.setattr(api, "_local_client", _factory(httpx.ASGITransport(app=celerp.main.app)))
    monkeypatch.setattr(settings_connectors, "_fetch_catalog", AsyncMock(return_value=([], "", False)))
    from ui.app import app as ui_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ui_app),
                                 base_url="http://localhost", follow_redirects=False) as c:
        yield c


async def _set_direction(ui, tok: str, connector: str, value: str) -> httpx.Response:
    ui.cookies.set("celerp_token", tok)
    return await ui.post(f"/settings/connectors/{connector}/direction", data={"direction": value})


async def test_owner_enables_outbound_after_restore(restored, real_engine, real_client, ui):
    assert await _claim(real_engine, restored.cid, "woocommerce") == "inbound"
    r = await _set_direction(ui, restored.tok, "woocommerce", "sideways")
    assert r.status_code == 200 and "flash--warning" in r.text, r.text
    assert await _direction(real_engine, restored.cid, "woocommerce") == "inbound"
    r = await _set_direction(ui, restored.tok, "woocommerce", "both")
    assert r.status_code == 200, r.text
    assert await _direction(real_engine, restored.cid, "woocommerce") == "both"
    await _patch_qty(real_client, restored.tok, restored.a, 5.0, 4.0)
    assert await _queued(real_engine, restored.cid) == 1


async def test_outbound_enable_requires_owner_and_is_per_connector(restored, real_engine, ui):
    from celerp.connectors.outbound_queue import enqueue_outbound
    assert await _claim(real_engine, restored.cid, "woocommerce") == "inbound"
    assert await _claim(real_engine, restored.cid, "xero") == "inbound"
    viewer = await owner(real_engine, email="viewer@example.test", name="Viewer")
    await member(real_engine, viewer, uuid.UUID(restored.cid), "viewer")
    r = await _set_direction(ui, await token(real_engine, viewer, restored.cid, "viewer"), "woocommerce", "both")
    assert r.status_code in (302, 303), r.text
    assert await _direction(real_engine, restored.cid, "woocommerce") == "inbound"
    r = await _set_direction(ui, restored.tok, "woocommerce", "both")
    assert r.status_code == 200, r.text
    assert await _direction(real_engine, restored.cid, "woocommerce") == "both"
    assert await _direction(real_engine, restored.cid, "xero") == "inbound"
    await enqueue_outbound(restored.cid, "xero", "invoice", "doc:native")
    assert await _queued(real_engine, restored.cid) == 0


async def test_restore_notice_says_integrations_disconnected(real_engine, real_client, ui, tmp_path, monkeypatch):
    """The page after restoring a company says its integrations are disconnected and
    outbound sync is off."""
    import re
    _local(monkeypatch, tmp_path)
    user = await owner(real_engine)
    source = await company(real_engine, user, "Notice Trading", "notice-marker")
    tok = await token(real_engine, user, source)
    data = await download(real_client, tok)
    base = "/settings/restore-backup"
    ui.cookies.set("celerp_token", tok)
    r = await ui.post(f"{base}/read", files={"file": ("copy.celerp-company", data, "application/octet-stream")})
    assert r.status_code == 200, r.text
    header = next(c for c in r.headers.get_list("set-cookie") if c.startswith("celerp_company_backup_upload="))
    ui.cookies.set("celerp_company_backup_upload", header.split(";", 1)[0].split("=", 1)[1].strip('"'), path=base)
    fields = {}
    for tag in re.findall(r'<input\b[^>]*type="hidden"[^>]*>', r.text):
        name, value = re.search(r'name="([^"]*)"', tag), re.search(r'value="([^"]*)"', tag)
        if name:
            fields[name.group(1)] = value.group(1) if value else ""
    r = await ui.post(f"{base}/restore", data=fields)
    assert r.status_code == 303, r.text
    for _ in range(5):
        if r.status_code not in (301, 302, 303, 307):
            break
        for c in r.headers.get_list("set-cookie"):
            if c.startswith("celerp_token="):
                ui.cookies.set("celerp_token", c.split(";", 1)[0].split("=", 1)[1].strip('"'))
        r = await ui.get(r.headers["location"])
    assert r.status_code == 200, r.text
    assert NOTICE in r.text


# ── Reconnect ────────────────────────────────────────────────────────────────

class _Connect:
    """Connect WooCommerce through the credential form with the store and relay faked;
    ``on_bind`` and ``on_revoke`` run at those steps."""

    def __init__(self, engine, cid, *, on_bind=None, on_revoke=None):
        self.engine, self.cid = engine, cid
        self.bind = AsyncMock(side_effect=on_bind)
        self.revoke = AsyncMock(side_effect=on_revoke)

    async def __call__(self) -> dict:
        from celerp_connectors.routes import ApiKeyCredentials, store_credentials
        creds = ApiKeyCredentials(consumer_key="ck_x", consumer_secret="cs_y", store_url=STORE)
        with patch("celerp.gateway.state.relay_http_url", return_value=RELAY), \
             patch("celerp.gateway.state.relay_session_headers", return_value={"X-Session-Token": "s"}), \
             patch("celerp.services.outbound_url.validate_public_base_url", AsyncMock(return_value=STORE)), \
             patch("celerp.services.outbound_url.fetch_public_bytes",
                   AsyncMock(return_value=SimpleNamespace(status_code=200))), \
             patch("celerp.connectors.ownership.bind_connector_store", self.bind), \
             patch("celerp.connectors.remote_state.revoke_connector_remote_state", self.revoke), \
             patch("celerp.connectors.woocommerce.WooCommerceConnector.register_webhooks",
                   AsyncMock(return_value=["11"])), \
             patch("celerp.connectors.woocommerce.WooCommerceConnector.deregister_webhooks", AsyncMock()), \
             respx.mock:
            respx.post(f"{RELAY}/tokens/woocommerce").mock(return_value=httpx.Response(200, json={"stored": True}))
            async with maker(self.engine)() as s:
                return await store_credentials("woocommerce", creds, str(self.cid), None, s)


async def _observe(engine, cid, eid: str) -> dict:
    """What another request sees right now: ownership and the item's link."""
    from celerp.connectors.ownership import connector_ownership_state
    from celerp_inventory.services import external_link_for_state
    async with maker(engine)() as s:
        state = await connector_ownership_state(s, cid, "woocommerce")
        item = (await s.execute(text("SELECT state FROM projections WHERE company_id = :c AND entity_id = :e"),
                                {"c": uuid.UUID(str(cid)), "e": eid})).scalar_one()
    return {"ownership": state, "link": external_link_for_state(dict(item), "woocommerce")}


async def test_reconnect_window_historical_ids_not_active_before_relink(restored, real_engine):
    """While a reconnect is in progress, no other request sees the connector owned with
    the old external ids still live."""
    from celerp.connectors.ownership import OWNERSHIP_OWNED
    seen: list[dict] = []

    async def on_revoke(*_a, **_k):
        seen.append(await _observe(real_engine, restored.cid, restored.a))

    assert await _Connect(real_engine, restored.cid, on_revoke=on_revoke)() == {"ok": True}
    assert seen, "the reconnect never cleared remote state"
    assert all(not (o["ownership"] == OWNERSHIP_OWNED and o["link"]) for o in seen), seen
    state, _ = await _projection(real_engine, restored.cid, restored.a)
    assert state["external_links"]["woocommerce"] == {"detached": True}


async def test_reconnect_same_store_relinks_then_protects(restored, real_engine, real_client):
    """Reconnecting the same store checks it against the historical ids, then a product
    the store links again is protected from merging."""
    bound: list[dict] = []

    async def on_bind(session, company_id, connector, ctx):
        bound.append(dict((await session.execute(text(
            "SELECT state FROM projections WHERE company_id = :c AND entity_id = :e"),
            {"c": uuid.UUID(str(company_id)), "e": restored.a})).scalar_one()))

    assert await _Connect(real_engine, restored.cid, on_bind=on_bind)() == {"ok": True}
    assert bound and bound[0]["external_links"]["woocommerce"]["product_id"] == "77"
    await _links(real_engine, restored.cid, restored.a, {"woocommerce": dict(LIVE_WOO)})
    r = await merge_items(real_client, headers=auth(restored.tok),
                          json={"source_entity_ids": [restored.a, restored.b], "target_sku_from": restored.a})
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == (
        "This catalog product is currently linked to WooCommerce. Merge its physical lots instead.")


async def test_reconnect_different_store_fails_closed(restored, real_engine):
    """A different store is refused before the connector is ever owned, and the
    historical ids stay as they were."""
    from celerp.connectors.ownership import OWNERSHIP_OWNED, ConnectorStoreChangedError
    seen: list[dict] = []

    async def on_bind(*_a, **_k):
        seen.append(await _observe(real_engine, restored.cid, restored.a))
        raise ConnectorStoreChangedError("This store holds different records.")

    result = await _Connect(real_engine, restored.cid, on_bind=on_bind)()
    assert result["ok"] is False and result["error"] == "store_changed", result
    assert seen and seen[0]["ownership"] != OWNERSHIP_OWNED, seen
    assert await _direction(real_engine, restored.cid, "woocommerce") is None
    state, _ = await _projection(real_engine, restored.cid, restored.a)
    assert state["external_links"]["woocommerce"] == LIVE_WOO


async def test_restored_company_reconnect_enqueues_no_outbound(restored, real_engine, real_client):
    """Reconnecting a restored company and relinking its products queues no push."""
    assert await _Connect(real_engine, restored.cid)() == {"ok": True}
    assert await _direction(real_engine, restored.cid, "woocommerce") == "inbound"
    await _links(real_engine, restored.cid, restored.a, {"woocommerce": dict(LIVE_WOO)})
    await _patch_qty(real_client, restored.tok, restored.a, 5.0, 4.0)
    assert await _queued(real_engine, restored.cid) == 0
