# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""
WooCommerce connector.

Auth model: REST API keys (consumer_key + consumer_secret) stored as
`consumer_key:consumer_secret` in ConnectorContext.access_token.
No OAuth relay needed - credentials are issued directly in WooCommerce admin.

API: WooCommerce REST API v3 (/wp-json/wc/v3/)
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

import json

from celerp.connectors.http import RateLimitedClient
from celerp.connectors.base import (
    ConnectorBase,
    ConnectorCategory,
    ConnectorContext,
    SyncDirection,
    SyncEntity,
    SyncFrequency,
    SyncResult,
)
import celerp.connectors.upsert as _upsert

log = logging.getLogger(__name__)

_PER_PAGE = 100  # WooCommerce max per page


def _base_url(ctx: ConnectorContext) -> str:
    if not ctx.store_handle:
        raise ValueError("ConnectorContext.store_handle is required for WooCommerce")
    store_url = ctx.store_handle.rstrip("/")
    return f"{store_url}/wp-json/wc/v3"


def _auth(ctx: ConnectorContext) -> tuple[str, str]:
    """Return (consumer_key, consumer_secret) Basic Auth tuple."""
    if not ctx.access_token or ":" not in ctx.access_token:
        raise ValueError("ConnectorContext.access_token must be 'consumer_key:consumer_secret'")
    key, secret = ctx.access_token.split(":", 1)
    return (key, secret)


