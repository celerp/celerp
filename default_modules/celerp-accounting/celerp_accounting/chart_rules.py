# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

"""The chart of accounts' own rules: the one place an account is checked before it is
added or changed, and the account lookup the core journal boundary uses.

Lock order: the company lock (when a caller takes it) comes before account rows.
A posting holds the accounts it uses FOR SHARE until it commits. Deactivating or
retyping an account takes only that row FOR UPDATE, so it waits for postings in
flight and they never wait for it while holding the company lock. Moving an account
in the hierarchy also takes the company lock: two moves of unrelated rows can close
a loop between them, so moves for one company happen one at a time.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import ROLE_LABELS, AccountRole, allowed_types
from celerp.models.projections import Projection
from celerp.services.account_roles import current_settings, role_map
from celerp_accounting.models import Account

# The account types an account may sit under. Cost of sales and operating expenses
# are both costs and are commonly grouped together; every other type keeps to its own
# section, so a header's total never mixes signs.
PARENT_TYPES: dict[str, frozenset[str]] = {
    "asset": frozenset({"asset"}),
    "liability": frozenset({"liability"}),
    "equity": frozenset({"equity"}),
    "revenue": frozenset({"revenue"}),
    "cogs": frozenset({"cogs", "expense"}),
    "expense": frozenset({"expense", "cogs"}),
    "other": frozenset({"other"}),
}


async def lock_accounts(session: AsyncSession, company_id: uuid.UUID, codes: set[str]) -> dict[str, dict]:
    """The company's accounts among ``codes``, share-locked in code order.

    The core journal boundary's view of the chart (slot ``journal_accounts``)."""
    if not codes:
        return {}
    rows = (await session.execute(
        select(Account)
        .where(Account.company_id == company_id, Account.code.in_(sorted(codes)))
        .order_by(Account.code)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )).scalars().all()
    parents = set((await session.execute(
        select(Account.parent_code).where(
            Account.company_id == company_id, Account.parent_code.in_([r.code for r in rows]),
        )
    )).scalars().all())
    return {
        r.code: {
            "code": r.code, "account_type": r.account_type,
            "is_active": r.is_active, "has_children": r.code in parents,
        }
        for r in rows
    }


def parent_problem(account_type: str, parent: Account | dict | None, parent_code: str | None) -> str | None:
    """Why ``parent_code`` cannot hold an account of ``account_type``, or None."""
    if parent_code is None:
        return None
    if parent is None:
        return f"Parent account {parent_code} is not in the chart of accounts."
    get = parent.get if isinstance(parent, dict) else lambda k: getattr(parent, k)
    if not get("is_active"):
        return f"Parent account {parent_code} is inactive."
    if get("account_type") not in PARENT_TYPES.get(account_type, frozenset()):
        return (f"A {account_type} account cannot sit under {parent_code}, "
                f"a {get('account_type')} account.")
    return None


async def _account(session: AsyncSession, company_id: uuid.UUID, code: str, *, lock: bool = False) -> Account | None:
    stmt = select(Account).where(Account.company_id == company_id, Account.code == code)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one_or_none()


async def check_new_account(
    session: AsyncSession, company_id: uuid.UUID, *, account_type: str, parent_code: str | None,
) -> None:
    """A new account's parent is a same-company, active, compatible account. A new
    code cannot close a loop, since nothing hangs under it yet."""
    if parent_code is None:
        return
    parent = await _account(session, company_id, parent_code)
    problem = parent_problem(account_type, parent, parent_code)
    if problem:
        raise HTTPException(status_code=422, detail=problem)


async def has_journal_history(session: AsyncSession, company_id: uuid.UUID, code: str) -> bool:
    rows = (await session.execute(
        select(Projection.state).where(
            Projection.company_id == company_id, Projection.entity_type == "journal_entry",
        )
    )).scalars().all()
    return any(
        (entry or {}).get("account") == code
        for state in rows for entry in ((state or {}).get("entries") or [])
    )


def _roles_targeting(settings: dict, code: str) -> list[str]:
    return [ROLE_LABELS[AccountRole(r)] for r, c in sorted(role_map(settings).items()) if c == code]


async def change_account(
    session: AsyncSession,
    company_id: uuid.UUID,
    code: str,
    *,
    name: str | None = None,
    account_type: str | None = None,
    parent_code: str | None | type[...] = ...,
    is_active: bool | None = None,
    cash_flow_category: str | None | type[...] = ...,
) -> Account:
    """Change one account. ``...`` leaves a field alone. The code never changes:
    postings, parents, banks and posting roles all refer to the account by it."""
    moving = parent_code is not ...
    if moving:
        from celerp.services.company_lock import lock_company

        await lock_company(session, company_id)
    acc = await _account(session, company_id, code, lock=True)
    if acc is None:
        raise HTTPException(status_code=404, detail="Account not found")
    # Read after the row lock, so a role remap that committed meanwhile is seen.
    settings = await current_settings(session, company_id)
    targeted = _roles_targeting(settings, code)

    if account_type is not None and account_type != acc.account_type:
        if targeted:
            current = role_map(settings)
            bad = [ROLE_LABELS[AccountRole(r)] for r, c in current.items()
                   if c == code and account_type not in allowed_types(r, current)]
            if bad:
                raise HTTPException(
                    status_code=409,
                    detail=f"Account {code} is the posting account for {', '.join(bad)}; "
                           f"a {account_type} account cannot serve it.",
                )
        if await has_journal_history(session, company_id, code):
            raise HTTPException(
                status_code=409,
                detail=f"Account {code} has journal entries, so its type cannot change. "
                       "Create a new account of the right type instead.",
            )
        children = (await session.execute(
            select(Account.code, Account.account_type).where(
                Account.company_id == company_id, Account.parent_code == code,
            )
        )).all()
        for child_code, child_type in children:
            if account_type not in PARENT_TYPES.get(child_type, frozenset()):
                raise HTTPException(
                    status_code=422,
                    detail=f"Account {child_code} ({child_type}) sits under {code}; "
                           f"it cannot sit under a {account_type} account.",
                )
        acc.account_type = account_type

    if is_active is False and acc.is_active and targeted:
        raise HTTPException(
            status_code=409,
            detail=f"Account {code} is the posting account for {', '.join(targeted)}. "
                   "Choose another account in Settings > Accounting > Posting accounts first.",
        )
    if is_active is not None:
        acc.is_active = is_active

    if moving and parent_code != acc.parent_code:
        if parent_code is not None:
            if parent_code == code:
                raise HTTPException(status_code=422, detail="An account cannot be its own parent.")
            parent = await _account(session, company_id, parent_code)
            problem = parent_problem(acc.account_type, parent, parent_code)
            if problem:
                raise HTTPException(status_code=422, detail=problem)
            parents = dict((await session.execute(
                select(Account.code, Account.parent_code).where(Account.company_id == company_id)
            )).all())
            node, seen = parent_code, {code}
            while node is not None and node not in seen:
                seen.add(node)
                node = parents.get(node)
            if node is not None:
                raise HTTPException(
                    status_code=422,
                    detail=f"Account {parent_code} sits under {code}, so it cannot be its parent.",
                )
        acc.parent_code = parent_code

    if name is not None:
        acc.name = name
    if cash_flow_category is not ...:
        acc.cash_flow_category = cash_flow_category
    return acc
