# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A catalog product is channel-linked only while its connector is connected to the
company. Historical external ids (after a disconnect, a deactivation, a legacy key or a
restored copy) stay on the item as provenance but never block a merge, never show as a
live channel and never authorize outbound work. Catalog state, the connector's own
state and merge protection all read the same definition."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import inspect
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy import text

from company_backup_support import company, owner, token
from migration_support import auth, count, maker, real_client, real_engine  # noqa: F401

pytestmark = pytest.mark.asyncio

REPO = Path(__file__).resolve().parents[1]
PRODUCTION_ROOTS = ("celerp", "ui", "default_modules")
SESSION_TOKEN = "test-session-token-channel-links"
LIVE_SHOPIFY = {"product_id": "9001", "variant_id": "9002", "sync_enabled": True}
LIVE_WOO = {"product_id": "77", "sync_enabled": True}


# ── Static structure ─────────────────────────────────────────────────────────

def _production_files():
    for base in PRODUCTION_ROOTS:
        for path in sorted((REPO / base).rglob("*.py")):
            rel = path.relative_to(REPO)
            if "tests" in rel.parts or path.name.startswith("test_"):
                continue
            yield rel, path


def _channel_literals() -> list[tuple[str, int]]:
    """Every literal set, tuple or list naming exactly the two product channels (bare or
    as key prefixes) in production code."""
    hits = []
    for rel, path in _production_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Set, ast.Tuple, ast.List)) or len(node.elts) < 2:
                continue
            values = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if len(values) == len(node.elts) and {v.rstrip(":") for v in values} == {"shopify", "woocommerce"}:
                hits.append((str(rel), node.lineno))
    return hits


def test_product_channel_platforms_single_source():
    """The product channels are named once; every merge, disconnect and Catalog rule reads that one tuple."""
    from celerp.connectors import ownership
    assert tuple(sorted(ownership.PRODUCT_CHANNEL_PLATFORMS)) == ("shopify", "woocommerce")
    hits = _channel_literals()
    assert [f for f, _ in hits] == ["celerp/connectors/ownership.py"], hits


