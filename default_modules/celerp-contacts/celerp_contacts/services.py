# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

import uuid

from sqlalchemy import text

from celerp.events.engine import emit_event


async def create_crm_entity(session, company_id: str, entity_type: str, data: dict):
    return await emit_event(
        session,
        company_id=company_id,
        entity_id=data["id"],
        entity_type="crm",
        event_type=f"crm.{entity_type}.created",
        data=data,
        actor_id=None,
        location_id=None,
        source="api",
        idempotency_key=data["idempotency_key"],
        metadata_={},
    )


async def upsert_contact_from_shopify(company_id: str, customer: dict) -> bool:
    """
    Create a CRM contact from a Shopify customer dict. Returns True if newly created.

    Idempotency key: shopify:customer:{customer_id}
    Maps: id, email, first_name, last_name, phone → CelERP contact fields.
    """
    from celerp.db import SessionLocal

    idem_key = f"shopify:customer:{customer['id']}"

    async with SessionLocal() as session:
        existing = (
            await session.execute(
                text("SELECT id FROM ledger WHERE idempotency_key=:k"),
                {"k": idem_key},
            )
        ).first()
        if existing:
            return False

        addr = (customer.get("addresses") or [{}])[0]
        name_parts = [customer.get("first_name", ""), customer.get("last_name", "")]
        name = " ".join(p for p in name_parts if p).strip() or customer.get("email", f"shopify:{customer['id']}")

        data = {
            "name": name,
            "email": customer.get("email"),
            "phone": customer.get("phone") or addr.get("phone"),
            "attributes": {
                "shopify_id": str(customer["id"]),
                "city": addr.get("city"),
                "country": addr.get("country"),
            },
        }
        # Remove None values
        data = {k: v for k, v in data.items() if v is not None}

        entity_id = f"contact:{uuid.uuid4()}"
        await emit_event(
            session,
            company_id=company_id,
            entity_id=entity_id,
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
