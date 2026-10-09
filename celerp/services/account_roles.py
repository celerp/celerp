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
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import (
    CONSIGNOR_FIELD,
    CONSIGNOR_PAYABLE_FIELD,
    LOT_ACCOUNT_FIELD,
    POSTING_ACCOUNTS_PATH,
    POSTING_ROLES_SCHEMA,
    ROLE_LABELS,
    ROLES_KEY,
    SCHEMA_KEY,
    SCOPES_KEY,
    SEEDED_TARGETS,
    SOURCE_CONTROLS_KEY,
    UNGUESSED_ROLES,
    AccountRole,
    is_role,
    needs_accounting,
    no_account_chosen,
    refusal,
    target_problem,
    unknown_role,
)
from celerp.models.company import Company
from celerp.models.projections import Projection


class PostingRoleError(HTTPException):
    """A role new recognition needs is missing or its account cannot take the posting."""

    def __init__(self, problems: list[dict]):
        text = " ".join(p["message"] for p in problems)
        super().__init__(
            status_code=409,
            detail=refusal("posting.refused", f"{text} Choose the account in Settings > Accounting > Posting accounts.",
                           problems=problems),
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


def reconciled_settings(settings: dict, accounts: dict[str, dict], claim=frozenset()) -> dict:
    """``settings`` with every unmapped role pointed at its seeded target, where the
    chart (``accounts``, keyed by code) holds that account active and of a type the
    role can use. A role already mapped is never changed, a missing or colliding
    account leaves its role unmapped, and no account is ever created. Running it
    again changes nothing. A company whose roles come from its source books (a
    migration) keeps its own chart's numbering: a default number its chart happens
    to hold proves nothing there. Nor does it for a role added after the chart was
    written (UNGUESSED_ROLES), whose number the user may already have used for
    something else: such a role takes its seeded account only when Celerp has just
    created that account itself (``claim``)."""
    out = {**settings, SCHEMA_KEY: POSTING_ROLES_SCHEMA}
    if SOURCE_CONTROLS_KEY in out:
        return out
    current = role_map(out)
    trial = {**{r.value: c for r, c in SEEDED_TARGETS.items()}, **current}
    for role, code in SEEDED_TARGETS.items():
        if role in UNGUESSED_ROLES and role not in claim:
            continue
        if not current.get(role.value) and target_problem(role.value, trial, accounts.get(code)) is None:
            out = with_role(out, role.value, code)
    return out


def unmapped_roles(settings: dict | None) -> list[str]:
    current = role_map(settings)
    return [role.value for role in AccountRole if not current.get(role.value)]


async def reconcile_company(session: AsyncSession, company_id, claim=frozenset()) -> list[str]:
    """Map the company's unmapped roles to the seeded chart's accounts where they
    exist and fit (see ``reconciled_settings``, and ``claim`` there). Returns the roles left unmapped.
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
    after = reconciled_settings(before, accounts, claim)
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
    """A lot from before lots recorded their inventory account was left unplaced by the
    upgrade (celerp.services.lot_origin), so its cost cannot move until the user picks
    the account (lot_origin.choose_lot_account)."""

    def __init__(self, sku: str):
        super().__init__(
            status_code=409,
            detail=refusal(
                "posting.older_stock.no_account",
                f"Stock {sku or 'item'} has no recorded inventory account, so its cost cannot be moved "
                "without guessing. Choose its inventory account under Older stock in "
                "Settings > Accounting > Posting accounts.", sku=sku or "item"),
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


class NotOwnedError(HTTPException):
    """Goods held on consignment are the consignor's, not the company's stock, so they
    have no inventory account and their cost cannot move as if they were owned."""

    def __init__(self, sku: str):
        super().__init__(
            status_code=409,
            detail=refusal(
                "consignment.not_owned",
                f"Stock {sku or 'item'} is held on consignment and is not the company's inventory, "
                "so it cannot be used this way until it is bought from the consignor.", sku=sku or "item"),
        )


class ConsignmentNoCostError(HTTPException):
    """Consigned goods whose cost is not known (a foreign consignment with no rate yet)
    cannot be sold, since what is owed to the consignor would be a guess."""

    def __init__(self, sku: str):
        super().__init__(status_code=409, detail=refusal(
            "consignment.no_cost",
            f"Stock {sku or 'item'} is held on consignment with no known cost, so what is owed to the "
            "consignor for it cannot be recorded. Set the consignment's exchange rate or the item's cost first.",
            sku=sku or "item"))


class ConsignorUnknownError(HTTPException):
    """Consigned goods whose consignor is not known cannot be sold, since there is no one
    to owe their cost to."""

    def __init__(self, sku: str):
        super().__init__(status_code=409, detail=refusal(
            "consignment.no_consignor",
            f"Stock {sku or 'item'} is held on a consignment with no consignor, so there is no one to owe "
            "for it when it sells. Open the consignment and choose the consignor first.", sku=sku or "item"))


def is_consigned(state: dict) -> bool:
    """Whether a lot holds goods on consignment from a supplier."""
    return state.get("consignment_flag") == "in"


def lot_account(state: dict) -> str:
    """The inventory account a lot's value sits in: the one it recorded when it first
    took on stock, or for a lot from before lots recorded it, the one the upgrade or the
    user placed it on (celerp.services.lot_origin). Never today's role target, never a
    company-wide guess. Consigned goods are not the company's stock and have none."""
    if is_consigned(state):
        raise NotOwnedError(str(state.get("sku") or ""))
    code = state.get(LOT_ACCOUNT_FIELD)
    if not code:
        raise LotOriginError(str(state.get("sku") or ""))
    return code


def sold_lot_account(state: dict) -> str:
    """The account a lot's cost leaves when it is sold and returns to when the sale is
    undone: its inventory account, or for consigned goods the consignor payable it
    recorded on its first sale, since what the company owes the consignor is the cost
    of consigned goods it sells."""
    if is_consigned(state):
        code = state.get(CONSIGNOR_PAYABLE_FIELD)
        if not code:
            raise NotOwnedError(str(state.get("sku") or ""))
        return code
    return lot_account(state)


# How a lot came from another one, followed back to the lot received on a consignment.
_LOT_PARENT_KEYS = ("split_from", "transformed_from", "returned_from")


async def lineage(session: AsyncSession, company_id, roots) -> list[tuple[Projection, str | None, str | None]]:
    """Every lot that came from ``roots``, roots first and each lot after the one it came
    from: (lot, parent, link), where link names how it came from its parent (its parts
    ``split_from``, ``transformed_from``, goods a customer returned ``returned_from``, or
    the lot it was merged into ``merged_into``), and parent and link are None for a root."""
    out: list[tuple[Projection, str | None, str | None]] = []
    seen: set[str] = set()
    frontier: dict[str, tuple[str | None, str | None]] = {root: (None, None) for root in roots}
    while frontier:
        seen |= set(frontier)
        rows = (await session.execute(select(Projection).where(
            Projection.company_id == company_id, Projection.entity_id.in_(sorted(frontier))))).scalars().all()
        rows = sorted(rows, key=lambda r: r.entity_id)
        out += [(row, *frontier[row.entity_id]) for row in rows]
        children = (await session.execute(select(Projection).where(
            Projection.company_id == company_id, Projection.entity_type == "item",
            or_(*(Projection.state[key].as_string().in_(sorted(frontier)) for key in _LOT_PARENT_KEYS))))).scalars().all()
        frontier = {}
        for child in sorted(children, key=lambda r: r.entity_id):
            if child.entity_id in seen:
                continue
            cs = child.state or {}
            link = next(key for key in ("transformed_from", "split_from", "returned_from") if cs.get(key) in seen)
            frontier[child.entity_id] = (cs[link], link)
        for row in rows:
            into = (row.state or {}).get("merged_into")
            if into and into not in seen and into not in frontier:
                frontier[str(into)] = (row.entity_id, "merged_into")
    return out


async def consignor_of(session: AsyncSession, company_id, lot_id: str, state: dict) -> str | None:
    """The consignor the consigned lot ``lot_id`` (projection ``state``) belongs to: the
    contact of the consignment it was received on. A lot records it at receipt and every
    lot made from it keeps it; a lot from before lots recorded it is traced back through
    the lots it came from to the consignment that received it. None when that consignment
    cannot be found."""
    seen: set[str] = set()
    while not state.get(CONSIGNOR_FIELD):
        parent = next((str(state[k]) for k in _LOT_PARENT_KEYS if state.get(k)), None)
        if parent is None or parent in seen:
            return await _received_on(session, company_id, lot_id)
        seen.add(parent)
        row = await session.get(Projection, (company_id, parent))
        if row is None:
            return None
        lot_id, state = parent, row.state or {}
    return state[CONSIGNOR_FIELD]


async def _received_on(session: AsyncSession, company_id, lot_id: str) -> str | None:
    """The contact of the consignment whose receipts list ``lot_id``, or None."""
    if not lot_id:
        return None
    docs = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "doc",
        Projection.state["doc_type"].as_string() == "consignment_in"))).scalars().all()
    for doc in docs:
        if lot_id in ((doc.state or {}).get("received_item_ids") or []):
            return (doc.state or {}).get("contact_id") or None
    return None