def _catalog_channel_ids() -> set[str]:
    spec = importlib.util.spec_from_file_location(
        "_connectors_manifest", REPO / "default_modules" / "celerp-connectors" / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {slot["id"] for slot in module.PLUGIN_MANIFEST["slots"]["catalog_channel"]}


def test_product_channel_platforms_match_registry():
    """The product channels are exactly the registered connectors that write item
    external links, and exactly the Catalog's channel columns."""
    from celerp.connectors import registry
    from celerp.connectors.ownership import PRODUCT_CHANNEL_PLATFORMS
    writers = {
        c.name for c in registry.all_connectors()
        if "upsert_external_product" in Path(inspect.getsourcefile(type(c))).read_text()
    }
    assert set(PRODUCT_CHANNEL_PLATFORMS) == writers
    assert set(PRODUCT_CHANNEL_PLATFORMS) == _catalog_channel_ids()


def test_external_link_for_state_stays_pure():
    """The decoder needs no database: a stale link is still decoded, a detached one is
    not, and a legacy key only decodes for the platform it names."""
    from celerp_inventory.services import external_link_for_state
    stale = {"external_links": {"shopify": dict(LIVE_SHOPIFY)}}
    assert external_link_for_state(stale, "shopify")["product_id"] == "9001"
    assert external_link_for_state({"external_links": {"shopify": {"detached": True}}}, "shopify") == {}
    legacy = {"idempotency_key": "shopify:1:2"}
    assert external_link_for_state(legacy, "shopify") == {"product_id": "1", "variant_id": "2", "sync_enabled": True}
    assert external_link_for_state(legacy, "woocommerce") == {}
    assert external_link_for_state({"idempotency_key": "woocommerce:5"}, "shopify") == {}
    params = list(inspect.signature(external_link_for_state).parameters)
    assert params == ["state", "platform"]


# ── Caller census ────────────────────────────────────────────────────────────

INVENTORY = "default_modules/celerp-inventory/celerp_inventory"
_SYNC = "connector sync under an owned connector: pure decoder"
_PURE = "pure helper: decodes identity for its caller, gates nothing"
# ACTIVE callers allow or refuse a user operation because of a channel link and must
# use the active-link predicate (the connected platform set).
EXTERNAL_LINK_CALLERS: dict[tuple[str, str], tuple[str, str]] = {
    ("celerp/connectors/woocommerce.py", "_reconcile_missing_product_links"): ("SYNC", _SYNC),
    ("celerp/connectors/woocommerce.py", "ensure_product_link"): ("SYNC", _SYNC),
    ("celerp/connectors/woocommerce.py", "handle_product_deleted"): ("SYNC", _SYNC),
    ("celerp/connectors/outbound_queue.py", "enqueue_item_change"):
        ("SYNC", "queues Woo pushes only while an outbound WooCommerce config is held by the company"),
    ("default_modules/celerp-connectors/celerp_connectors/routes.py", "set_item_sync"):
        ("SYNC", "requires the connector to be owned before changing a link"),
    ("default_modules/celerp-docs/celerp_docs/doc_service.py", "_set_woocommerce_order_stock_paused"): ("SYNC", _SYNC),
    ("default_modules/celerp-docs/celerp_docs/doc_service.py", "upsert_order_from_woocommerce"): ("SYNC", _SYNC),
    (f"{INVENTORY}/routes.py", "_plan_merge"): ("ACTIVE", "refuses a merge for a live channel link"),
    (f"{INVENTORY}/routes.py", "query_items"): ("ACTIVE", "Catalog channel state and the source filter"),
    (f"{INVENTORY}/services.py", "build_channel_states"): ("ACTIVE", "Catalog linked/historical state"),
    (f"{INVENTORY}/services.py", "_assert_external_identity_available"): ("SYNC", _SYNC),
    (f"{INVENTORY}/services.py", "_external_ids"): ("PURE", _PURE),
    (f"{INVENTORY}/services.py", "_outbound_link"): ("SYNC", _SYNC),
    (f"{INVENTORY}/services.py", "_outbound_row"): ("SYNC", _SYNC),
    (f"{INVENTORY}/services.py", "_select_external_anchor"): ("SYNC", _SYNC),
    (f"{INVENTORY}/services.py", "detach_external_link"): ("PURE", "decides whether a link is left to detach"),
    (f"{INVENTORY}/services.py", "list_item_for_external_identity"): ("SYNC", _SYNC),
    (f"{INVENTORY}/services.py", "resolve_external_product"): ("SYNC", _SYNC),
    (f"{INVENTORY}/services.py", "set_external_link"): ("SYNC", _SYNC),
    (f"{INVENTORY}/services.py", "set_external_link_state"): ("SYNC", _SYNC),
    (f"{INVENTORY}/services.py", "upsert_external_product"): ("SYNC", _SYNC),
}


def _callers() -> dict[tuple[str, str], ast.AST]:
    """(file, function or method) of every production reference to external_link_for_state."""
    found: dict[tuple[str, str], ast.AST] = {}
    functions = (ast.FunctionDef, ast.AsyncFunctionDef)
    for rel, path in _production_files():
        tree = ast.parse(path.read_text())
        tops = [n for n in tree.body if isinstance(n, functions)]
        tops += [m for c in tree.body if isinstance(c, ast.ClassDef) for m in c.body if isinstance(m, functions)]
        for top in tops:
            for node in ast.walk(top):
                if (isinstance(node, ast.Name) and node.id == "external_link_for_state"
                        and isinstance(node.ctx, ast.Load)) or (
                        isinstance(node, ast.Attribute) and node.attr == "external_link_for_state"):
                    found[(str(rel), top.name)] = top
    return found


def check_external_link_for_state_callers_census():
    """Every caller of the link decoder has a recorded disposition, and every caller that
    gates a user operation reads the connected platform set."""
    callers = _callers()
    assert set(callers) == set(EXTERNAL_LINK_CALLERS), (
        sorted(set(callers) - set(EXTERNAL_LINK_CALLERS)), sorted(set(EXTERNAL_LINK_CALLERS) - set(callers)))
    for key, (kind, reason) in EXTERNAL_LINK_CALLERS.items():
        assert kind in {"ACTIVE", "SYNC", "PURE"} and reason.strip(), key
        if kind == "ACTIVE":
            names = {n.id for n in ast.walk(callers[key]) if isinstance(n, ast.Name)}
            names |= {a.arg for a in ast.walk(callers[key]) if isinstance(a, ast.arg)}
            names |= {k.arg for k in ast.walk(callers[key]) if isinstance(k, ast.keyword) and k.arg}
            assert any("connected" in n for n in names), key


def test_external_link_for_state_callers_census():
    check_external_link_for_state_callers_census()


# ── Shared helpers (rolled-back `client` fixture) ────────────────────────────

async def _register(client) -> tuple[dict, str]:
    from celerp.services.auth import decode_access_token
    r = await client.post("/auth/register", json={
        "company_name": "Channel Co", "email": f"owner-{uuid.uuid4().hex[:8]}@example.test",
        "name": "Owner", "password": "pwvalid12"})
    assert r.status_code == 200, r.text
    tok = r.json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}, decode_access_token(tok)["company_id"]


