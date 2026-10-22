# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""
Country-based tax regime defaults.

Keyed by ISO 3166-1 alpha-2 country code (uppercase).
Used to seed company.settings["taxes"] and company.settings["currency"]
when a default location with a known country is first set.

Each entry:
  currency: str          ISO 4217 code
  taxes: list[dict]      matches the TaxRate schema in routers/companies.py

"_default" is the fallback for unknown/unset country.
"""
from __future__ import annotations

import copy

from celerp.services.list_behavior import SUPPLIER_DOC_TYPES

_T = dict  # type alias for readability

TAX_REGIMES: dict[str, _T] = {
    # ── Southeast Asia ────────────────────────────────────────────────────
    "TH": {
        "currency": "THB",
        "taxes": [
            {"name": "VAT 7%", "rate": 7.0, "tax_type": "both", "is_default": True,
             "description": "Thailand standard VAT", "is_compound": False, "default_order": 0},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "VAT-exempt", "is_compound": False, "default_order": 0},
            {"name": "WHT 3%", "rate": -3.0, "tax_type": "purchase", "is_default": False,
             "description": "Withholding tax on services", "is_compound": False, "default_order": 1},
            {"name": "WHT 5%", "rate": -5.0, "tax_type": "purchase", "is_default": False,
             "description": "Withholding tax on rentals", "is_compound": False, "default_order": 1},
        ],
    },
    "SG": {
        "currency": "SGD",
        "taxes": [
            {"name": "GST 9%", "rate": 9.0, "tax_type": "both", "is_default": True,
             "description": "Singapore GST", "is_compound": False, "default_order": 0},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "GST-exempt", "is_compound": False, "default_order": 0},
        ],
    },
    "MY": {
        "currency": "MYR",
        "taxes": [
            {"name": "SST 6%", "rate": 6.0, "tax_type": "sales", "is_default": True,
             "description": "Malaysia Sales and Service Tax", "is_compound": False, "default_order": 0},
            {"name": "Service Tax 8%", "rate": 8.0, "tax_type": "sales", "is_default": False,
             "description": "Malaysia service tax (selected services)", "is_compound": False, "default_order": 0},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "Tax-exempt", "is_compound": False, "default_order": 0},
        ],
    },
    "PH": {
        "currency": "PHP",
        "taxes": [
            {"name": "VAT 12%", "rate": 12.0, "tax_type": "both", "is_default": True,
             "description": "Philippines VAT", "is_compound": False, "default_order": 0},
            {"name": "Zero-rated", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "Zero-rated VAT (exports)", "is_compound": False, "default_order": 0},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "VAT-exempt", "is_compound": False, "default_order": 0},
            {"name": "EWT 2%", "rate": -2.0, "tax_type": "purchase", "is_default": False,
             "description": "Expanded withholding tax", "is_compound": False, "default_order": 1},
        ],
    },
    "ID": {
        "currency": "IDR",
        "taxes": [
            {"name": "PPN 11%", "rate": 11.0, "tax_type": "both", "is_default": True,
             "description": "Indonesia VAT (PPN)", "is_compound": False, "default_order": 0},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "PPN-exempt", "is_compound": False, "default_order": 0},
            {"name": "PPh 2%", "rate": -2.0, "tax_type": "purchase", "is_default": False,
             "description": "Income tax withholding (PPh 23)", "is_compound": False, "default_order": 1},
        ],
    },
    "VN": {
        "currency": "VND",
        "taxes": [
            {"name": "VAT 10%", "rate": 10.0, "tax_type": "both", "is_default": True,
             "description": "Vietnam standard VAT", "is_compound": False, "default_order": 0},
            {"name": "VAT 5%", "rate": 5.0, "tax_type": "both", "is_default": False,
             "description": "Vietnam reduced VAT (essential goods)", "is_compound": False, "default_order": 0},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "VAT-exempt", "is_compound": False, "default_order": 0},
        ],
    },
    # ── Oceania ───────────────────────────────────────────────────────────
    "AU": {
        "currency": "AUD",
        "taxes": [
            {"name": "GST 10%", "rate": 10.0, "tax_type": "both", "is_default": True,
             "description": "Australia GST", "is_compound": False, "default_order": 0},
            {"name": "GST-free", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "GST-free supply", "is_compound": False, "default_order": 0},
        ],
    },
    "NZ": {
        "currency": "NZD",
        "taxes": [
            {"name": "GST 15%", "rate": 15.0, "tax_type": "both", "is_default": True,
             "description": "New Zealand GST", "is_compound": False, "default_order": 0},
            {"name": "Zero-rated", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "Zero-rated GST (exports)", "is_compound": False, "default_order": 0},
        ],
    },
    # ── Europe ────────────────────────────────────────────────────────────
    "GB": {
        "currency": "GBP",
        "taxes": [
            {"name": "VAT 20%", "rate": 20.0, "tax_type": "both", "is_default": True,
             "description": "UK standard VAT", "is_compound": False, "default_order": 0},
            {"name": "VAT 5%", "rate": 5.0, "tax_type": "both", "is_default": False,
             "description": "UK reduced rate VAT", "is_compound": False, "default_order": 0},
            {"name": "Zero-rated", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "Zero-rated VAT", "is_compound": False, "default_order": 0},
        ],
    },
    "DE": {
        "currency": "EUR",
        "taxes": [
            {"name": "MwSt 19%", "rate": 19.0, "tax_type": "both", "is_default": True,
             "description": "Germany standard VAT", "is_compound": False, "default_order": 0},
            {"name": "MwSt 7%", "rate": 7.0, "tax_type": "both", "is_default": False,
             "description": "Germany reduced VAT", "is_compound": False, "default_order": 0},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "VAT-exempt", "is_compound": False, "default_order": 0},
        ],
    },
    "FR": {
        "currency": "EUR",
        "taxes": [
            {"name": "TVA 20%", "rate": 20.0, "tax_type": "both", "is_default": True,
             "description": "France standard VAT", "is_compound": False, "default_order": 0},
            {"name": "TVA 10%", "rate": 10.0, "tax_type": "both", "is_default": False,
             "description": "France intermediate rate", "is_compound": False, "default_order": 0},
            {"name": "TVA 5.5%", "rate": 5.5, "tax_type": "both", "is_default": False,
             "description": "France reduced rate", "is_compound": False, "default_order": 0},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "VAT-exempt", "is_compound": False, "default_order": 0},
        ],
    },
    # ── North America ─────────────────────────────────────────────────────
    "US": {
        "currency": "USD",
        "taxes": [
            # No federal VAT; state/local sales taxes vary. User must configure.
            {"name": "Sales Tax", "rate": 0.0, "tax_type": "sales", "is_default": True,
             "description": "State and local sales tax. Set the rate for your area",
             "is_compound": False, "default_order": 0},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "Tax-exempt", "is_compound": False, "default_order": 0},
        ],
    },
    "CA": {
        "currency": "CAD",
        "taxes": [
            {"name": "GST 5%", "rate": 5.0, "tax_type": "both", "is_default": True,
             "description": "Canada federal GST", "is_compound": False, "default_order": 0},
            {"name": "PST", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "Provincial sales tax. Set the rate for your province",
             "is_compound": False, "default_order": 1},
            {"name": "QST 9.975%", "rate": 9.975, "tax_type": "both", "is_default": False,
             "description": "Quebec sales tax, charged on the price before GST",
             "is_compound": False, "default_order": 1},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "Tax-exempt", "is_compound": False, "default_order": 0},
        ],
    },
    # ── Asia ──────────────────────────────────────────────────────────────
    "IN": {
        "currency": "INR",
        "taxes": [
            {"name": "CGST 9%", "rate": 9.0, "tax_type": "both", "is_default": False,
             "description": "India central GST (standard rate split)", "is_compound": False, "default_order": 0},
            {"name": "SGST 9%", "rate": 9.0, "tax_type": "both", "is_default": False,
             "description": "India state GST (standard rate split)", "is_compound": False, "default_order": 0},
            {"name": "IGST 18%", "rate": 18.0, "tax_type": "both", "is_default": True,
             "description": "Integrated GST on sales between states. Within a state, 18% is charged as "
                            "CGST 9% plus SGST 9%", "is_compound": False, "default_order": 0},
            {"name": "GST 5%", "rate": 5.0, "tax_type": "both", "is_default": False,
             "description": "India reduced GST rate", "is_compound": False, "default_order": 0},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "GST-exempt", "is_compound": False, "default_order": 0},
            {"name": "TDS 2%", "rate": -2.0, "tax_type": "purchase", "is_default": False,
             "description": "Tax deducted at source", "is_compound": False, "default_order": 1},
        ],
    },
    "JP": {
        "currency": "JPY",
        "taxes": [
            {"name": "消費税 10%", "rate": 10.0, "tax_type": "both", "is_default": True,
             "description": "Japan consumption tax (standard)", "is_compound": False, "default_order": 0},
            {"name": "消費税 8%", "rate": 8.0, "tax_type": "both", "is_default": False,
             "description": "Japan consumption tax (reduced rate for food and drink)", "is_compound": False, "default_order": 0},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "Tax-exempt", "is_compound": False, "default_order": 0},
        ],
    },
    # ── Africa ────────────────────────────────────────────────────────────
    "ET": {
        "currency": "ETB",
        "taxes": [
            {"name": "VAT 15%", "rate": 15.0, "tax_type": "both", "is_default": True,
             "description": "Ethiopia standard VAT", "is_compound": False, "default_order": 0},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "VAT-exempt", "is_compound": False, "default_order": 0},
            {"name": "WHT 3%", "rate": -3.0, "tax_type": "purchase", "is_default": False,
             "description": "Withholding on goods contracts above ETB 20,000 and services from ETB 10,000. "
                            "30% when the supplier gives no TIN", "is_compound": False, "default_order": 1},
        ],
    },
    # ── Fallback ──────────────────────────────────────────────────────────
    "_default": {
        "currency": "USD",
        "taxes": [
            {"name": "Standard Tax", "rate": 0.0, "tax_type": "both", "is_default": True,
             "description": "Set your local tax rate", "is_compound": False, "default_order": 0},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "Tax-exempt", "is_compound": False, "default_order": 0},
        ],
    },
}


def get_regime(country_code: str | None) -> dict:
    """Return the tax regime for a country code (case-insensitive). Falls back to _default."""
    if not country_code:
        return TAX_REGIMES["_default"]
    return TAX_REGIMES.get(country_code.strip().upper(), TAX_REGIMES["_default"])


def generic_taxes() -> list[dict]:
    """The neutral tax list a company starts from until it configures its own."""
    return copy.deepcopy(TAX_REGIMES["_default"]["taxes"])


def configured(settings: dict, key: str, default: list[dict]) -> list[dict]:
    """The list saved under ``key``, an explicitly saved empty list included; a copy
    of ``default`` only when nothing was ever saved."""
    value = settings.get(key)
    return value if value is not None else copy.deepcopy(default)


def sales_taxes(settings: dict) -> list[dict]:
    """The company's sales taxes: what it saved, else the generic list."""
    return configured(settings, "taxes", generic_taxes())


