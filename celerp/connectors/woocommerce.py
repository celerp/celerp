# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
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
from celerp.connectors.util import money
from celerp.connectors.base import (
    ConnectorBase,
    ConnectorCategory,
    ConnectorContext,
    SyncEntity,
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
    supported_entities = [SyncEntity.PRODUCTS, SyncEntity.ORDERS, SyncEntity.CONTACTS]
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
                if not data:
                    break
                total_pages_hdr = resp.headers.get("X-WP-TotalPages")
                if total_pages_hdr is not None:
                    if page >= int(total_pages_hdr):
                        break
                elif len(data) < _PER_PAGE:
                    # No total-pages header (proxy stripped it / error envelope):
                    # keep paging until a short page rather than truncating at 1.
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

        result = SyncResult(entity=SyncEntity.PRODUCTS)
        errors: list[str] = []

        params: dict = {}
        if since:
            params["modified_after"] = since.isoformat()
            params["dates_are_gmt"] = "true"  # our watermark is UTC; make Woo interpret it as UTC

        try:
            products = await self._paginate(ctx, "/products", params=params or None)
        except (httpx.HTTPStatusError, ValueError) as exc:
            result.errors = [f"WooCommerce API error: {exc}"]
            return result

        async def _upsert_item(sku: str, name: str, price, idem: str) -> bool:
            item = ItemCreate(sku=sku, name=name, sell_by="piece", sale_price=price, idempotency_key=idem)
            try:
                created = await _upsert.upsert_item(ctx.company_id, item)
                if created:
                    result.created += 1
                else:
                    result.skipped += 1
                return True
            except Exception as exc:
                errors.append(f"SKU {sku}: {exc}")
                return False

        for product in products:
            pid = product.get("id")
            name = product.get("name") or f"WC-{pid}"

            # Variable products are containers; import each variation as its own sellable
            # item (its own SKU / price), not the price-less parent.
            if product.get("type") == "variable":
                try:
                    variations = await self._paginate(ctx, f"/products/{pid}/variations")
                except (httpx.HTTPStatusError, ValueError) as exc:
                    errors.append(f"Product {pid} variations: {exc}")
                    continue
                for var in variations:
                    vid = var.get("id")
                    var_sku = (var.get("sku") or "").strip() or f"WC-{pid}-{vid}"
                    opts = " / ".join(
                        str(a.get("option", "")) for a in (var.get("attributes") or []) if a.get("option")
                    )
                    var_price = money(var.get("price"))
                    if var_price is None:
                        var_price = money(var.get("regular_price"))
                    await _upsert_item(var_sku, f"{name} - {opts}" if opts else name,
                                       var_price, f"woocommerce:{pid}:{vid}")
                continue

            # Simple product: regular_price is the base; fall back to the active price only
            # when regular_price is genuinely absent (a real 0 = free item is kept).
            sku = (product.get("sku") or "").strip() or f"WC-{pid}"
            sell_price = money(product.get("regular_price"))
            if sell_price is None:
                sell_price = money(product.get("price"))
            if await _upsert_item(sku, name, sell_price, f"woocommerce:{pid}"):
                # Pull images/certs after upsert (idempotent, best-effort)
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
        import uuid as _uuid
        from celerp.db import get_session_ctx as get_async_session
        from celerp.models.projections import Projection
        from celerp.connectors.images import download_and_emit_file, _CERT_TAGS
        from sqlalchemy import func, select

        images: list[dict] = product.get("images", [])
        meta_data: list[dict] = product.get("meta_data", [])

        async with get_async_session() as session:
            # Resolve the item by SKU with a single DB-side, case-insensitive
            # query, limited to the one matching row.
            row = (await session.execute(
                select(Projection).where(
                    Projection.company_id == _uuid.UUID(str(ctx.company_id)),
                    Projection.entity_type == "item",
                    func.lower(Projection.state["sku"].as_string()) == sku.strip().lower(),
                ).limit(1)
            )).scalar_one_or_none()
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

    # -- Orders ----------------------------------------------------------------

    async def sync_orders(self, ctx: ConnectorContext, since: datetime | None = None) -> SyncResult:
        """Pull WooCommerce orders -> Celerp documents."""
        result = SyncResult(entity=SyncEntity.ORDERS)
        errors: list[str] = []

        params: dict = {}
        if since:
            params["modified_after"] = since.isoformat()
            params["dates_are_gmt"] = "true"  # our watermark is UTC; make Woo interpret it as UTC

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
        result = SyncResult(entity=SyncEntity.CONTACTS)
        errors: list[str] = []

        params: dict = {}
        if since:
            params["modified_after"] = since.isoformat()
            params["dates_are_gmt"] = "true"  # our watermark is UTC; make Woo interpret it as UTC

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

    # -- Webhook lifecycle -----------------------------------------------------

    _WEBHOOK_TOPICS = [
        "product.created", "product.updated", "product.deleted",
        "order.created", "order.updated",
        "customer.created", "customer.updated",
    ]

    async def register_webhooks(
        self, ctx: ConnectorContext, webhook_url: str, secret: str | None = None
    ) -> list[str]:
        """Register WooCommerce webhooks via REST API.

        When `secret` is supplied it is set on every webhook so deliveries are
        signed with a value we already hold (X-WC-Webhook-Signature = base64
        HMAC-SHA256 of the body), letting the receiver verify them. Returns the
        created webhook ids."""
        base_url = _base_url(ctx)
        auth = _auth(ctx)
        ids: list[str] = []
        async with RateLimitedClient() as client:
            for topic in self._WEBHOOK_TOPICS:
                body = {
                    "name": f"CelERP {topic}",
                    "topic": topic,
                    "delivery_url": webhook_url,
                    "status": "active",
                }
                if secret:
                    body["secret"] = secret
                resp = await client.post(f"{base_url}/webhooks", auth=auth, json=body)
                if resp.status_code in (200, 201):
                    ids.append(str(resp.json().get("id", "")))
                else:
                    log.warning("woocommerce.register_webhook topic=%s status=%d", topic, resp.status_code)
        return ids