async def _item(client, headers, sku: str, **extra) -> str:
    r = await client.post("/items", headers=headers, json={
        "sku": sku, "name": sku, "quantity": 5.0, "sell_by": "piece", "category": "Raw",
        "status": "available", **extra})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _set_state(session, cid: str, eid: str, **fields) -> None:
    from celerp.models.projections import Projection
    row = await session.get(Projection, {"company_id": uuid.UUID(cid), "entity_id": eid}, populate_existing=True)
    row.state = {**(row.state or {}), **fields}
    await session.commit()


async def _state(session, cid: str, eid: str) -> dict:
    from celerp.models.projections import Projection
    row = await session.get(Projection, {"company_id": uuid.UUID(cid), "entity_id": eid}, populate_existing=True)
    return dict(row.state or {})


async def _connect(session, company_id: str, connector: str, **kw) -> None:
    from celerp.models.connector_config import ConnectorConfig
    session.add(ConnectorConfig(company_id=company_id, connector=connector, **kw))
    await session.commit()


def _legacy_id() -> str:
    from celerp.config import ensure_instance_id
    return ensure_instance_id()


async def _merge(client, headers, a: str, b: str):
    return await client.post("/items/merge", headers=headers,
                             json={"source_entity_ids": [a, b], "target_sku_from": a})


async def _channel_state(client, headers, eid: str) -> dict:
    r = await client.get("/items", headers=headers, params={"limit": 500})
    assert r.status_code == 200, r.text
    return next(i for i in r.json()["items"] if i["id"] == eid)["_channel_state"]


async def _pair(client, session, headers, cid, tag: str, **links) -> tuple[str, str]:
    """Two mergeable items; the first carries ``links`` as its external links."""
    a = await _item(client, headers, f"{tag}-A")
    b = await _item(client, headers, f"{tag}-B")
    if links:
        await _set_state(session, cid, a, external_links=links)
    return a, b


@pytest.fixture
def connect_session(client):
    """An active Connect session for the /connectors routes."""
    from celerp.gateway import state
    saved = state.get_session_token()
    state.set_session_token(SESSION_TOKEN)
    yield
    state.set_session_token(saved)


# ── Active vs historical ─────────────────────────────────────────────────────

