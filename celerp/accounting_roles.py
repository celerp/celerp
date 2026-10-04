# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Posting roles - the accounts automatic accounting posts new recognition to.

A role is a company's current default account for one kind of new economic
recognition (a receivable, a sale, stock bought in). It is never the identity of
an account: once a balance exists, its lifecycle continues on the account it was
recognized on, whatever the role points at later.

This module owns the role vocabulary and the account-type rules a target must
satisfy. It knows nothing about the accounts table; the accounting module checks
targets against the chart (celerp.services.journal_accounts).

AccountControl (celerp.importers.schema) is a different concept: it describes a
SOURCE system's account semantics during migration, and several source accounts
may share one control. It is not collapsed into AccountRole.
"""

from __future__ import annotations

from celerp.compat import StrEnum

# Bumped when a release adds a role. Older companies and backups stay readable:
# an unmapped role only fails the operation that needs it.
POSTING_ROLES_SCHEMA = 2

# Company.settings keys. ``posting_roles`` is the current target per role;
# ``posting_role_scopes`` is every account that has legitimately served the role,
# kept for historical readers and never used as a posting fallback.
ROLES_KEY = "posting_roles"
SCOPES_KEY = "posting_role_scopes"
SCHEMA_KEY = "posting_roles_schema"
# The accounts a migrated company's source books marked as its controls, per role,
# in import order. A migration fills it; finishing the migration maps the roles from it.
SOURCE_CONTROLS_KEY = "posting_source_controls"

# The item-state field holding the inventory account a lot's value sits in.
LOT_ACCOUNT_FIELD = "inventory_account_code"
# The event-metadata key naming the lot a new lot takes its cost from (goods back on a
# credit note come in at the cost of the lot that was sold).
VALUED_FROM_KEY = "valued_from"
# Company.settings key set once every lot's stock records its inventory account or has
# been left for the user to place (celerp.services.lot_origin). A company without it
# predates lots recording their account.
INVENTORY_ORIGIN_KEY = "inventory_origin_schema"
INVENTORY_ORIGIN_SCHEMA = 1
# The item-state field set while an archived or expired lot still holds the company's
# stock (celerp.services.lot_origin.in_stock). Only the system writes it.
ON_BOOKS_FIELD = "inventory_on_books"

# Where a user fixes a missing or invalid role.
POSTING_ACCOUNTS_PATH = "/settings/accounting?tab=posting-accounts"


class AccountRole(StrEnum):
    CASH_AND_EQUIVALENTS = "cash_and_equivalents"
    DEFAULT_DEPOSIT = "default_deposit"
    RECEIVABLE = "receivable"
    PAYABLE = "payable"
    INVENTORY = "inventory"
    INVENTORY_PURCHASED = "inventory_purchased"
    INVENTORY_OPENING = "inventory_opening"
    LANDED_FREIGHT = "landed_freight"
    LANDED_INSURANCE = "landed_insurance"
    LANDED_DUTY = "landed_duty"
    LANDED_IMPORT_VAT = "landed_import_vat"
    TAX_INPUT = "tax_input"
    TAX_OUTPUT = "tax_output"
    FIXED_ASSETS = "fixed_assets"
    RETAINED_EARNINGS = "retained_earnings"
    SALES_REVENUE = "sales_revenue"
    STOCK_GAIN = "stock_gain"
    COGS = "cogs"
    GENERAL_EXPENSE = "general_expense"
    FX_GAIN = "fx_gain"
    FX_LOSS = "fx_loss"
    STOCK_SHRINKAGE = "stock_shrinkage"
    WORK_IN_PROGRESS = "work_in_progress"


R = AccountRole

ROLE_LABELS: dict[AccountRole, str] = {
    R.CASH_AND_EQUIVALENTS: "Cash and cash equivalents",
    R.DEFAULT_DEPOSIT: "Default deposit account",
    R.RECEIVABLE: "Accounts receivable",
    R.PAYABLE: "Accounts payable",
    R.INVENTORY: "Inventory",
    R.INVENTORY_PURCHASED: "Inventory purchased",
    R.INVENTORY_OPENING: "Opening inventory",
    R.LANDED_FREIGHT: "Freight clearing",
    R.LANDED_INSURANCE: "Insurance clearing",
    R.LANDED_DUTY: "Import duty clearing",
    R.LANDED_IMPORT_VAT: "Non-recoverable import VAT clearing",
    R.TAX_INPUT: "Input tax",
    R.TAX_OUTPUT: "Output tax",
    R.FIXED_ASSETS: "Fixed assets",
    R.RETAINED_EARNINGS: "Retained earnings",
    R.SALES_REVENUE: "Sales revenue",
    R.STOCK_GAIN: "Stock gains",
    R.COGS: "Cost of goods sold",
    R.GENERAL_EXPENSE: "General expenses",
    R.FX_GAIN: "Exchange gain",
    R.FX_LOSS: "Exchange loss",
    R.STOCK_SHRINKAGE: "Stock shrinkage and write-offs",
    R.WORK_IN_PROGRESS: "Work in progress",
}

_ASSET = frozenset({"asset"})
_REVENUE = frozenset({"revenue"})
_EXPENSE = frozenset({"expense"})

ROLE_TYPES: dict[AccountRole, frozenset[str]] = {
    R.CASH_AND_EQUIVALENTS: _ASSET,
    R.DEFAULT_DEPOSIT: _ASSET,
    R.RECEIVABLE: _ASSET,
    R.PAYABLE: frozenset({"liability"}),
    R.INVENTORY: _ASSET,
    R.INVENTORY_PURCHASED: _ASSET,
    R.INVENTORY_OPENING: _ASSET,
    R.LANDED_FREIGHT: _ASSET,
    R.LANDED_INSURANCE: _ASSET,
    R.LANDED_DUTY: _ASSET,
    R.LANDED_IMPORT_VAT: _ASSET,
    R.TAX_INPUT: _ASSET,
    R.TAX_OUTPUT: frozenset({"liability"}),
    R.FIXED_ASSETS: _ASSET,
    R.RETAINED_EARNINGS: frozenset({"equity"}),
    R.SALES_REVENUE: _REVENUE,
    R.STOCK_GAIN: _REVENUE,
    R.COGS: frozenset({"cogs"}),
    R.GENERAL_EXPENSE: _EXPENSE,
    R.FX_GAIN: _REVENUE,
    R.FX_LOSS: _EXPENSE,
    R.STOCK_SHRINKAGE: _EXPENSE,
    R.WORK_IN_PROGRESS: _ASSET,
}

# Roles a posting lands on directly, so the target must be a concrete account,
# not a header other accounts roll up into. Cash and inventory only group the
# accounts under them; every other role receives debits and credits.
POSTABLE_ROLES: frozenset[AccountRole] = frozenset(R) - {R.CASH_AND_EQUIVALENTS, R.INVENTORY}

# The targets of a seeded chart. A company created by this release starts with
# these, and the upgrade maps an existing company to them only where the account
# exists with a compatible type. They are never a posting fallback.
SEEDED_TARGETS: dict[AccountRole, str] = {
    R.CASH_AND_EQUIVALENTS: "1110",
    R.DEFAULT_DEPOSIT: "1111",
    R.RECEIVABLE: "1120",
    R.PAYABLE: "2110",
    R.INVENTORY: "1130",
    R.INVENTORY_PURCHASED: "1130-P",
    R.INVENTORY_OPENING: "1130-OB",
    R.LANDED_FREIGHT: "1130-FRT",
    R.LANDED_INSURANCE: "1130-INS",
    R.LANDED_DUTY: "1130-DTY",
    R.LANDED_IMPORT_VAT: "1130-IVT",
    R.TAX_INPUT: "1150",
    R.TAX_OUTPUT: "2120",
    R.FIXED_ASSETS: "1210",
    R.RETAINED_EARNINGS: "3200",
    R.SALES_REVENUE: "4100",
    R.STOCK_GAIN: "4300",
    R.COGS: "5100",
    R.GENERAL_EXPENSE: "6950",
    R.FX_GAIN: "6960",
    R.FX_LOSS: "6960",
    R.STOCK_SHRINKAGE: "6970",
    R.WORK_IN_PROGRESS: "1130-WIP",
}

# Landed-cost clearing role per landed-cost kind, and back.
LANDED_ROLE_BY_KIND: dict[str, AccountRole] = {
    "freight": R.LANDED_FREIGHT,
    "insurance": R.LANDED_INSURANCE,
    "duty": R.LANDED_DUTY,
    "import_vat": R.LANDED_IMPORT_VAT,
}
LANDED_KIND_BY_ROLE: dict[str, str] = {role.value: kind for kind, role in LANDED_ROLE_BY_KIND.items()}

# Roles whose accounts hold the value of goods on hand: stock itself, its opening
# balance, and landed cost waiting to be capitalized into it. Work in progress is not
# one of them: it carries material issued to a production run, which is no longer a
# lot on hand, and is checked against the open runs instead.
INVENTORY_VALUE_ROLES: frozenset[AccountRole] = frozenset({
    R.INVENTORY, R.INVENTORY_PURCHASED, R.INVENTORY_OPENING, *LANDED_ROLE_BY_KIND.values()})

# Roles grouped by the workflow that needs them, for migration readiness and the
# Settings panel. A group's roles only block finalization when the company uses it.
ROLE_GROUPS: dict[str, tuple[AccountRole, ...]] = {
    "core": (R.RECEIVABLE, R.PAYABLE, R.SALES_REVENUE, R.GENERAL_EXPENSE, R.DEFAULT_DEPOSIT,
             R.RETAINED_EARNINGS, R.CASH_AND_EQUIVALENTS),
    "tax": (R.TAX_INPUT, R.TAX_OUTPUT),
    "inventory": (R.INVENTORY, R.INVENTORY_PURCHASED, R.INVENTORY_OPENING, R.COGS,
                  R.STOCK_GAIN, R.STOCK_SHRINKAGE),
    "landed_cost": (R.LANDED_FREIGHT, R.LANDED_INSURANCE, R.LANDED_DUTY, R.LANDED_IMPORT_VAT),
    "fx": (R.FX_GAIN, R.FX_LOSS),
    "fixed_assets": (R.FIXED_ASSETS,),
    "manufacturing": (R.WORK_IN_PROGRESS,),
}

# Roles a company whose chart came from elsewhere (a migration or a restored backup)
# never has mapped by default code: its chart predates the role, so an account that
# happens to carry the default number proves nothing about what it holds.
UNGUESSED_ROLES: frozenset[AccountRole] = frozenset({R.WORK_IN_PROGRESS})


def is_role(value: object) -> bool:
    return isinstance(value, str) and value in AccountRole._value2member_map_


def allowed_types(role: str, role_map: dict[str, str]) -> frozenset[str]:
    """Account types a target of ``role`` may have under ``role_map``.

    Exchange gain and loss either share one net account, which may then be revenue
    or expense, or are split, when gain must be revenue and loss expense.
    """
    role = AccountRole(role)
    if role in (R.FX_GAIN, R.FX_LOSS):
        gain, loss = role_map.get(R.FX_GAIN.value), role_map.get(R.FX_LOSS.value)
        if gain and gain == loss:
            return _REVENUE | _EXPENSE
    return ROLE_TYPES[role]


def target_problem(role: str, role_map: dict[str, str], account: dict | None) -> str | None:
    """Why ``account`` cannot take new recognition for ``role``, or None when it can.

    ``account`` is the chart row as {"code", "account_type", "is_active",
    "has_children"}, or None when the company has no such account."""
    code = role_map.get(str(role))
    label = ROLE_LABELS[AccountRole(role)]
    if account is None:
        return f"{label} is set to account {code}, which is not in the chart of accounts."
    if not account.get("is_active", True):
        return f"{label} is set to account {account['code']}, which is inactive."
    types = allowed_types(role, role_map)
    if account.get("account_type") not in types:
        want = " or ".join(sorted(types))
        return (f"{label} is set to account {account['code']}, a {account.get('account_type')} "
                f"account; it must be {want}.")
    if AccountRole(role) in POSTABLE_ROLES and account.get("has_children"):
        return f"{label} is set to account {account['code']}, a header account; choose an account under it."
    return None
