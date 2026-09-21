# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Pure customer-facing document data fallback."""
from __future__ import annotations


def _company_value(company: dict, key: str):
    value = company.get(key)
    if value not in (None, ""):
        return value
    return (company.get("settings") or {}).get(key)


def _address_text(raw) -> str:
    if not raw:
        return ""
    if isinstance(raw, dict):
        if raw.get("text"):
            return str(raw["text"])
        return ", ".join(
            str(raw.get(key) or "")
            for key in ("line1", "line2", "city", "state", "postal_code", "country")
            if raw.get(key)
        )
    return str(raw)


def _primary_address(contact: dict, address_type: str) -> dict:
    addresses = [
        a for a in (contact.get("addresses") or [])
        if isinstance(a, dict) and a.get("address_type") == address_type
    ]
    return next((a for a in addresses if a.get("is_default")), None) or (addresses[0] if addresses else {})


def prepare_document_output(
    document: dict,
    *,
    company: dict | None = None,
    self_contact: dict | None = None,
    contact: dict | None = None,
) -> dict:
    """Fill only missing customer-facing fields; stored document values always win."""
    out = dict(document or {})
    company = company or {}
    self_contact = self_contact or {}
    contact = contact or {}

    # "terms" is the historical customer-facing field. Internal "notes" is
    # deliberately never a fallback here.
    if not out.get("terms_text") and out.get("terms"):
        out["terms_text"] = out["terms"]

    self_billing = _primary_address(self_contact, "billing")
    seller = {
        "company_name": (
            self_contact.get("company_name")
            or _company_value(company, "name")
            or self_contact.get("name")
            or ""
        ),
        "company_address": (
            self_contact.get("billing_address")
            or _address_text(self_billing)
            or _address_text(_company_value(company, "address"))
        ),
        "company_phone": self_contact.get("phone") or _company_value(company, "phone") or "",
        "company_tax_id": self_contact.get("tax_id") or _company_value(company, "tax_id") or "",
        "company_email": self_contact.get("email") or _company_value(company, "email") or "",
    }
    for key, value in seller.items():
        if not out.get(key) and value:
            out[key] = value

    billing = _primary_address(contact, "billing")
    shipping = _primary_address(contact, "shipping")
    customer = {
        "contact_name": contact.get("name") or "",
        "contact_company_name": contact.get("company_name") or "",
        "contact_email": contact.get("email") or "",
        "contact_phone": contact.get("phone") or "",
        "contact_tax_id": contact.get("tax_id") or "",
        "contact_billing_address": contact.get("billing_address") or contact.get("address") or _address_text(billing),
        "contact_shipping_address": contact.get("shipping_address") or _address_text(shipping),
        "contact_billing_attn": billing.get("attn") or "",
        "shipping_attn": shipping.get("attn") or "",
    }
    for key, value in customer.items():
        if not out.get(key) and value:
            out[key] = value

    return out
