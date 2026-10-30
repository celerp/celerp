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
    EntityFlow,
    FirstSync,
    RemoteOrder,
    RemoteProduct,
    SyncDirection,
    SyncEntity,
    SyncResult,
    contact_matches,
    direction_label,
    entity_allowed,
    store_holds_records,
)
from ui.i18n import t
import celerp.connectors.upsert as _upsert
from celerp.modules.boundary import needs_modules

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


_WEBHOOK_NAME_PREFIX = "Celerp "


def webhook_delivery_url() -> str:
    """Where the store delivers Celerp's WooCommerce webhooks."""
    from celerp.gateway.state import relay_http_url

    return f"{relay_http_url().rstrip('/')}/webhooks/woocommerce/events"


# Longest store error text kept with a failed record.
_ERROR_TEXT_LIMIT = 300


def _raise_for_status(resp: httpx.Response) -> None:
    """Raise for a failed store reply, keeping WooCommerce's own error code and message
    (trimmed) so the person sees why it refused. Credentials travel only in the
    Basic auth header and never appear here."""
    if resp.is_success:
        return
    try:
        body = resp.json()
    except ValueError:
        body = None
    reason = resp.reason_phrase or "error"
    if isinstance(body, dict) and (body.get("code") or body.get("message")):
        reason = ": ".join(str(part) for part in (body.get("code"), body.get("message")) if part)
    text = f"HTTP {resp.status_code} {reason}"
    if len(text) > _ERROR_TEXT_LIMIT:
        text = text[:_ERROR_TEXT_LIMIT - 1] + "…"
    raise httpx.HTTPStatusError(text, request=resp.request, response=resp)


def _gmt(value) -> datetime | None:
    """A WooCommerce *_gmt timestamp (no zone, in UTC) as an aware datetime."""
    from datetime import timezone

    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _modified(raw: dict) -> datetime | None:
    return _gmt(raw.get("date_modified_gmt"))


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

class LinkNeedsConfirmation(ValueError):
    """WooCommerce already has a product with the item's SKU; linking it hands its
    listing to Celerp, so a person confirms it first."""

    def __init__(self, connector, remote: dict) -> None:
        self.remote_id = str(remote.get("id"))
        self.remote_name = str(remote.get("name") or remote.get("sku") or self.remote_id)
        super().__init__(t("connectors.link_confirm", name=self.remote_name, service=connector.display_name))


# The WooCommerce field each sent item field goes to.
_WOO_FIELDS = {"sku": "sku", "description": "description", "sale_price": "regular_price", "name": "name"}


