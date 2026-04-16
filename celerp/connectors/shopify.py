# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""
Shopify connector.

OAuth model: CelERP relay service holds one registered Shopify app.
Paying customers authorize via relay → relay returns a short-lived
access_token injected into ConnectorContext.

Self-hosters can bring their own Shopify app creds by setting:
  SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET
and running their own relay, or using direct token flow.

API version: 2024-01 (stable)
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from celerp.connectors.base import (
    ConnectorBase,
    ConnectorContext,
    SyncDirection,
    SyncEntity,
    SyncResult,
)

log = logging.getLogger(__name__)

_API_VERSION = "2024-01"
_PAGE_LIMIT = 250  # Shopify max per page


def _base_url(ctx: ConnectorContext) -> str:
    if not ctx.store_handle:
        raise ValueError("ConnectorContext.store_handle is required for Shopify")
    handle = ctx.store_handle.removesuffix(".myshopify.com")
    return f"https://{handle}.myshopify.com/admin/api/{_API_VERSION}"


def _headers(ctx: ConnectorContext) -> dict[str, str]:
    return {
        "X-Shopify-Access-Token": ctx.access_token,
        "Content-Type": "application/json",
    }


class ShopifyConnector(ConnectorBase):
    name = "shopify"
    display_name = "Shopify"
    supported_entities = [SyncEntity.PRODUCTS, SyncEntity.ORDERS, SyncEntity.CONTACTS]
    direction = SyncDirection.BIDIRECTIONAL

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _paginate(self, ctx: ConnectorContext, path: str, key: str) -> list[dict[str, Any]]:
        """Fetch all pages for a resource using cursor-based pagination."""
        results: list[dict[str, Any]] = []
        url = f"{_base_url(ctx)}{path}?limit={_PAGE_LIMIT}"
        async with httpx.AsyncClient(timeout=30) as client:
            while url:
                resp = await client.get(url, headers=_headers(ctx))
                resp.raise_for_status()
                data = resp.json()
                results.extend(data.get(key, []))
                # Shopify cursor pagination via Link header
                link = resp.headers.get("Link", "")
                url = _next_page_url(link)
        return results

    # ── Products ─────────────────────────────────────────────────────────────

    async def sync_products(self, ctx: ConnectorContext) -> SyncResult:
        """
        Pull Shopify products → Celerp items (one item per variant).

        Mapping:
          product.title + variant.title → item.name
          variant.sku                   → item.sku  (skipped if blank)
          variant.price                 → item.sale_price
          variant.inventory_quantity    → item.quantity (informational; not authoritative)
          product.id:variant.id         → idempotency_key
        """
        from celerp_inventory.routes import ItemCreate
        from celerp.db import get_session as _get_session
        # Import here to avoid circular at module load
        
        from sqlalchemy.ext.asyncio import AsyncSession

        result = SyncResult(entity=SyncEntity.PRODUCTS, direction=SyncDirection.INBOUND)
        errors: list[str] = []

        try:
            products = await self._paginate(ctx, "/products.json", "products")
        except (httpx.HTTPStatusError, ValueError) as exc:
            result.errors = [f"Shopify API error: {exc}"]
            return result

        for product in products:
            for variant in product.get("variants", []):
                sku = (variant.get("sku") or "").strip()
                if not sku:
                    result.skipped += 1
                    continue

                variant_title = variant.get("title", "")
                name = product["title"]
                if variant_title and variant_title.lower() != "default title":
                    name = f"{name} — {variant_title}"

                idempotency_key = f"shopify:{product['id']}:{variant['id']}"

                item = ItemCreate(
                    sku=sku,
                    name=name,
                    sell_by="piece",
                    sale_price=float(variant.get("price") or 0) or None,
                    quantity=float(variant.get("inventory_quantity") or 0),
                    idempotency_key=idempotency_key,
                )

                # Delegate to existing items router logic via direct service call
                try:
                    created = await _upsert_item(ctx.company_id, item)
                    if created:
                        result.created += 1
                    else:
                        result.skipped += 1
                except Exception as exc:
                    errors.append(f"SKU {sku}: {exc}")
                    result.errors = errors

        log.info(
            "shopify.sync_products company=%s created=%d skipped=%d errors=%d",
            ctx.company_id, result.created, result.skipped, len(errors),
        )
        return result

    # ── Orders ───────────────────────────────────────────────────────────────

    async def sync_orders(self, ctx: ConnectorContext) -> SyncResult:
        """
        Pull Shopify orders → Celerp documents (type=invoice, status=unpaid/paid).

        Mapping:
          order.name (#1001)          → doc.reference
          order.email / billing_address → contact lookup/create
          line_items                  → doc line items
          financial_status            → doc status (paid → closed, pending → open)
          order.id                    → idempotency_key
        """
        result = SyncResult(entity=SyncEntity.ORDERS, direction=SyncDirection.INBOUND)

        try:
            orders = await self._paginate(ctx, "/orders.json?status=any", "orders")
        except (httpx.HTTPStatusError, ValueError) as exc:
            result.errors = [f"Shopify API error: {exc}"]
            return result

        for order in orders:
            try:
                created = await _upsert_order(ctx.company_id, order)
                if created:
                    result.created += 1
                else:
                    result.skipped += 1
            except Exception as exc:
                msg = f"Order {order.get('name')}: {exc}"
                log.warning("shopify.sync_orders error: %s", msg)
                (result.errors or []).append(msg)
                result.errors = result.errors or [msg]

        log.info(
            "shopify.sync_orders company=%s created=%d skipped=%d",
            ctx.company_id, result.created, result.skipped,
        )
        return result

    # ── Contacts ─────────────────────────────────────────────────────────────

    async def sync_contacts(self, ctx: ConnectorContext) -> SyncResult:
        """Pull Shopify customers → Celerp CRM contacts."""
        result = SyncResult(entity=SyncEntity.CONTACTS, direction=SyncDirection.INBOUND)

        try:
            customers = await self._paginate(ctx, "/customers.json", "customers")
        except (httpx.HTTPStatusError, ValueError) as exc:
            result.errors = [f"Shopify API error: {exc}"]
            return result

        for customer in customers:
            try:
                created = await _upsert_contact(ctx.company_id, customer)
                if created:
                    result.created += 1
                else:
                    result.skipped += 1
            except Exception as exc:
                result.errors = (result.errors or []) + [f"Customer {customer.get('id')}: {exc}"]

        return result


# ── Pagination helper ─────────────────────────────────────────────────────────

def _next_page_url(link_header: str) -> str | None:
    """Parse Shopify Link header for next page URL."""
    for part in link_header.split(","):
        part = part.strip()
        if 'rel="next"' in part:
            url_part = part.split(";")[0].strip()
            return url_part.strip("<>")
    return None


# ── DB upsert helpers (thin wrappers — delegate to existing service layer) ────

async def _upsert_item(company_id: str, item) -> bool:
    """
    Insert item if idempotency_key not seen. Returns True if created.
    Delegates to the existing items service to keep logic DRY.
    """
    # Lazy import to avoid circular
    from celerp_inventory import services as items_svc
    return await items_svc.upsert_from_connector(company_id, item)


async def _upsert_order(company_id: str, order: dict) -> bool:
    from celerp.services import docs as docs_svc
    return await docs_svc.upsert_order_from_shopify(company_id, order)


async def _upsert_contact(company_id: str, customer: dict) -> bool:
    from celerp_sales_funnel import services as crm_svc
    return await crm_svc.upsert_contact_from_shopify(company_id, customer)
