# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""
Lazada connector.

Auth model: Lazada Open Platform uses HMAC-SHA256 request signing.
Each request is sorted by param name, concatenated, and signed with app_secret.

ConnectorContext fields:
  access_token  - Lazada OAuth access_token (obtained via relay OAuth flow)
  store_handle  - Lazada region code: "sg" | "my" | "th" | "ph" | "id" | "vn"
  extra = {
    "app_key": str,     # Lazada app_key (numeric string)
    "app_secret": str,  # Lazada app_secret (HMAC signing key)
  }

API: Lazada Open Platform REST API
Base URLs per region:
  sg → https://api.lazada.sg/rest
  my → https://api.lazada.com.my/rest
  th → https://api.lazada.co.th/rest
  ph → https://api.lazada.com.ph/rest
  id → https://api.lazada.co.id/rest
  vn → https://api.lazada.vn/rest
  (fallback to .sg)

Sync scope (inbound only):
  - Products (GetProducts)
  - Orders (GetOrders + GetOrderItems)
  - Contacts (buyer info from orders)
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
import uuid
from typing import Any
from urllib.parse import urlencode

import httpx

from celerp.connectors.base import (
    ConnectorBase,
    ConnectorContext,
    SyncDirection,
    SyncEntity,
    SyncResult,
)

log = logging.getLogger(__name__)

_REGION_URLS: dict[str, str] = {
    "sg": "https://api.lazada.sg/rest",
    "my": "https://api.lazada.com.my/rest",
    "th": "https://api.lazada.co.th/rest",
    "ph": "https://api.lazada.com.ph/rest",
    "id": "https://api.lazada.co.id/rest",
    "vn": "https://api.lazada.vn/rest",
}
_DEFAULT_REGION = "sg"
_PAGE_SIZE = 100


def _base_url(ctx: ConnectorContext) -> str:
    region = (ctx.store_handle or _DEFAULT_REGION).lower()
    return _REGION_URLS.get(region, _REGION_URLS[_DEFAULT_REGION])


def _sign(app_secret: str, path: str, params: dict[str, Any]) -> str:
    """
    Lazada signing: sort params by key, concatenate as key+value pairs,
    prepend path, then HMAC-SHA256 with app_secret.
    """
    sorted_params = "".join(f"{k}{v}" for k, v in sorted(params.items()))
    message = path + sorted_params
    return hmac.new(app_secret.encode(), message.encode(), hashlib.sha256).hexdigest().upper()