def purchasing_taxes(settings: dict) -> list[dict]:
    """The company's purchasing taxes: what it saved, else a copy of its sales taxes."""
    return configured(settings, "purchasing_taxes", sales_taxes(settings))


def doc_side(doc_type: str | None) -> str:
    """The side a document's taxes apply to: "purchase" for a supplier document
    (SUPPLIER_DOC_TYPES), else "sales"."""
    return "purchase" if doc_type in SUPPLIER_DOC_TYPES else "sales"


def side_taxes(settings: dict, doc_type: str | None) -> list[dict]:
    """The tax list a document of this type draws from: purchasing taxes for a purchase
    document, sales taxes for any other."""
    return purchasing_taxes(settings) if doc_side(doc_type) == "purchase" else sales_taxes(settings)


def taxes_for(taxes: list[dict], doc_type: str | None) -> list[dict]:
    """The taxes a document of this type can carry: those for its side and those for both."""
    side = doc_side(doc_type)
    return [x for x in taxes if (x.get("tax_type") or "both") in (side, "both")]


def default_tax(taxes: list[dict], doc_type: str | None) -> dict | None:
    """The tax a new line on a document of this type starts with: the company default, when it
    applies to that side (a purchase document takes no sales-only tax, a sale no purchase-only one)."""
    return next((x for x in taxes_for(taxes, doc_type) if x.get("is_default")), None)


