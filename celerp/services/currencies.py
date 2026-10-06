# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Supported currencies: the one list every screen and route validates against."""
from __future__ import annotations

from fastapi import HTTPException

from ui.i18n import t

# ISO-4217 codes, in display order. Add new currencies here only; each one's name
# is the catalog key currency.name.<code> in every UI language.
CURRENCIES: tuple[str, ...] = (
    "AED", "AUD", "BDT", "BRL", "CAD", "CHF", "CLP", "CNY", "COP", "CZK", "DKK", "EGP",
    "EUR", "GBP", "HKD", "HUF", "IDR", "ILS", "INR", "IRR", "JPY", "KRW", "KWD", "LAK",
    "LBP", "MXN", "MYR", "NGN", "NOK", "NZD", "PEN", "PHP", "PKR", "PLN", "QAR", "RON",
    "RUB", "SAR", "SEK", "SGD", "SLE", "THB", "TRY", "TWD", "UAH", "USD", "VND", "ZAR",
)

CURRENCY_CODES: frozenset[str] = frozenset(CURRENCIES)


def require_currency_code(code: str | None) -> None:
    """Refuse a currency outside the supported list at a write boundary. Empty means no currency."""
    if code and code not in CURRENCY_CODES:
        raise HTTPException(status_code=422, detail=t("error.invalid_currency", value=code))
