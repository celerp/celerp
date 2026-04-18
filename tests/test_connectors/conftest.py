# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Shared fixtures for connector tests."""
from __future__ import annotations

import os
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
from unittest.mock import AsyncMock, patch

from celerp.connectors.base import ConnectorContext


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
        store_handle="realm-123",
    )


@pytest.fixture
def ctx_xero():
    return ConnectorContext(
        company_id="test-co",
        access_token="xero_test_token",
        store_handle="tenant-abc",
        extra={"tenant_id": "tenant-abc"},
    )


@pytest.fixture
def mock_upsert_item():
    with patch("celerp.connectors.upsert.upsert_item", new_callable=AsyncMock, return_value=True) as m:
        yield m


@pytest.fixture
def mock_upsert_order():
    with patch("celerp.connectors.upsert.upsert_order_from_shopify", new_callable=AsyncMock, return_value=True) as m:
        yield m


@pytest.fixture
def mock_upsert_contact_shopify():
    with patch("celerp.connectors.upsert.upsert_contact_from_shopify", new_callable=AsyncMock, return_value=True) as m:
        yield m


@pytest.fixture
def mock_upsert_invoice_qb():
    with patch("celerp.connectors.upsert.upsert_invoice_from_quickbooks", new_callable=AsyncMock, return_value=True) as m:
        yield m


@pytest.fixture
def mock_upsert_contact_qb():
    with patch("celerp.connectors.upsert.upsert_contact_from_quickbooks", new_callable=AsyncMock, return_value=True) as m:
        yield m


@pytest.fixture
def mock_upsert_invoice_xero():
    with patch("celerp.connectors.upsert.upsert_invoice_from_xero", new_callable=AsyncMock, return_value=True) as m:
        yield m


@pytest.fixture
def mock_upsert_contact_xero():
    with patch("celerp.connectors.upsert.upsert_contact_from_xero", new_callable=AsyncMock, return_value=True) as m:
        yield m
