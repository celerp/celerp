# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Shared fixtures for connector tests."""
from __future__ import annotations

import os
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

from celerp.connectors.base import ConnectorContext
from connector_test_support import inside_execution  # noqa: F401


@pytest.fixture(autouse=True)
def _connectors_depend_on_their_modules(connector_modules_running):
    """Connectors depend on Inventory, Documents and Contacts, so they run here."""


@pytest_asyncio.fixture
async def media_tokens_in_test_transaction(session, monkeypatch):
    """Image link tokens are issued and read in sessions of their own
    (celerp.services.connector_media); here those join the test's transaction, so
    nothing is committed."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from celerp.services import connector_media

    monkeypatch.setattr(connector_media, "SessionLocal", async_sessionmaker(
        bind=await session.connection(), class_=AsyncSession, expire_on_commit=False,
        join_transaction_mode="create_savepoint"))


@pytest.fixture
def ctx_shopify():
    return ConnectorContext(
        company_id="test-co",
        access_token="shpat_test",
        store_handle="test-store.myshopify.com",
    )


@pytest.fixture
def ctx_quickbooks():
    return ConnectorContext(
        company_id="test-co",
        access_token="qb_test_token",
        store_handle="1234567890",  # realmId is Intuit-issued and numeric
    )


@pytest.fixture
def ctx_xero():
    return ConnectorContext(
        company_id="test-co",
        access_token="",
        store_handle="tenant-abc",
    )


@pytest.fixture
def xero_relay():
    """Point Xero calls at a test relay; yields the Xero API base on it."""
    with patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"), \
         patch("celerp.gateway.state.relay_session_headers",
               return_value={"X-Session-Token": "sess-1", "X-Instance-ID": "inst-1"}):
        yield "https://relay.test/connectors/xero/api"


@pytest.fixture
def mock_upsert_item():
    with patch("celerp.connectors.upsert.upsert_item", new_callable=AsyncMock, return_value="created") as m:
        yield m


@pytest.fixture
def mock_upsert_order():
    with patch("celerp.connectors.upsert.upsert_order_from_shopify", new_callable=AsyncMock, return_value="created") as m:
        yield m


@pytest.fixture
def mock_upsert_contact_shopify():
    with patch("celerp.connectors.upsert.upsert_contact_from_shopify", new_callable=AsyncMock, return_value="created") as m:
        yield m


@pytest.fixture
def mock_upsert_invoice_qb():
    with patch("celerp.connectors.upsert.upsert_invoice_from_quickbooks", new_callable=AsyncMock, return_value="created") as m:
        yield m


@pytest.fixture
def mock_upsert_contact_qb():
    with patch("celerp.connectors.upsert.upsert_contact_from_quickbooks", new_callable=AsyncMock, return_value="created") as m:
        yield m


@pytest.fixture
def mock_upsert_invoice_xero():
    with patch("celerp.connectors.upsert.upsert_invoice_from_xero", new_callable=AsyncMock, return_value="created") as m:
        yield m


@pytest.fixture
def mock_upsert_contact_xero():
    with patch("celerp.connectors.upsert.upsert_contact_from_xero", new_callable=AsyncMock, return_value="created") as m:
        yield m


@pytest.fixture(autouse=True)
def _public_woocommerce_test_urls():
    with patch(
        "celerp.connectors.woocommerce.validate_public_base_url",
        new=AsyncMock(side_effect=lambda value, **_kwargs: value.rstrip("/")),
    ):
        yield
