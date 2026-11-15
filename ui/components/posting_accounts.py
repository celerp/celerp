# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The account picker for a posting role, shared by finishing a migration and settings, the
item label the posting and reconcile pages share, and the income account cell items and
categories share.

Offers the chart accounts that can serve the role and, where one is proposed, adding
a new account. The picker is always searchable: a chart can grow past ten accounts (rule i).
"""
from __future__ import annotations

from fasthtml.common import *

from urllib.parse import urlencode

import ui.api_client as api
from celerp.accounting_roles import INCOME_ACCOUNT_FIELD, ROLE_TYPES, AccountRole, account_label
from celerp.services.permissions import role_has_permission
from ui.components.shell import module_active
from ui.components.table import EMPTY, display_cell, display_enum, editable_cell, searchable_select
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
    options = [(c["code"], account_label(c)) for c in candidates]
    if proposal:
        options.append((NEW_ACCOUNT, proposal_label(proposal)))
    return searchable_select(name, options, value=value, placeholder=t("posting.choose"),
                             aria_label=aria_label, **attrs)


INCOME_ACCOUNT_URL = "/settings/accounting/income-account"
INCOME_ACCOUNT_KINDS = ("item", "category")


def income_accounts(chart: list[dict]) -> list[dict]:
    """The accounts a sale can be credited to: active revenue accounts with nothing under them."""
    parents = {a.get("parent_code") for a in chart}
    return [a for a in chart if a.get("is_active") and a.get("code") not in parents
            and a.get("account_type") in ROLE_TYPES[AccountRole.SALES_REVENUE]]


def income_account_url(action: str, kind: str, key: str) -> str:
    """The income account cell route for ``action`` (edit, display, or "" to save) of one
    item or category (``kind``)."""
    return f"{INCOME_ACCOUNT_URL}{'/' + action if action else ''}?{urlencode({'kind': kind, 'key': key})}"


def income_account_cell(kind: str, key: str, code: str | None, chart: list[dict], *, editable: bool,
                        inherited: str | None = None) -> FT:
    """The income account an item or category is sold on, click to edit, "--" when it names
    none; an item shows its category's account greyed (``inherited``) until it names its own."""
    accounts = {a["code"]: a for a in chart}
    return display_cell(entity_id=key, field="income_account", value=code or "", editable=editable,
                        edit_url=income_account_url("edit", kind, key),
                        label_map={c: account_label(a) for c, a in accounts.items()},
                        placeholder=t("item.income_account_from_category",
                                      account=account_label(accounts.get(inherited) or {"code": inherited}))
                        if inherited else None)


def income_account_editor(kind: str, key: str, code: str | None, chart: list[dict], error: str = "") -> FT:
    """The searchable picker of income accounts in place of the cell; "--" clears it. A refused
    save keeps the picker open with the reason under it; Escape puts the cell back."""
    options = [("", EMPTY), *((a["code"], account_label(a)) for a in income_accounts(chart))]
    cell = editable_cell(entity_id=key, field="income_account", value=code or "", cell_type="select",
                         options=options, searchable=True, aria_label=t("item.income_account"),
                         patch_url=income_account_url("", kind, key),
                         restore_url=income_account_url("display", kind, key))
    if error:
        cell.attrs["class"] = f'{cell.attrs.get("class", "")} cell--error'.strip()
        cell = cell(P(error, cls="cell-error"))
    return cell


async def income_account_state(token: str, kind: str, key: str) -> tuple[str | None, str | None, list[dict]]:
    """(the account an item or category names, the one an item inherits from its category,
    the chart) for the income account cell."""
    chart = (await api.get_chart(token)).get("items", [])
    mapping = await api.get_category_income_accounts(token)
    if kind == "category":
        return mapping.get(key), None, chart
    item = await api.get_item(token, key)
    return item.get(INCOME_ACCOUNT_FIELD), mapping.get(str(item.get("category") or "")), chart


async def income_account_view(token: str, kind: str, key: str, settings: dict, role: str) -> FT | None:
    """The income account cell of an item or category, editable with the permission its save
    needs (edit_inventory, manage_accounting). None when the company keeps no accounts or the
    role cannot read the chart (view_financial_reports)."""
    if not module_active(settings, "celerp-accounting") or not role_has_permission(settings, role, "view_financial_reports"):
        return None
    code, inherited, chart = await income_account_state(token, kind, key)
    editable = role_has_permission(settings, role, "edit_inventory" if kind == "item" else "manage_accounting")
    return income_account_cell(kind, key, code, chart, editable=editable, inherited=inherited)