async def test_connected_connector_platforms_follows_ownership(client, session):
    """Connected means the company owns the connector's configuration; none, another
    company's, an unassigned legacy row or ambiguous ownership is not connected."""
    from celerp.connectors import ownership
    from celerp.services.auth import decode_access_token
    h, cid = await _register(client)
    r = await client.post("/companies", json={"name": "Other Channel Co"}, headers=h)
    assert r.status_code == 200, r.text
    other = decode_access_token(r.json()["access_token"])["company_id"]
    assert await ownership.connected_connector_platforms(session, cid) == set()
    await _connect(session, cid, "shopify")
    await _connect(session, other, "woocommerce")
    assert await ownership.connected_connector_platforms(session, cid) == {"shopify"}
    assert await ownership.connected_connector_platforms(session, other) == {"woocommerce"}
    assert await ownership.connected_connector_platforms(session, cid, ("woocommerce",)) == set()
    await _connect(session, _legacy_id(), "shopify")
    assert await ownership.connected_connector_platforms(session, cid) == set()
    third = str(uuid.uuid4())
    assert await ownership.connected_connector_platforms(session, third) == set()


async def test_stale_external_id_without_connector_is_historical(client, session):
    h, cid = await _register(client)
    a, b = await _pair(client, session, h, cid, "STALE", shopify=dict(LIVE_SHOPIFY))
    assert await _channel_state(client, h, a) == {
        "shopify": {"linked": False, "enabled": False, "historical": True}}
    r = await _merge(client, h, a, b)
    assert r.status_code == 200, r.text
    assert (await _state(session, cid, a))["external_links"]["shopify"]["product_id"] == "9001"


async def test_historical_identity_blocks_no_local_operation(client, session):
    h, cid = await _register(client)
    a, b = await _pair(client, session, h, cid, "HIST", shopify=dict(LIVE_SHOPIFY), woocommerce=dict(LIVE_WOO))
    r = await client.patch(f"/items/{a}", headers=h,
                           json={"fields_changed": {"name": {"old": "HIST-A", "new": "Renamed"}}})
    assert r.status_code == 200, r.text
    r = await _merge(client, h, a, b)
    assert r.status_code == 200, r.text


async def test_historical_identity_does_not_block_split(client, session):
    h, cid = await _register(client)
    a = await _item(client, h, "SPLIT-A", weight=10.0, allow_splitting=True)
    await _set_state(session, cid, a, external_links={"shopify": dict(LIVE_SHOPIFY)})
    r = await client.post(f"/items/{a}/split", headers=h, json={"children": [{"sku": "SPLIT-A.1", "quantity": 3}]})
    assert r.status_code == 200, r.text


async def test_historical_identity_authorizes_no_outbound(client, session):
    """A detached link or a leftover legacy key never queues a WooCommerce push, with or
    without an outbound WooCommerce connection."""
    from celerp.models.connector_config import OutboundQueue
    h, cid = await _register(client)
    a = await _item(client, h, "OUT-A")
    await _set_state(session, cid, a, external_links={"woocommerce": {"detached": True}},
                     idempotency_key="woocommerce:77")

    async def queued() -> int:
        return await session.scalar(sa.select(sa.func.count()).select_from(OutboundQueue).where(
            OutboundQueue.company_id == cid))

    r = await client.patch(f"/items/{a}", headers=h, json={"fields_changed": {"quantity": {"old": 5.0, "new": 4.0}}})
    assert r.status_code == 200, r.text
    assert await queued() == 0
    await _connect(session, cid, "woocommerce", direction="both")
    r = await client.patch(f"/items/{a}", headers=h, json={"fields_changed": {"quantity": {"old": 4.0, "new": 3.0}}})
    assert r.status_code == 200, r.text
    assert await queued() == 0


async def test_active_channel_link_blocks_merge_naming_provider(client, session):
    h, cid = await _register(client)
    await _connect(session, cid, "shopify")
    a, b = await _pair(client, session, h, cid, "LIVE", shopify=dict(LIVE_SHOPIFY))
    assert (await _channel_state(client, h, a))["shopify"]["linked"] is True
    r = await _merge(client, h, a, b)
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == (
        "This catalog product is currently linked to Shopify. Merge its physical lots instead.")
    assert (await _state(session, cid, a)).get("status") == "available"


