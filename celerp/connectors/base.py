# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""
Connector base interface.

Each connector (Shopify, QuickBooks, Xero, …) implements ConnectorBase.
Connectors do NOT hold OAuth tokens directly — tokens are brokered by the
CelERP relay service and injected at call time via ConnectorContext.

Sync direction:
  - INBOUND: external platform → Celerp (pull products, orders, contacts)
  - OUTBOUND: Celerp → external platform (push invoices, inventory updates)
  - BIDIRECTIONAL: both
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class SyncDirection(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    BIDIRECTIONAL = "bidirectional"


class SyncEntity(str, Enum):
    PRODUCTS = "products"
    ORDERS = "orders"
    CONTACTS = "contacts"
    INVENTORY = "inventory"
    INVOICES = "invoices"


class ConnectorCategory(str, Enum):
    WEBSITE = "website"
    ACCOUNTING = "accounting"


@dataclass
class ConnectorContext:
    """Runtime context injected per sync call. Never stored on the connector."""
    company_id: str
    access_token: str          # short-lived token from relay service
    store_handle: str | None = None   # e.g. Shopify myshopify domain
    extra: dict[str, Any] | None = None


@dataclass
class SyncResult:
    entity: SyncEntity
    direction: SyncDirection
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


class ConnectorBase(ABC):
    """Abstract base for all platform connectors."""

    name: str
    display_name: str
    supported_entities: list[SyncEntity]
    direction: SyncDirection
    category: ConnectorCategory
    conflict_strategy: dict[str, str]

    @abstractmethod
    async def sync_products(self, ctx: ConnectorContext, since: datetime | None = None) -> SyncResult:
        """Pull products/variants from platform -> Celerp items."""
        ...

    @abstractmethod
    async def sync_orders(self, ctx: ConnectorContext, since: datetime | None = None) -> SyncResult:
        """Pull orders from platform -> Celerp documents."""
        ...

    async def sync_inventory(self, ctx: ConnectorContext, since: datetime | None = None) -> SyncResult:
        """Push Celerp inventory levels -> platform. Override if supported."""
        raise NotImplementedError(f"{self.name} does not support inventory push")

    async def sync_contacts(self, ctx: ConnectorContext, since: datetime | None = None) -> SyncResult:
        """Pull customers/vendors from platform -> Celerp CRM. Override if supported."""
        raise NotImplementedError(f"{self.name} does not support contact sync")

    async def sync_products_out(self, ctx: ConnectorContext) -> SyncResult:
        """Push Celerp items -> platform. Override if supported."""
        raise NotImplementedError(f"{self.name} does not support outbound product sync")

    async def sync_invoices_out(self, ctx: ConnectorContext) -> SyncResult:
        """Push Celerp invoices -> platform. Override if supported."""
        raise NotImplementedError(f"{self.name} does not support outbound invoice sync")
