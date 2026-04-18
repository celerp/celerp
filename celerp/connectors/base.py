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
  - BOTH: bidirectional
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
    BOTH = "both"


class SyncEntity(str, Enum):
    PRODUCTS = "products"
    ORDERS = "orders"
    CONTACTS = "contacts"
    INVENTORY = "inventory"
    INVOICES = "invoices"


class ConnectorCategory(str, Enum):
    WEBSITE = "website"
    ACCOUNTING = "accounting"


class SyncFrequency(str, Enum):
    REALTIME = "realtime"   # webhook-driven (e-commerce only)
    MANUAL = "manual"       # user clicks Sync Now
    DAILY = "daily"         # once per day at configured hour


# Entity classification for direction filtering
_INBOUND_ENTITIES = {"products", "orders", "contacts", "inventory"}
_OUTBOUND_ENTITIES = {"products_out", "invoices_out", "inventory_out"}


def entity_allowed(entity: str, direction: SyncDirection) -> bool:
    """Check whether an entity sync is allowed given the configured direction."""
    if direction == SyncDirection.BOTH:
        return True
    if direction == SyncDirection.INBOUND:
        return entity in _INBOUND_ENTITIES
    if direction == SyncDirection.OUTBOUND:
        return entity in _OUTBOUND_ENTITIES
    return False


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

    async def sync_inventory_out(self, ctx: ConnectorContext) -> SyncResult:
        """Push Celerp inventory -> platform. Override if supported."""
        raise NotImplementedError(f"{self.name} does not support outbound inventory sync")

    # -- Webhook lifecycle (override for e-commerce connectors) ----------------

    async def register_webhooks(self, ctx: ConnectorContext, webhook_url: str) -> list[str]:
        """Register platform webhooks. Returns list of webhook IDs. Override if supported."""
        return []

    async def deregister_webhooks(self, ctx: ConnectorContext, webhook_ids: list[str]) -> None:
        """Remove registered webhooks. Override if supported."""
        pass

    def webhook_topics_for_direction(self, direction: SyncDirection) -> list[str]:
        """Return webhook topics to register for the given direction. Override if supported."""
        return []

    def validate_webhook(self, payload: bytes, signature: str, secret: str) -> bool:
        """Validate HMAC signature of an incoming webhook. Override per platform."""
        return False