async def test_merge_error_lists_live_providers_in_order(client, session):
    """The refusal names every live channel, sorted, and never a disconnected one."""
    h, cid = await _register(client)
    await _connect(session, cid, "woocommerce")
    a, b = await _pair(client, session, h, cid, "BOTH", shopify=dict(LIVE_SHOPIFY), woocommerce=dict(LIVE_WOO))
    r = await _merge(client, h, a, b)
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == (
        "This catalog product is currently linked to WooCommerce. Merge its physical lots instead.")
    await _connect(session, cid, "shopify")
    r = await _merge(client, h, a, b)
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == (
        "This catalog product is currently linked to Shopify and WooCommerce. Merge its physical lots instead.")


async def test_legacy_idempotency_key_identity_does_not_block_merge(client, session):
    h, cid = await _register(client)
    a, b = await _pair(client, session, h, cid, "LEGACY")
    await _set_state(session, cid, a, idempotency_key="shopify:11:12")
    assert await _channel_state(client, h, a) == {
        "shopify": {"linked": False, "enabled": False, "historical": True}}
    r = await _merge(client, h, a, b)
    assert r.status_code == 200, r.text


async def test_legacy_unassigned_disconnect_keeps_identities(client, session, connect_session):
    """Resetting a connector no company owns leaves item identities in place; they are
    historical, so they do not block a merge."""
    h, cid = await _register(client)
    await _connect(session, _legacy_id(), "shopify")
    a, b = await _pair(client, session, h, cid, "UNASSIGNED", shopify=dict(LIVE_SHOPIFY))
    assert await _channel_state(client, h, a) == {
        "shopify": {"linked": False, "enabled": False, "historical": True}}
    with patch("celerp.connectors.remote_state.revoke_connector_remote_state", AsyncMock()):
        r = await client.delete("/connectors/shopify/unassigned", headers=h)
    assert r.status_code == 200 and r.json() == {"ok": True}, r.text
    assert (await _state(session, cid, a))["external_links"]["shopify"] == LIVE_SHOPIFY
    r = await _merge(client, h, a, b)
    assert r.status_code == 200, r.text


async def test_local_product_merge_never_channel_linked(client, session):
    h, cid = await _register(client)
    await _connect(session, cid, "shopify")
    await _connect(session, cid, "woocommerce")
    a, b = await _pair(client, session, h, cid, "LOCAL")
    assert await _channel_state(client, h, a) == {}
    r = await _merge(client, h, a, b)
    assert r.status_code == 200, r.text
    assert await _channel_state(client, h, r.json()["id"]) == {}


async def test_disconnect_detaches_identity_and_allows_merge(client, session, connect_session):
    h, cid = await _register(client)
    await _connect(session, cid, "shopify")
    a, b = await _pair(client, session, h, cid, "DISC", shopify=dict(LIVE_SHOPIFY))
    r = await _merge(client, h, a, b)
    assert r.status_code == 409 and "currently linked to Shopify" in r.json()["detail"], r.text
    with patch("celerp.connectors.remote_state.revoke_connector_remote_state", AsyncMock()):
        r = await client.delete("/connectors/shopify/credentials", headers=h)
    assert r.status_code == 200 and r.json() == {"ok": True}, r.text
    assert (await _state(session, cid, a))["external_links"]["shopify"] == {"detached": True}
    r = await _merge(client, h, a, b)
    assert r.status_code == 200, r.text


# ── Deactivation ─────────────────────────────────────────────────────────────

async def _ledger_rows(session, cid: str) -> int:
    return await session.scalar(text("SELECT count(*) FROM ledger WHERE company_id = :c"), {"c": uuid.UUID(cid)})


