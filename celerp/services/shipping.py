# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Shipping-document domain data shared by the API, the UI, and the printouts.

A shipping document (list_type="shipping_doc") is one shipment record with two
paper renderings: the Delivery Note (no prices, signed by the receiver) and the
Commercial Invoice (customs values + declaration, required by cross-border
carriers). Everything the two papers need beyond the generic list spine is
declared here so no surface can drift.
"""
from __future__ import annotations

SHIPPING_LIST_TYPE = "shipping_doc"

# The 11 Incoterms 2020 codes - the full, closed set. Any-country shipments pick
# from these; free text would print terms customs brokers cannot parse.
INCOTERMS_2020: tuple[str, ...] = (
    "EXW", "FCA", "CPT", "CIP", "DAP", "DPU", "DDP", "FAS", "FOB", "CFR", "CIF",
)

# Reason-for-export values carriers accept on a commercial invoice, keyed by a
# stable code. The English labels below go on the printed paper (customs paperwork
# is filed in English); the UI translates screen labels via `reason_export.<code>`.
REASON_EXPORT_LABELS: dict[str, str] = {
    "sale": "Sale",
    "sample": "Sample",
    "repair_return": "Repair / return",
    "gift": "Gift",
    "personal_effects": "Personal effects",
    "not_for_resale": "Not for resale",
}
REASONS_FOR_EXPORT: tuple[str, ...] = tuple(REASON_EXPORT_LABELS)

# Shipment header fields, single source for the API payload, the detail page,
# and the printouts. contact_shipping_address / shipping_attn ride the existing
# Ship To fields every document already has. gross_weight is the FINAL weighed
# package (box and packing included), typed after weighing - per-line gross
# weights derive from each item's catalog gross_weight instead.
SHIPMENT_HEADER_FIELDS: tuple[str, ...] = (
    "carrier", "tracking", "incoterms", "package_count", "gross_weight",
    "reason_for_export", "country_of_export", "country_of_destination", "importer",
)


def line_gross_weight(line: dict, item_state: dict, qty_is_weight: bool) -> tuple[float | None, str]:
    """A line's gross (packaged) weight: the item's catalog gross_weight x quantity.

    Only defined for piece-quantity lines - when the quantity IS a weight, the
    per-unit multiplication would be meaningless, so we return None and the
    paper prints blank (never a fabricated figure).
    """
    per_unit = item_state.get("gross_weight")
    if not per_unit or qty_is_weight:
        return None, ""
    try:
        qty = float(line.get("quantity") or 0)
        return (float(per_unit) * qty) or None, str(item_state.get("gross_weight_unit") or "")
    except (TypeError, ValueError):
        return None, ""


def customs_backfill(line: dict, item_state: dict) -> None:
    """Fill a line's customs fields from its catalog item when the line lacks them.

    The item is the durable home of HS code / country of origin; a line only
    overrides it. Used by both print-enrichment paths (UI and share view).
    """
    for key in ("hs_code", "country_of_origin"):
        if not line.get(key):
            v = item_state.get(key)
            if v:
                line[key] = v


def missing_customs_fields(state: dict) -> list[str]:
    """What a commercial invoice still needs before it can clear customs.

    Returns stable field codes (i18n keys resolve the labels): header codes as-is,
    line-level gaps as "<code>:<n>" where n = how many lines lack the value.
    Empty list = customs-ready. Warn, never block (GDR): the printout renders
    blanks for these, it never fabricates values.
    """
    missing: list[str] = []
    for key in ("incoterms", "reason_for_export"):
        if not state.get(key):
            missing.append(key)
    lines = state.get("line_items") or []
    no_hs = sum(1 for l in lines if not l.get("hs_code"))
    no_origin = sum(1 for l in lines if not l.get("country_of_origin"))
    no_value = sum(1 for l in lines if not float(l.get("unit_price") or 0))
    if no_hs:
        missing.append(f"hs_code:{no_hs}")
    if no_origin:
        missing.append(f"country_of_origin:{no_origin}")
    if no_value:
        missing.append(f"customs_value:{no_value}")
    return missing