class WooCommerceConnector(ConnectorBase):
    name = "woocommerce"
    display_name = "WooCommerce"
    category = ConnectorCategory.WEBSITE
    direction = SyncDirection.BOTH
    supported_entities = [SyncEntity.PRODUCTS, SyncEntity.ORDERS, SyncEntity.CONTACTS, SyncEntity.INVENTORY]
    conflict_strategy = {
        "products": "external_wins",
        "orders": "external_wins",
        "contacts": "external_wins",
    }

    # -- Internal helpers ------------------------------------------------------

    async def _paginate(
        self,
        ctx: ConnectorContext,
        path: str,
        params: dict | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all pages using WooCommerce page-based pagination."""
        results: list[dict[str, Any]] = []
        base_url = _base_url(ctx)
        auth = _auth(ctx)
        page = 1

        async with RateLimitedClient() as client:
            while True:
                page_params = {"per_page": _PER_PAGE, "page": page, **(params or {})}
                resp = await client.get(
                    f"{base_url}{path}",
                    auth=auth,
                    params=page_params,
                )
                resp.raise_for_status()
                data = resp.json()
                results.extend(data)
                total_pages = int(resp.headers.get("X-WP-TotalPages", 1))
                if page >= total_pages:
                    break
                page += 1

        return results

    # -- Products --------------------------------------------------------------

    async def sync_products(self, ctx: ConnectorContext, since: datetime | None = None) -> SyncResult:
        """
        Pull WooCommerce products -> Celerp items.

        Mapping:
          product.id                -> external_id / idempotency key
          product.sku or WC-{id}   -> item.sku
          product.name             -> item.name
          product.regular_price    -> item.sell_price
          product.description      -> item.description
        """
        from celerp_inventory.routes import ItemCreate

        result = SyncResult(entity=SyncEntity.PRODUCTS, direction=SyncDirection.INBOUND)
        errors: list[str] = []

        params: dict = {}
        if since:
            params["modified_after"] = since.isoformat()

        try:
            products = await self._paginate(ctx, "/products", params=params or None)
        except (httpx.HTTPStatusError, ValueError) as exc:
            result.errors = [f"WooCommerce API error: {exc}"]
            return result

        for product in products:
            pid = product.get("id")
            sku = (product.get("sku") or "").strip() or f"WC-{pid}"
            name = product.get("name") or sku
            sell_price_raw = product.get("regular_price") or product.get("price") or "0"
            try:
                sell_price = float(sell_price_raw) or None
            except (ValueError, TypeError):
                sell_price = None

            idempotency_key = f"woocommerce:{pid}"

            item = ItemCreate(
                sku=sku,
                name=name,
                sell_by="piece",
                sale_price=sell_price,
                idempotency_key=idempotency_key,
            )

            try:
                created = await _upsert.upsert_item(ctx.company_id, item)
                if created:
                    result.created += 1
                else:
                    result.skipped += 1
            except Exception as exc:
                errors.append(f"SKU {sku}: {exc}")
                continue
            # Pull images and certificates after upsert (idempotent, best-effort)
            try:
                await self._pull_product_files(ctx, product, sku)
            except Exception as img_exc:
                log.warning("woocommerce file pull failed for SKU %s: %s", sku, img_exc)

        result.errors = errors or None
        log.info(
            "woocommerce.sync_products company=%s created=%d skipped=%d errors=%d",
            ctx.company_id, result.created, result.skipped, len(errors),
        )
        return result

    async def _pull_product_files(self, ctx: ConnectorContext, product: dict[str, Any], sku: str) -> None:
        """Pull images and cert metafields from a WC product and store as item files."""
        from celerp.db import get_session_ctx as get_async_session
        from celerp.models.projections import Projection
        from celerp.connectors.images import download_and_emit_file, _CERT_TAGS
        from sqlalchemy import select

        images: list[dict] = product.get("images", [])
        meta_data: list[dict] = product.get("meta_data", [])

        async with get_async_session() as session:
            rows = (await session.execute(
                select(Projection).where(
                    Projection.company_id == ctx.company_id,
                    Projection.entity_type == "item",
                )
            )).scalars().all()
            row = next((r for r in rows if str(r.state.get("sku", "")).strip().lower() == sku.strip().lower()), None)
            if row is None:
                return

            for i, img in enumerate(images):
                src = img.get("src")
                if not src:
                    continue
                fname = img.get("name") or f"product-{i}.jpg"
                await download_and_emit_file(
                    session, ctx.company_id, row.entity_id,
                    "system", src, fname, "product_images", is_hero=(i == 0)
                )

            # Certificate metafields
            meta = {m["key"]: m["value"] for m in meta_data}
            for tag_key in _CERT_TAGS:
                raw = meta.get(tag_key)
                if not raw:
                    continue
                try:
                    certs = json.loads(raw) if isinstance(raw, str) else raw
                    for cert in (certs if isinstance(certs, list) else []):
                        cert_url = cert.get("url")
                        cert_name = cert.get("name", "cert.pdf")
                        if cert_url:
                            await download_and_emit_file(
                                session, ctx.company_id, row.entity_id,
                                "system", cert_url, cert_name, tag_key, is_hero=False
                            )
                except Exception as exc:
                    log.warning("woocommerce: failed to pull cert metafield %s: %s", tag_key, exc)

            await session.commit()

    async def sync_products_out(self, ctx: ConnectorContext) -> SyncResult:
        """Push Celerp item updates (images + certs) -> WooCommerce products."""
        from celerp.connectors.images import build_platform_image_payload, build_platform_cert_payload

        result = SyncResult(entity=SyncEntity.PRODUCTS, direction=SyncDirection.OUTBOUND)
        errors: list[str] = []

        try:
            items = await _upsert.list_items_with_external_id(ctx.company_id, platform="woocommerce")
        except Exception as exc:
            result.errors = [f"Failed to load items: {exc}"]
            return result

        base_url = _base_url(ctx)
        auth = _auth(ctx)

        async with RateLimitedClient() as client:
            for item in items:
                wc_id = item.get("woocommerce_product_id")
                if not wc_id:
                    result.skipped += 1
                    continue
                try:
                    files: list[dict] = item.get("files") or []
                    image_payload = build_platform_image_payload(files)
                    cert_payload = build_platform_cert_payload(files)

                    product_patch: dict = {}
                    if image_payload["hero_url"]:
                        product_patch["images"] = [{"src": image_payload["hero_url"]}] + [
                            {"src": u} for u in image_payload["additional_urls"]
                        ]
                    if cert_payload:
                        product_patch["meta_data"] = [
                            {"key": tag_key, "value": json.dumps(certs)}
                            for tag_key, certs in cert_payload.items()
                        ]
                    if not product_patch:
                        result.skipped += 1
                        continue

                    resp = await client.put(
                        f"{base_url}/products/{wc_id}", auth=auth, json=product_patch
                    )
                    resp.raise_for_status()
                    result.updated += 1
                except Exception as exc:
                    errors.append(f"WC product {wc_id}: {exc}")

        result.errors = errors or None
        log.info(
            "woocommerce.sync_products_out company=%s updated=%d skipped=%d errors=%d",
            ctx.company_id, result.updated, result.skipped, len(errors),
        )
        return result

    # -- Orders ----------------------------------------------------------------

    async def sync_orders(self, ctx: ConnectorContext, since: datetime | None = None) -> SyncResult:
        """Pull WooCommerce orders -> Celerp documents."""
        result = SyncResult(entity=SyncEntity.ORDERS, direction=SyncDirection.INBOUND)
        errors: list[str] = []

        params: dict = {}
        if since:
            params["modified_after"] = since.isoformat()

        try:
            orders = await self._paginate(ctx, "/orders", params=params or None)
        except (httpx.HTTPStatusError, ValueError) as exc:
            result.errors = [f"WooCommerce API error: {exc}"]
            return result

        for order in orders:
            try:
                created = await _upsert.upsert_order_from_woocommerce(ctx.company_id, order)
                if created:
                    result.created += 1
                else:
                    result.skipped += 1
            except Exception as exc:
                msg = f"Order {order.get('id')}: {exc}"
                log.warning("woocommerce.sync_orders error: %s", msg)
                errors.append(msg)

        result.errors = errors or None
        log.info(
            "woocommerce.sync_orders company=%s created=%d skipped=%d",
            ctx.company_id, result.created, result.skipped,
        )
        return result

    # -- Contacts --------------------------------------------------------------

    async def sync_contacts(self, ctx: ConnectorContext, since: datetime | None = None) -> SyncResult:
        """Pull WooCommerce customers -> Celerp contacts."""
        result = SyncResult(entity=SyncEntity.CONTACTS, direction=SyncDirection.INBOUND)
        errors: list[str] = []

        params: dict = {}
        if since:
            params["modified_after"] = since.isoformat()

        try:
            customers = await self._paginate(ctx, "/customers", params=params or None)
        except (httpx.HTTPStatusError, ValueError) as exc:
            result.errors = [f"WooCommerce API error: {exc}"]
            return result

        for customer in customers:
            try:
                created = await _upsert.upsert_contact_from_woocommerce(ctx.company_id, customer)
                if created:
                    result.created += 1
                else:
                    result.skipped += 1
            except Exception as exc:
                errors.append(f"Customer {customer.get('id')}: {exc}")

        result.errors = errors or None
        log.info(
            "woocommerce.sync_contacts company=%s created=%d skipped=%d",
            ctx.company_id, result.created, result.skipped,
        )
        return result

    # -- Outbound: Inventory push ----------------------------------------------

    async def sync_inventory_out(self, ctx: ConnectorContext) -> SyncResult:
        """Push Celerp stock levels -> WooCommerce product stock_quantity."""
        result = SyncResult(entity=SyncEntity.INVENTORY, direction=SyncDirection.OUTBOUND)
        errors: list[str] = []

        try:
            items = await _upsert.list_items_with_external_id(ctx.company_id, platform="woocommerce")
        except Exception as exc:
            result.errors = [f"Failed to load inventory: {exc}"]
            return result

        base_url = _base_url(ctx)
        auth = _auth(ctx)

        async with RateLimitedClient() as client:
            for item in items:
                product_id = item.get("woocommerce_product_id")
                if not product_id:
                    result.skipped += 1
                    continue
                try:
                    resp = await client.put(
                        f"{base_url}/products/{product_id}",
                        auth=auth,
                        json={
                            "stock_quantity": int(item.get("quantity", 0)),
                            "manage_stock": True,
                        },
                    )
                    resp.raise_for_status()
                    result.updated += 1
                except Exception as exc:
                    errors.append(f"Product {product_id}: {exc}")

        result.errors = errors or None
        log.info(
            "woocommerce.sync_inventory_out company=%s updated=%d skipped=%d errors=%d",
            ctx.company_id, result.updated, result.skipped, len(errors),
        )
        return result

    # -- Webhook lifecycle -----------------------------------------------------

    _INBOUND_TOPICS = [
        "product.created", "product.updated", "product.deleted",
        "order.created", "order.updated",
        "customer.created", "customer.updated",
    ]

    def webhook_topics_for_direction(self, direction: SyncDirection) -> list[str]:
        if direction == SyncDirection.OUTBOUND:
            return []
        return self._INBOUND_TOPICS

    async def register_webhooks(self, ctx: ConnectorContext, webhook_url: str) -> list[str]:
        """Register WooCommerce webhooks via REST API."""
        base_url = _base_url(ctx)
        auth = _auth(ctx)
        ids: list[str] = []
        topics = self.webhook_topics_for_direction(self.direction)
        async with RateLimitedClient() as client:
            for topic in topics:
                resp = await client.post(
                    f"{base_url}/webhooks",
                    auth=auth,
                    json={
                        "name": f"CelERP {topic}",
                        "topic": topic,
                        "delivery_url": webhook_url,
                        "status": "active",
                    },
                )
                if resp.status_code in (200, 201):
                    wh = resp.json()
                    ids.append(str(wh.get("id", "")))
                    # WC generates its own secret, stored in the response
                else:
                    log.warning("woocommerce.register_webhook topic=%s status=%d", topic, resp.status_code)
        return ids

    async def deregister_webhooks(self, ctx: ConnectorContext, webhook_ids: list[str]) -> None:
        base_url = _base_url(ctx)
        auth = _auth(ctx)
        async with RateLimitedClient() as client:
            for wid in webhook_ids:
                resp = await client.delete(f"{base_url}/webhooks/{wid}", auth=auth, params={"force": "true"})
                if resp.status_code not in (200, 204):
                    log.warning("woocommerce.deregister_webhook id=%s status=%d", wid, resp.status_code)
