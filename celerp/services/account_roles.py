# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Posting-role state and resolution.

The company's role map lives in its settings (celerp.accounting_roles names the
keys). ``resolve_many`` is the only way automatic accounting picks an account for
new recognition: one settings read per economic entry, every target checked
against the chart and held, and no numeric fallback when a role is missing or its
account cannot take the posting. Historical readers use ``roles_for_line`` and the
scopes; they never resolve today's map.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import (
    LOT_ACCOUNT_FIELD,
    POSTING_ACCOUNTS_PATH,
    POSTING_ROLES_SCHEMA,
    ROLE_LABELS,
    ROLES_KEY,
    SCHEMA_KEY,
    SCOPES_KEY,
    SEEDED_TARGETS,
    SOURCE_CONTROLS_KEY,
    AccountRole,
    is_role,
    target_problem,
)
from celerp.models.company import Company


class PostingRoleError(HTTPException):
    """A role new recognition needs is missing or its account cannot take the posting."""

    def __init__(self, message: str):
        super().__init__(
            status_code=409,
            detail=f"{message} Choose the account in Settings > Accounting > Posting accounts.",
            headers={"X-Celerp-Fix": POSTING_ACCOUNTS_PATH},
        )


def role_map(settings: dict | None) -> dict[str, str]:
    return dict((settings or {}).get(ROLES_KEY) or {})


def scope_list(settings: dict | None, role: str) -> list[str]:
    """Every account that has legitimately served ``role``, in the order it began to."""
    return list(((settings or {}).get(SCOPES_KEY) or {}).get(str(role)) or ())


def scope_codes(settings: dict | None, role: str) -> set[str]:
    """Every account that has legitimately served ``role``, for historical readers."""
    return set(scope_list(settings, role))


def roles_for_account(settings: dict | None, code: str) -> list[str]:
    """The roles whose current target or historical scope includes ``code``."""
    current = role_map(settings)
    return sorted(
        role.value for role in AccountRole
        if current.get(role.value) == code or code in scope_codes(settings, role.value)
    )


def line_roles(settings: dict | None, entry: dict) -> list[str]:
    """What a journal line meant: its immutable snapshot, or for a line posted before
    snapshots existed, the roles whose frozen scope includes its account."""
    snapshot = entry.get("account_roles")
    if snapshot is not None:
        return list(snapshot)
    code = entry.get("account")
    return roles_for_account(settings, code) if code else []


def line_has_role(settings: dict | None, entry: dict, role: str) -> bool:
    return str(role) in line_roles(settings, entry)


def with_role(settings: dict, role: str, code: str) -> dict:
    """``settings`` with ``role`` pointing at ``code``; the scope only ever grows."""
    roles = role_map(settings)
    roles[str(role)] = code
    scopes = {k: list(v) for k, v in ((settings or {}).get(SCOPES_KEY) or {}).items()}
    if code not in scopes.setdefault(str(role), []):
        scopes[str(role)].append(code)
    return {**settings, SCHEMA_KEY: POSTING_ROLES_SCHEMA, ROLES_KEY: roles, SCOPES_KEY: scopes}


def reconciled_settings(settings: dict, accounts: dict[str, dict]) -> dict:
    """``settings`` with every unmapped role pointed at its seeded target, where the
    chart (``accounts``, keyed by code) holds that account active and of a type the
    role can use. A role already mapped is never changed, a missing or colliding
    account leaves its role unmapped, and no account is ever created. Running it
    again changes nothing. A company whose roles come from its source books (a
    migration) keeps its own chart's numbering: a default number its chart happens
    to hold proves nothing there."""
    out = {**settings, SCHEMA_KEY: POSTING_ROLES_SCHEMA}
    if SOURCE_CONTROLS_KEY in out:
        return out
    current = role_map(out)
    trial = {**{r.value: c for r, c in SEEDED_TARGETS.items()}, **current}
    for role, code in SEEDED_TARGETS.items():
        if not current.get(role.value) and target_problem(role.value, trial, accounts.get(code)) is None:
            out = with_role(out, role.value, code)
    return out


