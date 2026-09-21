# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Terms and Conditions policy shared by settings and document creation."""
from __future__ import annotations

from copy import deepcopy


DEFAULT_TERMS_CONDITIONS: list[dict] = [
    {"name": "Standard Sales Terms", "text": "Goods remain property of the seller until paid in full.", "doc_types": ["invoice", "receipt", "credit_note"], "default_for": ["invoice", "receipt", "credit_note"]},
    {"name": "Standard Consignment Out Terms", "text": "Consigned goods remain property of the consignor until sold or returned.", "doc_types": ["memo"], "default_for": ["memo"]},
    {"name": "Standard Purchase Terms", "text": "Goods must conform to agreed specifications.", "doc_types": ["purchase_order", "bill"], "default_for": ["purchase_order", "bill"]},
    {"name": "Standard Consignment In Terms", "text": "Consigned goods remain property of the consignor. Unsold goods may be returned per agreed schedule.", "doc_types": ["consignment_in"], "default_for": ["consignment_in"]},
]


def normalize_terms_templates(templates: list[dict]) -> list[dict]:
    """Return independent templates in the current default_for shape."""
    out = deepcopy(templates)
    for template in out:
        if "default_for" not in template:
            template["default_for"] = (
                list(template.get("doc_types") or [])
                if template.get("is_default")
                else []
            )
        template.pop("is_default", None)
    return out


def terms_templates(settings: dict | None) -> list[dict]:
    """Return the effective templates without mutating company settings."""
    configured = (settings or {}).get("terms_conditions")
    return normalize_terms_templates(configured or DEFAULT_TERMS_CONDITIONS)


def default_terms_for(settings: dict | None, doc_type: str) -> dict | None:
    """Return the configured default template for doc_type, if any."""
    return next(
        (
            template
            for template in terms_templates(settings)
            if doc_type in (template.get("default_for") or [])
        ),
        None,
    )
