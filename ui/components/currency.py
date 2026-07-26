# Copyright (c) 2026 Data Universal Limited
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Currency data and combobox widget - single source of truth for all currency UI."""

from __future__ import annotations

from fasthtml.common import *

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


def currency_combobox_td(
    *,
    value: str,
    hidden_id: str,
    patch_url: str,
    cancel_url: str,
    target: str = "closest td",
    include: str | None = None,
) -> FT:
    """Searchable currency combobox wrapped in a Td for inline-edit cells.

    Args:
        value: currently selected ISO-4217 code.
        hidden_id: id for the hidden value input (used by hx_include).
        patch_url: HTMX PATCH URL for the save button.
        cancel_url: HTMX GET URL for the cancel button (restores display cell).
        target: HTMX swap target for both buttons (default "closest td").
        include: CSS selector for hx_include on save (default f"#{hidden_id}").
    """
    from ui.i18n import t
    display_val = currency_label(value)
    _include = include or f"#{hidden_id}"
    return Td(
        Div(
            Input(
                type="text", value=display_val, placeholder="Search currency...",
                cls="cell-input combobox-input", autofocus=True,
            ),
            Input(type="hidden", name="value", value=value, id=hidden_id),
            Div(
                *[Div(
                    Span(f"{code} – {lbl.split('–', 1)[-1].strip()}"),
                    cls="combobox-option",
                    data_value=code,
                    data_search=f"{code} {lbl}".lower(),
                ) for code, lbl in CURRENCIES],
                cls="combobox-list",
            ),
            cls="combobox-wrap",
        ),
        Button(t("btn.save"), type="button",
               hx_patch=patch_url,
               hx_target=target, hx_swap="outerHTML",
               hx_include=_include,
               cls="btn btn--primary btn--xs ml-sm"),
        Button(t("btn.cancel"), type="button",
               hx_get=cancel_url,
               hx_target=target, hx_swap="outerHTML",
               cls="btn btn--secondary btn--xs ml-xs"),
        cls="cell cell--editing",
    )
