# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The account picker for a posting role, shared by finishing a migration and settings, and
the item label the posting and reconcile pages share.

Offers the chart accounts that can serve the role and, where one is proposed, adding
a new account. More than ten options become a searchable picker (rule i).
"""
from __future__ import annotations

from fasthtml.common import *

from ui.components.table import _SEARCHABLE_THRESHOLD, display_enum, searchable_select
from ui.i18n import t

NEW_ACCOUNT = "__new__"


def distinct_name(sku: str | None, name: str | None) -> str:
    """An item's name when it says something its SKU does not, else empty."""
    return name if name and name != sku else ""


def proposal_label(proposal: dict) -> str:
    return t("posting.add_account", code=proposal["code"], name=proposal["name"],
             type=display_enum(proposal["account_type"], "account_type"))


def account_picker(name: str, candidates: list[dict], value: str = "", proposal: dict | None = None,
                   aria_label: str = "", **attrs) -> FT:
    """A picker submitting the chosen account code under ``name``, or NEW_ACCOUNT when
    the user chooses to add ``proposal``."""
    options = [(c["code"], f"{c['code']} {c['name']}") for c in candidates]
    if proposal:
        options.append((NEW_ACCOUNT, proposal_label(proposal)))
    if len(options) > _SEARCHABLE_THRESHOLD:
        return searchable_select(name, options, value=value, placeholder=t("posting.choose"),
                                 aria_label=aria_label, **attrs)
    return Select(
        Option(t("posting.choose"), value="", selected=not value),
        *[Option(label, value=code, selected=code == value) for code, label in options],
        name=name, cls="form-input", aria_label=aria_label or None, **attrs,
    )
