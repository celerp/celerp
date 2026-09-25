# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

import uuid




def _f(v, default: float = 0.0) -> float:
    """Null-safe float. Missing/null/empty -> default; a real 0 stays 0.0."""
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _amt(v, currency: str | None) -> float:
    """Quantize a money AMOUNT to currency dp via Decimal — never raw float arithmetic.
    Blank/missing/non-numeric coerces to 0.0 (a bad total must not poison the whole
    run: a raised InvalidOperation would error the record forever and pin the watermark)."""
    from celerp.services.money import round_money, to_stored_float
    try:
        return to_stored_float(round_money(v, currency or ""))
    except (ArithmeticError, ValueError, TypeError):
        return 0.0


def _line_total(li_total, qty: float, unit_price: float, currency: str | None) -> float:
    """Line total from the platform value, else qty*unit_price computed in Decimal."""
    from celerp.services.money import to_decimal
    base = li_total if li_total not in (None, "") else to_decimal(qty) * to_decimal(unit_price)
    return _amt(base, currency)


async def upsert_order_from_shopify(company_id: str, order: dict) -> str:
    """
    Create/update a doc (invoice) from a Shopify order dict.
    Returns "created", "updated", or "noop".

    Idempotency key: shopify:order:{order_id}

    Mapping:
      order.name (#1001)      → ref_id
      order.financial_status  → status (paid → closed, else open)
      order.line_items        → line_items (name, quantity, price)
      order.total_price       → total
      order.id                → idempotency_key
    """
    from celerp.db import SessionLocal

    idem_key = f"shopify:order:{order['id']}"

    async with SessionLocal() as session:
        ref_id = order.get("name", f"shopify-{order['id']}")
        financial_status = order.get("financial_status", "pending")
        status = "closed" if financial_status == "paid" else "open"
        currency = order.get("currency")

        line_items = []
        for li in order.get("line_items", []):
            qty = _f(li.get("quantity"), 1)
            price = _f(li.get("price"))
            line_items.append({
                "name": li.get("title", ""),
                "quantity": qty,
                "unit_price": price,
                "line_total": _line_total(None, qty, price, currency),
            })
        total = _amt(order.get("total_price"), currency)

        data = {
            "doc_type": "invoice",
            "ref_id": ref_id,
            "status": status,
            "line_items": line_items,
            "total": total,
            "amount_outstanding": 0.0 if status == "closed" else total,
            "currency": currency,
            "shopify_order_id": str(order["id"]),
        }
        return await _emit_doc(session, company_id, data, idem_key)


async def list_unsynced_invoices(company_id: str, platform: str) -> list[dict]:
    """Native CelERP invoices that are candidates to push out to `platform`.

    Returns invoices that did NOT originate from an external platform (no
    *_order_id / *_invoice_id marker) and are not yet stamped as pushed to this
    platform ({platform}_invoice_id). The outbound push stamps that id via a
    doc.pushed event (see doc_projections) so a pushed invoice drops off this list
    on the next run and is never created twice.
    """
    import uuid as _uuid
    from celerp.db import SessionLocal
    from celerp.models.projections import Projection
    from sqlalchemy import select

    _IMPORTED_MARKERS = (
        "shopify_order_id", "woocommerce_order_id",
        "quickbooks_invoice_id", "xero_invoice_id",
    )
    cid = _uuid.UUID(str(company_id))
    out: list[dict] = []
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(Projection).where(
                Projection.company_id == cid,
                Projection.entity_type == "doc",
                Projection.state["doc_type"].as_string() == "invoice",
            )
        )).scalars().all()
        for r in rows:
            st = r.state or {}
            if any(st.get(m) for m in _IMPORTED_MARKERS):
                continue  # imported from a platform, not ours to push back
            if st.get(f"{platform}_invoice_id"):
                continue  # already pushed to this platform
            out.append({
                "entity_id": r.entity_id,
                "ref_id": st.get("ref_id") or st.get("doc_number"),
                "line_items": st.get("line_items") or [],
                "total": st.get("total"),
                "customer_name": st.get("customer_name"),
                "customer_external_id": st.get("customer_external_id"),
            })
    return out


async def mark_doc_pushed(
    company_id: str, entity_id: str, platform: str, external_id: str, entity: str = "invoice"
) -> None:
    """Outbound write-back: stamp the external id a platform returned onto the doc, so a
    re-run's list_unsynced_invoices skips it and it is never created on the platform twice.
    Idempotent per (platform, entity, external_id)."""
    from celerp.db import SessionLocal
    from celerp.events.engine import emit_event

    async with SessionLocal() as session:
        await emit_event(
            session, company_id=company_id, entity_id=entity_id, entity_type="doc",
            event_type="doc.pushed",
            data={"platform": platform, "external_id": str(external_id), "entity": entity},
            actor_id=None, location_id=None, source="connector",
            idempotency_key=f"{platform}:pushed:{entity}:{external_id}",
            metadata_={},
        )
        await session.commit()


async def _emit_doc(session, company_id: str, data: dict, idem_key: str) -> str:
    # connector_upsert keys the projection on idem_key (the unique platform id), NOT the
    # human ref_id/DocNumber — two source invoices can share a DocNumber (QB allows it)
    # and would otherwise collapse into one doc. A changed re-import updates the same doc.
    # Returns "created", "updated", or "noop".
    from celerp.events.engine import connector_upsert
    outcome = await connector_upsert(
        session, company_id=company_id, entity_type="doc",
        event_type="doc.created", idem_key=idem_key, data=data,
    )
    await session.commit()
    return outcome


