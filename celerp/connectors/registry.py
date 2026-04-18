# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""
Connector registry — maps connector name → singleton instance.
Import connectors here; add new ones in one line.
"""
from __future__ import annotations

from celerp.connectors.base import ConnectorBase
from celerp.connectors.lazada import LazadaConnector
from celerp.connectors.quickbooks import QuickBooksConnector
from celerp.connectors.shopee import ShopeeConnector
from celerp.connectors.shopify import ShopifyConnector
from celerp.connectors.woocommerce import WooCommerceConnector
from celerp.connectors.xero import XeroConnector

_registry: dict[str, ConnectorBase] = {
    "lazada": LazadaConnector(),
    "quickbooks": QuickBooksConnector(),
    "shopee": ShopeeConnector(),
    "shopify": ShopifyConnector(),
    "woocommerce": WooCommerceConnector(),
    "xero": XeroConnector(),
}


def get(name: str) -> ConnectorBase:
    connector = _registry.get(name)
    if not connector:
        available = ", ".join(_registry)
        raise KeyError(f"Unknown connector '{name}'. Available: {available}")
    return connector


def all_connectors() -> list[ConnectorBase]:
    return list(_registry.values())
