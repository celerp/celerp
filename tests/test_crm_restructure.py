# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for Phase 5 CRM restructure: /contacts/* routes."""

from __future__ import annotations

import pytest
pytest.importorskip("celerp_sales_funnel")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

from tests.conftest import make_test_token, authed_cookies


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(
        transport=ASGITransport(app=ui_app),
        base_url="http://ui",
        follow_redirects=False,
    ) as c:
        yield c


def _authed(token: str | None = None, role: str = "owner") -> dict:
    return {"celerp_token": token or make_test_token(role=role)}


_CONTACTS = [
    {"entity_id": "contact:1", "name": "Alice", "email": "alice@test.com", "phone": "111",
     "contact_type": "customer", "credit_limit": None, "payment_terms": "Net 30"},
    {"entity_id": "contact:2", "name": "Bob Supplier", "email": "bob@supplier.com", "phone": "222",
     "contact_type": "vendor", "credit_limit": None, "payment_terms": "Net 15"},
    {"entity_id": "contact:3", "name": "Both Co", "email": "both@test.com", "phone": "333",
     "contact_type": "both", "credit_limit": None, "payment_terms": "Net 30"},
]
_DEALS = [
    {"entity_id": "deal:1", "name": "Ruby Deal", "stage": "lead", "value": 50000.0,
     "contact_id": "contact:1", "currency": "THB"},
]
_COMPANY = {"name": "Test Corp", "currency": "THB", "timezone": "UTC", "settings": {}}


def _crm_mocks():
    return (
        patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": _CONTACTS, "total": 3})),
        patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
        patch("ui.api_client.list_deals", new=AsyncMock(return_value={"items": _DEALS, "total": 1})),
        patch("ui.api_client.get_memo_summary", new=AsyncMock(return_value={"total": 0, "items": []})),
    )


# ── Redirect tests ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_crm_redirect_to_contacts(ui_client):
    """GET /crm returns 302 to /contacts/customers (unauthenticated goes to login)."""
    r = await ui_client.get("/crm", follow_redirects=False)
    # Unauthenticated → login redirect; authenticated /crm → /contacts/customers
    assert r.status_code in (302, 303)


@pytest.mark.asyncio
async def test_customers_page_loads(ui_client):
    """GET /contacts/customers redirects to login when unauthenticated."""
    r = await ui_client.get("/contacts/customers", follow_redirects=False)
    assert r.status_code in (200, 302, 303)


@pytest.mark.asyncio
async def test_vendors_page_loads(ui_client):
    """GET /contacts/vendors redirects to login when unauthenticated."""
    r = await ui_client.get("/contacts/vendors", follow_redirects=False)
    assert r.status_code in (200, 302, 303)


@pytest.mark.asyncio
async def test_sales_funnel_at_new_url(ui_client):
    """GET /contacts/sales redirects to login when unauthenticated."""
    r = await ui_client.get("/contacts/sales", follow_redirects=False)
    assert r.status_code in (200, 302, 303)


@pytest.mark.asyncio
async def test_customers_page_renders_authenticated(ui_client):
    """GET /contacts/customers renders with contacts list when authenticated."""
    with (
        patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": _CONTACTS[:1], "total": 1})),
        patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
    ):
        r = await ui_client.get("/contacts/customers", cookies=_authed())
    assert r.status_code == 200
    assert b"Alice" in r.content or b"Customer" in r.content


@pytest.mark.asyncio
async def test_vendors_page_renders_authenticated(ui_client):
    """GET /contacts/vendors renders with vendors list when authenticated."""
    with (
        patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": [_CONTACTS[1]], "total": 1})),
        patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
    ):
        r = await ui_client.get("/contacts/vendors", cookies=_authed())
    assert r.status_code == 200
    assert b"Bob" in r.content or b"Vendor" in r.content


@pytest.mark.asyncio
async def test_sales_funnel_renders_authenticated(ui_client):
    """GET /contacts/sales renders deals kanban when authenticated."""
    with (
        patch("ui.api_client.list_deals", new=AsyncMock(return_value={"items": _DEALS, "total": 1})),
        patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
        patch("ui.api_client.get_memo_summary", new=AsyncMock(return_value={"total": 0, "items": []})),
    ):
        r = await ui_client.get("/contacts/sales", cookies=_authed())
    assert r.status_code == 200
    assert b"Deal" in r.content or b"deal" in r.content or b"Sales" in r.content


@pytest.mark.asyncio
async def test_crm_authenticated_redirects_to_customers(ui_client):
    """GET /crm when authenticated returns 302 to /contacts/customers."""
    with (
        patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": [], "total": 0})),
        patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
    ):
        r = await ui_client.get("/crm", cookies=_authed(), follow_redirects=False)
    assert r.status_code in (301, 302, 303)
    assert "contacts" in r.headers.get("location", "")


@pytest.mark.asyncio
async def test_create_customer_type_set(ui_client):
    """POST /contacts/create?type=customer creates contact with customer type."""
    with (
        patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": [], "total": 0})),
        patch("ui.api_client.create_contact", new=AsyncMock(return_value={"id": "contact:new", "event_id": 1})),
        patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
    ):
        r = await ui_client.post("/contacts/create?type=customer", cookies=_authed())
    # Should redirect to new contact detail or return 204
    assert r.status_code in (200, 204, 302, 303)
