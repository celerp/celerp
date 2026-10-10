# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Contact references held by other records (Documents, Lists, Deals, consigned stock).

Every writer that makes a record point at a contact, or that retires a contact,
locks the contact rows through lock_contacts(), then the dependent record.
lock_contacts() takes the company lock before the contact rows, so the order is
company, then contacts, then records. One lock order everywhere means a selection cannot commit a reference to a contact
that a concurrent merge or delete has already tombstoned.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.projections import Projection
from celerp.services.company_lock import lock_projections
from celerp_contacts.projections import _compose_address

CONTACT_TYPE_FILTER: dict[str, tuple[str, ...]] = {
    "customer": ("customer", "both"),
    "vendor": ("vendor", "both"),
    "both": ("both",),
}


async def lock_contacts(session: AsyncSession, company_id, contact_ids) -> dict[str, Projection]:
    """Lock the contact rows for contact_ids FOR UPDATE in sorted id order.

    Sorting keeps two writers that share contacts (merge vs merge, merge vs delete)
    from locking them in opposite orders. Ids without a local contact row are absent
    from the result; the caller decides whether that is an error.
    """
    rows = await lock_projections(session, company_id, contact_ids)
    return {eid: r for eid, r in rows.items() if r.entity_type == "contact"}


async def lock_referencing_records(
    session: AsyncSession, company_id, contact_ids, *, entity_types: tuple[str, ...] = ("doc", "list"),
    field: str = "contact_id",
) -> list[Projection]:
    """Lock the records of entity_types whose ``field`` names one of contact_ids, in entity-id order.

    Callers lock the contacts themselves first (lock_contacts), the order every
    contact-reference writer takes.
    """
    want = sorted({str(c) for c in contact_ids if c})
    if not want:
        return []
    return list((await session.execute(
        select(Projection)
        .where(
            Projection.company_id == company_id,
            Projection.entity_type.in_(entity_types),
            Projection.state[field].as_string().in_(want),
        )
        .order_by(Projection.entity_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all())


def contact_accepts(state: dict, contact_type: str) -> bool:
    """Whether a contact serves as contact_type (customer or vendor). An untyped contact is a customer."""
    return (state.get("contact_type") or "customer") in CONTACT_TYPE_FILTER[contact_type]


def contact_snapshot(state: dict) -> dict:
    """Header fields a Document or List copies from its selected contact.

    Address precedence (billing and shipping alike): the default address of that type,
    else the first address of that type, else the contact's top-level address field.
    The shipping attention comes from the same address the shipping text does.
    """
    addresses = state.get("addresses") or []

    def _pick(addr_type: str) -> dict | None:
        typed = [a for a in addresses if a.get("address_type") == addr_type]
        return next((a for a in typed if a.get("is_default")), typed[0] if typed else None)

    def _text(addr_type: str) -> str:
        a = _pick(addr_type)
        if a:
            return a.get("full_address") or a.get("address") or _compose_address(a) or a.get("label") or ""
        return state.get(f"{addr_type}_address") or ""

    shipping = _pick("shipping")
    return {
        "contact_name": state.get("name") or state.get("display_name") or "",
        "contact_company_name": state.get("company_name") or "",
        "contact_email": state.get("email") or "",
        "contact_phone": state.get("phone") or "",
        "contact_tax_id": state.get("tax_id") or "",
        "contact_billing_address": _text("billing"),
        "contact_shipping_address": _text("shipping"),
        "shipping_attn": (shipping.get("attn") or "") if shipping else "",
    }
