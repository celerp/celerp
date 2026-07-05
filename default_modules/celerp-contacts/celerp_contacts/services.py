# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT



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


async def upsert_contact_from_shopify(company_id: str, customer: dict) -> str:
    """
    Create or update a CRM contact from a Shopify customer dict.
    Returns "created", "updated", or "noop".

    Idempotency key: shopify:customer:{customer_id}
    Maps: id, email, first_name, last_name, phone → CelERP contact fields.
    """
    idem_key = f"shopify:customer:{customer['id']}"
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
    return await _emit_contact(company_id, idem_key, data)


async def _emit_contact(company_id: str, idem_key: str, data: dict) -> str:
    """Shared contact create-or-update: drop None fields, then upsert (content-based
    dedup, resolved to the contact's stable idem_key). Returns "created", "updated",
    or "noop"; a changed re-import updates the same contact."""
    from celerp.db import SessionLocal
    from celerp.events.engine import connector_upsert

    data = {k: v for k, v in data.items() if v is not None}
    async with SessionLocal() as session:
        outcome = await connector_upsert(
            session, company_id=company_id, entity_type="contact",
            event_type="crm.contact.created", idem_key=idem_key, data=data,
        )
        await session.commit()
        return outcome


async def upsert_contact_from_woocommerce(company_id: str, customer: dict) -> str:
    """Create/update a CRM contact from a WooCommerce customer dict. Idempotency: woocommerce:customer:{id}."""
    idem_key = f"woocommerce:customer:{customer['id']}"
    billing = customer.get("billing") or {}
    name = " ".join(
        p for p in (customer.get("first_name", ""), customer.get("last_name", "")) if p
    ).strip() or customer.get("email") or f"woocommerce:{customer['id']}"
    data = {
        "name": name,
        "email": customer.get("email"),
        "phone": billing.get("phone"),
        "attributes": {
            "woocommerce_id": str(customer["id"]),
            "city": billing.get("city"),
            "country": billing.get("country"),
        },
    }
    return await _emit_contact(company_id, idem_key, data)


async def upsert_contact_from_quickbooks(company_id: str, customer: dict) -> str:
    """Create/update a CRM contact from a QuickBooks Customer dict. Idempotency: quickbooks:customer:{Id}."""
    idem_key = f"quickbooks:customer:{customer['Id']}"
    bill_addr = customer.get("BillAddr") or {}
    name = (customer.get("DisplayName") or " ".join(
        p for p in (customer.get("GivenName", ""), customer.get("FamilyName", "")) if p
    ).strip() or f"quickbooks:{customer['Id']}")
    data = {
        "name": name,
        "email": (customer.get("PrimaryEmailAddr") or {}).get("Address"),
        "phone": (customer.get("PrimaryPhone") or {}).get("FreeFormNumber"),
        "attributes": {
            "quickbooks_id": str(customer["Id"]),
            "city": bill_addr.get("City"),
            "country": bill_addr.get("Country"),
        },
    }
    return await _emit_contact(company_id, idem_key, data)


async def upsert_contact_from_xero(company_id: str, contact: dict) -> str:
    """Create/update a CRM contact from a Xero Contact dict. Idempotency: xero:contact:{ContactID}."""
    idem_key = f"xero:contact:{contact['ContactID']}"
    phones = contact.get("Phones") or []
    phone = next(
        (p.get("PhoneNumber") for p in phones if p.get("PhoneType") == "DEFAULT" and p.get("PhoneNumber")),
        next((p.get("PhoneNumber") for p in phones if p.get("PhoneNumber")), None),
    )
    addr = next((a for a in (contact.get("Addresses") or []) if a.get("City")), {})
    data = {
        "name": contact.get("Name") or f"xero:{contact['ContactID']}",
        "email": contact.get("EmailAddress"),
        "phone": phone,
        "attributes": {
            "xero_id": str(contact["ContactID"]),
            "city": addr.get("City"),
            "country": addr.get("Country"),
        },
    }
    return await _emit_contact(company_id, idem_key, data)
