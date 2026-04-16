# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""
Country-based tax regime defaults.

Keyed by ISO 3166-1 alpha-2 country code (uppercase).
Used to seed company.settings["taxes"] and company.settings["currency"]
when a default location with a known country is first set.

Each entry:
  currency: str          — ISO 4217 code
  taxes: list[dict]      — matches TaxRate schema in routers/companies.py

"_default" is the fallback for unknown/unset country.
"""
from __future__ import annotations

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
             "description": "State/local sales tax — set rate for your jurisdiction",
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
             "description": "Provincial sales tax — set rate for your province",
             "is_compound": False, "default_order": 1},
            {"name": "QST 9.975%", "rate": 9.975, "tax_type": "both", "is_default": False,
             "description": "Quebec sales tax (compound — applies on GST-inclusive amount)",
             "is_compound": True, "default_order": 1},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "Tax-exempt", "is_compound": False, "default_order": 0},
        ],
    },
    # ── Asia ──────────────────────────────────────────────────────────────
    "IN": {
        "currency": "INR",
        "taxes": [
            {"name": "CGST 9%", "rate": 9.0, "tax_type": "both", "is_default": True,
             "description": "India central GST (standard rate split)", "is_compound": False, "default_order": 0},
            {"name": "SGST 9%", "rate": 9.0, "tax_type": "both", "is_default": True,
             "description": "India state GST (standard rate split)", "is_compound": False, "default_order": 0},
            {"name": "IGST 18%", "rate": 18.0, "tax_type": "both", "is_default": False,
             "description": "India integrated GST (interstate)", "is_compound": False, "default_order": 0},
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
             "description": "Japan consumption tax (reduced — food/drink)", "is_compound": False, "default_order": 0},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "Tax-exempt", "is_compound": False, "default_order": 0},
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
