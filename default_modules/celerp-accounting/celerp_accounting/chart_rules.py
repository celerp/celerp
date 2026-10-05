# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

"""The chart of accounts' own rules: the one place an account is checked before it is
added or changed, and the account lookup the core journal boundary uses.

Lock order: the company lock (when a caller takes it), then the chart lock, then
account rows. Every change to the chart's shape (adding an account, moving it,
retyping it, switching it on or off, pointing a posting role at it) takes the chart
lock first, so those changes happen one at a time and each checks the parents,
children and role targets the previous one left. A posting holds the accounts it
uses FOR SHARE until it commits and never takes the chart lock; a change to an
account row takes that row FOR UPDATE, so it waits for postings in flight.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import CONTINUED_ROLES, POSTABLE_ROLES, ROLE_LABELS, AccountRole, allowed_types, refusal
from celerp.models.projections import Projection
from celerp.services.account_roles import current_settings, role_map, scope_codes
from celerp.services.company_lock import lock_chart
from celerp.services.lot_origin import account_balances
from celerp_accounting.models import Account

# The account types the reports know how to sign and classify. This module is
# authoritative; the chart of accounts screen holds a copy for its dropdown, and a
# test keeps the two in lockstep.
ACCOUNT_TYPES = ("asset", "liability", "equity", "revenue", "cogs", "expense", "other")

ACCOUNT_CODE_MAX = 32  # Account.code column width


# The fields a refusal names, with their English labels.
FIELD_LABELS = {"code": "Account code", "name": "Account name", "parent_code": "Parent code"}


def _field(field: str) -> dict:
    return refusal(f"chart.field.{field}", FIELD_LABELS[field])


def trimmed_text(value: Any, field: str) -> str:
    """A text field (a FIELD_LABELS key), trimmed. The database cannot store a NUL
    character, so one is refused here with the field's name rather than failing the
    whole write."""
    label = FIELD_LABELS[field]
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail=refusal(
            "chart.not_text", f"{label} must be text.", field=_field(field)))
    if "\x00" in value:
        raise HTTPException(status_code=422, detail=refusal(
            "chart.has_nul", f"{label} cannot contain a NUL character.", field=_field(field)))
    return value.strip()


def checked_code_length(code: str, field: str) -> str:
    """``code`` (a code field), refused when it would not fit the code column."""
    if len(code) > ACCOUNT_CODE_MAX:
        raise HTTPException(status_code=422, detail=refusal(
            "chart.code_too_long", f"{FIELD_LABELS[field]} must be {ACCOUNT_CODE_MAX} characters or fewer.",
            field=_field(field), max=ACCOUNT_CODE_MAX))
    return code


def checked_account_code(value: Any) -> str:
    """The account code, trimmed. Postings and parents refer to accounts by code,
    so a blank or over-long one is refused rather than stored or cut short."""
    code = trimmed_text(value, "code")
    if not code:
        raise HTTPException(status_code=422, detail=refusal("chart.code_required", "Account code is required."))
    return checked_code_length(code, "code")


def checked_account_name(value: Any) -> str:
    """The account name, trimmed. Every report labels the account with it."""
    name = trimmed_text(value, "name")
    if not name:
        raise HTTPException(status_code=422, detail=refusal("chart.name_required", "Account name is required."))
    return name


def checked_account_type(value: Any) -> str:
    """The account's type, which decides its sign on every report. A type outside
    the known set has no honest sign convention, so it is refused rather than
    guessed at."""
    if value not in ACCOUNT_TYPES:
        choices = ", ".join(ACCOUNT_TYPES)
        raise HTTPException(status_code=422, detail=refusal(
            "chart.type_unknown", f"Account type must be one of: {choices}.", choices=choices))
    return value


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

    The core journal boundary's view of the chart (``ChartAccess.lock_accounts``)."""
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


async def chart_accounts(session: AsyncSession, company_id: uuid.UUID) -> list[dict]:
    """Every account in the company's chart, in code order, as the posting-accounts
    choices see it (``ChartAccess.list_accounts``)."""
    rows = (await session.execute(
        select(Account).where(Account.company_id == company_id).order_by(Account.code)
    )).scalars().all()
    parents = {r.parent_code for r in rows if r.parent_code}
    return [
        {"code": r.code, "name": r.name, "account_type": r.account_type,
         "is_active": r.is_active, "has_children": r.code in parents}
        for r in rows
    ]


