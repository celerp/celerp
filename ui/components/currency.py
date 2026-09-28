# Copyright (c) 2026 Data Universal Limited
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Currency combobox widget."""

from __future__ import annotations

from fasthtml.common import *

from celerp.services.currencies import CURRENCIES, currency_label


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
                type="text", value=display_val, placeholder=t("currency.search_placeholder"),
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
