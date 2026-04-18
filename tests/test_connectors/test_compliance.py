# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Parametrized compliance tests for all connectors."""
from __future__ import annotations

import os
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
from celerp.connectors.base import ConnectorCategory, SyncDirection, SyncEntity
from celerp.connectors.registry import all_connectors

# Only test the three release connectors (exclude marketplace)
_RELEASE_CONNECTORS = [c for c in all_connectors() if c.name in ("shopify", "quickbooks", "xero")]


@pytest.fixture(params=_RELEASE_CONNECTORS, ids=lambda c: c.name)
def connector(request):
    return request.param


def test_has_name(connector):
    assert connector.name and isinstance(connector.name, str)


def test_has_display_name(connector):
    assert connector.display_name and isinstance(connector.display_name, str)


def test_has_category(connector):
    assert isinstance(connector.category, ConnectorCategory)


def test_has_supported_entities(connector):
    assert len(connector.supported_entities) > 0
    for e in connector.supported_entities:
        assert isinstance(e, SyncEntity)


def test_has_direction(connector):
    assert isinstance(connector.direction, SyncDirection)


def test_has_conflict_strategy(connector):
    assert connector.conflict_strategy and isinstance(connector.conflict_strategy, dict)


def test_conflict_strategy_covers_entities(connector):
    """Every supported entity must have a conflict strategy."""
    for entity in connector.supported_entities:
        assert entity.value in connector.conflict_strategy or entity in connector.conflict_strategy, \
            f"{connector.name} missing conflict_strategy for {entity}"


def test_sync_methods_exist(connector):
    """All connectors must have sync_products and sync_orders."""
    assert hasattr(connector, "sync_products")
    assert hasattr(connector, "sync_orders")


def test_sync_methods_accept_since(connector):
    """All sync methods accept a since parameter."""
    import inspect
    for method_name in ("sync_products", "sync_orders", "sync_contacts"):
        method = getattr(connector, method_name, None)
        if method is None:
            continue
        sig = inspect.signature(method)
        assert "since" in sig.parameters, f"{connector.name}.{method_name} missing since parameter"
