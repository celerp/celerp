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

from celerp.services.list_behavior import DRAFT

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


def reads_item_customs(state: dict) -> bool:
    """Whether a document's printout, share view and detail page read its items as they are now
    (customs codes via customs_backfill, weights, measures, barcode): a draft does, and so does an
    issued one whose lines were never saved with their items (``customs_saved``)."""
    return state.get("status") == DRAFT or not state.get("customs_saved")


# The item fields an issued customs document shows, saved on each linked line at Issue
# (``item_at_issue``): customs codes, the per-unit gross weight, the lot's measures the
# whole-lot fallback reads, and the barcode.
ITEM_AT_ISSUE = "item_at_issue"
_ITEM_AT_ISSUE_FIELDS = ("hs_code", "country_of_origin", "gross_weight", "gross_weight_unit", "sell_by",
                         "quantity", "pieces", "weight", "weight_unit", "barcode")


def item_at_issue(item_state: dict) -> dict:
    """What an issued customs document keeps of a line's item, attributes lifted to the top
    level the way the item reads flatten them (a core field is never overridden)."""
    attributes = item_state.get("attributes") or {}
    return {k: item_state.get(k, attributes.get(k)) for k in _ITEM_AT_ISSUE_FIELDS}


def issued_item(line: dict, state: dict) -> dict | None:
    """The item a line of ``state`` shows when the document keeps the item as it was at Issue,
    else None: the caller reads the item as it is now."""
    return None if reads_item_customs(state) else line.get(ITEM_AT_ISSUE)


def customs_backfill(line: dict, item_state: dict) -> None:
    """Fill a line's customs fields from its catalog item when the line lacks them.

    The item is the durable home of HS code / country of origin; a line only
    overrides it. A draft's printout and share view read them from the item;
    Issue saves them on the lines (celerp-docs, ``customs_saved``), so an issued
    document keeps them; one not saved yet still reads them from the item.
    ``item_state`` is the stored item or its flattened read: a field the item has no
    column for (country of origin) is stored in its attributes.
    """
    attributes = item_state.get("attributes") or {}
    for key in ("hs_code", "country_of_origin"):
        if not line.get(key):
            v = item_state.get(key) or attributes.get(key)
            if v:
                line[key] = v


def backfill_item_customs(line: dict, item_state: dict, state: dict) -> None:
    """customs_backfill on a line of a shipping list *state* that reads its items' customs codes
    (reads_item_customs). The one rule its page, printout and share view show them by."""
    if state.get("list_type") == SHIPPING_LIST_TYPE and reads_item_customs(state):
        customs_backfill(line, item_state)


# Country names for the export/destination pickers (English short names - customs
# paperwork is filed in English). Rendered as a searchable datalist, so any
# free-text value still saves; this list only powers the type-ahead.
COUNTRIES: tuple[str, ...] = (
    "Afghanistan", "Albania", "Algeria", "Andorra", "Angola", "Antigua and Barbuda",
    "Argentina", "Armenia", "Australia", "Austria", "Azerbaijan", "Bahamas", "Bahrain",
    "Bangladesh", "Barbados", "Belarus", "Belgium", "Belize", "Benin", "Bhutan",
    "Bolivia", "Bosnia and Herzegovina", "Botswana", "Brazil", "Brunei", "Bulgaria",
    "Burkina Faso", "Burundi", "Cabo Verde", "Cambodia", "Cameroon", "Canada",
    "Central African Republic", "Chad", "Chile", "China", "Colombia", "Comoros",
    "Congo", "Costa Rica", "Croatia", "Cuba", "Cyprus", "Czechia",
    "Democratic Republic of the Congo", "Denmark", "Djibouti", "Dominica",
    "Dominican Republic", "Ecuador", "Egypt", "El Salvador", "Equatorial Guinea",
    "Eritrea", "Estonia", "Eswatini", "Ethiopia", "Fiji", "Finland", "France",
    "Gabon", "Gambia", "Georgia", "Germany", "Ghana", "Greece", "Grenada",
    "Guatemala", "Guinea", "Guinea-Bissau", "Guyana", "Haiti", "Honduras",
    "Hong Kong", "Hungary", "Iceland", "India", "Indonesia", "Iran", "Iraq",
    "Ireland", "Israel", "Italy", "Ivory Coast", "Jamaica", "Japan", "Jordan",
    "Kazakhstan", "Kenya", "Kiribati", "Kosovo", "Kuwait", "Kyrgyzstan", "Laos",
    "Latvia", "Lebanon", "Lesotho", "Liberia", "Libya", "Liechtenstein",
    "Lithuania", "Luxembourg", "Macau", "Madagascar", "Malawi", "Malaysia",
    "Maldives", "Mali", "Malta", "Marshall Islands", "Mauritania", "Mauritius",
    "Mexico", "Micronesia", "Moldova", "Monaco", "Mongolia", "Montenegro",
    "Morocco", "Mozambique", "Myanmar", "Namibia", "Nauru", "Nepal",
    "Netherlands", "New Zealand", "Nicaragua", "Niger", "Nigeria",
    "North Korea", "North Macedonia", "Norway", "Oman", "Pakistan", "Palau",
    "Palestine", "Panama", "Papua New Guinea", "Paraguay", "Peru", "Philippines",
    "Poland", "Portugal", "Qatar", "Romania", "Russia", "Rwanda",
    "Saint Kitts and Nevis", "Saint Lucia", "Saint Vincent and the Grenadines",
    "Samoa", "San Marino", "Sao Tome and Principe", "Saudi Arabia", "Senegal",
    "Serbia", "Seychelles", "Sierra Leone", "Singapore", "Slovakia", "Slovenia",
    "Solomon Islands", "Somalia", "South Africa", "South Korea", "South Sudan",
    "Spain", "Sri Lanka", "Sudan", "Suriname", "Sweden", "Switzerland", "Syria",
    "Taiwan", "Tajikistan", "Tanzania", "Thailand", "Timor-Leste", "Togo",
    "Tonga", "Trinidad and Tobago", "Tunisia", "Turkey", "Turkmenistan",
    "Tuvalu", "Uganda", "Ukraine", "United Arab Emirates", "United Kingdom",
    "United States", "Uruguay", "Uzbekistan", "Vanuatu", "Vatican City",
    "Venezuela", "Vietnam", "Yemen", "Zambia", "Zimbabwe",
)
