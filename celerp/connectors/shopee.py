# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""
Shopee connector.

Auth model: Shopee Open Platform uses HMAC-SHA256 request signing.
Each request is signed with partner_key + timestamp + path + access_token + shop_id.

ConnectorContext fields:
  access_token  - OAuth access_token (from Shopee OAuth flow via relay)
  store_handle  - shop_id (numeric string, e.g. "12345678")
  extra = {
    "partner_id": int,     # Shopee app partner ID
    "partner_key": str,    # Shopee app partner key (secret)
  }

API: Shopee Open Platform v2 (https://open.shopee.com/documents)

Sync scope (inbound only):
  - Products (item list + item_base_info)
  - Orders (get_order_list + get_order_detail)
  - Contacts (buyer info from orders)
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
import uuid
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

_BASE_URL = "https://partner.shopeemobile.com/api/v2"
_SANDBOX_URL = "https://partner.test-stable.shopeemobile.com/api/v2"
_PAGE_SIZE = 100


def _sign(partner_id: int, path: str, timestamp: int, access_token: str, shop_id: int,
          partner_key: str) -> str:
    """
    Shopee v2 signature: HMAC-SHA256 of
    "{partner_id}{path}{timestamp}{access_token}{shop_id}"
    using partner_key as the HMAC key.
    """
    base = f"{partner_id}{path}{timestamp}{access_token}{shop_id}"
    return hmac.new(partner_key.encode(), base.encode(), hashlib.sha256).hexdigest()


def _common_params(ctx: ConnectorContext, path: str) -> dict[str, Any]:
    """Build the common signed query params for every Shopee API call."""
    extra = ctx.extra or {}
    partner_id = int(extra["partner_id"])
    partner_key = str(extra["partner_key"])
    shop_id = int(ctx.store_handle or 0)
    ts = int(time.time())
    sign = _sign(partner_id, path, ts, ctx.access_token, shop_id, partner_key)
    return {
        "partner_id": partner_id,
        "timestamp": ts,
        "access_token": ctx.access_token,
        "shop_id": shop_id,
        "sign": sign,
    }


def _validate_ctx(ctx: ConnectorContext) -> None:
    if not ctx.store_handle:
        raise ValueError("ConnectorContext.store_handle (shop_id) required for Shopee")
    extra = ctx.extra or {}
    if "partner_id" not in extra or "partner_key" not in extra:
        raise ValueError("ConnectorContext.extra must contain partner_id and partner_key")


class ShopeeConnector(ConnectorBase):
    name = "shopee"
    display_name = "Shopee"
    supported_entities = [SyncEntity.PRODUCTS, SyncEntity.ORDERS, SyncEntity.CONTACTS]
    direction = SyncDirection.INBOUND

    # ── Products ─────────────────────────────────────────────────────────────

    async def sync_products(self, ctx: ConnectorContext) -> SyncResult:
        """
        Pull Shopee items → Celerp items.

        Mapping:
          item.item_id          → idempotency_key (shopee:item:{item_id})
          item.item_name        → name
          item.item_sku         → sku (skipped if blank)
          model.current_price   → sale_price (first model, or item.price_info)
          item.stock            → quantity (informational)
          item.item_status      → skipped if not NORMAL
        """
        from celerp_inventory.routes import ItemCreate
        from celerp_inventory import services as items_svc

        result = SyncResult(entity=SyncEntity.PRODUCTS, direction=SyncDirection.INBOUND)

        try:
            _validate_ctx(ctx)
        except ValueError as exc:
            result.errors = [str(exc)]
            return result

        try:
            item_ids = await self._list_item_ids(ctx)
        except (httpx.HTTPStatusError, KeyError) as exc:
            result.errors = [f"Shopee API error listing items: {exc}"]
            return result

        errors: list[str] = []
        # Fetch item details in batches of 50 (Shopee limit)
        for batch_start in range(0, len(item_ids), 50):
            batch = item_ids[batch_start:batch_start + 50]
            try:
                details = await self._get_item_base_info(ctx, batch)
            except (httpx.HTTPStatusError, KeyError) as exc:
                errors.append(f"Batch {batch_start}: {exc}")
                continue

            for item in details:
                if item.get("item_status") != "NORMAL":
                    result.skipped += 1
                    continue

                sku = (item.get("item_sku") or "").strip()
                if not sku:
                    # Fall back to item_id as SKU
                    sku = f"shopee-{item['item_id']}"

                name = item.get("item_name", sku)
                # Price: from price_info list → min_current_price
                price_info = item.get("price_info") or []
                sale_price: float | None = None
                if price_info:
                    sale_price = float(price_info[0].get("current_price", 0)) or None

                # Stock: sum across models
                stock_info = item.get("stock_info_v2") or {}
                summary = stock_info.get("summary_info") or {}
                quantity = float(summary.get("total_available_stock", 0))

                idem_key = f"shopee:item:{item['item_id']}"
                item_obj = ItemCreate(
                    sku=sku,
                    name=name,
                    sell_by="piece",
                    sale_price=sale_price,
                    quantity=quantity,
                    idempotency_key=idem_key,
                )
                try:
                    created = await items_svc.upsert_from_connector(ctx.company_id, item_obj)
                    if created:
                        result.created += 1
                    else:
                        result.skipped += 1
                except Exception as exc:
                    errors.append(f"SKU {sku}: {exc}")

        result.errors = errors or None
        log.info(
            "shopee.sync_products company=%s created=%d skipped=%d errors=%d",
            ctx.company_id, result.created, result.skipped, len(errors),
        )
        return result

    # ── Orders ───────────────────────────────────────────────────────────────

    async def sync_orders(self, ctx: ConnectorContext) -> SyncResult:
        """
        Pull Shopee orders → Celerp documents (type=invoice).

        Mapping:
          order.order_sn          → ref_id + idempotency_key
          order.order_status      → COMPLETED → closed, else open
          order.item_list         → line_items
          order.total_amount      → total
        """
        result = SyncResult(entity=SyncEntity.ORDERS, direction=SyncDirection.INBOUND)

        try:
            _validate_ctx(ctx)
        except ValueError as exc:
            result.errors = [str(exc)]
            return result

        try:
            order_sns = await self._list_order_sns(ctx)
        except (httpx.HTTPStatusError, KeyError) as exc:
            result.errors = [f"Shopee API error listing orders: {exc}"]
            return result

        errors: list[str] = []
        # Fetch order details in batches of 50
        for batch_start in range(0, len(order_sns), 50):
            batch = order_sns[batch_start:batch_start + 50]
            try:
                orders = await self._get_order_detail(ctx, batch)
            except (httpx.HTTPStatusError, KeyError) as exc:
                errors.append(f"Order batch {batch_start}: {exc}")
                continue

            for order in orders:
                try:
                    created = await _upsert_order(ctx.company_id, order)
                    if created:
                        result.created += 1
                    else:
                        result.skipped += 1
                except Exception as exc:
                    errors.append(f"Order {order.get('order_sn')}: {exc}")

        result.errors = errors or None
        log.info(
            "shopee.sync_orders company=%s created=%d skipped=%d errors=%d",
            ctx.company_id, result.created, result.skipped, len(errors),
        )
        return result

    # ── Contacts ─────────────────────────────────────────────────────────────

    async def sync_contacts(self, ctx: ConnectorContext) -> SyncResult:
        """
        Derive contacts from Shopee orders (buyer info).
        Shopee does not expose a standalone customer API.
        """
        result = SyncResult(entity=SyncEntity.CONTACTS, direction=SyncDirection.INBOUND)

        try:
            _validate_ctx(ctx)
        except ValueError as exc:
            result.errors = [str(exc)]
            return result

        try:
            order_sns = await self._list_order_sns(ctx)
        except (httpx.HTTPStatusError, KeyError) as exc:
            result.errors = [f"Shopee API error: {exc}"]
            return result

        errors: list[str] = []
        for batch_start in range(0, len(order_sns), 50):
            batch = order_sns[batch_start:batch_start + 50]
            try:
                orders = await self._get_order_detail(ctx, batch)
            except (httpx.HTTPStatusError, KeyError) as exc:
                errors.append(f"Contact batch {batch_start}: {exc}")
                continue

            for order in orders:
                try:
                    created = await _upsert_contact(ctx.company_id, order)
                    if created:
                        result.created += 1
                    else:
                        result.skipped += 1
                except Exception as exc:
                    errors.append(f"Order {order.get('order_sn')}: {exc}")

        result.errors = errors or None
        return result

    # ── Private API helpers ───────────────────────────────────────────────────

    async def _list_item_ids(self, ctx: ConnectorContext) -> list[int]:
        """Paginate item.get_item_list → collect all item_ids."""
        path = "/product/get_item_list"
        item_ids: list[int] = []
        offset = 0

        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                params = _common_params(ctx, path)
                params.update({
                    "offset": offset,
                    "page_size": _PAGE_SIZE,
                    "item_status": "NORMAL",
                })
                resp = await client.get(f"{_BASE_URL}{path}", params=params)
                resp.raise_for_status()
                body = resp.json()
                response = body.get("response", {})
                items = response.get("item", [])
                item_ids.extend(i["item_id"] for i in items)
                if not response.get("has_next_page", False):
                    break
                offset += _PAGE_SIZE

        return item_ids

    async def _get_item_base_info(self, ctx: ConnectorContext,
                                   item_ids: list[int]) -> list[dict[str, Any]]:
        """Fetch base info for up to 50 item_ids."""
        path = "/product/get_item_base_info"
        params = _common_params(ctx, path)
        params["item_id_list"] = ",".join(str(i) for i in item_ids)

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{_BASE_URL}{path}", params=params)
            resp.raise_for_status()
            body = resp.json()
            return body.get("response", {}).get("item_list", [])

    async def _list_order_sns(self, ctx: ConnectorContext) -> list[str]:
        """Paginate order.get_order_list → collect all order_sns."""
        path = "/order/get_order_list"
        order_sns: list[str] = []
        cursor = ""

        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                params = _common_params(ctx, path)
                params.update({
                    "time_range_field": "create_time",
                    "time_from": 0,
                    "time_to": int(time.time()),
                    "page_size": _PAGE_SIZE,
                    "cursor": cursor,
                    "order_status": "ALL",
                    "response_optional_fields": "order_status",
                })
                resp = await client.get(f"{_BASE_URL}{path}", params=params)
                resp.raise_for_status()
                body = resp.json()
                response = body.get("response", {})
                order_list = response.get("order_list", [])
                order_sns.extend(o["order_sn"] for o in order_list)
                if not response.get("more", False):
                    break
                cursor = response.get("next_cursor", "")

        return order_sns

    async def _get_order_detail(self, ctx: ConnectorContext,
                                 order_sns: list[str]) -> list[dict[str, Any]]:
        """Fetch full detail for up to 50 order_sns."""
        path = "/order/get_order_detail"
        params = _common_params(ctx, path)
        params["order_sn_list"] = ",".join(order_sns)
        params["response_optional_fields"] = (
            "buyer_user_id,buyer_username,estimated_shipping_fee,"
            "recipient_address,actual_shipping_fee,goods_to_declare,"
            "note,note_update_time,item_list,pay_time,dropshipper,"
            "credit_card_number,dropshipper_phone,split_up,buyer_cancel_reason,"
            "cancel_by,cancel_reason,actual_shipping_cost_amount"
        )

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{_BASE_URL}{path}", params=params)
            resp.raise_for_status()
            body = resp.json()
            return body.get("response", {}).get("order_list", [])


# ── DB upsert helpers ─────────────────────────────────────────────────────────

async def _upsert_order(company_id: str, order: dict) -> bool:
    """Create a doc (invoice) from a Shopee order dict. Returns True if newly created."""
    from celerp.db import SessionLocal
    from sqlalchemy import text
    from celerp.events.engine import emit_event

    order_sn = order["order_sn"]
    idem_key = f"shopee:order:{order_sn}"

    async with SessionLocal() as session:
        existing = (
            await session.execute(
                text("SELECT id FROM ledger WHERE idempotency_key=:k"),
                {"k": idem_key},
            )
        ).first()
        if existing:
            return False

        status_map = {"COMPLETED": "closed", "CANCELLED": "cancelled"}
        raw_status = order.get("order_status", "")
        status = status_map.get(raw_status, "open")

        line_items = []
        for li in order.get("item_list", []):
            qty = float(li.get("model_quantity_purchased", 1))
            price = float(li.get("model_discounted_price", li.get("model_original_price", 0)))
            line_items.append({
                "name": li.get("item_name", ""),
                "sku": li.get("model_sku") or li.get("item_sku") or "",
                "quantity": qty,
                "unit_price": price,
                "line_total": qty * price,
            })

        total = float(order.get("total_amount", 0) or 0)
        data = {
            "doc_type": "invoice",
            "ref_id": order_sn,
            "status": status,
            "line_items": line_items,
            "total": total,
            "amount_outstanding": 0.0 if status == "closed" else total,
            "attributes": {
                "shopee_order_sn": order_sn,
                "buyer_username": order.get("buyer_username"),
            },
        }

        await emit_event(
            session,
            company_id=company_id,
            entity_id=f"doc:{order_sn}",
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
    Derive a CRM contact from a Shopee order's buyer info.
    Uses buyer_user_id as stable identifier.
    Returns True if newly created.
    """
    from celerp.db import SessionLocal
    from sqlalchemy import text
    from celerp.events.engine import emit_event

    buyer_id = order.get("buyer_user_id")
    if not buyer_id:
        return False

    idem_key = f"shopee:buyer:{buyer_id}"

    async with SessionLocal() as session:
        existing = (
            await session.execute(
                text("SELECT id FROM ledger WHERE idempotency_key=:k"),
                {"k": idem_key},
            )
        ).first()
        if existing:
            return False

        recipient = order.get("recipient_address") or {}
        name = order.get("buyer_username") or f"shopee:{buyer_id}"

        data = {
            "name": name,
            "phone": recipient.get("phone"),
            "attributes": {
                "shopee_buyer_id": str(buyer_id),
                "city": recipient.get("city"),
                "region": recipient.get("region"),
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
