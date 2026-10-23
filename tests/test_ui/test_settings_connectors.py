# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A connector's settings show one row per kind of record, each offering only the
directions that connector can sync it in, and a refused choice says why plainly."""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fasthtml.common import to_xml

_WOO = {"products": "outbound", "stock": "outbound", "orders": "inbound", "customers": "off"}


def _button(html: str, entity: str, label: str) -> str:
    row = html.split(f'id="connector-flow-woocommerce-{entity}"')[1].split("</div>")[0]
    return row.split(f">{label}<")[0].rsplit("<button", 1)[1]


def test_entity_direction_rows_render():
    from ui.routes.settings_connectors import _entity_direction_rows

    html = to_xml(_entity_direction_rows("woocommerce", _WOO, detail=True))
    for label in ("Products", "Stock", "Orders", "Customers"):
        assert f">{label}<" in html
    # Products offer every way; stock only goes out; customers are never imported.
    products = html.split('id="connector-flow-woocommerce-products"')[1].split("</div>")[0]
    for label in ("WooCommerce to Celerp", "Celerp to WooCommerce", "Both ways", "Off"):
        assert f">{label}<" in products
    stock = html.split('id="connector-flow-woocommerce-stock"')[1].split("</div>")[0]
    assert ">WooCommerce to Celerp<" not in stock and ">Both ways<" not in stock
    customers = html.split('id="connector-flow-woocommerce-customers"')[1].split("</div>")[0]
    assert customers.count("<button") == 1 and ">Off<" in customers
    assert "btn--primary" in _button(html, "products", "Celerp to WooCommerce")
    assert "btn--primary" not in _button(html, "products", "Both ways")
    assert 'hx-post="/settings/connectors/woocommerce/direction?detail=1"' in html


def _route(monkeypatch, config):
    """The settings route, with the API's save standing in: it applies the same
    validation the API route does to ``config``."""
    from fastapi import FastAPI

    import ui.api_client as api
    from celerp.connectors.base import DirectionError, validate_directions
    from celerp.connectors.registry import get
    from ui.api_client import APIError
    from ui.routes import settings_connectors as routes

    async def set_direction(token, platform, entity, direction):
        try:
            config.entity_directions = validate_directions(
                get(platform), {**config.entity_directions, entity: direction})
        except DirectionError as exc:
            raise APIError(422, str(exc)) from exc
        return {"entity_directions": config.entity_directions}

    async def allowed(request, permission):
        return None

    monkeypatch.setattr(api, "set_connector_direction", set_direction)
    monkeypatch.setattr(routes, "_token", lambda request: "jwt")
    monkeypatch.setattr(routes, "_check_permission", allowed)
    monkeypatch.setattr(routes, "_request_company_id", lambda request: "company-1")
    app = FastAPI()
    routes.setup_routes(app)
    return next(r.endpoint for r in app.routes
                if getattr(r, "path", "") == "/settings/connectors/{platform}/direction")


def _html(response) -> str:
    return response.body.decode() if hasattr(response, "body") else to_xml(response)


def _post(body: bytes):
    from starlette.requests import Request

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({
        "type": "http", "method": "POST", "path": "/settings/connectors/woocommerce/direction",
        "query_string": b"detail=1",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
    }, receive)


@pytest.mark.asyncio
async def test_entity_direction_change_saves_that_record_only(monkeypatch):
    config = SimpleNamespace(entity_directions=dict(_WOO), first_sync_at=None)
    endpoint = _route(monkeypatch, config)
    html = _html(await endpoint(_post(b"entity=products&direction=both"), "woocommerce"))
    assert config.entity_directions == {**_WOO, "products": "both"}
    assert "btn--primary" in _button(html, "products", "Both ways")


@pytest.mark.asyncio
async def test_invalid_entity_direction_message(monkeypatch):
    config = SimpleNamespace(entity_directions=dict(_WOO), first_sync_at=None)
    endpoint = _route(monkeypatch, config)
    html = _html(await endpoint(_post(b"entity=stock&direction=inbound"), "woocommerce"))
    assert config.entity_directions == _WOO
    assert "Stock cannot sync WooCommerce to Celerp" in html