# A posting key naming an account and, for the consignor payable, the consignor the
# line is owed to: "<account>|<contact>". Writers sum amounts by it, so two consignors'
# amounts never net on one line, and _lot_line puts the contact on the line it posts.
_PARTY_SEPARATOR = "|"


def party_key(code: str, contact: str | None) -> str:
    """The posting key for ``code`` owed to ``contact`` (just the account without one)."""
    return f"{code}{_PARTY_SEPARATOR}{contact}" if contact else code


def split_party_key(key: str) -> tuple[str, str | None]:
    """(account, contact or None) of a posting key (party_key)."""
    code, _, contact = key.partition(_PARTY_SEPARATOR)
    return code, contact or None


async def sold_lot_key(session: AsyncSession, company_id, lot_id: str, state: dict) -> str:
    """sold_lot_account of lot ``lot_id`` as a posting key: for consigned goods the
    consignor payable owed to the lot's consignor (consignor_of)."""
    code = sold_lot_account(state)
    if not is_consigned(state):
        return code
    return party_key(code, await consignor_of(session, company_id, lot_id, state))


async def new_lot_account(session: AsyncSession, company_id, role: AccountRole) -> str | None:
    """The inventory account a writer that books a new lot's stock itself debits: the
    account of ``role``. A company being migrated, which has no posting accounts yet,
    books its stock where the source books kept inventory (``source_lot_account``).
    Otherwise None, and the lot moves no cost until its account is placed
    (``lot_account``)."""
    settings = await _committed_settings(session, company_id)
    if SCHEMA_KEY not in settings:
        return _source_inventory(settings)
    return role_map(settings).get(role.value) or None