def _signed_params(ctx: ConnectorContext, path: str,
                   extra_params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build complete signed params for a Lazada API call."""
    extra = ctx.extra or {}
    app_key = str(extra["app_key"])
    app_secret = str(extra["app_secret"])
    ts = int(time.time() * 1000)  # Lazada uses milliseconds

    params: dict[str, Any] = {
        "app_key": app_key,
        "timestamp": ts,
        "access_token": ctx.access_token,
        "sign_method": "sha256",
    }
    if extra_params:
        params.update(extra_params)

    params["sign"] = _sign(app_secret, path, params)
    return params


def _validate_ctx(ctx: ConnectorContext) -> None:
    extra = ctx.extra or {}
    if "app_key" not in extra or "app_secret" not in extra:
        raise ValueError("ConnectorContext.extra must contain app_key and app_secret")


class LazadaConnector(ConnectorBase):
    name = "lazada"
    display_name = "Lazada"
    supported_entities = [SyncEntity.PRODUCTS, SyncEntity.ORDERS, SyncEntity.CONTACTS]
    direction = SyncDirection.INBOUND

    # ── Products ─────────────────────────────────────────────────────────────

    async def sync_products(self, ctx: ConnectorContext) -> SyncResult:
        """
        Pull Lazada products → Celerp items.

        Mapping:
          item_id               → idempotency_key (lazada:item:{item_id})
          attributes.name       → name
          skus[0].SkuId + ShopSku → sku
          skus[0].price         → sale_price
          skus[0].quantity      → quantity
        """
        from celerp_inventory import services as items_svc
        from celerp_inventory.routes import ItemCreate

        result = SyncResult(entity=SyncEntity.PRODUCTS, direction=SyncDirection.INBOUND)

        try:
            _validate_ctx(ctx)
        except ValueError as exc:
            result.errors = [str(exc)]
            return result

        errors: list[str] = []
        offset = 0

        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                path = "/products/get"
                params = _signed_params(ctx, path, {
                    "filter": "all",
                    "offset": offset,
                    "limit": _PAGE_SIZE,
                })
                try:
                    resp = await client.get(f"{_base_url(ctx)}{path}", params=params)
                    resp.raise_for_status()
                    body = resp.json()
                except httpx.HTTPStatusError as exc:
                    errors.append(f"Lazada products API error at offset {offset}: {exc}")
                    break

                products = (body.get("data") or {}).get("products", [])
                if not products:
                    break

                for product in products:
                    attrs = product.get("attributes") or {}
                    name = attrs.get("name", f"lazada-{product.get('item_id', 'unknown')}")
                    item_id = product.get("item_id", "")

                    skus = product.get("skus") or []
                    if not skus:
                        result.skipped += 1
                        continue

                    for sku_data in skus:
                        sku = (sku_data.get("ShopSku") or sku_data.get("SkuId") or "").strip()
                        if not sku:
                            sku = f"lazada-{item_id}-{sku_data.get('SkuId', '')}"

                        try:
                            sale_price_raw = sku_data.get("price") or sku_data.get("special_price")
                            sale_price = float(sale_price_raw) if sale_price_raw else None
                        except (TypeError, ValueError):
                            sale_price = None

                        try:
                            quantity = float(sku_data.get("quantity", 0) or 0)
                        except (TypeError, ValueError):
                            quantity = 0.0

                        idem_key = f"lazada:sku:{sku_data.get('SkuId', sku)}"
                        item_obj = ItemCreate(
                            sku=sku,
                            name=name,
                            sell_by="piece",
                            sale_price=sale_price,
                            quantity=quantity,
                            idempotency_key=idem_key,
                        )
                        try:
                            created = await items_svc.upsert_from_connector(
                                ctx.company_id, item_obj)
                            if created:
                                result.created += 1
                            else:
                                result.skipped += 1
                        except Exception as exc:
                            errors.append(f"SKU {sku}: {exc}")

                total_products = (body.get("data") or {}).get("total_products", 0)
                offset += _PAGE_SIZE
                if offset >= total_products:
                    break

        result.errors = errors or None
        log.info(
            "lazada.sync_products company=%s created=%d skipped=%d errors=%d",
            ctx.company_id, result.created, result.skipped, len(errors),
        )
        return result

    # ── Orders ───────────────────────────────────────────────────────────────

    async def sync_orders(self, ctx: ConnectorContext) -> SyncResult:
        """
        Pull Lazada orders → Celerp documents.

        Mapping:
          order_id              → idempotency_key (lazada:order:{order_id})
          order_number          → ref_id
          statuses              → "delivered" → closed, "canceled" → cancelled, else open
          order_items           → line_items
          price                 → total
        """
        result = SyncResult(entity=SyncEntity.ORDERS, direction=SyncDirection.INBOUND)

        try:
            _validate_ctx(ctx)
        except ValueError as exc:
            result.errors = [str(exc)]
            return result

        errors: list[str] = []
        offset = 0

        async with httpx.AsyncClient(timeout=60) as client:
            while True:
                path = "/orders/get"
                params = _signed_params(ctx, path, {
                    "sort_by": "created_at",
                    "sort_direction": "DESC",
                    "offset": offset,
                    "limit": _PAGE_SIZE,
                })
                try:
                    resp = await client.get(f"{_base_url(ctx)}{path}", params=params)
                    resp.raise_for_status()
                    body = resp.json()
                except httpx.HTTPStatusError as exc:
                    errors.append(f"Lazada orders API error at offset {offset}: {exc}")
                    break

                orders = (body.get("data") or {}).get("orders", [])
                if not orders:
                    break

                # Fetch order items for this batch
                order_ids = [str(o["order_id"]) for o in orders]
                try:
                    items_by_order = await self._get_order_items(ctx, client, order_ids)
                except (httpx.HTTPStatusError, KeyError) as exc:
                    errors.append(f"Order items fetch failed: {exc}")
                    items_by_order = {}

                for order in orders:
                    order["_items"] = items_by_order.get(str(order["order_id"]), [])
                    try:
                        created = await _upsert_order(ctx.company_id, order)
                        if created:
                            result.created += 1
                        else:
                            result.skipped += 1
                    except Exception as exc:
                        errors.append(f"Order {order.get('order_id')}: {exc}")

                count = (body.get("data") or {}).get("count", 0)
                offset += _PAGE_SIZE
                if offset >= count:
                    break

        result.errors = errors or None
        log.info(
            "lazada.sync_orders company=%s created=%d skipped=%d errors=%d",
            ctx.company_id, result.created, result.skipped, len(errors),
        )
        return result

    # ── Contacts ─────────────────────────────────────────────────────────────

    async def sync_contacts(self, ctx: ConnectorContext) -> SyncResult:
        """
        Derive contacts from Lazada orders (address info).
        Lazada does not expose a buyer identity API.
        """
        result = SyncResult(entity=SyncEntity.CONTACTS, direction=SyncDirection.INBOUND)

        try:
            _validate_ctx(ctx)
        except ValueError as exc:
            result.errors = [str(exc)]
            return result

        errors: list[str] = []
        offset = 0

        async with httpx.AsyncClient(timeout=60) as client:
            while True:
                path = "/orders/get"
                params = _signed_params(ctx, path, {
                    "sort_by": "created_at",
                    "sort_direction": "DESC",
                    "offset": offset,
                    "limit": _PAGE_SIZE,
                })
                try:
                    resp = await client.get(f"{_base_url(ctx)}{path}", params=params)
                    resp.raise_for_status()
                    body = resp.json()
                except httpx.HTTPStatusError as exc:
                    errors.append(f"Lazada orders API error: {exc}")
                    break

                orders = (body.get("data") or {}).get("orders", [])
                if not orders:
                    break

                for order in orders:
                    try:
                        created = await _upsert_contact(ctx.company_id, order)
                        if created:
                            result.created += 1
                        else:
                            result.skipped += 1
                    except Exception as exc:
                        errors.append(f"Order {order.get('order_id')}: {exc}")

                count = (body.get("data") or {}).get("count", 0)
                offset += _PAGE_SIZE
                if offset >= count:
                    break

        result.errors = errors or None
        return result

    # ── Private API helpers ───────────────────────────────────────────────────

    async def _get_order_items(self, ctx: ConnectorContext,
                                client: httpx.AsyncClient,
                                order_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Fetch order items for a list of order IDs. Returns {order_id: [items]}."""
        path = "/orders/items/get"
        params = _signed_params(ctx, path, {
            "order_ids": "[" + ",".join(order_ids) + "]",
        })
        resp = await client.get(f"{_base_url(ctx)}{path}", params=params)
        resp.raise_for_status()
        body = resp.json()
        result: dict[str, list[dict[str, Any]]] = {}
        for entry in (body.get("data") or []):
            oid = str(entry.get("order_id", ""))
            result[oid] = entry.get("order_items", [])
        return result


# ── DB upsert helpers ─────────────────────────────────────────────────────────

async def _upsert_order(company_id: str, order: dict) -> bool:
    """Create a doc (invoice) from a Lazada order dict. Returns True if newly created."""
    from celerp.db import SessionLocal
    from sqlalchemy import text
    from celerp.events.engine import emit_event

    order_id = order["order_id"]
    idem_key = f"lazada:order:{order_id}"

    async with SessionLocal() as session:
        existing = (
            await session.execute(
                text("SELECT id FROM ledger WHERE idempotency_key=:k"),
                {"k": idem_key},
            )
        ).first()
        if existing:
            return False

        statuses = order.get("statuses", [])
        if "delivered" in statuses or "shipped" in statuses:
            status = "closed"
        elif "canceled" in statuses:
            status = "cancelled"
        else:
            status = "open"

        line_items = []
        for li in order.get("_items", []):
            try:
                qty = float(1)  # Lazada items are always qty=1 per order item
                price = float(li.get("paid_price") or li.get("item_price") or 0)
            except (TypeError, ValueError):
                qty, price = 1.0, 0.0
            line_items.append({
                "name": li.get("name", ""),
                "sku": li.get("shop_sku") or li.get("sku") or "",
                "quantity": qty,
                "unit_price": price,
                "line_total": qty * price,
            })

        try:
            total = float(order.get("price", 0) or 0)
        except (TypeError, ValueError):
            total = 0.0

        data = {
            "doc_type": "invoice",
            "ref_id": str(order.get("order_number", order_id)),
            "status": status,
            "line_items": line_items,
            "total": total,
            "amount_outstanding": 0.0 if status == "closed" else total,
            "attributes": {
                "lazada_order_id": str(order_id),
            },
        }

        await emit_event(
            session,
            company_id=company_id,
            entity_id=f"doc:lazada-{order_id}",
            entity_type="doc",
            event_type="doc.created",
            data=data,
            actor_id=None,
            location_id=None,
            source="connector",
            idempotency_key=idem_key,
            metadata_={},
        )
        await session.commit()
        return True


async def _upsert_contact(company_id: str, order: dict) -> bool:
    """
    Derive a CRM contact from a Lazada order's address info.
    Uses address_name + phone as stable key (no buyer_id exposed).
    """
    from celerp.db import SessionLocal
    from sqlalchemy import text
    from celerp.events.engine import emit_event

    addr = order.get("address_billing") or order.get("address_shipping") or {}
    phone = addr.get("phone") or addr.get("phone2") or ""
    name = addr.get("first_name", "") + " " + addr.get("last_name", "")
    name = name.strip()
    if not name and not phone:
        return False

    # Use phone as stable key; fall back to order_id-based key
    if phone:
        idem_key = f"lazada:contact:phone:{phone}"
    else:
        idem_key = f"lazada:contact:order:{order['order_id']}"

    async with SessionLocal() as session:
        existing = (
            await session.execute(
                text("SELECT id FROM ledger WHERE idempotency_key=:k"),
                {"k": idem_key},
            )
        ).first()
        if existing:
            return False

        data = {
            "name": name or f"lazada:{order.get('order_id', '')}",
            "phone": phone or None,
            "attributes": {
                "city": addr.get("city"),
                "country": addr.get("country"),
                "lazada_order_id": str(order.get("order_id", "")),
            },
        }
        data = {k: v for k, v in data.items() if v is not None}

        await emit_event(
            session,
            company_id=company_id,
            entity_id=f"contact:{uuid.uuid4()}",
            entity_type="contact",
            event_type="crm.contact.created",
            data=data,
            actor_id=None,
            location_id=None,
            source="connector",
            idempotency_key=idem_key,
            metadata_={},
        )
        await session.commit()
        return True
