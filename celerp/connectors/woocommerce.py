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
from celerp.services.outbound_url import validate_public_base_url
from celerp.connectors.util import money
from celerp.connectors.base import (
    ConnectorBase,
    ConnectorCategory,
    ConnectorContext,
    SyncDirection,
    SyncEntity,
    SyncResult,
    contact_matches,
    store_holds_records,
)
import celerp.connectors.upsert as _upsert

log = logging.getLogger(__name__)

_PER_PAGE = 100  # WooCommerce max per page


def _base_url(ctx: ConnectorContext) -> str:
    if not ctx.store_handle:
        raise ValueError("ConnectorContext.store_handle is required for WooCommerce")
    store_url = ctx.store_handle.rstrip("/")
    return f"{store_url}/wp-json/wc/v3"


async def _validate_request_url(url: str) -> None:
    import os
    await validate_public_base_url(
        url,
        allow_http=bool(os.environ.get("CELERP_ALLOW_HTTP_STORE")),
        reject_query=False,
        reject_fragment=True,
    )


def _http_client(*, max_retries: int = 3) -> RateLimitedClient:
    return RateLimitedClient(
        max_retries=max_retries,
        before_request=_validate_request_url,
        public_only=True,
    )


def _auth(ctx: ConnectorContext) -> tuple[str, str]:
    """Return (consumer_key, consumer_secret) Basic Auth tuple."""
    if not ctx.access_token or ":" not in ctx.access_token:
        raise ValueError("ConnectorContext.access_token must be 'consumer_key:consumer_secret'")
    key, secret = ctx.access_token.split(":", 1)
    return (key, secret)

def _direction_allows_remote_product_create(direction) -> bool:
    """Only outbound-capable connector modes may publish a new remote product."""
    try:
        resolved = direction if isinstance(direction, SyncDirection) else SyncDirection(direction)
    except (TypeError, ValueError):
        return False
    return resolved in (SyncDirection.OUTBOUND, SyncDirection.BOTH)


def _link_needs_rediscovery(link: dict) -> bool:
    """A remotely deleted identity may be safely replaced by an exact-SKU match."""
    return bool(link and link.get("remote_deleted") is True)


def _deleted_variation_requires_import(link: dict) -> bool:
    """Never guess a new parent by recreating a deleted variation as a simple product."""
    return _link_needs_rediscovery(link) and bool(link.get("variation_id"))


def _link_matches_deleted_product(
    link: dict, product_id: str, variation_id: str | None
) -> bool:
    """Match an exact deleted variation or every variation under a deleted parent."""
    if str(link.get("product_id") or "") != str(product_id):
        return False
    if variation_id is None:
        return True
    return str(link.get("variation_id") or "") == str(variation_id)



