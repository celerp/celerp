# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

import uuid

from sqlalchemy import text

from celerp.events.engine import emit_event


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
        existing = (
            await session.execute(
                text("SELECT id FROM ledger WHERE idempotency_key=:k"),
                {"k": idem_key},
            )
        ).first()
        if existing:
            return False

        ref_id = order.get("name", f"shopify-{order['id']}")
        entity_id = f"doc:{ref_id}"

        financial_status = order.get("financial_status", "pending")
        status = "closed" if financial_status == "paid" else "open"

        line_items = [
            {
                "name": li.get("title", ""),
                "quantity": float(li.get("quantity", 1)),
                "unit_price": float(li.get("price", 0)),
                "line_total": float(li.get("quantity", 1)) * float(li.get("price", 0)),
            }
            for li in order.get("line_items", [])
        ]
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

        await emit_event(
            session,
            company_id=company_id,
            entity_id=entity_id,
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
