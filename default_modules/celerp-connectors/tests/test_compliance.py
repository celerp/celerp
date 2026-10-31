# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Parametrized compliance tests for all connectors."""
from __future__ import annotations

import os
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
from celerp.connectors.base import ConnectorCategory, SyncEntity
from celerp.connectors.registry import all_connectors

_RELEASE_CONNECTORS = [
    c for c in all_connectors() if c.name in ("shopify", "quickbooks", "xero", "woocommerce")
]


@pytest.fixture(params=_RELEASE_CONNECTORS, ids=lambda c: c.name)
def connector(request):
    return request.param


def test_has_name(connector):
    assert connector.name and isinstance(connector.name, str)


def test_has_display_name(connector):
    assert connector.display_name and isinstance(connector.display_name, str)


def test_has_category(connector):
    assert isinstance(connector.category, ConnectorCategory)


def test_has_entity_flows(connector):
    """Each kind of record names syncs the connector implements, and a new connection
    starts with a direction each record can take."""
    from celerp.connectors.sync_runner import implements

    assert connector.entity_flows
    for key, flow in connector.entity_flows.items():
        for entity in (*flow.inbound, *flow.outbound):
            assert implements(connector, entity), (connector.name, key, entity)
        assert connector.default_directions[key] in {d.value for d in flow.allowed()}


def test_has_conflict_strategy(connector):
    assert connector.conflict_strategy and isinstance(connector.conflict_strategy, dict)


def test_conflict_strategy_covers_entities(connector):
    """Every entity pulled in must have a conflict strategy."""
    for flow in connector.entity_flows.values():
        for entity in flow.inbound:
            assert entity in connector.conflict_strategy or SyncEntity(entity) in connector.conflict_strategy, \
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