def posting_targets(settings: dict) -> dict[str, list[str]]:
    """Each account a role posts to directly, with those roles' labels. Nothing can
    sit under such an account: its postings would land on a header."""
    out: dict[str, list[str]] = {}
    for role, code in sorted(role_map(settings).items()):
        if AccountRole(role) in POSTABLE_ROLES:
            out.setdefault(code, []).append(ROLE_LABELS[AccountRole(role)])
    return out


def parent_problem(
    account_type: str, parent: Account | dict | None, parent_code: str | None, targets: dict[str, list[str]],
) -> str | None:
    """Why ``parent_code`` cannot hold an account of ``account_type``, or None.
    ``targets`` is posting_targets() of the company's settings."""
    if parent_code is None:
        return None
    if parent_code in targets:
        return (f"Account {parent_code} is the posting account for {', '.join(targets[parent_code])}, "
                "so no account can sit under it.")
    if parent is None:
        return f"Parent account {parent_code} is not in the chart of accounts."
    get = parent.get if isinstance(parent, dict) else lambda k: getattr(parent, k)
    if not get("is_active"):
        return f"Parent account {parent_code} is inactive."
    if get("account_type") not in PARENT_TYPES.get(account_type, frozenset()):
        return (f"An account of type {account_type} cannot sit under {parent_code}, "
                f"an account of type {get('account_type')}.")
    return None


async def _account(session: AsyncSession, company_id: uuid.UUID, code: str, *, lock: bool = False) -> Account | None:
    stmt = select(Account).where(Account.company_id == company_id, Account.code == code)
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt.execution_options(populate_existing=True))).scalar_one_or_none()


async def check_new_account(
    session: AsyncSession, company_id: uuid.UUID, *, account_type: str, parent_code: str | None,
) -> None:
    """A new account's parent is a same-company, active, compatible account that no
    role posts to directly. A new code cannot close a loop, since nothing hangs under
    it yet."""
    await lock_chart(session, company_id)
    if parent_code is None:
        return
    parent = await _account(session, company_id, parent_code, lock=True)
    targets = posting_targets(await current_settings(session, company_id))
    problem = parent_problem(account_type, parent, parent_code, targets)
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
    if moving or account_type is not None or is_active is not None:
        await lock_chart(session, company_id)
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
                           f"an account of type {account_type} cannot serve it.",
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
                           f"it cannot sit under an account of type {account_type}.",
                )
        acc.account_type = account_type

    if is_active is False and acc.is_active and targeted:
        raise HTTPException(
            status_code=409,
            detail=f"Account {code} is the posting account for {', '.join(targeted)}. "
                   "Choose another account in Settings > Accounting > Posting accounts first.",
        )
    if is_active is False and acc.is_active:
        kept = [r for r in CONTINUED_ROLES if code in scope_codes(settings, r.value)]
        if kept and (balance := (await account_balances(session, company_id, {code}))[code]):
            # A remap leaves such a balance where it was recognized (account_roles.continue_role).
            raise HTTPException(status_code=409, detail=refusal(
                "posting.account_keeps_balance",
                (f"Account {code} still carries {balance} of {ROLE_LABELS[kept[0]]}, which keeps moving "
                 "on this account until it is cleared, so it cannot be switched off yet."),
                code=code, role=kept[0].value, balance=str(balance)))
        active_children = (await session.execute(
            select(Account.code).where(
                Account.company_id == company_id, Account.parent_code == code, Account.is_active.is_(True),
            ).order_by(Account.code)
        )).scalars().all()
        if active_children:
            raise HTTPException(
                status_code=409,
                detail=f"Accounts under {code} are still active ({', '.join(active_children)}). "
                       "Switch them off or move them first.",
            )
    if is_active is True and not acc.is_active and acc.parent_code is not None and (
            not moving or parent_code == acc.parent_code):
        parent = await _account(session, company_id, acc.parent_code)
        if parent is not None and not parent.is_active:
            raise HTTPException(status_code=422, detail=f"Parent account {acc.parent_code} is inactive.")
    if is_active is not None:
        acc.is_active = is_active

    if moving and parent_code != acc.parent_code:
        if parent_code is not None:
            if parent_code == code:
                raise HTTPException(status_code=422, detail="An account cannot be its own parent.")
            parent = await _account(session, company_id, parent_code, lock=True)
            problem = parent_problem(acc.account_type, parent, parent_code, posting_targets(settings))
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
