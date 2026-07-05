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


async def upsert_order_from_woocommerce(company_id: str, order: dict) -> str:
    """
    Create/update a doc (invoice) from a WooCommerce order dict.
    Returns "created", "updated", or "noop".

    Idempotency key: woocommerce:order:{id}

    Mapping:
      order.number                 -> ref_id
      order.status (completed)     -> closed, else open
      order.line_items[].price     -> unit_price (per-unit, post-discount)
      order.line_items[].total     -> line_total
      order.total                  -> total
    """
    from celerp.db import SessionLocal

    idem_key = f"woocommerce:order:{order['id']}"

    async with SessionLocal() as session:
        ref_id = str(order.get("number") or f"woocommerce-{order['id']}")
        # Paid/outstanding must come from PAYMENT status, not fulfillment: WooCommerce
        # "completed" means shipped, not paid. A processing-but-paid order is closed;
        # an unpaid one is open regardless of fulfillment.
        paid = bool(order.get("date_paid")) and not order.get("needs_payment", False)
        status = "closed" if paid else "open"
        currency = order.get("currency")

        line_items = []
        for li in order.get("line_items", []):
            qty = _f(li.get("quantity"), 1)
            unit_price = _f(li.get("price"))
            line_total = _line_total(li.get("total"), qty, unit_price, currency)
            line_items.append({
                "name": li.get("name", ""),
                "quantity": qty,
                "unit_price": unit_price,
                "line_total": line_total,
            })
        total = _amt(order.get("total"), currency)

        data = {
            "doc_type": "invoice",
            "ref_id": ref_id,
            "status": status,
            "line_items": line_items,
            "total": total,
            "amount_outstanding": 0.0 if status == "closed" else total,
            "currency": currency,
            "woocommerce_order_id": str(order["id"]),
        }
        return await _emit_doc(session, company_id, data, idem_key)


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
