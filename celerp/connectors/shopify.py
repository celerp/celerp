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
    EntityFlow,
    FirstSync,
    RemoteOrder,
    RemoteProduct,
    SyncDirection,
    SyncEntity,
    SyncResult,
)
import celerp.connectors.upsert as _upsert
from ui.i18n import t
from celerp.modules.boundary import needs_modules

log = logging.getLogger(__name__)

_API_VERSION = "2024-01"
_PAGE_LIMIT = 250  # Shopify max per page


def _when(value) -> datetime | None:
    """A Shopify ISO timestamp as a datetime, or None."""
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


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
    reviewed_first_sync = True
    store_scoped_ids = False  # Shopify ids are unique across all stores
    imported_doc_marker = "shopify_order_id"
    entity_flows = {
        "products": EntityFlow(inbound=("products",), outbound=("products_out",)),
        "stock": EntityFlow(outbound=("inventory_out",)),
        "orders": EntityFlow(inbound=("orders",)),
        "customers": EntityFlow(inbound=("contacts",)),
    }
    category = ConnectorCategory.WEBSITE
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
        async with RateLimitedClient(require_execution=True) as client:
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

    @needs_modules("celerp-inventory")
    async def sync_products(
        self,
        ctx: ConnectorContext,
        since: datetime | None = None,
        attention: list[dict] | None = None,
        first_sync: FirstSync | None = None,
    ) -> SyncResult:
        """
        Pull Shopify products into Celerp catalog product anchors (one per variant).

        Mapping:
          product.title + variant.title -> item.name
          variant.sku                   -> item.sku  (a variant without one waits on a person)
          variant.price                 -> item.sale_price
          variant.inventory_quantity    -> item.quantity (seeded once, for tracked variants)
          product.id + variant.id       -> the item's Shopify link

        A first sync (``first_sync``) reads every product and follows its choices.
        """
        result = SyncResult(entity=SyncEntity.PRODUCTS)
        errors: list[str] = []
        pending: dict[str, dict] = {}
        processed: set[str] = set()
        if first_sync is not None:
            since = None

        try:
            products = await self._store_products(ctx, since)
        except (httpx.HTTPStatusError, ValueError) as exc:
            result.errors = [f"Shopify API error: {exc}"]
            return result

        for remote in products:
            processed.add(remote.key)
            entity_id = await _upsert.import_store_product(
                ctx, self, key=remote.key, label=remote.name, result=result, pending=pending,
                first_sync=first_sync, errors=errors,
                product_id=remote.product_id,
                variation_id=remote.variation_id,
                sku=remote.sku,
                name=remote.name,
                sale_price=remote.price,
                quantity=float(remote.extra["inventory_quantity"] or 0),
                seed_quantity=remote.extra["inventory_management"] == "shopify",
                sell_by="piece",
            )
            if entity_id is None:
                continue
            # Pull product images after upsert (idempotent, best-effort)
            try:
                await self._pull_product_images(ctx, remote.extra["product"], entity_id, first_sync)
            except Exception as img_exc:
                log.warning("shopify image pull failed for %s: %s", remote.key, img_exc)

        result.errors = errors or None
        result.attention = _upsert.carried_attention(attention, pending, processed)
        log.info(
            "shopify.sync_products company=%s created=%d skipped=%d errors=%d",
            ctx.company_id, result.created, result.skipped, len(errors),
        )
        return result

    async def _store_products(self, ctx: ConnectorContext, since: datetime | None) -> list[RemoteProduct]:
        """Every variant the store returns, each a sellable product of its own."""
        products = await self._paginate(
            ctx, "/products.json", "products",
            params={"updated_at_min": since.isoformat()} if since else None)
        out: list[RemoteProduct] = []
        for product in products:
            for variant in product.get("variants", []):
                variant_title = variant.get("title", "")
                name = product["title"]
                if variant_title and variant_title.lower() != "default title":
                    name = f"{name} - {variant_title}"
                out.append(RemoteProduct(
                    key=f"{product['id']}:{variant['id']}", product_id=str(product["id"]),
                    variation_id=str(variant["id"]), sku=(variant.get("sku") or "").strip(),
                    name=name, description=None, price=money(variant.get("price")),
                    modified=_when(variant.get("updated_at") or product.get("updated_at")),
                    stock=(float(variant.get("inventory_quantity") or 0)
                           if variant.get("inventory_management") == "shopify" else None),
                    extra={"inventory_quantity": variant.get("inventory_quantity"),
                           "inventory_management": variant.get("inventory_management"),
                           "product": product},
                ))
        return out

    async def preview_products(self, ctx: ConnectorContext) -> list[RemoteProduct]:
        return await self._store_products(ctx, None)

    async def preview_orders(self, ctx: ConnectorContext, orders_from: datetime) -> list[RemoteOrder]:
        orders = await self._paginate(ctx, "/orders.json", "orders", params={
            "status": "any", "created_at_min": orders_from.isoformat(),
            "fields": "id,name,financial_status,created_at,total_price,currency",
        })
        return [RemoteOrder(
            id=str(o["id"]), number=str(o.get("name") or o["id"]),
            status=str(o.get("financial_status") or ""), created=_when(o.get("created_at")),
            total=money(o.get("total_price")), currency=o.get("currency"), posts=False,
        ) for o in orders]

    async def _pull_product_images(
        self, ctx: ConnectorContext, product: dict[str, Any], entity_id: str,
        first_sync: FirstSync | None = None,
    ) -> None:
        """Pull images from a Shopify product and store them as the item's files. Those a
        first sync pulls join its import or the link's undo record, as on WooCommerce."""
        from celerp.db import get_session_ctx as get_async_session
        from celerp.connectors.images import download_and_emit_file

        images: list[dict] = product.get("images", [])
        if not images:
            return

        async with get_async_session() as session:
            for img in sorted(images, key=lambda x: x.get("position", 99)):
                src = img.get("src")
                if not src:
                    continue
                fname = img.get("alt") or f"product-{img.get('id', '')}.jpg"
                await download_and_emit_file(
                    session, ctx.company_id, entity_id, src, fname, "product_images",
                    is_hero=(img.get("position", 99) == 1),
                    platform=self.name, store=ctx.store_handle, remote_id=img.get("id"),
                    operation=first_sync and first_sync.operation,
                )
            await session.commit()
        if first_sync is not None:
            await first_sync.record.attribute_files(entity_id)

    # -- Orders ----------------------------------------------------------------

    @needs_modules("celerp-docs")
    async def sync_orders(
        self, ctx: ConnectorContext, since: datetime | None = None,
        orders_from: datetime | None = None, first_sync: FirstSync | None = None,
    ) -> SyncResult:
        """
        Pull Shopify orders -> Celerp documents (type=invoice, status=unpaid/paid).
        Orders placed before ``orders_from``, the connection's earliest order date,
        are not fetched.

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
            if orders_from:
                params["created_at_min"] = orders_from.isoformat()
            orders = await self._paginate(ctx, "/orders.json", "orders", params=params)
        except (httpx.HTTPStatusError, ValueError) as exc:
            result.errors = [f"Shopify API error: {exc}"]
            return result

        for order in orders:
            try:
                result.record(await _upsert.upsert_order_from_shopify(ctx.company_id, order, first_sync))
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

    @needs_modules("celerp-contacts")
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

    @needs_modules("celerp-inventory")
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

        async with RateLimitedClient(require_execution=True) as client:
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

    @needs_modules("celerp-inventory")
    async def sync_products_out(self, ctx: ConnectorContext) -> SyncResult:
        """Push Celerp item updates -> Shopify products (fields and images). An image
        goes by Shopify's own id when Shopify holds it, by a short-lived public link
        otherwise; when one cannot be shared the images are left as they are and the
        product waits on a person."""
        from celerp.connectors.images import store_images

        result = SyncResult(entity=SyncEntity.PRODUCTS, direction=SyncDirection.OUTBOUND)
        errors: list[str] = []
        attention: list[dict] = []

        try:
            items = await _upsert.list_items_modified_since_last_sync(ctx.company_id, platform="shopify")
        except Exception as exc:
            result.errors = [f"Failed to load items: {exc}"]
            return result

        async with RateLimitedClient(require_execution=True) as client:
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

                    images, unshared = await store_images(
                        ctx, self.name, item.get("files") or [], item.get("external_link") or {})
                    if unshared:
                        attention.append({"id": str(product_id), "label": item.get("name") or item.get("sku"),
                                          "kind": "image", "reason": t("connectors.attention_image_unshared",
                                                                       service=self.display_name)})
                    elif images:
                        payload["product"]["images"] = images

                    resp = await client.put(
                        f"{_base_url(ctx)}/products/{product_id}.json",
                        headers=_headers(ctx),
                        json=payload,
                    )
                    resp.raise_for_status()
                    result.updated += 1
                except Exception as exc:
                    errors.append(f"Product {product_id}: {exc}")

        result.errors = errors or None
        result.attention = attention
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