def _tax_name_key(name) -> str:
    """A tax name as a lookup key: the same name in another case or with spaces around it
    is the same tax."""
    return str(name or "").strip().casefold()


def misplaced_tax(settings: dict, doc_type: str | None, applied: list[dict]) -> str | None:
    """The name of the first tax in ``applied`` (each a ``code`` and ``rate``) a document of
    this type cannot carry, else None. A tax takes its side from the company's tax lists by
    name, ignoring case and surrounding spaces, the document's own side's list deciding a name
    both lists hold. On a sale a negative rate is always refused: it is tax withheld on a
    purchase, and a sale has no output tax to net it against."""
    side = doc_side(doc_type)
    other = sales_taxes(settings) if side == "purchase" else purchasing_taxes(settings)
    sides = {_tax_name_key(x.get("name")): x.get("tax_type") or "both"
             for x in [*other, *side_taxes(settings, doc_type)]}
    for tax in applied:
        code = str(tax.get("code") or "").strip()
        try:
            rate = float(tax.get("rate") or 0)
        except (TypeError, ValueError):
            rate = 0.0  # an unreadable rate is refused where the money is computed
        tax_side = "purchase" if side == "sales" and rate < 0 else sides.get(_tax_name_key(code), "both")
        if tax_side not in (side, "both"):
            return code or f"{rate:g}%"
    return None
