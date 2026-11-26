# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""What an imported tax code is: each source system's tax types mapped to a Celerp tax
category and the side the code applies to, in one table every importer reads. A type with no
row here has no category; the sink refuses that code by name and imports the rest."""
from __future__ import annotations

import re

# Per source system: (type pattern, category, side). A pattern matches the whole source type,
# ignoring case; the first matching row wins. Xero and QuickBooks Online name a code by its
# type; Manager, Odoo and Sage 50 types are the reader's description of the source's settings.
_TYPES: dict[str, list[tuple[str, str, str]]] = {
    "xero": [
        (r".*reverse charge.*", "reverse_charge", "purchase"),
        (r".*\((vat|gst) on (income|capital sales)\)|(vat|gst) on income|tax on sales", "standard", "sales"),
        (r".*\((vat|gst) on (expenses|capital purchases)\)|(vat|gst) on expenses|tax on purchases", "standard",
         "purchase"),
        (r"zero rated income|gst free income", "zero", "sales"),
        (r"zero rated expenses|gst free expenses", "zero", "purchase"),
        (r"exempt income", "exempt", "sales"),
        (r"exempt expenses|input taxed", "exempt", "purchase"),
        (r"no (vat|gst)|bas excluded|tax exempt", "out_of_scope", "both"),
    ],
    "quickbooks_online": [
        (r".*\brc\b.*", "reverse_charge", "purchase"),
        (r"[\d.]+% s", "standard", "both"),
        (r"[\d.]+% r", "reduced", "both"),
        (r"[\d.]+% z", "zero", "both"),
        (r"exempt", "exempt", "both"),
        (r"no vat|out of scope", "out_of_scope", "both"),
    ],
    "manager_io": [
        (r"reverse charge", "reverse_charge", "purchase"),
        (r"rate", "standard", "both"),
        (r"no rate", "zero", "both"),
    ],
    "odoo": [
        (r"(sale|purchase) withholding", "withholding", r"\1"),
        (r"(sale|purchase) zero", "zero", r"\1"),
        (r"sale|purchase", "standard", r"\g<0>"),
    ],
    "sage50": [
        (r"sales tax agency", "standard", "both"),
    ],
}
_SIDES = {"sale": "sales"}


def _row(system: str, source_type: str) -> tuple[str, str] | None:
    for pattern, category, side in _TYPES.get(system, []):
        if match := re.fullmatch(pattern, source_type.strip(), re.IGNORECASE):
            side = match.expand(side).lower()
            return category, _SIDES.get(side, side)
    return None


def tax_fields(system: str, source_type: str) -> dict:
    """The CIFTaxCode fields ``system``'s tax type ``source_type`` sets: its category (None when
    it has no mapping), its side (both when unmapped) and the source type itself."""
    category, side = _row(system, source_type) or (None, "both")
    return {"category": category, "tax_type": side, "source_tax_type": source_type}
