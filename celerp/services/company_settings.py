# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Every company setting and the one place it changes.

``OWNERS`` names, for each key stored in ``Company.settings``, the route that changes it,
or ``SYSTEM`` when Celerp keeps the key itself and no user changes it directly. PATCH
/companies/me accepts only the ``GENERAL`` keys; any other key is refused with a keyed
message naming its route, and a key not in the table is refused as unknown. A setting
the books depend on changes only through its owning route, behind Manage accounting,
and records who changed it and when (``record_change``).
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException

from celerp.accounting_roles import refusal

SYSTEM = "system"

COMPANY_SETTINGS = "PATCH /companies/me"
BOOKS = "PATCH /companies/me/books"
PERIOD_LOCK = "POST /accounting/period-lock"
POSTING_ACCOUNTS = "PUT /accounting/posting-accounts/{role}"

OWNERS: dict[str, str] = {
    # General settings: no effect on the books.
    "timezone": COMPANY_SETTINGS,
    "phone": COMPANY_SETTINGS,
    "address": COMPANY_SETTINGS,
    "email": COMPANY_SETTINGS,
    "tax_id": COMPANY_SETTINGS,
    "reorder_alerts_enabled": COMPANY_SETTINGS,
    "reorder_alert_email": COMPANY_SETTINGS,
    # The order lots are picked in. Cost follows the lot that ships; nothing posted moves.
    "inventory_method": COMPANY_SETTINGS,
    "line_item_identifier": COMPANY_SETTINGS,
    "getting_started_dismissed": COMPANY_SETTINGS,
    "dashboard": COMPANY_SETTINGS,
    # Manufacturing preferences (automatic work orders, issue before complete).
    "manufacturing": COMPANY_SETTINGS,
    # The books.
    "currency": BOOKS,
    "fiscal_year_start": BOOKS,
    "import_vat_recoverable_default": BOOKS,
    "stripe_deposit_account": BOOKS,
    "woocommerce_deposit_account": BOOKS,
    # The date the opening balances are stated at: documents dated on or before it are
    # offered as already in them when imported.
    "opening_balance_date": BOOKS,
    "lock_date": PERIOD_LOCK,
    "posting_roles": POSTING_ACCOUNTS,
    "posting_role_scopes": POSTING_ACCOUNTS,
    "inventory_origin_schema": "PUT /accounting/posting-accounts/older-stock/{item_id}",
    # Lists and schemas with their own pages.
    "role_grants": "PATCH /companies/me/role-permissions",
    "vertical": "POST /companies/me/business-type",
    "enabled_modules": "POST /companies/me/modules/{module_name}/enable",
    "item_schema": "PATCH /companies/me/item-schema",
    "category_schemas": "PATCH /companies/me/category-schema/{category}",
    "category_display_names": "PATCH /companies/me/categories/{category_key}",
    "column_prefs": "PATCH /companies/me/column-prefs",
    "taxes": "PATCH /companies/me/taxes",
    "purchasing_taxes": "PATCH /companies/me/purchasing-taxes",
    "payment_terms": "PATCH /companies/me/payment-terms",
    "purchasing_payment_terms": "PATCH /companies/me/purchasing-payment-terms",
    "contact_tags": "PATCH /companies/me/contact-tags",
    "contact_defaults": "PATCH /companies/me/contact-defaults",
    "terms_conditions": "PATCH /companies/me/terms-conditions",
    "units": "PUT /companies/me/units",
    "price_lists": "PATCH /companies/me/price-lists",
    "base_price_list": "PATCH /companies/me/base-price-list",
    "default_price_list": "PATCH /companies/me/default-price-list",
    "restored_backup": "POST /company-backups/restore",
    # Kept by Celerp: written by the action that owns them, never set directly.
    "role_permissions": SYSTEM,  # retired storage of role grants
    "posting_roles_schema": SYSTEM,
    "posting_source_controls": SYSTEM,
    "sequences": SYSTEM,  # document numbering, advanced as documents are numbered
    "self_contact_id": SYSTEM,
    "ai_memory": SYSTEM,
    "reorder_last_scan_at": SYSTEM,
    "pay_tip_shown": SYSTEM,
    "erased_connector_items": SYSTEM,
}

GENERAL = frozenset(k for k, owner in OWNERS.items() if owner == COMPANY_SETTINGS)
BOOKS_KEYS = frozenset(k for k, owner in OWNERS.items() if owner == BOOKS)

# The audited keys and the stamp each change leaves: who (a person, so a company backup
# drops it) and when.
AUDITED = BOOKS_KEYS | {"lock_date", "posting_roles"}
SET_BY_KEYS = frozenset(f"{k}_set_by" for k in AUDITED)
for _key in AUDITED:
    OWNERS[f"{_key}_set_by"] = OWNERS[f"{_key}_set_at"] = SYSTEM


def require_general(keys) -> None:
    """Refuse the first key PATCH /companies/me does not own, naming where it changes."""
    for key in keys:
        owner = OWNERS.get(key)
        if owner == COMPANY_SETTINGS:
            continue
        if owner is None:
            raise HTTPException(status_code=422, detail=refusal(
                "company.setting_unknown",
                f"{key} is not a company setting. Remove it from the request and try again.", key=key))
        if owner == SYSTEM:
            raise HTTPException(status_code=422, detail=refusal(
                "company.setting_system_owned",
                f"{key} is kept by Celerp and cannot be set directly. Remove it from the request "
                "and try again.", key=key))
        raise HTTPException(status_code=422, detail=refusal(
            "company.setting_has_own_route",
            f"{key} is not changed in company settings. Change it through {owner}.", key=key, route=owner))


def record_change(settings: dict, key: str, user_id) -> None:
    """Stamp who changed the audited ``key`` and when, beside it in ``settings``."""
    settings[f"{key}_set_by"] = str(user_id) if user_id is not None else None
    settings[f"{key}_set_at"] = datetime.now(timezone.utc).isoformat()


def clear_change(settings: dict, key: str) -> None:
    settings.pop(f"{key}_set_by", None)
    settings.pop(f"{key}_set_at", None)