def _woocommerce_commercial_fingerprint(order: dict) -> str:
    """Stable financial identity of a Woo order, excluding lifecycle status metadata."""
    import hashlib
    import json

    lines: dict[tuple[str, str, str], dict] = {}
    for li in order.get("line_items", []):
        key = (
            str(li.get("product_id") or ""),
            str(li.get("variation_id") or ""),
            str(li.get("sku") or "").strip().casefold(),
        )
        entry = lines.setdefault(key, {"quantity": 0.0, "total": 0.0, "total_tax": 0.0})
        entry["quantity"] += _f(li.get("quantity"), 0)
        entry["total"] += _f(li.get("total"), 0)
        entry["total_tax"] += _f(li.get("total_tax"), 0)
    payload = {
        "currency": order.get("currency"),
        "lines": sorted(((*k, v["quantity"], v["total"], v["total_tax"]) for k, v in lines.items())),
        "shipping": sorted(
            (str(x.get("method_id") or ""), str(x.get("method_title") or ""),
             _f(x.get("total"), 0), _f(x.get("total_tax"), 0))
            for x in order.get("shipping_lines", [])
        ),
        "fees": sorted(
            (str(x.get("name") or ""), _f(x.get("total"), 0), _f(x.get("total_tax"), 0))
            for x in order.get("fee_lines", [])
        ),
        "total_tax": _f(order.get("total_tax"), 0),
        "total": _f(order.get("total"), 0),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _woocommerce_reconciliation_signature(order: dict) -> str:
    """Identity of the order state a person reconciles by hand: its commercial
    fingerprint, its status and its refunds. Any later change produces a new
    signature, so the order needs attention again."""
    import hashlib
    import json

    payload = {
        "fingerprint": _woocommerce_commercial_fingerprint(order),
        "status": str(order.get("status") or "").lower(),
        "refunds": sorted(
            (str(r.get("id") or ""), _f(r.get("total"), 0))
            for r in order.get("refunds") or []
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


class WooCommerceReconciliationRequired(ValueError):
    """A WooCommerce order change Celerp will not apply on its own. A person
    reconciles it by hand and marks this ``signature`` reconciled."""

    def __init__(self, message: str, signature: str):
        super().__init__(message)
        self.signature = signature


class WooCommerceReconciliationChanged(ValueError):
    """The order changed since the person reviewed it."""


async def _lock_woocommerce_order(session, cid, order_id: str) -> None:
    """Serialize every change to one external order (webhook, sync, a person)."""
    from sqlalchemy import text

    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"woocommerce-order:{cid}:{order_id}"},
    )


async def _woocommerce_order_anchor_ids(session, cid, doc) -> list[str]:
    """The catalog products one WooCommerce order touches."""
    from celerp_inventory.services import resolve_catalog_anchor_for_item

    anchors: list[str] = []
    for li in (doc.state or {}).get("line_items", []):
        item_id = li.get("item_id") or li.get("entity_id")
        if not item_id:
            continue
        try:
            anchor = await resolve_catalog_anchor_for_item(session, cid, item_id)
        except ValueError:
            continue
        if anchor.entity_id not in anchors:
            anchors.append(anchor.entity_id)
    return anchors


async def woocommerce_products_awaiting_reconciliation(session, company_id) -> set[str]:
    """Catalog products on any WooCommerce order still waiting for a person to
    reconcile it. Outbound stock sync stays paused for each of them."""
    import uuid

    from sqlalchemy import func, select

    from celerp.models.projections import Projection

    cid = uuid.UUID(str(company_id))
    open_docs = (await session.execute(
        select(Projection).where(
            Projection.company_id == cid,
            Projection.entity_type == "doc",
            Projection.entity_id.like("doc:woocommerce:order:%"),
            func.coalesce(
                Projection.state["woocommerce_reconciliation_required"].as_string(), ""
            ) != "",
        )
    )).scalars().all()
    anchors: set[str] = set()
    for open_doc in open_docs:
        anchors.update(await _woocommerce_order_anchor_ids(session, cid, open_doc))
    return anchors


async def _set_woocommerce_order_stock_paused(session, cid, doc, paused: bool) -> None:
    """Pause outbound stock sync for the linked products on one order
    (``paused``), or resume it for those no other open reconciliation still
    touches. Paused while a person reconciles a refund, so Celerp stock never
    overwrites a restock the merchant chose in WooCommerce."""
    from celerp_inventory.services import external_link_for_state, set_external_link_state
    from celerp.models.projections import Projection

    anchors = await _woocommerce_order_anchor_ids(session, cid, doc)
    held = set(anchors) if paused else await woocommerce_products_awaiting_reconciliation(session, cid)
    for anchor_id in anchors:
        anchor = await session.get(Projection, {"company_id": cid, "entity_id": anchor_id})
        want = anchor_id in held
        link = external_link_for_state((anchor.state or {}) if anchor else {}, "woocommerce")
        if link and (link.get("inventory_sync_paused") is True) != want:
            await set_external_link_state(
                session, cid, anchor_id, "woocommerce",
                link_updates={"inventory_sync_paused": want},
                source="connector",
            )


async def _record_woocommerce_hold(
    session, cid, doc, *, reason: str | None, signature: str, wc_status: str,
) -> bool:
    """Record on an imported order the change waiting on a person (``reason``)
    and the source state they review (``signature``), or clear both once
    nothing waits. Only a mark for that same signature reconciles it."""
    import uuid

    from celerp.events.engine import emit_event

    state = doc.state or {}
    wanted = {
        "woocommerce_status": wc_status,
        "woocommerce_reconciliation_required": reason,
        "woocommerce_reconciliation_signature": signature if reason else None,
    }
    fields = {
        key: {"old": state.get(key), "new": value}
        for key, value in wanted.items()
        if state.get(key) != value
    }
    if fields:
        await emit_event(
            session, company_id=cid, entity_id=doc.entity_id, entity_type="doc",
            event_type="doc.updated", data={"fields_changed": fields},
            actor_id=None, location_id=None, source="connector",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
    return bool(fields)


async def set_woocommerce_order_reconciled(
    session, company_id: str, order_id: str, *,
    signature: str, reconciled: bool, reason: str | None, actor_id,
) -> None:
    """Record (``reconciled``) or withdraw a person's note that they reconciled
    the WooCommerce order change with this ``signature`` by hand. The import
    leaves the order alone while its source still matches the signature, and
    stock sync resumes for its products once no other open order touches them;
    withdrawing puts ``reason`` back as the order's reconciliation note and
    pauses stock again at once. Raises WooCommerceReconciliationChanged when
    the order now waits on a different change. Caller commits."""
    import uuid

    from celerp.events.engine import emit_event
    from celerp.models.projections import Projection

    cid = uuid.UUID(str(company_id))
    await _lock_woocommerce_order(session, cid, order_id)
    entity_id = f"doc:woocommerce:order:{order_id}"
    doc = await session.get(
        Projection, {"company_id": cid, "entity_id": entity_id},
        with_for_update=True, populate_existing=True,
    )
    if doc is None or doc.entity_type != "doc":
        raise ValueError(f"WooCommerce order {order_id} has not been imported")
    state = doc.state or {}
    if state.get("woocommerce_reconciliation_signature") != signature:
        raise WooCommerceReconciliationChanged(
            "This order changed in WooCommerce; refresh to review the change"
        )
    wanted = {
        "woocommerce_reconciled_signature": signature if reconciled else None,
        "woocommerce_reconciliation_required": None if reconciled else reason,
    }
    fields = {
        key: {"old": state.get(key), "new": value}
        for key, value in wanted.items()
        if state.get(key) != value
    }
    if fields:
        await emit_event(
            session, company_id=cid, entity_id=entity_id, entity_type="doc",
            event_type="doc.updated", data={"fields_changed": fields},
            actor_id=actor_id, location_id=None, source="connector_ui",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
    await _set_woocommerce_order_stock_paused(
        session, cid, doc, bool(wanted["woocommerce_reconciliation_required"])
    )


def _woocommerce_order_customer(order: dict) -> dict | None:
    customer_id = int(order.get("customer_id") or 0)
    if customer_id <= 0:
        return None
    billing = order.get("billing") or {}
    shipping = order.get("shipping") or {}
    return {
        "id": customer_id,
        "first_name": billing.get("first_name"),
        "last_name": billing.get("last_name"),
        "email": billing.get("email"),
        "phone": billing.get("phone"),
        "billing": billing,
        "shipping": shipping,
    }


async def upsert_order_from_woocommerce(company_id: str, order: dict) -> str:
    """Reconcile one WooCommerce order through Celerp's canonical sales lifecycle.

    Draft source data may be refreshed until issuance. Once finalized, commercial
    fields are immutable: only supported lifecycle progress is applied, and a changed
    source fingerprint fails visibly rather than rewriting posted accounting.
    """
    from datetime import date as _date
    from types import SimpleNamespace

    from sqlalchemy import select

    from celerp.db import SessionLocal
    from celerp.events.engine import connector_upsert, emit_event, find_event_by_idempotency
    from celerp.models.accounting import UserCompany
    from celerp.models.company import Company
    from celerp.models.projections import Projection
    from celerp.services.money import to_decimal
    from celerp.services.pick import consolidate_sales_lots, plan_lot_draws, resolve_pick_method
    from celerp.services.units import is_non_stock_line
    from celerp_inventory.projections import is_item_available
    from celerp_inventory.services import (
        external_identity_key,
        external_link_for_state,
        load_catalog_family_rows,
        resolve_catalog_anchor_for_item,
        resolve_external_product,
        set_external_link,
        set_external_link_state,
    )

    from celerp_docs.routes_payments import (
        WOOCOMMERCE_DEPOSIT_ACCOUNT_KEY,
        deposit_account,
    )
    from celerp_docs.routes import (
        FulfillLinesRequest,
        _finalize_doc_impl,
        _fulfill_lines_impl,
        _reserve_lines_impl,
        _get_doc,
        apply_doc_payment,
    )

    order_id = str(order["id"])
    idem_key = f"woocommerce:order:{order_id}"
    entity_id = f"doc:{idem_key}"
    source_fingerprint = _woocommerce_commercial_fingerprint(order)
    signature = _woocommerce_reconciliation_signature(order)
    wc_status = str(order.get("status") or "pending").lower()
    currency = str(order.get("currency") or "").upper() or None
    stock_reduced_statuses = frozenset({"on-hold", "processing", "completed"})
    stock_release_statuses = frozenset({"pending", "cancelled", "failed"})
    manual_reconciliation_statuses = frozenset({"refunded"})
    # A partial refund leaves the status, lines and total unchanged; the
    # refunds list is its only trace. Whether stock came back is the
    # merchant's choice, so any refund is reconciled by a person.
    refund_pending = wc_status not in stock_release_statuses and (
        wc_status in manual_reconciliation_statuses or bool(order.get("refunds"))
    )
    handled_statuses = (
        stock_reduced_statuses
        | stock_release_statuses
        | manual_reconciliation_statuses
    )

    # Registered customers are independent CRM records. Import them first so the
    # document can carry a stable contact link; guest orders still keep snapshots.
    contact_id = None
    customer = _woocommerce_order_customer(order)
    if customer is not None:
        from celerp_contacts.services import upsert_contact_from_woocommerce
        await upsert_contact_from_woocommerce(company_id, customer)
        contact_id = f"contact:woocommerce:customer:{customer['id']}"

    billing = order.get("billing") or {}
    shipping = order.get("shipping") or {}
    from celerp_contacts.services import _woocommerce_address_text
    contact_name = " ".join(
        p for p in (billing.get("first_name"), billing.get("last_name")) if p
    ).strip() or billing.get("email") or f"WooCommerce order {order.get('number') or order_id}"

    async with SessionLocal() as session:
        cid = __import__("uuid").UUID(str(company_id))
        # One external order may arrive simultaneously by webhook, manual sync, and
        # scheduled reconciliation. Serialize its full materialize/post transition.
        await _lock_woocommerce_order(session, cid, order_id)

        existing = await session.get(
            Projection, {"company_id": cid, "entity_id": entity_id}, with_for_update=True
        )
        if existing is not None and existing.entity_type != "doc":
            raise ValueError(f"WooCommerce order identity collides with {existing.entity_type}")

        async def hold_for_reconciliation(reason: str):
            """Pause the order's products, record ``reason`` on the order, and
            stop: a person reconciles the change by hand."""
            doc = await _get_doc(session, cid, entity_id, for_update=True)
            await _set_woocommerce_order_stock_paused(session, cid, doc, True)
            doc = await _get_doc(session, cid, entity_id, for_update=True)
            await _record_woocommerce_hold(
                session, cid, doc, reason=reason, signature=signature,
                wc_status=wc_status,
            )
            await session.commit()
            raise WooCommerceReconciliationRequired(reason, signature)

        async def release_resolved_hold() -> bool:
            """Every check passed for the source state a hold was recorded
            for, so it was put right in Celerp (a payment corrected by hand,
            say): clear the hold and resume stock sync for its products."""
            doc = await _get_doc(session, cid, entity_id, for_update=True)
            if (doc.state or {}).get("woocommerce_reconciliation_signature") != signature:
                return False
            await _record_woocommerce_hold(
                session, cid, doc, reason=None, signature=signature, wc_status=wc_status,
            )
            doc = await _get_doc(session, cid, entity_id, for_update=True)
            await _set_woocommerce_order_stock_paused(session, cid, doc, False)
            return True

        existing_state = dict(existing.state or {}) if existing is not None else {}
        if existing_state.get("woocommerce_reconciled_signature") == signature:
            # A person reconciled exactly this source state by hand.
            await session.commit()
            return "noop"
        same_commercial_source = (
            existing is not None
            and existing_state.get("woocommerce_source_fingerprint") == source_fingerprint
        )
        owns_reserved_stock = False
        if existing is not None and not existing_state.get("finalized"):
            for li in existing_state.get("line_items", []):
                item_id = li.get("item_id") or li.get("entity_id")
                if not item_id:
                    continue
                item = await session.get(
                    Projection, {"company_id": cid, "entity_id": item_id}
                )
                if (
                    item is not None
                    and (item.state or {}).get("status") == "reserved"
                    and (item.state or {}).get("status_doc_id") == entity_id
                ):
                    owns_reserved_stock = True
                    break

        if (
            existing is not None
            and not existing_state.get("finalized")
            and not same_commercial_source
            and owns_reserved_stock
            and wc_status not in stock_release_statuses
            and not refund_pending
        ):
            await hold_for_reconciliation(
                f"WooCommerce order {order.get('number') or order_id} changed after "
                "stock was reserved; manual reconciliation is required"
            )

        if existing is not None and existing_state.get("finalized"):
            if (existing.state or {}).get("woocommerce_source_fingerprint") != source_fingerprint:
                await hold_for_reconciliation(
                    f"WooCommerce order {order.get('number') or order_id} changed after "
                    "the Celerp invoice was issued; manual reconciliation is required"
                )
            if wc_status not in handled_statuses:
                await hold_for_reconciliation(
                    f"WooCommerce order {order.get('number') or order_id} moved to "
                    f"{wc_status!r} after issuance; manual reconciliation is required"
                )
            outcome = "noop"
        elif existing is not None and (
            wc_status in stock_release_statuses
            or refund_pending
            or (same_commercial_source and owns_reserved_stock)
        ):
            # Preserve the exact physical line bindings already chosen for this draft.
            # Rebuilding them from current availability can orphan this order's
            # reserved split child during a status-only transition.
            outcome = "noop"
        else:
            company = await session.get(Company, cid)
            company_settings = dict(company.settings or {}) if company else {}
            if not currency:
                currency = str(company_settings.get("currency") or "USD").upper()

            # Coalesce repeated source lines for one external product identity. Celerp
            # intentionally forbids binding the same physical item to two invoice lines.
            grouped: dict[tuple[str, str, str], dict] = {}
            for li in order.get("line_items", []):
                key = (
                    str(li.get("product_id") or ""),
                    str(li.get("variation_id") or ""),
                    str(li.get("sku") or "").strip().casefold(),
                )
                entry = grouped.setdefault(key, {
                    "product_id": li.get("product_id"),
                    "variation_id": li.get("variation_id"),
                    "sku": str(li.get("sku") or "").strip(),
                    "name": li.get("name") or "",
                    "quantity": 0.0,
                    "total": 0.0,
                })
                entry["quantity"] += _f(li.get("quantity"), 0)
                entry["total"] += _f(li.get("total"), 0)

            line_items: list[dict] = []
            product_subtotal = 0.0
            resolved_skus: set[str] = set()
            for source_line in grouped.values():
                product_id = str(source_line.get("product_id") or "")
                variation_id = str(source_line.get("variation_id") or "") or None
                source_sku = str(source_line.get("sku") or "").strip()

                # Woo can contain custom/free-text order lines with neither a product
                # identity nor SKU. They are valid non-stock revenue lines. Any line
                # that does claim product/SKU identity must resolve or fail closed.
                if not product_id and not source_sku:
                    qty = float(source_line["quantity"])
                    if qty <= 0:
                        raise ValueError(
                            f"WooCommerce order line {source_line.get('name')!r} "
                            f"has non-positive quantity {qty}"
                        )
                    line_total = _amt(source_line["total"], currency)
                    product_subtotal += line_total
                    line_items.append({
                        "name": source_line.get("name") or "WooCommerce item",
                        "quantity": qty,
                        "unit_price": line_total / qty,
                        "line_total": line_total,
                    })
                    continue

                anchor = await resolve_external_product(
                    session, cid, "woocommerce", product_id, variation_id,
                    sku=source_sku or None,
                )
                if anchor is None:
                    identity = (
                        f"WooCommerce product {product_id}"
                        + (f" variation {variation_id}" if variation_id else "")
                        if product_id else f"WooCommerce SKU {source_sku!r}"
                    )
                    raise ValueError(
                        f"{identity} does not resolve to a Celerp catalog product"
                    )
                anchor = await resolve_catalog_anchor_for_item(
                    session, cid, anchor.entity_id
                )
                anchor_state = dict(anchor.state or {})
                link = external_link_for_state(anchor_state, "woocommerce")
                if product_id and not link:
                    await set_external_link(
                        session, cid, anchor.entity_id, "woocommerce",
                        {
                            "product_id": product_id,
                            **({"variation_id": variation_id} if variation_id else {}),
                            "sync_enabled": True,
                            "remote_deleted": False,
                            "manage_stock": None,
                        },
                        expected_sku=source_sku,
                        require_unlinked=True,
                        source="connector",
                    )

                sku = str(anchor_state.get("sku") or source_sku).strip()
                if not sku:
                    raise ValueError(f"WooCommerce product {product_id} resolves to an item without a SKU")
                sku_key = sku.casefold()
                if sku_key in resolved_skus:
                    raise ValueError(
                        f"Multiple WooCommerce product identities resolve to Celerp SKU {sku!r}; "
                        "manual reconciliation is required"
                    )
                resolved_skus.add(sku_key)
                qty = float(source_line["quantity"])
                if qty <= 0:
                    raise ValueError(f"WooCommerce order line {sku} has non-positive quantity {qty}")
                line_total = _amt(source_line["total"], currency)
                product_subtotal += line_total

                unit_price = line_total / qty
                line_name = source_line.get("name") or anchor_state.get("name") or sku
                if is_non_stock_line(anchor_state.get("inventory_type"), anchor_state.get("sell_by")):
                    line_items.append({
                        "item_id": anchor.entity_id,
                        "sku": sku,
                        "name": line_name,
                        "quantity": qty,
                        "unit_price": unit_price,
                        "line_total": line_total,
                        "sell_by": anchor_state.get("sell_by"),
                    })
                else:
                    if wc_status not in stock_reduced_statuses:
                        # Woo has not reduced stock for this status. Keep the
                        # commercial line unbound; a later stock-reduced status
                        # rebuilds the draft against then-current sellable stock.
                        line_items.append({
                            "sku": sku,
                            "name": line_name,
                            "quantity": qty,
                            "unit_price": unit_price,
                            "line_total": line_total,
                            "sell_by": anchor_state.get("sell_by"),
                        })
                        continue
                    family: list[dict] = []
                    for row in await load_catalog_family_rows(session, cid, anchor):
                        st = row.state or {}
                        if not is_item_available(st) or float(st.get("quantity") or 0) <= 0:
                            continue
                        family.append({
                            **st,
                            "entity_id": row.entity_id,
                            "id": row.entity_id,
                            "created_at": row.created_at.isoformat() if row.created_at else "",
                            "updated_at": row.updated_at.isoformat() if row.updated_at else "",
                        })
                    if not family and link.get("manage_stock") in (False, "parent"):
                        line_items.append({
                            "sku": sku,
                            "name": line_name,
                            "quantity": qty,
                            "unit_price": unit_price,
                            "line_total": line_total,
                            "sell_by": anchor_state.get("sell_by"),
                        })
                        continue
                    options = consolidate_sales_lots(family, company_settings) if family else []
                    if len(options) != 1:
                        raise ValueError(
                            f"WooCommerce SKU {sku!r} does not resolve to one automatic "
                            "sellable inventory choice"
                        )
                    representative_id = options[0].get("entity_id") or options[0].get("id")
                    primary = next(
                        (item for item in family
                         if (item.get("entity_id") or item.get("id")) == representative_id),
                        None,
                    )
                    if primary is None:
                        raise ValueError(f"WooCommerce SKU {sku!r} has no sellable inventory")
                    method = resolve_pick_method(primary, company_settings)
                    draws, short_qty = plan_lot_draws(
                        primary,
                        qty,
                        [
                            item for item in family
                            if (item.get("entity_id") or item.get("id")) != representative_id
                        ],
                        method,
                    )
                    if short_qty > 1e-9:
                        available_qty = qty - short_qty
                        raise ValueError(
                            f"WooCommerce SKU {sku!r} requires {qty:g}, but only "
                            f"{available_qty:g} is sellable"
                        )

                    remaining_total = line_total
                    for index, (lot, take_qty, _is_full) in enumerate(draws):
                        is_last = index == len(draws) - 1
                        draw_total = (
                            remaining_total
                            if is_last
                            else _amt(unit_price * float(take_qty), currency)
                        )
                        remaining_total = _amt(
                            to_decimal(remaining_total) - to_decimal(draw_total),
                            currency,
                        )
                        line_items.append({
                            "item_id": lot.get("entity_id") or lot.get("id"),
                            "sku": sku,
                            "name": line_name,
                            "quantity": float(take_qty),
                            "unit_price": unit_price,
                            "line_total": draw_total,
                            "sell_by": lot.get("sell_by") or anchor_state.get("sell_by"),
                        })

            fee_total = 0.0
            for fee in order.get("fee_lines", []):
                amount = _amt(fee.get("total"), currency)
                fee_total += amount
                line_items.append({
                    "name": fee.get("name") or "WooCommerce fee",
                    "quantity": 1,
                    "unit_price": amount,
                    "line_total": amount,
                })

            shipping_total = _amt(
                sum(to_decimal(x.get("total") or 0) for x in order.get("shipping_lines", [])),
                currency,
            )
            tax_total = _amt(order.get("total_tax"), currency)
            subtotal = _amt(product_subtotal + fee_total, currency)
            total = _amt(order.get("total"), currency)
            mapped_total = _amt(
                to_decimal(subtotal) + to_decimal(shipping_total) + to_decimal(tax_total),
                currency,
            )
            if mapped_total != total:
                raise ValueError(
                    f"WooCommerce order {order.get('number') or order_id} total {total:g} "
                    f"does not reconcile to mapped subtotal/tax/shipping {mapped_total:g}"
                )

            data = {
                "doc_type": "invoice",
                "ref_id": f"WOO-{order.get('number') or order_id}",
                "status": "draft",
                "line_items": line_items,
                "subtotal": subtotal,
                "tax": tax_total,
                "shipping": shipping_total,
                "discount": 0.0,
                "total": total,
                "amount_paid": 0.0,
                "amount_outstanding": total,
                "currency": currency,
                "issue_date": str(order.get("date_created") or "")[:10] or _date.today().isoformat(),
                "contact_id": contact_id,
                "contact_name": contact_name,
                "contact_email": billing.get("email"),
                "contact_phone": billing.get("phone"),
                "contact_billing_address": _woocommerce_address_text(billing),
                "contact_shipping_address": _woocommerce_address_text(shipping)
                    or _woocommerce_address_text(billing),
                "woocommerce_order_id": order_id,
                "woocommerce_order_number": str(order.get("number") or order_id),
                "woocommerce_status": wc_status,
                "woocommerce_source_fingerprint": source_fingerprint,
                "woocommerce_transaction_id": order.get("transaction_id"),
            }
            outcome = await connector_upsert(
                session, company_id=cid, entity_type="doc",
                event_type="doc.created", idem_key=idem_key, data=data,
            )
            existing = await session.get(
                Projection,
                {"company_id": cid, "entity_id": entity_id},
                with_for_update=True,
                populate_existing=True,
            )

        if wc_status not in handled_statuses:
            await session.commit()
            return outcome

        owner_id = (await session.execute(
            select(UserCompany.user_id).where(
                UserCompany.company_id == cid,
                UserCompany.role == "owner",
            ).order_by(UserCompany.id).limit(1)
        )).scalar_one_or_none()
        if owner_id is None:
            raise ValueError("Company has no owner available to post the WooCommerce sale")
        actor = SimpleNamespace(id=owner_id)

        doc = await _get_doc(session, cid, entity_id, for_update=True)
        changed = outcome != "noop"
        if refund_pending:
            # WooCommerce does not automatically restore stock merely because an
            # order is refunded. Whether the merchant restocked the refund is an
            # explicit refund choice, so keep Celerp inventory and accounting
            # unchanged and stop outbound stock from overwriting that decision.
            refund_state = "is refunded" if wc_status == "refunded" else "has a refund"
            await hold_for_reconciliation(
                f"WooCommerce order {order.get('number') or order_id} {refund_state}; "
                "manual financial/inventory reconciliation is required"
            )

        if wc_status in {"processing", "completed"} and not doc.state.get("finalized"):
            await _finalize_doc_impl(entity_id, cid, actor, session, commit=False)
            changed = True
            doc = await _get_doc(session, cid, entity_id, for_update=True)

        stock_ids: list[str] = []
        fulfill_ids: list[str] = []
        for li in doc.state.get("line_items", []):
            item_id = li.get("item_id") or li.get("entity_id")
            if not item_id:
                continue
            item = await session.get(Projection, {"company_id": cid, "entity_id": item_id})
            if item is None:
                raise ValueError(f"Invoice line item {item_id} no longer exists")
            st = item.state or {}
            non_stock = is_non_stock_line(st.get("inventory_type"), st.get("sell_by"))
            if non_stock:
                if wc_status == "completed":
                    fulfill_ids.append(item_id)
                continue
            item_status = str(st.get("status") or "")
            owner_doc = st.get("status_doc_id")
            if wc_status in {"on-hold", "processing"}:
                if item_status == "available":
                    stock_ids.append(item_id)
                elif item_status == "reserved" and owner_doc == entity_id:
                    pass
                elif item_status == "sold" and owner_doc == entity_id:
                    pass
                else:
                    raise ValueError(
                        f"Cannot reserve WooCommerce SKU {st.get('sku') or item_id}: "
                        f"inventory is {item_status!r}"
                    )
            elif wc_status == "completed":
                if item_status in {"available", "reserved"} and (
                    item_status == "available" or owner_doc == entity_id
                ):
                    fulfill_ids.append(item_id)
                elif item_status == "sold" and owner_doc == entity_id:
                    pass
                else:
                    raise ValueError(
                        f"Cannot fulfill WooCommerce SKU {st.get('sku') or item_id}: "
                        f"inventory is {item_status!r}"
                    )

        if wc_status in {"on-hold", "processing"} and stock_ids:
            doc = await _get_doc(session, cid, entity_id, for_update=True)
            await _reserve_lines_impl(
                doc, entity_id, "reserved", stock_ids, actor, session, commit=False
            )
            changed = True
        elif wc_status == "completed" and fulfill_ids:
            await _fulfill_lines_impl(
                entity_id, FulfillLinesRequest(line_entity_ids=fulfill_ids),
                cid, actor, session, commit=False,
            )
            changed = True

        if wc_status in stock_release_statuses:
            doc = await _get_doc(session, cid, entity_id, for_update=True)
            reserved_ids: list[str] = []
            sold_items: list[Projection] = []
            for li in doc.state.get("line_items", []):
                item_id = li.get("item_id") or li.get("entity_id")
                if not item_id:
                    continue
                item = await session.get(Projection, {"company_id": cid, "entity_id": item_id})
                if item is None:
                    continue
                st = item.state or {}
                if st.get("status") == "reserved" and st.get("status_doc_id") == entity_id:
                    reserved_ids.append(item_id)
                elif st.get("status") == "sold" and st.get("status_doc_id") == entity_id:
                    sold_items.append(item)
            if reserved_ids:
                await _reserve_lines_impl(
                    doc, entity_id, "available", reserved_ids, actor, session, commit=False
                )
                changed = True
            for sold in sold_items:
                try:
                    anchor = await resolve_catalog_anchor_for_item(session, cid, sold.entity_id)
                    link = external_link_for_state(
                        anchor.state or {}, "woocommerce"
                    )
                    if link:
                        await set_external_link_state(
                            session, cid, anchor.entity_id, "woocommerce",
                            link_updates={"inventory_sync_paused": True},
                            expected_identity=external_identity_key(
                                "woocommerce", link
                            ),
                            source="connector",
                        )
                except ValueError:
                    pass
            doc = await _get_doc(session, cid, entity_id, for_update=True)
            needs_manual = bool(
                doc.state.get("finalized")
                or sold_items
                or float(doc.state.get("amount_paid") or 0) > 0
            )
            reason = (
                f"WooCommerce order {order.get('number') or order_id} is {wc_status}; "
                "manual financial/fulfillment reconciliation is required"
                if needs_manual else None
            )
            if await _record_woocommerce_hold(
                session, cid, doc, reason=reason, signature=signature,
                wc_status=wc_status,
            ):
                changed = True
            await session.commit()
            if needs_manual:
                raise WooCommerceReconciliationRequired(reason, signature)
            if outcome == "created":
                return "created"
            return "updated" if changed else outcome

        doc = await _get_doc(session, cid, entity_id, for_update=True)
        if (
            wc_status in {"processing", "completed"}
            and order.get("date_paid")
            and float(doc.state.get("amount_outstanding") or 0) > 0
        ):
            if await find_event_by_idempotency(session, cid, f"{idem_key}:payment") is not None:
                # WooCommerce's payment was already recorded once; a balance
                # showing again means a person changed the payments since.
                # Posting again would override that, so it goes to a person.
                await hold_for_reconciliation(
                    f"WooCommerce order {order.get('number') or order_id} is paid but its "
                    "invoice has a balance again after a payment change; manual "
                    "financial reconciliation is required"
                )
            payment_date = str(order.get("date_paid"))[:10]
            await apply_doc_payment(
                session, cid, entity_id,
                {
                    "amount": float(doc.state.get("amount_outstanding") or 0),
                    "payment_date": payment_date,
                    "currency": doc.state.get("currency"),
                    "method": order.get("payment_method") or "woocommerce",
                    "reference": order.get("transaction_id") or f"woocommerce-order-{order_id}",
                    "bank_account": await deposit_account(
                        session, cid, override_key=WOOCOMMERCE_DEPOSIT_ACCOUNT_KEY
                    ),
                },
                source="woocommerce",
                actor_id=owner_id,
                idempotency_key=f"{idem_key}:payment",
                commit=False,
            )
            changed = True

        current_status = str((doc.state or {}).get("woocommerce_status") or "")
        if current_status != wc_status:
            fields = {"woocommerce_status": {"old": current_status, "new": wc_status}}
            if order.get("transaction_id") != (doc.state or {}).get("woocommerce_transaction_id"):
                fields["woocommerce_transaction_id"] = {
                    "old": (doc.state or {}).get("woocommerce_transaction_id"),
                    "new": order.get("transaction_id"),
                }
            await emit_event(
                session, company_id=cid, entity_id=entity_id, entity_type="doc",
                event_type="doc.updated", data={"fields_changed": fields},
                actor_id=owner_id, location_id=None, source="connector",
                idempotency_key=f"{idem_key}:status:{wc_status}", metadata_={},
            )
            changed = True

        if await release_resolved_hold():
            changed = True
        await session.commit()
        if outcome == "created":
            return "created"
        return "updated" if changed else "noop"


async def upsert_invoice_from_quickbooks(company_id: str, invoice: dict) -> str:
    """
    Create/update a doc (invoice) from a QuickBooks Invoice dict.
    Returns "created", "updated", or "noop".

    Idempotency key: quickbooks:invoice:{Id}

    Mapping:
      Invoice.DocNumber               -> ref_id
      Invoice.Balance (0)             -> closed, else open
      Invoice.Line[SalesItemLineDetail] -> line_items
      Invoice.TotalAmt                -> total
      Invoice.Balance                 -> amount_outstanding
    """
    from celerp.db import SessionLocal

    idem_key = f"quickbooks:invoice:{invoice['Id']}"

    async with SessionLocal() as session:
        ref_id = str(invoice.get("DocNumber") or f"quickbooks-{invoice['Id']}")
        currency = (invoice.get("CurrencyRef") or {}).get("value")
        balance = _amt(invoice.get("Balance"), currency)
        status = "closed" if balance == 0 else "open"

        line_items = []
        for line in invoice.get("Line", []):
            if line.get("DetailType") != "SalesItemLineDetail":
                continue  # skip subtotal/discount/other detail rows
            detail = line.get("SalesItemLineDetail") or {}
            qty = _f(detail.get("Qty"), 1)
            unit_price = _f(detail.get("UnitPrice"))
            line_total = _line_total(line.get("Amount"), qty, unit_price, currency)
            line_items.append({
                "name": line.get("Description", ""),
                "quantity": qty,
                "unit_price": unit_price,
                "line_total": line_total,
            })
        total = _amt(invoice.get("TotalAmt"), currency)

        data = {
            "doc_type": "invoice",
            "ref_id": ref_id,
            "status": status,
            "line_items": line_items,
            "total": total,
            "amount_outstanding": balance,
            "currency": currency,
            "quickbooks_invoice_id": str(invoice["Id"]),
        }
        return await _emit_doc(session, company_id, data, idem_key)


async def upsert_invoice_from_xero(company_id: str, invoice: dict) -> str:
    """
    Create/update a doc (invoice) from a Xero Invoice dict (ACCREC).
    Returns "created", "updated", or "noop".

    Idempotency key: xero:invoice:{InvoiceID}

    Mapping:
      Invoice.InvoiceNumber   -> ref_id
      Invoice.Status (PAID)   -> closed, else open
      Invoice.LineItems       -> line_items
      Invoice.Total           -> total
      Invoice.AmountDue       -> amount_outstanding
    """
    from celerp.db import SessionLocal

    idem_key = f"xero:invoice:{invoice['InvoiceID']}"

    async with SessionLocal() as session:
        ref_id = str(invoice.get("InvoiceNumber") or f"xero-{invoice['InvoiceID']}")
        currency = invoice.get("CurrencyCode")
        status = "closed" if invoice.get("Status") == "PAID" else "open"

        line_items = []
        for li in invoice.get("LineItems", []):
            qty = _f(li.get("Quantity"), 1)
            unit_price = _f(li.get("UnitAmount"))
            line_total = _line_total(li.get("LineAmount"), qty, unit_price, currency)
            line_items.append({
                "name": li.get("Description", ""),
                "quantity": qty,
                "unit_price": unit_price,
                "line_total": line_total,
            })
        total = _amt(invoice.get("Total"), currency)
        amount_due = _amt(_f(invoice.get("AmountDue"), total if status == "open" else 0.0), currency)

        data = {
            "doc_type": "invoice",
            "ref_id": ref_id,
            "status": status,
            "line_items": line_items,
            "total": total,
            "amount_outstanding": amount_due,
            "currency": currency,
            "xero_invoice_id": str(invoice["InvoiceID"]),
        }
        return await _emit_doc(session, company_id, data, idem_key)
