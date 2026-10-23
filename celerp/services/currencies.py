# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Supported currencies: the one list every screen and route validates against."""
from __future__ import annotations

from fastapi import HTTPException

from ui.i18n import t

# The currencies a company, contact, account or document may be kept in: ISO 4217's active
# circulating currencies (list one, as SIX Group maintains it), without its fund, metal and
# testing codes, in display order. Each one's name is the catalog key currency.name.<code>.
CURRENCIES: tuple[str, ...] = (
    "AED", "AFN", "ALL", "AMD", "AOA", "ARS", "AUD", "AWG", "AZN", "BAM", "BBD", "BDT", "BHD",
    "BIF", "BMD", "BND", "BOB", "BRL", "BSD", "BTN", "BWP", "BYN", "BZD", "CAD", "CDF", "CHF",
    "CLP", "CNY", "COP", "CRC", "CUP", "CVE", "CZK", "DJF", "DKK", "DOP", "DZD", "EGP", "ERN",
    "ETB", "EUR", "FJD", "FKP", "GBP", "GEL", "GHS", "GIP", "GMD", "GNF", "GTQ", "GYD", "HKD",
    "HNL", "HTG", "HUF", "IDR", "ILS", "INR", "IQD", "IRR", "ISK", "JMD", "JOD", "JPY", "KES",
    "KGS", "KHR", "KMF", "KPW", "KRW", "KWD", "KYD", "KZT", "LAK", "LBP", "LKR", "LRD", "LSL",
    "LYD", "MAD", "MDL", "MGA", "MKD", "MMK", "MNT", "MOP", "MRU", "MUR", "MVR", "MWK", "MXN",
    "MYR", "MZN", "NAD", "NGN", "NIO", "NOK", "NPR", "NZD", "OMR", "PAB", "PEN", "PGK", "PHP",
    "PKR", "PLN", "PYG", "QAR", "RON", "RSD", "RUB", "RWF", "SAR", "SBD", "SCR", "SDG", "SEK",
    "SGD", "SHP", "SLE", "SOS", "SRD", "SSP", "STN", "SVC", "SYP", "SZL", "THB", "TJS", "TMT",
    "TND", "TOP", "TRY", "TTD", "TWD", "TZS", "UAH", "UGX", "USD", "UYU", "UZS", "VED", "VES",
    "VND", "VUV", "WST", "XAF", "XCD", "XCG", "XOF", "XPF", "YER", "ZAR", "ZMW", "ZWG",
)
# Codes ISO 4217 has withdrawn that records made before still carry: read and shown with
# their names, never offered for anything new.
HISTORICAL: tuple[str, ...] = ("ANG", "BGN", "HRK", "ZWL")

CURRENCY_CODES: frozenset[str] = frozenset(CURRENCIES)
KNOWN_CURRENCY_CODES: frozenset[str] = CURRENCY_CODES | frozenset(HISTORICAL)


def require_currency_code(code: str | None) -> None:
    """Refuse a currency outside the supported list at a write boundary. Empty means no currency."""
    if code and code not in CURRENCY_CODES:
        raise HTTPException(status_code=422, detail=t("error.invalid_currency", value=code))


def require_phone(phone: str | None) -> None:
    """Refuse a phone with fewer than 4 digits or over 40 characters, not counting surrounding
    spaces, at a write boundary. Empty means no phone."""
    if phone and sum(c.isdigit() for c in str(phone)) < 4:
        raise HTTPException(status_code=422, detail=t("error.phone_too_short"))
    if phone and len(str(phone).strip()) > 40:
        raise HTTPException(status_code=422, detail=t("error.phone_too_long"))
