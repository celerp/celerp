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


async def upsert_order_from_shopify(company_id: str, order: dict) -> bool:
    """
    Create a doc (invoice) from a Shopify order dict. Returns True if newly created.

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

        line_items = []
        for li in order.get("line_items", []):
            qty = float(li.get("quantity") or 1)      # null/missing → 1
            price = float(li.get("price") or 0)       # null/missing → 0
            line_items.append({
                "name": li.get("title", ""),
                "quantity": qty,
                "unit_price": price,
                "line_total": qty * price,
            })
        total = float(order.get("total_price", 0) or 0)

        data = {
            "doc_type": "invoice",
            "ref_id": ref_id,
            "status": status,
            "line_items": line_items,
            "total": total,
            "amount_outstanding": 0.0 if status == "closed" else total,
            "shopify_order_id": str(order["id"]),
        }
        return await _emit_doc(session, company_id, data, idem_key)


async def list_unsynced_invoices(company_id: str, platform: str) -> list[dict]:
    """Native CelERP invoices that are candidates to push out to `platform`.

    Returns invoices that did NOT originate from an external platform (no
    *_order_id / *_invoice_id marker) and are not yet stamped as pushed to this
    platform. NOTE: the push write-back that stamps the returned platform id (to
    make this list shrink and prevent duplicate creates) is a scoped follow-up;
    until it lands, outbound invoice push must stay manual, not scheduled.
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
                "ref_id": st.get("ref_id") or st.get("doc_number"),
                "line_items": st.get("line_items") or [],
                "total": st.get("total"),
                "customer_name": st.get("customer_name"),
                "customer_external_id": st.get("customer_external_id"),
            })
    return out


async def _emit_doc(session, company_id: str, data: dict, idem_key: str) -> bool:
    # connector_upsert keys the projection on idem_key (the unique platform id), NOT the
    # human ref_id/DocNumber — two source invoices can share a DocNumber (QB allows it)
    # and would otherwise collapse into one doc. A changed re-import updates the same doc.
    from celerp.events.engine import connector_upsert
    wrote = await connector_upsert(
        session, company_id=company_id, entity_type="doc",
        event_type="doc.created", idem_key=idem_key, data=data,
    )
    await session.commit()
    return wrote


async def upsert_order_from_woocommerce(company_id: str, order: dict) -> bool:
    """
    Create a doc (invoice) from a WooCommerce order dict. Returns True if newly created.

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

        line_items = []
        for li in order.get("line_items", []):
            qty = _f(li.get("quantity"), 1)
            unit_price = _f(li.get("price"))
            line_total = _f(li.get("total"), qty * unit_price)
            line_items.append({
                "name": li.get("name", ""),
                "quantity": qty,
                "unit_price": unit_price,
                "line_total": line_total,
            })
        total = _f(order.get("total"))

        data = {
            "doc_type": "invoice",
            "ref_id": ref_id,
            "status": status,
            "line_items": line_items,
            "total": total,
            "amount_outstanding": 0.0 if status == "closed" else total,
            "woocommerce_order_id": str(order["id"]),
        }
        return await _emit_doc(session, company_id, data, idem_key)


async def upsert_invoice_from_quickbooks(company_id: str, invoice: dict) -> bool:
    """
    Create a doc (invoice) from a QuickBooks Invoice dict. Returns True if newly created.

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
        balance = _f(invoice.get("Balance"))
        status = "closed" if balance == 0 else "open"

        line_items = []
        for line in invoice.get("Line", []):
            if line.get("DetailType") != "SalesItemLineDetail":
                continue  # skip subtotal/discount/other detail rows
            detail = line.get("SalesItemLineDetail") or {}
            qty = _f(detail.get("Qty"), 1)
            unit_price = _f(detail.get("UnitPrice"))
            line_total = _f(line.get("Amount"), qty * unit_price)
            line_items.append({
                "name": line.get("Description", ""),
                "quantity": qty,
                "unit_price": unit_price,
                "line_total": line_total,
            })
        total = _f(invoice.get("TotalAmt"))

        data = {
            "doc_type": "invoice",
            "ref_id": ref_id,
            "status": status,
            "line_items": line_items,
            "total": total,
            "amount_outstanding": balance,
            "quickbooks_invoice_id": str(invoice["Id"]),
        }
        return await _emit_doc(session, company_id, data, idem_key)


async def upsert_invoice_from_xero(company_id: str, invoice: dict) -> bool:
    """
    Create a doc (invoice) from a Xero Invoice dict (ACCREC). Returns True if newly created.

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
        status = "closed" if invoice.get("Status") == "PAID" else "open"

        line_items = []
        for li in invoice.get("LineItems", []):
            qty = _f(li.get("Quantity"), 1)
            unit_price = _f(li.get("UnitAmount"))
            line_total = _f(li.get("LineAmount"), qty * unit_price)
            line_items.append({
                "name": li.get("Description", ""),
                "quantity": qty,
                "unit_price": unit_price,
                "line_total": line_total,
            })
        total = _f(invoice.get("Total"))
        amount_due = _f(invoice.get("AmountDue"), total if status == "open" else 0.0)

        data = {
            "doc_type": "invoice",
            "ref_id": ref_id,
            "status": status,
            "line_items": line_items,
            "total": total,
            "amount_outstanding": amount_due,
            "xero_invoice_id": str(invoice["InvoiceID"]),
        }
        return await _emit_doc(session, company_id, data, idem_key)