async def test_deactivate_reactivate_detaches_links_and_allows_merge(client, session):
    h, cid = await _register(client)
    await _connect(session, cid, "shopify")
    a, b = await _pair(client, session, h, cid, "DEACT", shopify=dict(LIVE_SHOPIFY))
    with patch("celerp.connectors.remote_state.revoke_connector_remote_state", AsyncMock()):
        r = await client.delete("/companies/me", headers=h)
    assert r.status_code == 200, r.text
    assert (await _state(session, cid, a))["external_links"]["shopify"] == {"detached": True}
    r = await client.post("/companies/me/reactivate", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["connectors_to_reconnect"] == ["shopify"]
    r = await _merge(client, h, a, b)
    assert r.status_code == 200, r.text


async def test_deactivation_detach_is_idempotent_and_cleans_stale_links(client, session):
    """Deactivation detaches every product-channel identity, stale ones included, and a
    repeated deactivation writes nothing more."""
    h, cid = await _register(client)
    a, _ = await _pair(client, session, h, cid, "STALEDEACT", shopify=dict(LIVE_SHOPIFY), woocommerce=dict(LIVE_WOO))
    c = await _item(client, h, "STALEDEACT-C")
    await _set_state(session, cid, c, idempotency_key="woocommerce:88")
    with patch("celerp.connectors.remote_state.revoke_connector_remote_state", AsyncMock()):
        r = await client.delete("/companies/me", headers=h)
        assert r.status_code == 200, r.text
        links = (await _state(session, cid, a))["external_links"]
        assert links == {"shopify": {"detached": True}, "woocommerce": {"detached": True}}
        assert (await _state(session, cid, c))["external_links"] == {"woocommerce": {"detached": True}}
        before = await _ledger_rows(session, cid)
        r = await client.delete("/companies/me", headers=h)
        assert r.status_code == 200, r.text
    assert await _ledger_rows(session, cid) == before


async def test_deactivation_rolls_back_when_detach_fails(client, session):
    from celerp.models.company import Company
    from celerp.models.connector_config import ConnectorConfig
    h, cid = await _register(client)
    await _connect(session, cid, "shopify")
    a, _ = await _pair(client, session, h, cid, "ROLLBACK", shopify=dict(LIVE_SHOPIFY))
    with patch("celerp.connectors.remote_state.revoke_connector_remote_state", AsyncMock()), \
         patch("celerp_inventory.services.detach_external_links_for_platform",
               AsyncMock(side_effect=RuntimeError("detach failed"))):
        r = await client.delete("/companies/me", headers=h)
    assert r.status_code == 503, r.text
    assert r.json()["detail"].endswith("the company was not deactivated.")
    company_row = await session.get(Company, uuid.UUID(cid), populate_existing=True)
    assert company_row.is_active is True
    assert await session.scalar(sa.select(sa.func.count()).select_from(ConnectorConfig).where(
        ConnectorConfig.company_id == cid)) == 1
    assert (await _state(session, cid, a))["external_links"]["shopify"] == LIVE_SHOPIFY


# ── Ownership edge cases ─────────────────────────────────────────────────────

async def test_merge_ownership_lookup_failure_refuses_without_channel_claim(client, session, monkeypatch):
    from celerp.connectors import ownership
    h, cid = await _register(client)
    a, b = await _pair(client, session, h, cid, "LOOKUP", shopify=dict(LIVE_SHOPIFY))
    monkeypatch.setattr(ownership, "connected_connector_platforms",
                        AsyncMock(side_effect=ownership.ConnectorOwnershipError("lookup failed")))
    r = await _merge(client, h, a, b)
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert "linked" not in detail.lower() and "Nothing was merged" in detail
    assert (await _state(session, cid, a)).get("status") == "available"


async def test_merge_with_ambiguous_connector_ownership_is_not_blocked(client, session):
    from celerp.connectors import ownership
    h, cid = await _register(client)
    await _connect(session, cid, "shopify")
    await _connect(session, _legacy_id(), "shopify")
    assert await ownership.connector_ownership_state(session, cid, "shopify") == ownership.OWNERSHIP_AMBIGUOUS
    a, b = await _pair(client, session, h, cid, "AMBIG", shopify=dict(LIVE_SHOPIFY))
    r = await _merge(client, h, a, b)
    assert r.status_code == 200, r.text


# ── Committed state (real database) ──────────────────────────────────────────

async def _real_setup(engine, name: str = "Link Trading"):
    user = await owner(engine)
    cid = await company(engine, user, name, f"m{uuid.uuid4().hex[:8]}")
    return user, cid, await token(engine, user, cid)


async def _real_item(client, tok: str, sku: str) -> str:
    r = await client.post("/items", headers=auth(tok), json={
        "sku": sku, "name": sku, "quantity": 5.0, "sell_by": "piece", "category": "Raw", "status": "available"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _sql(engine, sql: str, **params) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(sql), params)


async def _real_links(engine, cid, eid: str, links: dict) -> None:
    import json
    async with engine.begin() as conn:
        state = (await conn.execute(text(
            "SELECT state FROM projections WHERE company_id = :c AND entity_id = :e"),
            {"c": cid, "e": eid})).scalar_one()
        await conn.execute(text(
            "UPDATE projections SET state = CAST(:s AS json) WHERE company_id = :c AND entity_id = :e"),
            {"s": json.dumps({**state, "external_links": links}), "c": cid, "e": eid})


async def _set_shopify_owner(engine, owner_id: str | None) -> None:
    await _sql(engine, "DELETE FROM connector_configs WHERE connector = 'shopify'")
    if owner_id is not None:
        await _sql(engine, "INSERT INTO connector_configs (company_id, connector, direction, sync_frequency, "
                           "daily_sync_hour) VALUES (:c, 'shopify', 'both', 'realtime', 2)", c=owner_id)


async def test_connector_state_and_catalog_state_agree(real_engine, real_client):
    """For every ownership state, the connector's own state, the Catalog channel column,
    the Catalog's connected-channel gate and merge protection agree."""
    from celerp.connectors import ownership
    from ui.routes.inventory import _connected_connector_ids
    _, cid, tok = await _real_setup(real_engine)
    user2 = await owner(real_engine, email="second-owner@example.test")
    other = await company(real_engine, user2, "Other Trading", "other-marker")
    scenarios = {
        ownership.OWNERSHIP_NONE: None,
        ownership.OWNERSHIP_OWNED: str(cid),
        ownership.OWNERSHIP_OTHER: str(other),
        ownership.OWNERSHIP_UNASSIGNED: _legacy_id(),
    }
    for expected, owner_id in scenarios.items():
        await _set_shopify_owner(real_engine, owner_id)
        tag = f"AGREE-{expected}".upper()
        a = await _real_item(real_client, tok, f"{tag}-A")
        b = await _real_item(real_client, tok, f"{tag}-B")
        await _real_links(real_engine, cid, a, {"shopify": dict(LIVE_SHOPIFY)})
        async with maker(real_engine)() as s:
            assert await ownership.connector_ownership_state(s, cid, "shopify") == expected
        active = expected == ownership.OWNERSHIP_OWNED
        r = await real_client.get("/items", headers=auth(tok), params={"q": tag})
        state = next(i for i in r.json()["items"] if i["id"] == a)["_channel_state"]["shopify"]
        assert state["linked"] is active, (expected, state)
        assert state.get("historical", False) is (not active), (expected, state)
        assert ("shopify" in await _connected_connector_ids(str(cid))) is active, expected
        r = await real_client.post("/items/merge", headers=auth(tok),
                                   json={"source_entity_ids": [a, b], "target_sku_from": a})
        assert r.status_code == (409 if active else 200), (expected, r.text)


# ── Merge against concurrent connect / disconnect ────────────────────────────

_WAITERS = text(
    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())")


async def _await_advisory_waiter(engine, task: asyncio.Task) -> None:
    """Wait until the merge is queued on an advisory (connector) lock; it must not finish first."""
    async with engine.connect() as conn:
        for _ in range(100):
            if task.done():
                pytest.fail(f"the merge did not wait for the connector lock: {task.result().text}")
            if (await conn.execute(_WAITERS)).scalar_one():
                return
            await conn.rollback()
            await asyncio.sleep(0.02)
    pytest.fail("the merge never queued on the connector lock")


async def _hold_connector(session, platform: str) -> None:
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                          {"k": f"connector-owner:{platform}"})


def _merge_task(client, tok: str, a: str, b: str) -> asyncio.Task:
    return asyncio.create_task(client.post("/items/merge", headers=auth(tok),
                                           json={"source_entity_ids": [a, b], "target_sku_from": a}))


async def test_merge_takes_shared_fences_sorted_before_item_locks(real_engine, real_client):
    """The merge joins every product-channel fence, in sorted order, before it locks the
    company's item codes or the items."""
    _, cid, tok = await _real_setup(real_engine)
    a = await _real_item(real_client, tok, "FENCE-A")
    b = await _real_item(real_client, tok, "FENCE-B")
    holder = maker(real_engine)()
    probe = maker(real_engine)()
    try:
        await _hold_connector(holder, "woocommerce")
        task = _merge_task(real_client, tok, a, b)
        try:
            await _await_advisory_waiter(real_engine, task)
            assert (await probe.execute(text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k, 0))"),
                                        {"k": "connector-owner:shopify"})).scalar_one() is False
            await probe.execute(text("SELECT 1 FROM companies WHERE id = :c FOR NO KEY UPDATE NOWAIT"), {"c": cid})
            await probe.execute(text(
                "SELECT 1 FROM projections WHERE company_id = :c AND entity_id IN (:a, :b) FOR UPDATE NOWAIT"),
                {"c": cid, "a": a, "b": b})
            await probe.rollback()
        finally:
            await holder.commit()
        r = await task
        assert r.status_code == 200, r.text
    finally:
        await probe.close()
        await holder.close()


