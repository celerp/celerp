# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

import uuid
from collections.abc import Sequence

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from celerp.events.engine import emit_event, find_event_by_idempotency
from celerp.importers.results import ImportOutcome, failure_reason
from celerp.models.projections import Projection
from celerp.services.currencies import require_currency_code

CONTACT_CREATED = "crm.contact.created"


class CRMImportRecord(BaseModel):
    entity_id: str
    event_type: str
    data: dict
    source: str
    idempotency_key: str
    source_ts: str | None = None


def contact_import_identity(data: dict) -> str:
    """The contact an import row stands for: its email, else its phone, else its name,
    compared case-insensitively. Empty when the row carries none of them."""
    for key in ("email", "phone", "name"):
        value = str(data.get(key) or "").strip()
        if value:
            return value.lower()
    return ""


def _live(state: dict) -> bool:
    return not state.get("deleted") and not state.get("merged_into")


async def import_contact_records(
    session,
    company_id,
    actor_id,
    records: Sequence[CRMImportRecord],
    entity_type: str = "contact",
) -> ImportOutcome:
    """Create each imported contact, or update the contact it already is.

    A row is an existing contact when that contact shares its import identity, carries
    its entity id, or was created under its idempotency key, whatever key the row is
    sent under now. One such contact takes the row's values (blank values leave a field
    as it is); a row with nothing new is skipped, so replaying a file writes nothing.
    More than one refuses the row with nothing written. The caller runs one import per
    company at a time and commits."""
    rows = (await session.execute(
        select(Projection).where(Projection.company_id == company_id, Projection.entity_type == "contact")
    )).scalars().all()
    states = {r.entity_id: dict(r.state or {}) for r in rows}
    outcome = ImportOutcome()
    for rec in records:
        label = str(rec.data.get("name") or rec.entity_id)
        if rec.event_type != CONTACT_CREATED:
            outcome.add(rec.entity_id, "rejected", f"{rec.entity_id}: event type {rec.event_type!r} is not import-safe")
            continue
        try:
            require_currency_code(rec.data.get("currency"))
        except HTTPException as exc:
            outcome.add(rec.entity_id, "rejected", f"{label}: {exc.detail}")
            continue
        identity = contact_import_identity(rec.data)
        keyed = await find_event_by_idempotency(session, company_id, rec.idempotency_key)
        # The company's own contact is kept in company settings, never matched by an import.
        candidates = {cid for cid, state in states.items() if identity and _live(state) and not state.get("is_self")
                      and contact_import_identity(state) == identity}
        for cid in (rec.entity_id, keyed.entity_id if keyed is not None else None):
            if cid in states and _live(states[cid]):
                candidates.add(cid)
        if rec.entity_id in states and not _live(states[rec.entity_id]):
            outcome.add(rec.entity_id, "rejected", f"{label}: contact {rec.entity_id} was merged or deleted")
            continue
        if len(candidates) > 1:
            outcome.add(rec.entity_id, "rejected",
                        f"{label}: matches {len(candidates)} existing contacts ({', '.join(sorted(candidates))}); "
                        "merge them or give the row a unique email, phone or name")
            continue
        try:
            if candidates:
                contact_id = candidates.pop()
                state = states[contact_id]
                changes = {k: {"old": state.get(k), "new": v} for k, v in rec.data.items()
                           if v is not None and state.get(k) != v}
                if not changes:
                    outcome.add(contact_id, "skipped")
                    continue
                await emit_event(
                    session, company_id=company_id, entity_id=contact_id, entity_type=entity_type,
                    event_type="crm.contact.updated", data={"fields_changed": changes},
                    actor_id=actor_id, location_id=None, source=rec.source,
                    idempotency_key=str(uuid.uuid4()),
                    metadata_={"import_key": rec.idempotency_key, **({"source_ts": rec.source_ts} if rec.source_ts else {})},
                )
                state.update({k: c["new"] for k, c in changes.items()})
                outcome.add(contact_id, "updated")
                continue
            data = {"contact_type": "customer", **{k: v for k, v in rec.data.items() if v is not None}}
            entry = await emit_event(
                session, company_id=company_id, entity_id=rec.entity_id, entity_type=entity_type,
                event_type=rec.event_type, data=data, actor_id=actor_id, location_id=None,
                source=rec.source, idempotency_key=rec.idempotency_key,
                metadata_={"source_ts": rec.source_ts} if rec.source_ts else {},
            )
            if getattr(entry, "was_deduped", False):
                # The key already wrote something that is no longer a live contact.
                outcome.add(rec.entity_id, "skipped")
                continue
            states[rec.entity_id] = {"entity_type": "contact", **data}
            outcome.add(rec.entity_id, "created")
        except Exception as exc:
            outcome.add(rec.entity_id, "failed", f"{label}: {failure_reason(exc)}")
    return outcome


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


def _woocommerce_address_text(address: dict | None) -> str | None:
    """Flatten a WooCommerce billing/shipping address into the CRM's address field."""
    address = address or {}
    locality = ", ".join(
        str(v).strip() for v in (
            address.get("city"), address.get("state"), address.get("postcode")
        ) if str(v or "").strip()
    )
    parts = [
        address.get("company"),
        address.get("address_1"),
        address.get("address_2"),
        locality,
        address.get("country"),
    ]
    text = "\n".join(str(v).strip() for v in parts if str(v or "").strip())
    return text or None


async def upsert_contact_from_woocommerce(company_id: str, customer: dict) -> str:
    """Create/update a WooCommerce customer with complete billing/shipping details."""
    idem_key = f"woocommerce:customer:{customer['id']}"
    billing = customer.get("billing") or {}
    shipping = customer.get("shipping") or {}
    first = customer.get("first_name") or billing.get("first_name") or ""
    last = customer.get("last_name") or billing.get("last_name") or ""
    email = customer.get("email") or billing.get("email")
    name = " ".join(p for p in (first, last) if p).strip() or email or f"woocommerce:{customer['id']}"
    data = {
        "name": name,
        "email": email,
        "phone": customer.get("phone") or billing.get("phone"),
        "billing_address": _woocommerce_address_text(billing),
        "shipping_address": _woocommerce_address_text(shipping) or _woocommerce_address_text(billing),
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