async def source_lot_account(session: AsyncSession, company_id) -> str | None:
    """The account migrated books kept inventory in, when they name exactly one."""
    return _source_inventory(await _committed_settings(session, company_id))


def _source_inventory(settings: dict) -> str | None:
    codes = source_controls(settings, AccountRole.INVENTORY_PURCHASED.value)
    return codes[0] if len(codes) == 1 else None


async def _committed_settings(session: AsyncSession, company_id) -> dict:
    from sqlalchemy import select

    return (await session.execute(
        select(Company.settings).where(Company.id == company_id))).scalar_one_or_none() or {}


async def current_settings(session: AsyncSession, company_id) -> dict:
    """The company's settings as last committed: one read per economic entry."""
    company = await session.get(Company, company_id, populate_existing=True)
    return dict(company.settings or {}) if company is not None else {}


def target_problems(roles: list[str], current: dict[str, str], accounts: dict[str, dict] | None) -> dict[str, dict]:
    """Why each role's mapped account cannot take new recognition. ``accounts`` is
    None when accounting is not running, so only the mapping itself is checked."""
    problems: dict[str, dict] = {}
    for role in roles:
        code = current.get(role)
        if not code:
            problems[role] = refusal("posting.problem.unset", f"{ROLE_LABELS[AccountRole(role)]} has no account set.",
                                     role=role)
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
        raise PostingRoleError([problems[r] for r in wanted if r in problems])
    return {r: current[r] for r in wanted}


async def resolve(session: AsyncSession, company_id, role) -> str:
    return (await resolve_many(session, company_id, [role]))[str(AccountRole(role))]


async def continue_role(session: AsyncSession, company_id, role, code: str) -> str:
    """The account a balance already recognized for ``role`` keeps moving on: ``code``,
    where it was recognized, never today's target. A remap does not move the balance, so
    the account must still take postings for the role (in the chart, active, a leaf, of
    the role's type) and is held until the transaction ends; otherwise the entry is
    refused, since posting elsewhere would split the balance from its history."""
    from celerp.services.journal_accounts import lock_accounts

    role = AccountRole(role).value
    accounts = await lock_accounts(session, company_id, {code})
    if accounts is None:
        return code
    problem = target_problem(role, {role: code}, accounts.get(code))
    if problem:
        raise HTTPException(status_code=409, detail=refusal(
            "posting.continued_account_unusable",
            (f"This balance is kept on account {code}, which cannot take it now: {problem['message']} "
             "Make that account usable again in the chart of accounts."),
            code=code, problem=problem))
    return code


async def set_role(session: AsyncSession, company_id, role: str, code: str) -> dict:
    """Point ``role`` at ``code`` for new recognition. Existing balances stay where they
    were posted; the old account stays in the role's scope for historical readers."""
    from celerp.services.company_lock import lock_chart, locked_company
    from celerp.services.journal_accounts import lock_accounts

    if not is_role(role):
        raise HTTPException(status_code=422, detail=unknown_role(role))
    code = (code or "").strip()
    if not code:
        raise HTTPException(status_code=422, detail=no_account_chosen())
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
        raise HTTPException(status_code=409, detail=needs_accounting())
    for checked_role in checked:
        problem = target_problem(checked_role, new_map, accounts.get(new_map[checked_role]))
        if problem:
            raise HTTPException(status_code=422, detail=problem)
    company.settings = settings
    await session.flush()
    return settings
