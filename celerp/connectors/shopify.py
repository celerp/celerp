# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""
Shopify connector.

OAuth model: CelERP relay service holds one registered Shopify app.
Paying customers authorize via relay -> relay returns a short-lived
access_token injected into ConnectorContext.

Self-hosters can bring their own Shopify app creds by setting:
  SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET
and running their own relay, or using direct token flow.

API version: 2024-01 (stable)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import httpx

from celerp.connectors.http import RateLimitedClient
from celerp.connectors.util import money
from celerp.connectors.base import (
    ConnectorBase,
    ConnectorCategory,
    ConnectorContext,
    SyncDirection,
    SyncEntity,
    SyncResult,
)
import celerp.connectors.upsert as _upsert

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
    category = ConnectorCategory.WEBSITE
    direction = SyncDirection.BOTH
    conflict_strategy = {
        SyncEntity.PRODUCTS: "newest",
        SyncEntity.ORDERS: "platform",
        SyncEntity.CONTACTS: "merge",
    }

    # -- Internal helpers ------------------------------------------------------

    async def _paginate(self, ctx: ConnectorContext, path: str, key: str, params: dict | None = None) -> list[dict[str, Any]]:
        """Fetch all pages for a resource using cursor-based pagination."""
        results: list[dict[str, Any]] = []
        base_params: dict | None = {"limit": _PAGE_LIMIT, **(params or {})}
        url = f"{_base_url(ctx)}{path}"
        async with RateLimitedClient() as client:
            while url:
                resp = await client.get(url, headers=_headers(ctx), params=base_params)
                resp.raise_for_status()
                data = resp.json()
                results.extend(data.get(key, []))
                link = resp.headers.get("Link", "")
                url = _next_page_url(link)
                base_params = None  # subsequent pages use the full URL from Link header
        return results

    # -- Products --------------------------------------------------------------

    async def sync_products(self, ctx: ConnectorContext, since: datetime | None = None) -> SyncResult:
        """
        Pull Shopify products -> Celerp items (one item per variant).

        Mapping:
          product.title + variant.title -> item.name
          variant.sku                   -> item.sku  (skipped if blank)
          variant.price                 -> item.sale_price
          variant.inventory_quantity    -> item.quantity (informational; not authoritative)
          product.id:variant.id         -> idempotency_key
        """
        from celerp_inventory.routes import ItemCreate

        result = SyncResult(entity=SyncEntity.PRODUCTS)
        errors: list[str] = []

        try:
            products = await self._paginate(ctx, "/products.json", "products", params={"updated_at_min": since.isoformat()} if since else None)
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
                    name = f"{name} - {variant_title}"

                idempotency_key = f"shopify:{product['id']}:{variant['id']}"

                item = ItemCreate(
                    sku=sku,
                    name=name,
                    sell_by="piece",
                    sale_price=money(variant.get("price")),
                    quantity=float(variant.get("inventory_quantity") or 0),
                    idempotency_key=idempotency_key,
                )

                try:
                    result.record(await _upsert.upsert_item(ctx.company_id, item))
                except Exception as exc:
                    errors.append(f"SKU {sku}: {exc}")
                    continue
                # Pull product images after upsert (idempotent, best-effort)
                try:
                    await self._pull_product_images(ctx, product, sku)
                except Exception as img_exc:
                    log.warning("shopify image pull failed for SKU %s: %s", sku, img_exc)

        result.errors = errors or None
        log.info(
            "shopify.sync_products company=%s created=%d skipped=%d errors=%d",
            ctx.company_id, result.created, result.skipped, len(errors),
        )
        return result

    async def _pull_product_images(self, ctx: ConnectorContext, product: dict[str, Any], sku: str) -> None:
        """Pull images from a Shopify product and store as item files."""
        from celerp.db import get_session_ctx as get_async_session
        from celerp.models.projections import Projection
        from celerp.connectors.images import download_and_emit_file
        from sqlalchemy import select

        images: list[dict] = product.get("images", [])

        import uuid as _uuid

        from sqlalchemy import func

        async with get_async_session() as session:
            # Resolve the item by SKU with a single DB-side, case-insensitive
            # query that returns just the matching row.
            row = (await session.execute(
                select(Projection).where(
                    Projection.company_id == _uuid.UUID(str(ctx.company_id)),
                    Projection.entity_type == "item",
                    func.lower(Projection.state["sku"].as_string()) == sku.strip().lower(),
                ).limit(1)
            )).scalar_one_or_none()
            if row is None:
                return

            for img in sorted(images, key=lambda x: x.get("position", 99)):
                src = img.get("src")
                if not src:
                    continue
                fname = img.get("alt") or f"product-{img.get('id', '')}.jpg"
                await download_and_emit_file(
                    session, ctx.company_id, row.entity_id,
                    "system", src, fname, "product_images", is_hero=(img.get("position", 99) == 1)
                )
            await session.commit()

    # -- Orders ----------------------------------------------------------------

    async def sync_orders(self, ctx: ConnectorContext, since: datetime | None = None) -> SyncResult:
        """
        Pull Shopify orders -> Celerp documents (type=invoice, status=unpaid/paid).

        Mapping:
          order.name (#1001)          -> doc.reference
          order.email / billing_address -> contact lookup/create
          line_items                  -> doc line items
          financial_status            -> doc status (paid -> closed, pending -> open)
          order.id                    -> idempotency_key
        """
        result = SyncResult(entity=SyncEntity.ORDERS)
        errors: list[str] = []

        try:
            params: dict = {"status": "any"}
            if since:
                params["updated_at_min"] = since.isoformat()
            orders = await self._paginate(ctx, "/orders.json", "orders", params=params)
        except (httpx.HTTPStatusError, ValueError) as exc:
            result.errors = [f"Shopify API error: {exc}"]
            return result

        for order in orders:
            try:
                result.record(await _upsert.upsert_order_from_shopify(ctx.company_id, order))
            except Exception as exc:
                msg = f"Order {order.get('name')}: {exc}"
                log.warning("shopify.sync_orders error: %s", msg)
                errors.append(msg)

        result.errors = errors or None
        log.info(
            "shopify.sync_orders company=%s created=%d skipped=%d",
            ctx.company_id, result.created, result.skipped,
        )
        return result

    # -- Contacts --------------------------------------------------------------

    async def sync_contacts(self, ctx: ConnectorContext, since: datetime | None = None) -> SyncResult:
        """Pull Shopify customers -> Celerp CRM contacts."""
        result = SyncResult(entity=SyncEntity.CONTACTS)
        errors: list[str] = []

        try:
            customers = await self._paginate(ctx, "/customers.json", "customers", params={"updated_at_min": since.isoformat()} if since else None)
        except (httpx.HTTPStatusError, ValueError) as exc:
            result.errors = [f"Shopify API error: {exc}"]
            return result

        for customer in customers:
            try:
                result.record(await _upsert.upsert_contact_from_shopify(ctx.company_id, customer))
            except Exception as exc:
                errors.append(f"Customer {customer.get('id')}: {exc}")

        result.errors = errors or None
        return result

    # -- Outbound: Inventory push ----------------------------------------------

    async def sync_inventory_out(self, ctx: ConnectorContext) -> SyncResult:
        """Push Celerp stock levels -> Shopify inventory levels (outbound).

        Named *_out so it is classified as an outbound entity and actually gets
        dispatched by the scheduler; as `sync_inventory` it was mis-typed as the
        inbound 'inventory' entity and never ran (Shopify has no inbound inventory
        pull — stock arrives via product variants)."""
        result = SyncResult(entity=SyncEntity.INVENTORY, direction=SyncDirection.OUTBOUND)
        errors: list[str] = []

        try:
            items = await _upsert.list_items_with_external_id(ctx.company_id, platform="shopify")
        except Exception as exc:
            result.errors = [f"Failed to load inventory: {exc}"]
            return result

        async with RateLimitedClient() as client:
            for item in items:
                variant_id = item.get("shopify_variant_id")
                location_id = item.get("shopify_location_id")
                if not variant_id or not location_id:
                    result.skipped += 1
                    continue
                try:
                    resp = await client.post(
                        f"{_base_url(ctx)}/inventory_levels/set.json",
                        headers=_headers(ctx),
                        json={
                            "location_id": location_id,
                            "inventory_item_id": variant_id,
                            "available": int(item.get("quantity", 0)),
                        },
                    )
                    resp.raise_for_status()
                    result.updated += 1
                except Exception as exc:
                    errors.append(f"Item {item.get('sku')}: {exc}")

        result.errors = errors or None
        log.info(
            "shopify.sync_inventory_out company=%s updated=%d skipped=%d errors=%d",
            ctx.company_id, result.updated, result.skipped, len(errors),
        )
        return result

    # -- Outbound: Products push -----------------------------------------------

    async def sync_products_out(self, ctx: ConnectorContext) -> SyncResult:
        """Push Celerp item updates -> Shopify products (fields, images, certificates)."""
        from celerp.connectors.images import build_platform_image_payload, build_platform_cert_payload

        result = SyncResult(entity=SyncEntity.PRODUCTS, direction=SyncDirection.OUTBOUND)
        errors: list[str] = []

        try:
            items = await _upsert.list_items_modified_since_last_sync(ctx.company_id, platform="shopify")
        except Exception as exc:
            result.errors = [f"Failed to load items: {exc}"]
            return result

        async with RateLimitedClient() as client:
            for item in items:
                product_id = item.get("shopify_product_id")
                if not product_id:
                    result.skipped += 1
                    continue
                try:
                    payload: dict = {"product": {}}
                    if item.get("name"):
                        payload["product"]["title"] = item["name"]
                    if item.get("description"):
                        payload["product"]["body_html"] = item["description"]
                    if item.get("sale_price") is not None:
                        payload["product"]["variants"] = [{"price": str(item["sale_price"])}]

                    # Push images
                    files: list[dict] = item.get("files") or []
                    image_payload = build_platform_image_payload(files)
                    if image_payload["hero_url"]:
                        payload["product"]["images"] = [{"src": image_payload["hero_url"]}] + [
                            {"src": u} for u in image_payload["additional_urls"]
                        ]

                    resp = await client.put(
                        f"{_base_url(ctx)}/products/{product_id}.json",
                        headers=_headers(ctx),
                        json=payload,
                    )
                    resp.raise_for_status()

                    # Push certificate metafields (separate calls per metafield)
                    cert_payload = build_platform_cert_payload(files)
                    for tag_key, certs in cert_payload.items():
                        try:
                            mf_resp = await client.post(
                                f"{_base_url(ctx)}/products/{product_id}/metafields.json",
                                headers=_headers(ctx),
                                json={"metafield": {
                                    "namespace": "custom",
                                    "key": tag_key,
                                    "value": json.dumps(certs),
                                    "type": "json",
                                }},
                            )
                            mf_resp.raise_for_status()
                        except Exception as mf_exc:
                            log.warning("shopify: metafield push failed product=%s tag=%s: %s", product_id, tag_key, mf_exc)

                    result.updated += 1
                except Exception as exc:
                    errors.append(f"Product {product_id}: {exc}")

        result.errors = errors or None
        log.info(
            "shopify.sync_products_out company=%s updated=%d skipped=%d errors=%d",
            ctx.company_id, result.updated, result.skipped, len(errors),
        )
        return result


# -- Pagination helper ---------------------------------------------------------

def _next_page_url(link_header: str) -> str | None:
    """Parse Shopify Link header for next page URL."""
    for part in link_header.split(","):
        part = part.strip()
        if 'rel="next"' in part:
            url_part = part.split(";")[0].strip()
            return url_part.strip("<>")
    return None
