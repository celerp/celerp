# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""
Connector base interface.

Each connector (Shopify, QuickBooks, Xero, ...) implements ConnectorBase.
Connectors do NOT hold OAuth tokens directly - tokens are brokered by the
CelERP relay service and injected at call time via ConnectorContext.

Connectors are INBOUND only: they pull products, orders, and contacts from the
external platform into Celerp. Celerp is the downstream system of record for the
imported data; nothing is pushed back out.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class SyncEntity(str, Enum):
    PRODUCTS = "products"
    ORDERS = "orders"
    CONTACTS = "contacts"


class ConnectorCategory(str, Enum):
    WEBSITE = "website"
    ACCOUNTING = "accounting"


class SyncFrequency(str, Enum):
    REALTIME = "realtime"   # webhook-driven (e-commerce only)
    MANUAL = "manual"       # user clicks Sync Now
    DAILY = "daily"         # once per day at configured hour


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
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


class ConnectorBase(ABC):
    """Abstract base for all platform connectors (inbound only)."""

    name: str
    display_name: str
    supported_entities: list[SyncEntity]
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

    async def sync_contacts(self, ctx: ConnectorContext, since: datetime | None = None) -> SyncResult:
        """Pull customers/vendors from platform -> Celerp CRM. Override if supported."""
        raise NotImplementedError(f"{self.name} does not support contact sync")

    # -- Webhook lifecycle (override for e-commerce connectors) ----------------

    async def register_webhooks(self, ctx: ConnectorContext, webhook_url: str) -> list[str]:
        """Register platform webhooks. Returns list of webhook IDs. Override if supported."""
        return []

    def validate_webhook(self, payload: bytes, signature: str, secret: str) -> bool:
        """Validate HMAC-SHA256 webhook signature. Works for Shopify and WooCommerce."""
        computed = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
        expected = base64.b64encode(computed).decode()
        return hmac.compare_digest(expected, signature)
