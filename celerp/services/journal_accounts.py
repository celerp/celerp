# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""The one boundary every journal line passes before it enters the ledger.

emit_event calls ``prepare_journal_entry`` for every acc.journal_entry.created,
whoever produced it: automatic postings, manual journals, imports, bank
reconciliation, repairs. Replay applies stored events directly and never comes
back through here, so history is never revalidated or rewritten.

The chart of accounts belongs to the accounting module. It contributes one
``journal_accounts`` slot handler that reads and share-locks account rows; core
never touches its table. With no handler registered the accounting module is not
running, and only the role snapshot is written.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import ROLE_LABELS, SCHEMA_KEY, AccountRole, is_role
from celerp.models.company import Company
from celerp.services.account_roles import (
    PostingRoleError,
    role_map,
    roles_for_account,
    scope_codes,
    target_problems,
)

SLOT = "journal_accounts"


async def lock_accounts(session: AsyncSession, company_id, codes) -> dict[str, dict] | None:
    """The company's accounts among ``codes``, share-locked until the transaction ends.

    Each value is {"code", "account_type", "is_active", "has_children"}; a code the
    chart does not hold is absent. None when the accounting module is not running.
    """
    from celerp.modules.slots import get, resolve_handler

    contributions = get(SLOT)
    if not contributions:
        return None
    return await resolve_handler(contributions[0]["handler"])(session, company_id, set(codes))


async def prepare_journal_entry(session: AsyncSession, company_id, data: dict) -> None:
    """Check every line's account and write its immutable role snapshot in place.

    A line that names roles was chosen for them: its account must be the role's
    current target (then active and of a compatible type) or an account that has
    served the role before (then it only has to exist, as when a payment settles a
    receivable recognized before a remap). A line naming no roles is classified by
    every role whose target or scope holds its account; an explicit empty list stays
    deliberately unclassified. Before the company has posting roles at all (a company
    still being migrated), such a line is left without a snapshot, so readers classify
    it by the scopes the company's roles are given later.
    """
    entries = [e for e in (data.get("entries") or []) if isinstance(e, dict) and e.get("account")]
    if not entries:
        return
    company = await session.get(Company, company_id)
    settings = dict(company.settings or {}) if company is not None else {}
    current = role_map(settings)
    accounts = await lock_accounts(session, company_id, {e["account"] for e in entries})
    for entry in entries:
        code = entry["account"]
        if accounts is not None and code not in accounts:
            raise HTTPException(status_code=422, detail=f"Account {code} is not in the chart of accounts.")
        roles = entry.get("account_roles")
        if roles is None:
            if SCHEMA_KEY in settings:
                entry["account_roles"] = roles_for_account(settings, code)
            continue
        for role in roles:
            if not is_role(role):
                raise HTTPException(status_code=422, detail=f"Unknown posting role: {role}.")
            if current.get(role) == code:
                problems = target_problems([role], current, accounts)
                if problems:
                    raise PostingRoleError(problems[role])
            elif code not in scope_codes(settings, role):
                raise HTTPException(
                    status_code=422,
                    detail=f"Account {code} has never been the {ROLE_LABELS[AccountRole(role)].lower()} account.",
                )


async def require_settlement_account(session: AsyncSession, company_id, code: str) -> None:
    """Refuse a new payment or refund through ``code`` unless it can hold money: an
    active asset account with nothing under it, such as a bank or a clearing account.

    Only a new settlement is checked. Giving back money already recorded posts
    through the account it came in through, even if that account is now inactive.
    With the accounting module not running there is no chart to check against.
    """
    accounts = await lock_accounts(session, company_id, {code})
    if accounts is None:
        return
    account = accounts.get(code)
    if account is None:
        problem = f"Account {code} is not in the chart of accounts."
    elif not account["is_active"]:
        problem = f"Account {code} is inactive."
    elif account["has_children"]:
        problem = f"Account {code} is a header account. Choose one of the accounts under it."
    elif account["account_type"] != "asset":
        problem = (f"Account {code} is a {account['account_type']} account. Money is paid or refunded "
                   "through an asset account, such as a bank or a clearing account.")
    else:
        return
    raise HTTPException(status_code=422, detail=problem)