async def test_merge_racing_disconnect_is_clean(real_engine, real_client):
    """A merge that starts while the connector is being disconnected waits for the
    disconnect, then sees the detached identity and merges."""
    from celerp.models.connector_config import ConnectorConfig
    from celerp_inventory.services import detach_external_links_for_platform
    _, cid, tok = await _real_setup(real_engine)
    await _set_shopify_owner(real_engine, str(cid))
    a = await _real_item(real_client, tok, "RDISC-A")
    b = await _real_item(real_client, tok, "RDISC-B")
    await _real_links(real_engine, cid, a, {"shopify": dict(LIVE_SHOPIFY)})
    holder = maker(real_engine)()
    try:
        await _hold_connector(holder, "shopify")
        task = _merge_task(real_client, tok, a, b)
        try:
            await _await_advisory_waiter(real_engine, task)
            await detach_external_links_for_platform(holder, cid, "shopify")
            await holder.execute(sa.delete(ConnectorConfig).where(ConnectorConfig.company_id == str(cid)))
        finally:
            await holder.commit()
        r = await task
        assert r.status_code == 200, r.text
    finally:
        await holder.close()
    assert await count(real_engine, "connector_configs", "company_id = :c", c=str(cid)) == 0


async def test_merge_racing_connect_rechecks_ownership(real_engine, real_client):
    """A merge that starts while the connector is being connected and a product linked
    waits for the connect, then refuses because the link is now live."""
    import json
    _, cid, tok = await _real_setup(real_engine)
    a = await _real_item(real_client, tok, "RCONN-A")
    b = await _real_item(real_client, tok, "RCONN-B")
    holder = maker(real_engine)()
    try:
        await _hold_connector(holder, "shopify")
        task = _merge_task(real_client, tok, a, b)
        try:
            await _await_advisory_waiter(real_engine, task)
            await holder.execute(text(
                "INSERT INTO connector_configs (company_id, connector, direction, sync_frequency, daily_sync_hour) "
                "VALUES (:c, 'shopify', 'both', 'realtime', 2)"), {"c": str(cid)})
            state = (await holder.execute(text(
                "SELECT state FROM projections WHERE company_id = :c AND entity_id = :e"),
                {"c": cid, "e": a})).scalar_one()
            await holder.execute(text(
                "UPDATE projections SET state = CAST(:s AS json) WHERE company_id = :c AND entity_id = :e"),
                {"s": json.dumps({**state, "external_links": {"shopify": dict(LIVE_SHOPIFY)}}), "c": cid, "e": a})
        finally:
            await holder.commit()
        r = await task
        assert r.status_code == 409, r.text
        assert "currently linked to Shopify" in r.json()["detail"]
    finally:
        await holder.close()