def unmapped_roles(settings: dict | None) -> list[str]:
    current = role_map(settings)
    return [role.value for role in AccountRole if not current.get(role.value)]


async def reconcile_company(session: AsyncSession, company_id) -> list[str]:
    """Map the company's unmapped roles to the seeded chart's accounts where they
    exist and fit (see ``reconciled_settings``). Returns the roles left unmapped.
    Does nothing when accounting is not running."""
    from celerp.services.company_lock import lock_chart, locked_company
    from celerp.services.journal_accounts import lock_accounts

    # The same lock order as set_role: a child added under a seeded account meanwhile
    # would turn it into a header.
    company = await locked_company(session, company_id)
    if company is None:
        return []
    await lock_chart(session, company_id)
    accounts = await lock_accounts(session, company_id, set(SEEDED_TARGETS.values()))
    if accounts is None:
        return []
    before = dict(company.settings or {})
    after = reconciled_settings(before, accounts)
    if after != before:
        company.settings = after
        await session.flush()
    return unmapped_roles(after)


def source_controls(settings: dict | None, role) -> list[str]:
    """The accounts a migration's source books marked as ``role``'s control."""
    return list(((settings or {}).get(SOURCE_CONTROLS_KEY) or {}).get(str(role)) or ())


def source_control(settings: dict | None, role) -> str:
    """The one account the source books kept ``role``'s balances on; a missing or
    ambiguous control is refused rather than guessed."""
    codes = source_controls(settings, role)
    label = ROLE_LABELS[AccountRole(role)].lower().removeprefix("accounts ")
    if not codes:
        raise ValueError(f"the source books mark no {label} account.")
    if len(codes) > 1:
        raise ValueError(f"the source books mark more than one {label} account ({', '.join(codes)}).")
    return codes[0]


async def record_source_control(session: AsyncSession, company_id, role, code: str) -> None:
    """Note that the source books kept ``role``'s balances on ``code``; recording it again changes nothing."""
    from celerp.services.company_lock import locked_company

    company = await locked_company(session, company_id)
    settings = dict(company.settings or {})
    controls = {k: list(v) for k, v in (settings.get(SOURCE_CONTROLS_KEY) or {}).items()}
    if code in controls.setdefault(str(role), []):
        return
    controls[str(role)].append(code)
    company.settings = {**settings, SOURCE_CONTROLS_KEY: controls}
    await session.flush()


class LotOriginError(HTTPException):
    """A lot recorded no inventory account and its history proves none, so its cost
    cannot move until the user picks the account (lot_origin.choose_lot_account)."""

    def __init__(self, sku: str):
        super().__init__(
            status_code=409,
            detail=(f"Stock {sku or 'item'} has no recorded inventory account, so its cost cannot be moved "
                    "without guessing. Choose its inventory account under Older stock in "
                    "Settings > Accounting > Posting accounts."),
            headers={"X-Celerp-Fix": POSTING_ACCOUNTS_PATH},
        )


class AmbiguousOriginError(HTTPException):
    """An older entry's account cannot be told apart, so it is reported, not guessed."""

    def __init__(self, role: str, what: str, codes):
        label = ROLE_LABELS[AccountRole(role)].lower()
        super().__init__(
            status_code=409,
            detail=(f"{what} was recorded against more than one {label} account "
                    f"({', '.join(sorted(codes))}), so Celerp cannot tell which one to use. "
                    "The books check lists it for correction."),
        )


def lot_account(state: dict) -> str:
    """The inventory account a lot's value sits in: the one it recorded when it first
    took on stock, or the one its own history proved for a lot from before lots recorded
    it (celerp.services.lot_origin). Never today's role target, never a company-wide guess."""
    code = state.get(LOT_ACCOUNT_FIELD)
    if not code:
        raise LotOriginError(str(state.get("sku") or ""))
    return code


