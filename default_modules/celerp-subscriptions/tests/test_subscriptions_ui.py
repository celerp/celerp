# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""UI route coverage for celerp-subscriptions (ui/routes/subscriptions.py)."""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

from tests.helpers import make_test_token


def _authed(role: str = "owner") -> dict:
    return {"celerp_token": make_test_token(role=role)}


_CONTACTS = [{"entity_id": "ct:1", "name": "Alice", "phone": "555", "email": "a@b.c",
              "tax_id": "T1", "credit_limit": 1000, "contact_type": "customer"}]

_SUB_DOC = {
    "entity_id": "sub-001",
    "id": "sub-001",
    "doc_type": "subscription_invoice",
    "name": "Monthly Retainer",
    "status": "active",
    "frequency": "monthly",
    "start_date": "2026-01-01",
    "next_run_date": "2026-06-01",
    "contact_id": "ct:1",
    "contact_name": "Alice",
    "currency": "USD",
    "line_items": [{"description": "Service", "quantity": 1, "unit_price": 100}],
    "generated_doc_ids": [],
}


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(
        transport=ASGITransport(app=ui_app),
        base_url="http://ui",
        follow_redirects=False,
    ) as c:
        yield c


class TestSubscriptionsUI:
    """UI route coverage for ui/routes/subscriptions.py."""

    async def test_list_unauthenticated_redirects(self, ui_client):
        r = await ui_client.get("/subscriptions")
        assert r.status_code == 302
        assert "/login" in r.headers["location"]

    async def test_list_sales_authenticated(self, ui_client):
        with patch("ui.api_client.list_subscriptions", new=AsyncMock(return_value={"items": [_SUB_DOC]})):
            r = await ui_client.get("/subscriptions?direction=sales", cookies=_authed())
        assert r.status_code == 200
        assert "Sales Subscriptions" in r.text

    async def test_list_purchasing_authenticated(self, ui_client):
        with patch("ui.api_client.list_subscriptions", new=AsyncMock(return_value={"items": []})):
            r = await ui_client.get("/subscriptions?direction=purchasing", cookies=_authed())
        assert r.status_code == 200
        assert "Purchasing Subscriptions" in r.text

    async def test_new_form_sales(self, ui_client):
        with patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": _CONTACTS})):
            r = await ui_client.get("/subscriptions/new?direction=sales", cookies=_authed())
        assert r.status_code == 200
        assert "New Subscription" in r.text
        assert "subscription_invoice" in r.text

    async def test_new_form_purchasing(self, ui_client):
        with patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": _CONTACTS})):
            r = await ui_client.get("/subscriptions/new?direction=purchasing", cookies=_authed())
        assert r.status_code == 200
        assert "subscription_po" in r.text

    async def test_new_form_unauthenticated_redirects(self, ui_client):
        r = await ui_client.get("/subscriptions/new?direction=sales")
        assert r.status_code == 302
        assert "/login" in r.headers["location"]

    async def test_create_subscription_redirects_to_detail(self, ui_client):
        with patch("ui.api_client.create_subscription", new=AsyncMock(return_value=_SUB_DOC)):
            r = await ui_client.post(
                "/subscriptions/new",
                cookies=_authed(),
                data={
                    "doc_type": "subscription_invoice",
                    "name": "Test Sub",
                    "contact_id": "ct:1",
                    "frequency": "monthly",
                    "start_date": "2026-01-01",
                    "currency": "USD",
                },
            )
        assert r.status_code == 303
        assert "/subscriptions/sub-001" in r.headers["location"]

    async def test_detail_page_renders(self, ui_client):
        with patch("ui.api_client.get_subscription", new=AsyncMock(return_value=_SUB_DOC)):
            r = await ui_client.get("/subscriptions/sub-001", cookies=_authed())
        assert r.status_code == 200
        assert "Monthly Retainer" in r.text
        assert "monthly" in r.text.lower()

    async def test_detail_page_unauthenticated_redirects(self, ui_client):
        r = await ui_client.get("/subscriptions/sub-001")
        assert r.status_code == 302
        assert "/login" in r.headers["location"]

    async def test_detail_page_api_error_redirects_to_list(self, ui_client):
        from ui.api_client import APIError
        with patch("ui.api_client.get_subscription", new=AsyncMock(side_effect=APIError(404, "not found"))):
            r = await ui_client.get("/subscriptions/bad-id", cookies=_authed())
        assert r.status_code == 302
        assert "/subscriptions" in r.headers["location"]