class WooCommerceConnector(ConnectorBase):
    name = "woocommerce"
    display_name = "WooCommerce"
    category = ConnectorCategory.WEBSITE
    direction = SyncDirection.BOTH
    supported_entities = [SyncEntity.PRODUCTS, SyncEntity.ORDERS, SyncEntity.CONTACTS]
    conflict_strategy = {
        "products": "external_wins",
        "orders": "external_wins",
        "contacts": "external_wins",
    }

    async def same_store(self, ctx: ConnectorContext, records: list[dict]) -> bool:
        """Order, customer and product numbers repeat across stores, so a
        sampled order counts only when its total, currency and item names match
        what was imported, a sampled customer only when its contact details do,
        and a sampled product only when its SKU or name does."""
        def customer_id(record: dict) -> str:
            return str((record.get("attributes") or {}).get("woocommerce_id") or "")

        def product_id(record: dict) -> str:
            link = (record.get("external_links") or {}).get(self.name) or {}
            if link.get("product_id") not in (None, ""):
                return str(link["product_id"])
            parts = str(record.get("idempotency_key") or "").split(":")
            return parts[1] if len(parts) >= 2 and parts[0] == self.name else ""

        order_ids = {str(r["woocommerce_order_id"]) for r in records if r.get("woocommerce_order_id")}
        customer_ids = {customer_id(r) for r in records} - {""}
        product_ids = {product_id(r) for r in records} - {""}
        orders = {
            str(o.get("id")): o
            for o in (await self._paginate(ctx, "/orders", params={"include": ",".join(order_ids)}) if order_ids else [])
        }
        customers = {
            str(c.get("id")): c
            for c in (await self._paginate(ctx, "/customers", params={"include": ",".join(customer_ids)}) if customer_ids else [])
        }
        products = {
            str(p.get("id")): p
            for p in (await self._paginate(ctx, "/products", params={"include": ",".join(product_ids)}) if product_ids else [])
        }
        matches = []
        for record in records:
            order = orders.get(str(record.get("woocommerce_order_id") or ""))
            customer = customers.get(customer_id(record))
            product = products.get(product_id(record))
            if order is not None:
                names = {li.get("name") for li in order.get("line_items", []) if li.get("name")}
                matches.append(
                    abs((money(order.get("total")) or 0.0) - float(record.get("total") or 0)) < 0.005
                    and order.get("currency") == record.get("currency")
                    and names <= {li.get("name") for li in record.get("line_items") or []}
                )
            elif customer is not None:
                billing = customer.get("billing") or {}
                email = customer.get("email") or billing.get("email")
                first = customer.get("first_name") or billing.get("first_name") or ""
                last = customer.get("last_name") or billing.get("last_name") or ""
                matches.append(contact_matches(record, {
                    "email": email,
                    "phone": customer.get("phone") or billing.get("phone"),
                    "name": " ".join(p for p in (first, last) if p).strip() or email,
                }))
            elif product is not None:
                # A variation is imported as "<product name> - <options>".
                name = str(product.get("name") or "")
                stored = str(record.get("name") or "")
                matches.append(
                    bool(product.get("sku")) and product.get("sku") == record.get("sku")
                    or bool(name) and (stored == name or stored.startswith(f"{name} - "))
                )
            else:
                matches.append(False)
        return store_holds_records(matches)

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

        async with _http_client() as client:
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


    @staticmethod
    def _product_path(item: dict) -> str:
        product_id = item.get("woocommerce_product_id")
        variation_id = item.get("woocommerce_variation_id")
        if variation_id:
            return f"/products/{product_id}/variations/{variation_id}"
        return f"/products/{product_id}"

    async def sync_products(
        self, ctx: ConnectorContext, since: datetime | None = None, reconcile: bool = False
    ) -> SyncResult:
        """Pull WooCommerce products into Celerp catalog product anchors. The
        daily ``reconcile`` pass pulls every product, so ones deleted in the
        store are found."""
        from celerp_inventory.services import upsert_external_product

        result = SyncResult(entity=SyncEntity.PRODUCTS)
        errors: list[str] = []
        if reconcile:
            since = None
        full_scan = since is None
        seen_identities: set[tuple[str, str | None]] = set()
        incomplete_parents: set[str] = set()
        params: dict = {}
        if since:
            params["modified_after"] = since.isoformat()
            params["dates_are_gmt"] = "true"

        try:
            products = await self._paginate(ctx, "/products", params=params or None)
        except (httpx.HTTPStatusError, ValueError) as exc:
            result.errors = [f"WooCommerce API error: {exc}"]
            return result

        async def _import_one(*, sku, name, description, price, product_id,
                              variation_id=None, manage_stock=None, stock_quantity=None,
                              virtual=False):
            try:
                non_stock_virtual = bool(virtual) and manage_stock is False
                outcome, entity_id = await upsert_external_product(
                    ctx.company_id,
                    platform="woocommerce",
                    product_id=str(product_id),
                    variation_id=str(variation_id) if variation_id is not None else None,
                    sku=sku,
                    name=name,
                    description=description,
                    sale_price=price,
                    quantity=float(stock_quantity) if stock_quantity is not None else None,
                    seed_quantity=(manage_stock is True),
                    link_fields={"manage_stock": manage_stock},
                    inventory_type="service" if non_stock_virtual else None,
                    sell_by="service" if non_stock_virtual else None,
                )
                result.record(outcome)
                return None if outcome == "disabled" else entity_id
            except Exception as exc:
                errors.append(f"SKU {sku}: {exc}")
                return None

        for product in products:
            pid = product.get("id")
            name = product.get("name") or f"WC-{pid}"
            description = product.get("description") or ""
            if product.get("type") == "variable":
                try:
                    variations = await self._paginate(ctx, f"/products/{pid}/variations")
                except (httpx.HTTPStatusError, ValueError) as exc:
                    errors.append(f"Product {pid} variations: {exc}")
                    incomplete_parents.add(str(pid))
                    continue
                for var in variations:
                    vid = var.get("id")
                    seen_identities.add((str(pid), str(vid)))
                    var_sku = (var.get("sku") or "").strip() or f"WC-{pid}-{vid}"
                    opts = " / ".join(
                        str(a.get("option", "")) for a in (var.get("attributes") or [])
                        if a.get("option")
                    )
                    var_price = money(var.get("regular_price"))
                    if var_price is None:
                        var_price = money(var.get("price"))
                    entity_id = await _import_one(
                        sku=var_sku,
                        name=f"{name} - {opts}" if opts else name,
                        description=var.get("description") or description,
                        price=var_price,
                        product_id=pid,
                        variation_id=vid,
                        manage_stock=var.get("manage_stock"),
                        stock_quantity=var.get("stock_quantity"),
                        virtual=bool(var.get("virtual", product.get("virtual", False))),
                    )
                    if entity_id:
                        var_img = var.get("image")
                        files = {
                            "images": ([var_img] if var_img and var_img.get("src") else [])
                            + product.get("images", []),
                            "meta_data": product.get("meta_data", []),
                        }
                        try:
                            await self._pull_product_files(ctx, files, entity_id)
                        except Exception as img_exc:
                            log.warning("woocommerce file pull failed for item %s: %s", entity_id, img_exc)
                continue

            seen_identities.add((str(pid), None))
            sku = (product.get("sku") or "").strip() or f"WC-{pid}"
            sell_price = money(product.get("regular_price"))
            if sell_price is None:
                sell_price = money(product.get("price"))
            entity_id = await _import_one(
                sku=sku, name=name, description=description, price=sell_price,
                product_id=pid, manage_stock=product.get("manage_stock"),
                stock_quantity=product.get("stock_quantity"),
                virtual=bool(product.get("virtual", False)),
            )
            if entity_id:
                try:
                    await self._pull_product_files(ctx, product, entity_id)
                except Exception as img_exc:
                    log.warning("woocommerce file pull failed for item %s: %s", entity_id, img_exc)

        if full_scan:
            try:
                result.updated += await self._reconcile_missing_product_links(
                    ctx, seen_identities, incomplete_parents
                )
            except Exception as exc:
                errors.append(f"Product reconciliation: {exc}")

        result.errors = errors or None
        return result

    async def _reconcile_missing_product_links(
        self,
        ctx: ConnectorContext,
        seen: set[tuple[str, str | None]],
        incomplete_parents: set[str],
    ) -> int:
        from sqlalchemy import select
        from celerp.db import SessionLocal as AsyncSessionLocal
        from celerp.models.projections import Projection
        from celerp_inventory.services import (
            external_link_for_state,
            set_external_link_state,
        )

        changed = 0
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                select(Projection).where(
                    Projection.company_id == ctx.company_id,
                    Projection.entity_type == "item",
                )
            )).scalars().all()
            for row in rows:
                link = external_link_for_state(row.state or {}, "woocommerce")
                product_id = str(link.get("product_id") or "")
                if (
                    not product_id
                    or link.get("remote_deleted") is True
                    or product_id in incomplete_parents
                ):
                    continue
                variation = link.get("variation_id")
                identity = (
                    product_id,
                    str(variation) if variation not in (None, "") else None,
                )
                if identity in seen:
                    continue
                await set_external_link_state(
                    session,
                    ctx.company_id,
                    row.entity_id,
                    "woocommerce",
                    remote_deleted=True,
                    expected_identity=identity,
                    source="connector",
                )
                changed += 1
            if changed:
                await session.commit()
        return changed

    async def _pull_product_files(self, ctx: ConnectorContext, product: dict[str, Any], entity_id: str) -> None:
        """Pull images and certificate metafields onto an already-resolved item."""
        from celerp.db import get_session_ctx as get_async_session
        from celerp.models.projections import Projection
        from celerp.connectors.images import download_and_emit_file, _CERT_TAGS

        images: list[dict] = product.get("images", [])
        meta_data: list[dict] = product.get("meta_data", [])
        async with get_async_session() as session:
            row = await session.get(
                Projection, {"company_id": ctx.company_id, "entity_id": entity_id}
            )
            if row is None:
                # Backward-compatible helper boundary for older callers that pass a
                # SKU. Never choose arbitrarily when same-SKU physical rows exist.
                from sqlalchemy import func, select
                matches = (await session.execute(
                    select(Projection).where(
                        Projection.company_id == ctx.company_id,
                        Projection.entity_type == "item",
                        func.lower(Projection.state["sku"].as_string())
                        == str(entity_id).strip().lower(),
                    )
                )).scalars().all()
                if len(matches) != 1:
                    return
                row = matches[0]
            if row.entity_type != "item":
                return
            for i, img in enumerate(images):
                src = img.get("src")
                if not src:
                    continue
                await download_and_emit_file(
                    session, ctx.company_id, row.entity_id, "system", src,
                    img.get("name") or f"product-{i}.jpg",
                    "product_images", is_hero=(i == 0),
                )
            meta = {m["key"]: m["value"] for m in meta_data if m.get("key")}
            for tag_key in _CERT_TAGS:
                raw = meta.get(tag_key)
                if not raw:
                    continue
                try:
                    certs = json.loads(raw) if isinstance(raw, str) else raw
                    for cert in (certs if isinstance(certs, list) else []):
                        if cert.get("url"):
                            await download_and_emit_file(
                                session, ctx.company_id, row.entity_id, "system",
                                cert["url"], cert.get("name", "cert.pdf"),
                                tag_key, is_hero=False,
                            )
                except Exception as exc:
                    log.warning("woocommerce: failed to pull cert metafield %s: %s", tag_key, exc)
            await session.commit()


    async def ensure_product_link(self, ctx: ConnectorContext, entity_id: str, actor_id=None) -> str:
        """Enable one item, linking or creating a simple WooCommerce product safely."""
        from decimal import Decimal
        from celerp.db import SessionLocal as AsyncSessionLocal
        from celerp.models.connector_config import ConnectorConfig
        from sqlalchemy import select
        from celerp_inventory.services import (
            aggregate_sellable_quantity_for_anchor, external_link_for_state,
            resolve_catalog_anchor_for_item, set_external_link, set_external_link_state,
        )
        async with AsyncSessionLocal() as session:
            anchor = await resolve_catalog_anchor_for_item(session, ctx.company_id, entity_id)
            state = dict(anchor.state or {})
            anchor_id = anchor.entity_id
            sku = str(state.get("sku") or "").strip()
            if not sku:
                raise ValueError("A SKU is required before this item can sync with WooCommerce")
            link = external_link_for_state(state, "woocommerce")
            qty = await aggregate_sellable_quantity_for_anchor(
                session, ctx.company_id, anchor
            )
            config = await session.scalar(
                select(ConnectorConfig).where(
                    ConnectorConfig.company_id == str(ctx.company_id),
                    ConnectorConfig.connector == "woocommerce",
                ).limit(1)
            )
            allow_create = _direction_allows_remote_product_create(
                config.direction if config is not None else None
            )
        base_url, auth = _base_url(ctx), _auth(ctx)
        remote: dict | None = None
        rediscover = _link_needs_rediscovery(link)
        if link and not rediscover:
            item = {"woocommerce_product_id": link.get("product_id"),
                    "woocommerce_variation_id": link.get("variation_id")}
            async with _http_client() as client:
                resp = await client.get(f"{base_url}{self._product_path(item)}", auth=auth)
            if resp.status_code == 404:
                async with AsyncSessionLocal() as session:
                    await set_external_link_state(
                        session, ctx.company_id, anchor_id, "woocommerce",
                        remote_deleted=True,
                        expected_identity=(
                            str(link.get("product_id") or ""),
                            str(link.get("variation_id"))
                            if link.get("variation_id") not in (None, "")
                            else None,
                        ),
                        actor_id=actor_id, source="connector_ui",
                    )
                    await session.commit()
                rediscover = True
            else:
                resp.raise_for_status()
                remote = resp.json()
        if remote is None:
            if _deleted_variation_requires_import(link):
                raise ValueError(
                    "The linked WooCommerce variation no longer exists; "
                    "run product sync after recreating/importing the exact variation"
                )
            async with _http_client() as client:
                resp = await client.get(f"{base_url}/products", auth=auth, params={"sku": sku, "per_page": 100})
                resp.raise_for_status()
                exact = [p for p in resp.json() if str(p.get("sku") or "").strip().casefold() == sku.casefold()]
                if len(exact) > 1:
                    raise ValueError(f"WooCommerce has multiple products with SKU {sku!r}")
                if exact:
                    remote = exact[0]
                    if remote.get("type") == "variable":
                        raise ValueError("This SKU belongs to a variable WooCommerce product; import the exact variation first")
                else:
                    if not allow_create:
                        raise ValueError(
                            f"No WooCommerce product with SKU {sku!r} exists; "
                            "this connector direction does not allow publishing new products"
                        )
                    stocked = str(state.get("inventory_type") or "stocked") == "stocked"
                    q = Decimal(str(qty))
                    if stocked and q != q.to_integral_value():
                        raise ValueError(f"Fractional stock {q} cannot be published to WooCommerce without losing quantity")
                    payload: dict = {"name": state.get("name") or sku, "sku": sku, "type": "simple",
                                     "status": "publish", "description": state.get("description") or "",
                                     "manage_stock": stocked}
                    price = state.get("sale_price", state.get("retail_price"))
                    if price is not None: payload["regular_price"] = str(price)
                    if stocked: payload["stock_quantity"] = int(q)
                    create = await client.post(f"{base_url}/products", auth=auth, json=payload)
                    create.raise_for_status()
                    remote = create.json()
        if not remote or remote.get("id") in (None, ""):
            raise ValueError("WooCommerce did not return a product identity")
        if remote.get("type") == "variable":
            raise ValueError("A variable parent cannot be linked as a sellable catalog item; import an exact variation instead")
        new_link = {"product_id": str(remote["id"]), "sync_enabled": True,
                    "remote_deleted": False, "manage_stock": remote.get("manage_stock")}
        async with AsyncSessionLocal() as session:
            from celerp_docs.doc_service import woocommerce_products_awaiting_reconciliation

            new_link["inventory_sync_paused"] = anchor_id in (
                await woocommerce_products_awaiting_reconciliation(session, ctx.company_id)
            )
            if link and not rediscover:
                await set_external_link_state(
                    session, ctx.company_id, anchor_id, "woocommerce",
                    sync_enabled=True, remote_deleted=False,
                    link_updates={
                        "manage_stock": remote.get("manage_stock"),
                        "inventory_sync_paused": new_link["inventory_sync_paused"],
                    },
                    expected_identity=(
                        str(link.get("product_id") or ""),
                        str(link.get("variation_id"))
                        if link.get("variation_id") not in (None, "")
                        else None,
                    ),
                    actor_id=actor_id, source="connector_ui",
                )
            else:
                await set_external_link(
                    session, ctx.company_id, anchor_id, "woocommerce", new_link,
                    expected_sku=sku,
                    expected_identity=(
                        (
                            str(link.get("product_id") or ""),
                            str(link.get("variation_id"))
                            if link.get("variation_id") not in (None, "")
                            else None,
                        )
                        if link else None
                    ),
                    require_unlinked=not bool(link),
                    actor_id=actor_id, source="connector_ui",
                )
            await session.commit()
        return anchor_id

    # -- Orders ----------------------------------------------------------------

    async def sync_orders(
        self,
        ctx: ConnectorContext,
        since: datetime | None = None,
        attention: list[dict] | None = None,
        reconcile: bool = False,
    ) -> SyncResult:
        """Pull WooCommerce orders -> Celerp documents.

        An order the import cannot complete (unmapped product, no stock to
        draw, a refund to reconcile) is not a sync failure: it goes on the
        result's attention list for a person, the run still succeeds and the
        watermark advances. Entries carried from the previous run are fetched
        by id and retried first; one that imports drops off, one that still
        fails stays with its current reason, and an imported order WooCommerce
        no longer returns stays on the list, held for a person, saying the
        order is gone; Celerp never voids it. An entry a person marked
        reconciled stays, with its Undo, while the order in WooCommerce is
        still the state they reviewed; once it changes the mark goes and the
        order is imported again as any other. The daily ``reconcile`` pass
        also checks that every imported order still exists in the store."""
        result = SyncResult(entity=SyncEntity.ORDERS)
        carried = {
            str(entry.get("id")): entry for entry in (attention or []) if entry.get("id")
        }
        pending: dict[str, dict] = {}
        processed: set[str] = set()

        async def _import(order: dict) -> None:
            order_id = str(order.get("id"))
            processed.add(order_id)
            entry = carried.get(order_id)
            if (
                entry and entry.get("reconciled")
                and entry.get("signature") == _upsert.woocommerce_reconciliation_signature(order)
            ):
                pending[order_id] = entry
            try:
                result.record(await _upsert.upsert_order_from_woocommerce(ctx.company_id, order))
            except Exception as exc:
                log.warning("woocommerce.sync_orders order %s needs attention: %s", order_id, exc)
                pending[order_id] = {
                    "id": order_id,
                    "label": f"Order {order.get('number') or order_id}",
                    "reason": str(exc),
                }
                # A change only a person can reconcile carries the signature
                # they mark reconciled; other reasons clear once data is fixed.
                if getattr(exc, "signature", None):
                    pending[order_id]["signature"] = exc.signature

        carried_ids = list(carried)
        for start in range(0, len(carried_ids), _PER_PAGE):
            chunk = carried_ids[start:start + _PER_PAGE]
            try:
                retried = await self._paginate(
                    ctx, "/orders", params={"include": ",".join(chunk)}
                )
            except (httpx.HTTPStatusError, ValueError) as exc:
                result.errors = [f"WooCommerce API error: {exc}"]
                # Orders already retried keep their new outcome; the rest
                # stay as carried.
                result.attention = [
                    pending.get(order_id, entry) for order_id, entry in carried.items()
                    if order_id in pending or order_id not in processed
                ]
                return result
            for order in retried:
                await _import(order)
        async def _hold_missing(order_ids: list[str]) -> None:
            for order_id in order_ids:
                processed.add(order_id)
                entry = await _upsert.hold_missing_woocommerce_order(ctx.company_id, order_id)
                if entry is not None:
                    pending[order_id] = entry

        await _hold_missing([order_id for order_id in carried if order_id not in processed])

        params: dict = {}
        if since:
            params["modified_after"] = since.isoformat()
            params["dates_are_gmt"] = "true"  # our watermark is UTC; make Woo interpret it as UTC

        try:
            orders = await self._paginate(ctx, "/orders", params=params or None)
        except (httpx.HTTPStatusError, ValueError) as exc:
            result.errors = [f"WooCommerce API error: {exc}"]
            result.attention = list(pending.values())
            return result

        for order in orders:
            if str(order.get("id")) in processed:
                continue
            await _import(order)

        if reconcile:
            unchecked = [
                order_id
                for order_id in await _upsert.list_imported_woocommerce_order_ids(ctx.company_id)
                if order_id not in processed
            ]
            for start in range(0, len(unchecked), _PER_PAGE):
                chunk = unchecked[start:start + _PER_PAGE]
                try:
                    present = await self._paginate(
                        ctx, "/orders", params={"include": ",".join(chunk), "_fields": "id"}
                    )
                except (httpx.HTTPStatusError, ValueError) as exc:
                    result.errors = [f"WooCommerce API error: {exc}"]
                    break
                present_ids = {str(order.get("id")) for order in present}
                await _hold_missing([order_id for order_id in chunk if order_id not in present_ids])

        result.attention = list(pending.values())
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
                result.record(await _upsert.upsert_contact_from_woocommerce(ctx.company_id, customer))
            except Exception as exc:
                errors.append(f"Customer {customer.get('id')}: {exc}")

        result.errors = errors or None
        log.info(
            "woocommerce.sync_contacts company=%s created=%d skipped=%d",
            ctx.company_id, result.created, result.skipped,
        )
        return result


    async def sync_products_out(self, ctx: ConnectorContext) -> SyncResult:
        """Push enabled Celerp catalog product fields to WooCommerce."""
        from celerp.connectors.images import build_platform_image_payload, build_platform_cert_payload

        result = SyncResult(entity=SyncEntity.PRODUCTS, direction=SyncDirection.OUTBOUND)
        errors: list[str] = []
        try:
            items = await _upsert.list_items_modified_since_last_sync(
                ctx.company_id, platform="woocommerce"
            )
        except Exception as exc:
            result.errors = [f"Failed to load items: {exc}"]
            return result

        base_url, auth = _base_url(ctx), _auth(ctx)
        async with _http_client() as client:
            for item in items:
                product_id = item.get("woocommerce_product_id")
                if not product_id:
                    result.skipped += 1
                    continue
                try:
                    patch: dict = {}
                    is_variation = bool(item.get("woocommerce_variation_id"))
                    if item.get("sku"):
                        patch["sku"] = item["sku"]
                    if item.get("description") is not None:
                        patch["description"] = item["description"]
                    if item.get("sale_price") is not None:
                        patch["regular_price"] = str(item["sale_price"])
                    if not is_variation and item.get("name"):
                        patch["name"] = item["name"]

                    files = item.get("files") or []
                    image_payload = build_platform_image_payload(files)
                    if image_payload["hero_url"]:
                        if is_variation:
                            patch["image"] = {"src": image_payload["hero_url"]}
                        else:
                            patch["images"] = [{"src": image_payload["hero_url"]}] + [
                                {"src": u} for u in image_payload["additional_urls"]
                            ]
                    cert_payload = build_platform_cert_payload(files)
                    if cert_payload:
                        patch["meta_data"] = [
                            {"key": key, "value": json.dumps(certs)}
                            for key, certs in cert_payload.items()
                        ]
                    if not patch:
                        result.skipped += 1
                        continue
                    resp = await client.put(
                        f"{base_url}{self._product_path(item)}", auth=auth, json=patch
                    )
                    resp.raise_for_status()
                    result.updated += 1
                except Exception as exc:
                    errors.append(f"WooCommerce product {product_id}: {exc}")

        result.errors = errors or None
        return result

    async def _sync_inventory_items_out(
        self, ctx: ConnectorContext, items: list[dict], *, max_retries: int = 3
    ) -> SyncResult:
        """Push the supplied already-resolved Woo inventory rows."""
        from decimal import Decimal, InvalidOperation

        result = SyncResult(entity=SyncEntity.INVENTORY, direction=SyncDirection.OUTBOUND)
        errors: list[str] = []
        base_url, auth = _base_url(ctx), _auth(ctx)
        async with _http_client(max_retries=max_retries) as client:
            for item in items:
                product_id = item.get("woocommerce_product_id")
                if not product_id:
                    result.skipped += 1
                    continue
                link = item.get("external_link") or {}
                manage_stock = link.get("manage_stock")
                if manage_stock is False:
                    result.skipped += 1
                    continue
                if manage_stock == "parent":
                    errors.append(
                        f"WooCommerce variation {item.get('woocommerce_variation_id')}: "
                        "stock is managed by its parent product; per-variation stock was not changed"
                    )
                    continue
                if manage_stock is not True:
                    errors.append(
                        f"WooCommerce product {product_id}: stock-management mode is unknown; "
                        "run an inbound product sync before pushing inventory"
                    )
                    continue
                try:
                    qty = Decimal(str(item.get("quantity", 0)))
                except (InvalidOperation, ValueError):
                    errors.append(f"WooCommerce product {product_id}: invalid stock quantity")
                    continue
                if qty != qty.to_integral_value():
                    errors.append(
                        f"WooCommerce product {product_id}: fractional stock {qty} cannot be "
                        "sent to WooCommerce without losing quantity"
                    )
                    continue
                try:
                    resp = await client.put(
                        f"{base_url}{self._product_path(item)}",
                        auth=auth,
                        json={"stock_quantity": int(qty)},
                    )
                    resp.raise_for_status()
                    result.updated += 1
                except Exception as exc:
                    errors.append(f"WooCommerce product {product_id}: {exc}")
        result.errors = errors or None
        return result

    async def sync_inventory_out(self, ctx: ConnectorContext) -> SyncResult:
        """Push aggregate Celerp sellable stock to every enabled WooCommerce link."""
        try:
            items = await _upsert.list_items_with_external_id(
                ctx.company_id, platform="woocommerce"
            )
        except Exception as exc:
            result = SyncResult(
                entity=SyncEntity.INVENTORY, direction=SyncDirection.OUTBOUND
            )
            result.errors = [f"Failed to load inventory: {exc}"]
            return result
        return await self._sync_inventory_items_out(ctx, items)

    async def sync_inventory_identity_out(
        self, ctx: ConnectorContext, identity: str
    ) -> SyncResult:
        """Push one queued Woo product identity using freshly aggregated Celerp stock."""
        try:
            items = await _upsert.list_items_with_external_id(
                ctx.company_id, platform="woocommerce"
            )
        except Exception as exc:
            result = SyncResult(
                entity=SyncEntity.INVENTORY, direction=SyncDirection.OUTBOUND
            )
            result.errors = [f"Failed to load inventory: {exc}"]
            return result

        product_id, sep, variation_id = identity.partition(":")
        selected = [
            item for item in items
            if str(item.get("woocommerce_product_id") or "") == product_id
            and (
                str(item.get("woocommerce_variation_id") or "")
                == (variation_id if sep else "")
            )
        ]
        if not selected:
            return SyncResult(
                entity=SyncEntity.INVENTORY,
                direction=SyncDirection.OUTBOUND,
                skipped=1,
            )
        return await self._sync_inventory_items_out(
            ctx, selected, max_retries=0
        )

    # -- Webhook lifecycle -----------------------------------------------------

    _WEBHOOK_TOPICS = [
        "product.created", "product.updated", "product.deleted",
        "order.created", "order.updated", "order.deleted",
        "customer.created", "customer.updated",
    ]

    async def handle_product_deleted(self, ctx: ConnectorContext, payload: dict) -> None:
        """Mark an exact deleted variation or every linked variation under a deleted parent."""
        from sqlalchemy import select

        from celerp.db import SessionLocal as AsyncSessionLocal
        from celerp.models.projections import Projection
        from celerp_inventory.services import (
            external_link_for_state,
            set_external_link_state,
        )

        remote_id = payload.get("id")
        parent_id = payload.get("parent_id")
        if remote_id in (None, ""):
            return

        is_variation = parent_id not in (None, "", 0, "0")
        product_id = str(parent_id) if is_variation else str(remote_id)
        variation_id = str(remote_id) if is_variation else None

        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                select(Projection).where(
                    Projection.company_id == ctx.company_id,
                    Projection.entity_type == "item",
                )
            )).scalars().all()
            matches = [
                row for row in rows
                if _link_matches_deleted_product(
                    external_link_for_state(row.state or {}, "woocommerce"),
                    product_id,
                    variation_id,
                )
            ]
            for row in matches:
                observed = external_link_for_state(
                    row.state or {}, "woocommerce"
                )
                await set_external_link_state(
                    session, ctx.company_id, row.entity_id, "woocommerce",
                    remote_deleted=True,
                    expected_identity=(
                        str(observed.get("product_id") or ""),
                        str(observed.get("variation_id"))
                        if observed.get("variation_id") not in (None, "")
                        else None,
                    ),
                    source="connector",
                )
            if matches:
                await session.commit()


    async def register_webhooks(
        self, ctx: ConnectorContext, webhook_url: str, secret: str | None = None
    ) -> list[str]:
        """Register the full WooCommerce webhook set atomically."""
        base_url = _base_url(ctx)
        auth = _auth(ctx)
        ids: list[str] = []
        async with _http_client() as client:
            try:
                for topic in self._WEBHOOK_TOPICS:
                    body = {
                        "name": f"Celerp {topic}",
                        "topic": topic,
                        "delivery_url": webhook_url,
                        "status": "active",
                    }
                    if secret:
                        body["secret"] = secret
                    resp = await client.post(f"{base_url}/webhooks", auth=auth, json=body)
                    resp.raise_for_status()
                    webhook_id = str(resp.json().get("id") or "")
                    if not webhook_id:
                        raise RuntimeError(f"WooCommerce did not return a webhook id for {topic}")
                    ids.append(webhook_id)
            except Exception:
                for webhook_id in reversed(ids):
                    try:
                        cleanup = await client.delete(
                            f"{base_url}/webhooks/{webhook_id}", auth=auth, params={"force": "true"}
                        )
                        if cleanup.status_code not in (200, 204, 404):
                            log.warning(
                                "woocommerce webhook rollback failed id=%s status=%d",
                                webhook_id, cleanup.status_code,
                            )
                    except Exception:
                        log.warning(
                            "woocommerce webhook rollback failed id=%s",
                            webhook_id, exc_info=True,
                        )
                raise
        return ids

    async def deregister_webhooks(
        self, ctx: ConnectorContext, webhook_ids: list[str]
    ) -> None:
        """Delete all known WooCommerce hooks; 404 means the hook is already gone."""
        base_url = _base_url(ctx)
        auth = _auth(ctx)
        errors: list[str] = []
        async with _http_client() as client:
            for webhook_id in webhook_ids:
                try:
                    resp = await client.delete(
                        f"{base_url}/webhooks/{webhook_id}", auth=auth, params={"force": "true"}
                    )
                    if resp.status_code not in (200, 204, 404):
                        errors.append(f"{webhook_id}: HTTP {resp.status_code}")
                except Exception as exc:
                    errors.append(f"{webhook_id}: {exc}")
        if errors:
            raise RuntimeError("WooCommerce webhook cleanup failed: " + "; ".join(errors))