async def new_lot_account(session: AsyncSession, company_id,
                          role: AccountRole = AccountRole.INVENTORY_OPENING) -> str | None:
    """The inventory account a new lot's value is booked into: the account of ``role``.
    Stock entered with no purchase behind it is carried by the opening inventory entry,
    so it takes the opening inventory account; a writer that books the stock itself
    names the role it debits. A company being migrated, which has no posting accounts
    yet, books its stock where the source books kept inventory, when they name exactly
    one account. Otherwise None, and the lot then moves cost only where its history
    proves the account (``lot_account``)."""
    from sqlalchemy import select

    settings = (await session.execute(
        select(Company.settings).where(Company.id == company_id))).scalar_one_or_none() or {}
    if SCHEMA_KEY not in settings:
        codes = source_controls(settings, AccountRole.INVENTORY_PURCHASED.value)
        return codes[0] if len(codes) == 1 else None
    return role_map(settings).get(role.value) or None


async def current_settings(session: AsyncSession, company_id) -> dict:
    """The company's settings as last committed: one read per economic entry."""
    company = await session.get(Company, company_id, populate_existing=True)
    return dict(company.settings or {}) if company is not None else {}


def target_problems(roles: list[str], current: dict[str, str], accounts: dict[str, dict] | None) -> dict[str, str]:
    """Why each role's mapped account cannot take new recognition. ``accounts`` is
    None when accounting is not running, so only the mapping itself is checked."""
    problems: dict[str, str] = {}
    for role in roles:
        code = current.get(role)
        if not code:
            problems[role] = f"No account is set for {ROLE_LABELS[AccountRole(role)].lower()}."
        elif accounts is not None:
            problem = target_problem(role, current, accounts.get(code))
            if problem:
                problems[role] = problem
    return problems


async def resolve_many(session: AsyncSession, company_id, roles) -> dict[str, str]:
    """The current target of every role one economic entry needs, from one read.

    Each target is checked against the chart and held until the transaction ends,
    so it cannot be deactivated or retyped before the entry is written. Any problem
    fails the whole entry; nothing falls back to a default code.
    """
    from celerp.services.journal_accounts import lock_accounts

    wanted = sorted({AccountRole(r).value for r in roles})
    current = role_map(await current_settings(session, company_id))
    accounts = await lock_accounts(session, company_id, {current[r] for r in wanted if current.get(r)})
    problems = target_problems(wanted, current, accounts)
    if problems:
        raise PostingRoleError(" ".join(problems[r] for r in wanted if r in problems))
    return {r: current[r] for r in wanted}


async def resolve(session: AsyncSession, company_id, role) -> str:
    return (await resolve_many(session, company_id, [role]))[str(AccountRole(role))]


async def set_role(session: AsyncSession, company_id, role: str, code: str) -> dict:
    """Point ``role`` at ``code`` for new recognition. Existing balances stay where they
    were posted; the old account stays in the role's scope for historical readers."""
    from celerp.services.company_lock import lock_chart, locked_company
    from celerp.services.journal_accounts import lock_accounts

    if not is_role(role):
        raise HTTPException(status_code=422, detail=f"Unknown posting role: {role}.")
    code = (code or "").strip()
    if not code:
        raise HTTPException(status_code=422, detail="Choose an account.")
    company = await locked_company(session, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found.")
    # A child added under the account meanwhile would turn it into a header.
    await lock_chart(session, company_id)
    settings = with_role(dict(company.settings or {}), role, code)
    new_map = role_map(settings)
    # Exchange gain and loss are judged as a pair: moving one side can change what
    # the other side's account is allowed to be.
    fx_pair = (AccountRole.FX_GAIN.value, AccountRole.FX_LOSS.value)
    checked = [role, *(r for r in fx_pair if role in fx_pair and r != role and new_map.get(r))]
    accounts = await lock_accounts(session, company_id, {new_map[r] for r in checked})
    if accounts is None:
        raise HTTPException(status_code=409, detail="Posting accounts need the accounting module.")
    for checked_role in checked:
        problem = target_problem(checked_role, new_map, accounts.get(new_map[checked_role]))
        if problem:
            raise HTTPException(status_code=422, detail=problem)
    company.settings = settings
    await session.flush()
    return settings
