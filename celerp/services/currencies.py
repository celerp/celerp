# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Supported currencies: the one list every screen and route validates against."""
from __future__ import annotations

from fastapi import HTTPException

# Ordered list of (ISO-4217 code, human label). Add new currencies here only.
CURRENCIES: list[tuple[str, str]] = [
    ("AED", "AED – UAE Dirham"),
    ("AUD", "AUD – Australian Dollar"),
    ("BDT", "BDT – Bangladeshi Taka"),
    ("BRL", "BRL – Brazilian Real"),
    ("CAD", "CAD – Canadian Dollar"),
    ("CHF", "CHF – Swiss Franc"),
    ("CLP", "CLP – Chilean Peso"),
    ("CNY", "CNY – Chinese Yuan"),
    ("COP", "COP – Colombian Peso"),
    ("CZK", "CZK – Czech Koruna"),
    ("DKK", "DKK – Danish Krone"),
    ("EGP", "EGP – Egyptian Pound"),
    ("EUR", "EUR – Euro"),
    ("GBP", "GBP – British Pound"),
    ("HKD", "HKD – Hong Kong Dollar"),
    ("HUF", "HUF – Hungarian Forint"),
    ("IDR", "IDR – Indonesian Rupiah"),
    ("ILS", "ILS – Israeli Shekel"),
    ("INR", "INR – Indian Rupee"),
    ("IRR", "IRR – Iranian Rial"),
    ("JPY", "JPY – Japanese Yen"),
    ("KRW", "KRW – South Korean Won"),
    ("KWD", "KWD – Kuwaiti Dinar"),
    ("LAK", "LAK – Lao Kip"),
    ("LBP", "LBP – Lebanese Pound"),
    ("MXN", "MXN – Mexican Peso"),
    ("MYR", "MYR – Malaysian Ringgit"),
    ("NGN", "NGN – Nigerian Naira"),
    ("NOK", "NOK – Norwegian Krone"),
    ("NZD", "NZD – New Zealand Dollar"),
    ("PEN", "PEN – Peruvian Sol"),
    ("PHP", "PHP – Philippine Peso"),
    ("PKR", "PKR – Pakistani Rupee"),
    ("PLN", "PLN – Polish Złoty"),
    ("QAR", "QAR – Qatari Riyal"),
    ("RON", "RON – Romanian Leu"),
    ("RUB", "RUB – Russian Ruble"),
    ("SAR", "SAR – Saudi Riyal"),
    ("SEK", "SEK – Swedish Krona"),
    ("SGD", "SGD – Singapore Dollar"),
    ("SLE", "SLE – Sierra Leonean Leone"),
    ("THB", "THB – Thai Baht"),
    ("TRY", "TRY – Turkish Lira"),
    ("TWD", "TWD – Taiwan Dollar"),
    ("UAH", "UAH – Ukrainian Hryvnia"),
    ("USD", "USD – US Dollar"),
    ("VND", "VND – Vietnamese Dong"),
    ("ZAR", "ZAR – South African Rand"),
]

CURRENCY_CODES: frozenset[str] = frozenset(c for c, _ in CURRENCIES)

_LABEL_MAP: dict[str, str] = {c: lbl for c, lbl in CURRENCIES}


def currency_label(code: str) -> str:
    """Return the human-readable label for a currency code, or the code itself if unknown."""
    return _LABEL_MAP.get(code, code)


def require_currency_code(code: str | None) -> None:
    """Refuse a currency outside the supported list at a write boundary. Empty means no currency."""
    if code and code not in CURRENCY_CODES:
        raise HTTPException(status_code=422, detail=f"Invalid currency code: {code}")