def _made_up_sku(item: dict) -> bool:
    """Whether the item's SKU is the one an earlier import made up from the product id."""
    product_id, variation_id = item.get("woocommerce_product_id"), item.get("woocommerce_variation_id")
    made_up = f"WC-{product_id}" + (f"-{variation_id}" if variation_id else "")
    return str(item.get("sku") or "") == made_up


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
    reviewed_first_sync = True
    category = ConnectorCategory.WEBSITE
    # The store's customer list is never imported: a customer is created only as the
    # customer of an imported order.
    imported_doc_marker = "woocommerce_order_id"
    entity_flows = {
        "products": EntityFlow(inbound=("products",), outbound=("products_out",)),
        "stock": EntityFlow(outbound=("inventory_out",)),
        "orders": EntityFlow(inbound=("orders",)),
        "customers": EntityFlow(),
    }
    # A new connection publishes Celerp's catalog and stock and takes in new orders.
    default_directions = {
        "products": "outbound", "stock": "outbound", "orders": "inbound", "customers": "off",
    }
    conflict_strategy = {
        "products": "external_wins",
        "orders": "external_wins",
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
                _raise_for_status(resp)
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

    @needs_modules("celerp-inventory")
    async def sync_products(
        self,
        ctx: ConnectorContext,
        since: datetime | None = None,
        reconcile: bool = False,
        attention: list[dict] | None = None,
        first_sync: FirstSync | None = None,
    ) -> SyncResult:
        """Pull WooCommerce products into Celerp catalog product anchors. The
        daily ``reconcile`` pass pulls every product, so ones deleted in the
        store are found.

        A product with no SKU is not imported; it waits on a person to give it one
        in WooCommerce. A linked product changed in both systems keeps Celerp's
        values and waits on a person with both. Entries for products this run did
        not read are carried. ``first_sync`` carries the first sync's choices: whether
        new products are created, whether SKU matches keep Celerp's values, and the
        import batch created items join; what it did is recorded on it."""
        result = SyncResult(entity=SyncEntity.PRODUCTS)
        errors: list[str] = []
        pending: dict[str, dict] = {}
        processed: set[str] = set()
        if reconcile or first_sync is not None:
            since = None
        full_scan = since is None
        seen_identities: set[tuple[str, str | None]] = set()
        params: dict = {}
        if since:
            params["modified_after"] = since.isoformat()
            params["dates_are_gmt"] = "true"

        try:
            products, read_errors, incomplete_parents = await self._store_products(ctx, params)
        except (httpx.HTTPStatusError, ValueError) as exc:
            result.errors = [f"WooCommerce API error: {exc}"]
            return result
        errors += read_errors

        for remote in products:
            seen_identities.add((remote.product_id, remote.variation_id))
            processed.add(remote.key)
            manage_stock = remote.extra["manage_stock"]
            stock_quantity = remote.extra["stock_quantity"]
            non_stock_virtual = remote.extra["virtual"] and manage_stock is False
            entity_id = await _upsert.import_store_product(
                ctx, self, key=remote.key, label=remote.name, result=result, pending=pending,
                first_sync=first_sync, errors=errors,
                product_id=remote.product_id,
                variation_id=remote.variation_id,
                sku=remote.sku,
                name=remote.name or remote.sku,
                description=remote.description,
                sale_price=remote.price,
                quantity=float(stock_quantity) if stock_quantity is not None else None,
                seed_quantity=(manage_stock is True),
                link_fields={"manage_stock": manage_stock},
                inventory_type="service" if non_stock_virtual else None,
                sell_by="service" if non_stock_virtual else None,
            )
            if entity_id:
                try:
                    await self._pull_product_files(ctx, remote.extra["files"], entity_id, first_sync)
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
        result.attention = _upsert.carried_attention(attention, pending, processed)
        return result

    async def _store_products(
        self, ctx: ConnectorContext, params: dict
    ) -> tuple[list[RemoteProduct], list[str], set[str]]:
        """Every sellable product the store returns for ``params``, a variable product's
        variations each as their own: the products, the errors reading variations,
        and the parents whose variations could not be read."""
        products = await self._paginate(ctx, "/products", params=params or None)
        out: list[RemoteProduct] = []
        errors: list[str] = []
        incomplete: set[str] = set()

        def _price(raw: dict):
            price = money(raw.get("regular_price"))
            return price if price is not None else money(raw.get("price"))

        for product in products:
            pid = str(product.get("id"))
            name = product.get("name") or ""
            description = product.get("description") or ""
            if product.get("type") == "variable":
                try:
                    variations = await self._paginate(ctx, f"/products/{pid}/variations")
                except (httpx.HTTPStatusError, ValueError) as exc:
                    errors.append(f"Product {pid} variations: {exc}")
                    incomplete.add(pid)
                    continue
                for var in variations:
                    vid = str(var.get("id"))
                    opts = " / ".join(
                        str(a.get("option", "")) for a in (var.get("attributes") or [])
                        if a.get("option")
                    )
                    var_img = var.get("image")
                    out.append(RemoteProduct(
                        key=f"{pid}:{vid}", product_id=pid, variation_id=vid,
                        sku=(var.get("sku") or "").strip(),
                        name=f"{name} - {opts}" if opts else name,
                        description=var.get("description") or description,
                        price=_price(var), modified=_modified(var),
                        extra={
                            "manage_stock": var.get("manage_stock"),
                            "stock_quantity": var.get("stock_quantity"),
                            "virtual": bool(var.get("virtual", product.get("virtual", False))),
                            "files": {
                                "images": ([var_img] if var_img and var_img.get("src") else [])
                                + product.get("images", []),
                                "meta_data": product.get("meta_data", []),
                            },
                        },
                    ))
                continue
            out.append(RemoteProduct(
                key=pid, product_id=pid, variation_id=None,
                sku=(product.get("sku") or "").strip(), name=name, description=description,
                price=_price(product), modified=_modified(product),
                extra={
                    "manage_stock": product.get("manage_stock"),
                    "stock_quantity": product.get("stock_quantity"),
                    "virtual": bool(product.get("virtual", False)),
                    "files": product,
                },
            ))
        return out, errors, incomplete

    async def preview_products(self, ctx: ConnectorContext) -> list[RemoteProduct]:
        products, errors, _ = await self._store_products(ctx, {})
        if errors:
            raise ValueError("; ".join(errors))
        return products

    @needs_modules("celerp-docs")
    async def preview_orders(self, ctx: ConnectorContext, orders_from: datetime) -> list[RemoteOrder]:
        from celerp_docs.doc_service import WOOCOMMERCE_ISSUED_STATUSES

        orders = await self._paginate(ctx, "/orders", params={
            "after": orders_from.isoformat(), "dates_are_gmt": "true",
            "_fields": "id,number,status,date_created_gmt,total,currency",
        })
        return [RemoteOrder(
            id=str(o.get("id")), number=str(o.get("number") or o.get("id")),
            status=str(o.get("status") or ""), created=_gmt(o.get("date_created_gmt")),
            total=money(o.get("total")), currency=o.get("currency"),
            posts=str(o.get("status") or "") in WOOCOMMERCE_ISSUED_STATUSES,
        ) for o in orders]

    @needs_modules("celerp-inventory")
    async def _reconcile_missing_product_links(
        self,
        ctx: ConnectorContext,
        seen: set[tuple[str, str | None]],
        incomplete_parents: set[str],
    ) -> int:
        from sqlalchemy import select
        from celerp.db import SessionLocal as AsyncSessionLocal
        from celerp.models.projections import Projection
        from celerp.connectors.product_links import external_link_for_state
        from celerp_inventory.services import set_external_link_state

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

    async def _pull_product_files(
        self, ctx: ConnectorContext, product: dict[str, Any], entity_id: str,
        first_sync: FirstSync | None = None,
    ) -> None:
        """Pull images and certificate metafields onto an already-resolved item. Each
        image is pulled once, recognised by its WooCommerce image id. The files a first
        sync pulls onto an item it created join its import, so its undo removes them."""
        from celerp.db import get_session_ctx as get_async_session
        from celerp.models.projections import Projection
        from celerp.connectors.images import download_and_emit_file, _CERT_TAGS

        images: list[dict] = product.get("images", [])
        meta_data: list[dict] = product.get("meta_data", [])
        async with get_async_session() as session:
            row = await session.get(
                Projection, {"company_id": ctx.company_id, "entity_id": entity_id}
            )
            if row is None or row.entity_type != "item":
                return
            created = first_sync is not None and any(
                eid == entity_id for eid, _ in first_sync.record.created)
            keys: list[str | None] = []
            for i, img in enumerate(images):
                src = img.get("src")
                if not src:
                    continue
                keys.append(await download_and_emit_file(
                    session, ctx.company_id, row.entity_id, src,
                    img.get("name") or f"product-{i}.jpg",
                    "product_images", is_hero=(i == 0),
                    platform=self.name, store=ctx.store_handle, remote_id=img.get("id"),
                ))
            meta = {m["key"]: m["value"] for m in meta_data if m.get("key")}
            for tag_key in _CERT_TAGS:
                raw = meta.get(tag_key)
                if not raw:
                    continue
                try:
                    certs = json.loads(raw) if isinstance(raw, str) else raw
                    for cert in (certs if isinstance(certs, list) else []):
                        if cert.get("url"):
                            keys.append(await download_and_emit_file(
                                session, ctx.company_id, row.entity_id,
                                cert["url"], cert.get("name", "cert.pdf"),
                                tag_key, is_hero=False,
                                platform=self.name, store=ctx.store_handle,
                            ))
                except Exception as exc:
                    log.warning("woocommerce: failed to pull cert metafield %s: %s", tag_key, exc)
            if created:
                first_sync.record.files += [key for key in keys if key]
            await session.commit()


    @needs_modules("celerp-inventory")
    async def ensure_product_link(
        self, ctx: ConnectorContext, entity_id: str, actor_id=None, *, confirm_link: bool = False,
    ) -> str:
        """Publish one item: link it to its WooCommerce product, or create the product
        published. A linked product WooCommerce holds as a draft is published again.

        A product the store already has under the item's SKU is linked only with
        ``confirm_link``: linking hands its listing to Celerp, which replaces its
        name, description, price and images at the next sync, so it is asked first
        (LinkNeedsConfirmation). When the product was created but its link could not
        be saved, the error names the product so a person knows it is live; publishing
        again links it by SKU."""
        from decimal import Decimal
        from celerp.db import SessionLocal as AsyncSessionLocal
        from celerp.models.connector_config import ConnectorConfig
        from sqlalchemy import select
        from celerp.connectors.product_links import external_link_for_state
        from celerp_inventory.services import (
            aggregate_sellable_quantity_for_anchor,
            resolve_catalog_anchor_for_item,
            set_external_link,
            set_external_link_state,
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
            # Only a connection that sends products out may publish a new one.
            allow_create = config is not None and entity_allowed(
                self, "products_out", config.entity_directions
            )
            products_direction = SyncDirection(
                (config.entity_directions if config is not None else {}).get("products", "off")
            )
        base_url, auth = _base_url(ctx), _auth(ctx)
        remote: dict | None = None
        created = False
        rediscover = _link_needs_rediscovery(link)
        if link and not rediscover:
            item = {"woocommerce_product_id": link.get("product_id"),
                    "woocommerce_variation_id": link.get("variation_id")}
            async with _http_client() as client:
                resp = await client.get(f"{base_url}{self._product_path(item)}", auth=auth)
                if resp.status_code != 404:
                    _raise_for_status(resp)
                    remote = resp.json()
                    if remote.get("status") not in (None, "publish") and not link.get("variation_id"):
                        published = await client.put(
                            f"{base_url}{self._product_path(item)}", auth=auth, json={"status": "publish"})
                        _raise_for_status(published)
                        remote = published.json()
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
        if remote is None:
            if _deleted_variation_requires_import(link):
                raise ValueError(
                    "The linked WooCommerce variation no longer exists; "
                    "run product sync after recreating/importing the exact variation"
                )
            async with _http_client() as client:
                resp = await client.get(f"{base_url}/products", auth=auth, params={"sku": sku, "per_page": 100})
                _raise_for_status(resp)
                exact = [p for p in resp.json() if str(p.get("sku") or "").strip().casefold() == sku.casefold()]
                if len(exact) > 1:
                    raise ValueError(f"WooCommerce has multiple products with SKU {sku!r}")
                if exact:
                    remote = exact[0]
                    if remote.get("type") == "variable":
                        raise ValueError("This SKU belongs to a variable WooCommerce product; import the exact variation first")
                    if not confirm_link:
                        raise LinkNeedsConfirmation(self, remote)
                else:
                    if not allow_create:
                        raise ValueError(t(
                            "connectors.err_publish_needs_outbound", sku=sku,
                            direction=direction_label(self, products_direction),
                        ))
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
                    _raise_for_status(create)
                    remote = create.json()
                    created = True
        if not remote or remote.get("id") in (None, ""):
            raise ValueError("WooCommerce did not return a product identity")
        if remote.get("type") == "variable":
            raise ValueError("A variable parent cannot be linked as a sellable catalog item; import an exact variation instead")
        new_link = {"product_id": str(remote["id"]), "sync_enabled": True,
                    "remote_deleted": False, "manage_stock": remote.get("manage_stock"),
                    "remote_status": remote.get("status")}
        try:
            await self._save_link(
                ctx, anchor_id, sku, link, rediscover, new_link, actor_id,
                set_external_link=set_external_link, set_external_link_state=set_external_link_state,
            )
        except Exception as exc:
            if not created:
                raise
            raise ValueError(t(
                "connectors.err_link_save_failed", name=remote.get("name") or state.get("name") or sku,
                service=self.display_name, reason=exc,
            )) from exc
        return anchor_id

    async def _save_link(self, ctx, anchor_id, sku, link, rediscover, new_link, actor_id, *,
                         set_external_link, set_external_link_state) -> None:
        from celerp.db import SessionLocal as AsyncSessionLocal
        from celerp.connectors.product_links import woocommerce_products_awaiting_reconciliation

        async with AsyncSessionLocal() as session:
            new_link["inventory_sync_paused"] = anchor_id in (
                await woocommerce_products_awaiting_reconciliation(session, ctx.company_id)
            )
            if link and not rediscover:
                await set_external_link_state(
                    session, ctx.company_id, anchor_id, "woocommerce",
                    sync_enabled=True, remote_deleted=False,
                    link_updates={
                        "manage_stock": new_link["manage_stock"],
                        "inventory_sync_paused": new_link["inventory_sync_paused"],
                        "remote_status": new_link["remote_status"],
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

    @needs_modules("celerp-inventory")
    async def set_product_status(
        self, ctx: ConnectorContext, entity_id: str, status: str, actor_id=None
    ) -> None:
        """Unpublish (``draft``) or publish one linked product in WooCommerce. The link
        stays, so Publish puts it back; the product is never deleted."""
        from celerp.db import SessionLocal as AsyncSessionLocal
        from celerp_inventory.services import (
            resolve_catalog_anchor_for_item,
            set_external_link_state,
        )
        from celerp.connectors.product_links import external_link_for_state

        async with AsyncSessionLocal() as session:
            anchor = await resolve_catalog_anchor_for_item(session, ctx.company_id, entity_id)
            link = external_link_for_state(anchor.state or {}, "woocommerce")
        if not link or link.get("remote_deleted") is True:
            raise ValueError(t("connectors.err_not_published", service=self.display_name))
        item = {"woocommerce_product_id": link.get("product_id"),
                "woocommerce_variation_id": link.get("variation_id")}
        async with _http_client() as client:
            resp = await client.put(
                f"{_base_url(ctx)}{self._product_path(item)}", auth=_auth(ctx), json={"status": status})
            _raise_for_status(resp)
        async with AsyncSessionLocal() as session:
            await set_external_link_state(
                session, ctx.company_id, anchor.entity_id, "woocommerce",
                link_updates={"remote_status": resp.json().get("status", status)},
                actor_id=actor_id, source="connector_ui",
            )
            await session.commit()

    # -- Orders ----------------------------------------------------------------

    @needs_modules("celerp-docs")
    async def sync_orders(
        self,
        ctx: ConnectorContext,
        since: datetime | None = None,
        attention: list[dict] | None = None,
        reconcile: bool = False,
        orders_from: datetime | None = None,
    ) -> SyncResult:
        """Pull WooCommerce orders -> Celerp documents. Orders placed before
        ``orders_from``, the connection's earliest order date, are not fetched.

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
        if orders_from:
            params["after"] = orders_from.isoformat()
        if params:
            params["dates_are_gmt"] = "true"  # our dates are UTC; make Woo interpret them as UTC

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

    @needs_modules("celerp-inventory")
    async def sync_products_out(self, ctx: ConnectorContext) -> SyncResult:
        """Send linked Celerp products to WooCommerce.

        Only the fields Celerp changed since the link last agreed on them are sent
        (celerp.services.field_fingerprints); a product with nothing changed is not
        sent at all. A SKU an import made up is never sent unless a person changes it.
        Images go in a request of their own after the fields, by the store's own id
        when it holds them and by a short-lived public link otherwise, so an image the
        store refuses never holds back the fields; it waits on a person with the
        store's reason."""
        from celerp.connectors.images import product_image_files, store_images
        from celerp.services.field_fingerprints import LINK_KEY, changed_since, fingerprint

        result = SyncResult(entity=SyncEntity.PRODUCTS, direction=SyncDirection.OUTBOUND)
        errors: list[str] = []
        attention: list[dict] = []
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
                variation_id = item.get("woocommerce_variation_id")
                key = f"{product_id}:{variation_id}" if variation_id else str(product_id)
                label = item.get("name") or item.get("sku") or key
                link = dict(item.get("external_link") or {})
                agreed = dict(link.get(LINK_KEY) or {})
                fields = {
                    "sku": item.get("sku") or None,
                    "description": item.get("description"),
                    "sale_price": item.get("sale_price"),
                    "name": None if variation_id else (item.get("name") or None),
                }
                send = {k: v for k, v in fields.items() if v is not None and changed_since(link, k, v)}
                if "sku" in send and agreed.get("sku") is None and _made_up_sku(item):
                    del send["sku"]
                image_ids = [f["id"] for f in product_image_files(item.get("files") or [])]
                # An item with no images of its own never clears the store's.
                images_changed = bool(image_ids) and changed_since(link, "images", image_ids)
                if not send and not images_changed:
                    result.skipped += 1
                    continue

                url = f"{base_url}{self._product_path(item)}"
                updates: dict = {}
                if send:
                    try:
                        resp = await client.put(url, auth=auth, json={
                            _WOO_FIELDS[k]: str(v) if k == "sale_price" else v for k, v in send.items()
                        })
                        _raise_for_status(resp)
                    except Exception as exc:
                        errors.append(f"WooCommerce product {product_id}: {exc}")
                        continue
                    agreed.update({k: fingerprint(v) for k, v in send.items()})
                    updates["last_pushed_modified"] = resp.json().get("date_modified_gmt")
                if images_changed:
                    images, unshared = await store_images(ctx, self.name, item.get("files") or [], link)
                    if unshared:
                        attention.append({"id": key, "label": label, "kind": "image",
                                          "reason": t("connectors.attention_image_unshared",
                                                      service=self.display_name)})
                    else:
                        try:
                            resp = await client.put(url, auth=auth, json=(
                                {"image": images[0] if images else {}} if variation_id
                                else {"images": images}))
                            _raise_for_status(resp)
                        except Exception as exc:
                            attention.append({"id": key, "label": label, "kind": "image",
                                              "reason": t("connectors.attention_image_refused",
                                                          service=self.display_name, detail=exc)})
                        else:
                            body = resp.json()
                            returned = [body["image"]] if variation_id and body.get("image") else body.get("images") or []
                            updates["media_ids"] = {
                                **(link.get("media_ids") or {}),
                                **{fid: str(img["id"]) for fid, img in zip(image_ids, returned) if img.get("id")},
                            }
                            agreed["images"] = fingerprint(image_ids)
                            updates["last_pushed_modified"] = body.get("date_modified_gmt") or updates.get(
                                "last_pushed_modified")
                if not updates:
                    continue
                try:
                    await self._save_push(ctx, item, link, {LINK_KEY: agreed, **updates})
                except Exception as exc:
                    errors.append(f"WooCommerce product {product_id}: {exc}")
                    continue
                result.updated += 1

        result.errors = errors or None
        result.attention = attention
        return result

    @needs_modules("celerp-inventory")
    async def _save_push(self, ctx: ConnectorContext, item: dict, link: dict, updates: dict) -> None:
        """Record on the link what the store now holds, so the next run sends only changes."""
        from celerp.db import SessionLocal as AsyncSessionLocal
        from celerp_inventory.services import set_external_link_state

        async with AsyncSessionLocal() as session:
            await set_external_link_state(
                session, ctx.company_id, item["entity_id"], "woocommerce", link_updates=updates,
                expected_identity=(
                    str(link.get("product_id") or ""),
                    str(link.get("variation_id")) if link.get("variation_id") not in (None, "") else None,
                ),
            )
            await session.commit()

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
                    _raise_for_status(resp)
                    result.updated += 1
                except Exception as exc:
                    errors.append(f"WooCommerce product {product_id}: {exc}")
        result.errors = errors or None
        return result

    @needs_modules("celerp-inventory")
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

    @needs_modules("celerp-inventory")
    async def sync_inventory_identity_out(
        self, ctx: ConnectorContext, identity: str
    ) -> SyncResult:
        """Push one queued Woo product identity using freshly aggregated Celerp stock."""
        product_id, _, variation_id = identity.partition(":")
        try:
            selected = await _upsert.list_item_for_external_identity(
                ctx.company_id, "woocommerce", product_id, variation_id or None
            )
        except Exception as exc:
            result = SyncResult(
                entity=SyncEntity.INVENTORY, direction=SyncDirection.OUTBOUND
            )
            result.errors = [f"Failed to load inventory: {exc}"]
            return result
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

    def webhook_topics(self, directions: dict) -> list[str]:
        """The topics this connection listens to: orders when they come in, product
        changes when products come in, and deletions whenever products are linked."""
        topics: list[str] = []
        if entity_allowed(self, "orders", directions):
            topics += ["order.created", "order.updated", "order.deleted"]
        if entity_allowed(self, "products", directions):
            topics += ["product.created", "product.updated"]
        if directions.get("products", SyncDirection.OFF.value) != SyncDirection.OFF.value:
            topics.append("product.deleted")
        return topics

    @needs_modules("celerp-inventory")
    async def handle_product_deleted(self, ctx: ConnectorContext, payload: dict) -> None:
        """Mark an exact deleted variation or every linked variation under a deleted parent."""
        from sqlalchemy import select

        from celerp.db import SessionLocal as AsyncSessionLocal
        from celerp.models.projections import Projection
        from celerp.connectors.product_links import external_link_for_state
        from celerp_inventory.services import set_external_link_state

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


    async def list_webhooks(self, ctx: ConnectorContext, webhook_url: str) -> list[dict]:
        """Every hook named by Celerp on the store that delivers to `webhook_url`."""
        base_url = _base_url(ctx)
        auth = _auth(ctx)
        found: list[dict] = []
        async with _http_client() as client:
            page = 1
            while True:
                resp = await client.get(
                    f"{base_url}/webhooks", auth=auth,
                    params={"per_page": 100, "page": page},
                )
                _raise_for_status(resp)
                hooks = resp.json()
                if not isinstance(hooks, list):
                    raise RuntimeError("WooCommerce returned an invalid webhook list")
                found += [
                    hook for hook in hooks
                    if isinstance(hook, dict) and hook.get("id") is not None
                    and hook.get("delivery_url") == webhook_url
                    and str(hook.get("name") or "").startswith(_WEBHOOK_NAME_PREFIX)
                ]
                if len(hooks) < 100:
                    break
                page += 1
        return found

    async def create_webhook(
        self, ctx: ConnectorContext, webhook_url: str, topic: str, secret: str | None,
    ) -> dict:
        """Create one active hook for `topic`."""
        body = {"name": f"{_WEBHOOK_NAME_PREFIX}{topic}", "topic": topic,
                "delivery_url": webhook_url, "status": "active"}
        if secret:
            body["secret"] = secret
        async with _http_client() as client:
            resp = await client.post(f"{_base_url(ctx)}/webhooks", auth=_auth(ctx), json=body)
        _raise_for_status(resp)
        hook = resp.json()
        if not isinstance(hook, dict) or hook.get("id") is None:
            raise RuntimeError(f"WooCommerce did not return a webhook id for {topic}")
        return hook

    async def activate_webhook(self, ctx: ConnectorContext, webhook_id) -> dict:
        async with _http_client() as client:
            resp = await client.put(f"{_base_url(ctx)}/webhooks/{webhook_id}", auth=_auth(ctx),
                                    json={"status": "active"})
        _raise_for_status(resp)
        return resp.json()

    async def get_webhook(self, ctx: ConnectorContext, webhook_id) -> dict | None:
        async with _http_client() as client:
            resp = await client.get(f"{_base_url(ctx)}/webhooks/{webhook_id}", auth=_auth(ctx))
        if resp.status_code == 404:
            return None
        _raise_for_status(resp)
        return resp.json()

    async def delete_webhook(self, ctx: ConnectorContext, webhook_id) -> None:
        """Delete one hook; one already gone counts as deleted."""
        async with _http_client() as client:
            resp = await client.delete(f"{_base_url(ctx)}/webhooks/{webhook_id}", auth=_auth(ctx),
                                       params={"force": "true"})
        if resp.status_code not in (200, 204, 404):
            raise RuntimeError(f"WooCommerce webhook {webhook_id}: HTTP {resp.status_code}")

    async def deregister_webhooks(
        self, ctx: ConnectorContext, webhook_url: str, webhook_ids: list[str]
    ) -> None:
        """Delete every Celerp hook on the store: the known ids plus any hook
        named by Celerp that delivers to `webhook_url`. Raises unless each one is
        confirmed gone; 404 means it already was."""
        targets = {str(wid) for wid in webhook_ids}
        targets.update(str(hook["id"]) for hook in await self.list_webhooks(ctx, webhook_url))
        errors: list[str] = []
        for webhook_id in sorted(targets):
            try:
                await self.delete_webhook(ctx, webhook_id)
            except Exception as exc:
                errors.append(f"{webhook_id}: {exc}")
        if errors:
            raise RuntimeError("WooCommerce webhook cleanup failed: " + "; ".join(errors))
