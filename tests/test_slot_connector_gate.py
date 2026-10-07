# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A slot entry's "requires_connector" on item and document pages.

An item_action, pricing_action, doc_detail_actions or doc_detail_badges entry
that names a connector shows only while the company is connected to it, the same
rule the catalog channels and bulk actions follow. The page reads the company's
connectors once, and only those its slot entries ask for.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fasthtml.common import Button, Span, to_xml
from httpx import ASGITransport, AsyncClient

from celerp.modules import slots
from test_helpers import make_test_token


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as c:
        yield c


_COMPANY = {"id": "11111111-1111-1111-1111-111111111111", "settings": {}, "currency": "USD"}


def render_action(doc: dict):
    return Button("Push to Shopify", type="button")


def render_badge(doc: dict):
    return Span("On Shopify", cls="badge")


def _item_action():
    slots.register("item_action", {"label": "Push to Shopify", "href_template": "/shopify/{entity_id}",
                                   "requires_connector": "shopify", "_module": "celerp-ship"})


def _pricing_action():
    slots.register("pricing_action", {"label": "Push to Shopify", "href_template": "/shopify/{entity_id}",
                                      "requires_connector": "shopify", "_module": "celerp-ship"})


def _doc_slots(requires_connector="shopify"):
    for slot, fn in (("doc_detail_actions", "render_action"), ("doc_detail_badges", "render_badge")):
        slots.register(slot, {"render": f"{__name__}:{fn}", "requires_connector": requires_connector,
                              "_module": "celerp-ship"})


def _panel(connected):
    from ui.routes.inventory import _advanced_panel
    return to_xml(_advanced_panel("item:1", {"quantity": 1}, None, settings={}, connected_connectors=connected))


def _pricing(connected):
    from ui.routes.inventory import _pricing_form
    return to_xml(_pricing_form("item:1", {"retail_price": 10}, [{"name": "Retail", "description": ""}], "USD",
                                [{"key": "retail_price", "editable": True}], "Retail", settings={},
                                connected_connectors=connected))


def _doc(connected):
    from ui.routes.documents import _doc_detail
    doc = {"entity_id": "doc:inv-1", "doc_type": "invoice", "status": "draft", "ref_id": "D-1", "line_items": []}
    return to_xml(_doc_detail(doc, settings={}, connected_connectors=connected))


@pytest.mark.parametrize("register, render, marks", [
    (_item_action, _panel, ["Push to Shopify"]),
    (_pricing_action, _pricing, ["/shopify/item%3A1"]),
    (_doc_slots, _doc, ["Push to Shopify", "On Shopify"]),
], ids=["item_action", "pricing_action", "doc_detail"])
def test_connector_entry_follows_the_company_connection(register, render, marks):
    register()
    shown = render({"shopify"})
    for mark in marks:
        assert mark in shown
        assert mark not in render(set())
        assert mark not in render({"woocommerce"})
        assert mark not in render(None)


def test_required_connectors_reads_only_what_entries_ask_for():
    from ui.module_slots import required_connectors
    assert required_connectors("item_action") == set()
    _item_action()
    _doc_slots()
    slots.register("item_action", {"label": "Plain", "href_template": "/p/{entity_id}", "_module": "x"})
    assert required_connectors("item_action") == {"shopify"}
    assert required_connectors("doc_detail_actions", "doc_detail_badges") == {"shopify"}


async def test_no_connector_read_when_no_entry_needs_one():
    from ui.module_slots import connected_connector_ids
    with patch("celerp.connectors.ownership.connected_connector_platforms",
               new=AsyncMock(side_effect=AssertionError("read"))):
        assert await connected_connector_ids(_COMPANY["id"], set()) == set()


async def test_failed_connector_read_hides_gated_entries():
    from celerp.connectors.ownership import ConnectorOwnershipError
    from ui.module_slots import connected_connector_ids
    with patch("celerp.connectors.ownership.connected_connector_platforms",
               new=AsyncMock(side_effect=ConnectorOwnershipError("down"))):
        assert await connected_connector_ids(_COMPANY["id"], {"shopify"}) == set()


# ── the pages pass the company's connectors ─────────────────────────────────

def _owned(connected: set[str]):
    async def platforms(session, company_id, candidates):
        assert company_id == _COMPANY["id"]
        return {c for c in candidates if c in connected}
    return patch("celerp.connectors.ownership.connected_connector_platforms", new=platforms)


@pytest.mark.parametrize("connected, shown", [({"shopify"}, True), (set(), False)])
async def test_item_actions_card_follows_the_company_connection(ui_client, connected, shown):
    _item_action()
    with (
        _owned(connected),
        patch("ui.api_client.get_item", new=AsyncMock(return_value={"entity_id": "item:1", "quantity": 1})),
        patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
        patch("ui.api_client.split_preview", new=AsyncMock(return_value=None)),
    ):
        r = await ui_client.get("/api/items/item:1/advanced-panel",
                                cookies={"celerp_token": make_test_token(role="owner")})
    assert r.status_code == 200
    assert ("Push to Shopify" in r.text) is shown


@pytest.mark.parametrize("connected, shown", [({"shopify"}, True), (set(), False)])
async def test_subscription_detail_follows_the_company_connection(ui_client, connected, shown):
    _doc_slots()
    sub = {"entity_id": "sub:1", "doc_type": "invoice", "status": "active", "line_items": []}
    with (
        _owned(connected),
        patch("ui.api_client.get_subscription", new=AsyncMock(return_value=sub)),
        patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
    ):
        r = await ui_client.get("/subscriptions/sub:1", cookies={"celerp_token": make_test_token(role="owner")})
    assert r.status_code == 200
    assert ("On Shopify" in r.text) is shown


@pytest.mark.parametrize("enabled, shown", [(["celerp-ship"], True), (["celerp-other"], False)])
async def test_subscription_detail_follows_the_company_modules(ui_client, enabled, shown):
    _doc_slots(requires_connector="")
    company = {**_COMPANY, "settings": {"enabled_modules": enabled}}
    sub = {"entity_id": "sub:1", "doc_type": "invoice", "status": "active", "line_items": []}
    with (
        _owned({"shopify"}),
        patch("ui.api_client.get_subscription", new=AsyncMock(return_value=sub)),
        patch("ui.api_client.get_company", new=AsyncMock(return_value=company)),
    ):
        r = await ui_client.get("/subscriptions/sub:1", cookies={"celerp_token": make_test_token(role="owner")})
    assert r.status_code == 200
    assert ("On Shopify" in r.text) is shown
