# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""The one boundary every journal line passes before it enters the ledger.

emit_event calls ``prepare_journal_entry`` for every acc.journal_entry.created,
whoever produced it: automatic postings, manual journals, imports, bank
reconciliation, repairs. Replay applies stored events directly and never comes
back through here, so history is never revalidated or rewritten.

The chart of accounts belongs to the accounting module. When its API starts it
registers a ``ChartAccess`` here (``register_chart``): reading account rows under
the company lock, listing the chart, adding an account, and judging a journal change
against the completed bank reconciliations. Core never touches its tables. Only the
bundled accounting module can register; with nothing registered,
or the module stopped, accounting is not running and only the role snapshot is
written.

Any module may post journal entries, and adds the accounts they post to through
``add_account``, which the registered chart checks as Settings does.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import (
    INCOME_ACCOUNT_FIELD,
    ROLE_LABELS,
    ROLE_TYPES,
    SCHEMA_KEY,
    STOCK_ROLES,
    AccountRole,
    Refused,
    catalog_refusal,
    is_role,
    refusal,
    unknown_role,
)
from celerp.models.company import Company
from celerp.services.account_roles import (
    PostingRoleError,
    role_map,
    roles_for_account,
    scope_codes,
    target_problems,
)
from celerp.services import peer_lines

CHART_MODULE = "celerp-accounting"


@dataclass(frozen=True)
class ChartAccess:
    """The accounting module's chart, as core reads and extends it.

    lock_accounts(session, company_id, codes) -> {code: {"code", "account_type",
    "is_active", "has_children"}}, held as read by the company lock; list_accounts(session, company_id) ->
    the chart's rows, each with its id, code, name, account_type, parent_code,
    cash_flow_category, is_active, has_children and currency (a bank account's, else
    None); add_account(session, company_id, *, code, name, account_type, parent_code=None,
    cash_flow_category=None) -> the new account's id; guard_journal_change(session,
    company_id, entity_id, event_type, data) -> None, or ``ReconciledHistoryChanged``
    when the event would change what a bank reconciliation counts.
    """

    lock_accounts: Callable[..., Awaitable[dict[str, dict]]]
    list_accounts: Callable[..., Awaitable[list[dict]]]
    add_account: Callable[..., Awaitable[str]]
    guard_journal_change: Callable[..., Awaitable[None]]


class ReconciledHistoryChanged(HTTPException):
    """A journal change refused because it would change a bank reconciliation: an entry
    a reconciliation counts, or a day inside a completed one. Core callers catch it
    without importing accounting. The refusal carries its own code: 409 also answers
    other conflicts on the same routes."""

    CODE = "reconciled_history_changed"

    def __init__(self, refusal: dict):
        super().__init__(status_code=409, detail={"code": self.CODE, **refusal})


class UntrustedChartError(ValueError):
    """A chart offered by code that is not the bundled accounting module."""


class UnbalancedJournalEntry(Refused):
    """An automatic journal entry whose lines do not balance in the company currency."""
    status_code = 422
    fix: dict | None = None


class RestrictedFundMovement(UnbalancedJournalEntry):
    """A journal entry that moves money between funds other than through
    post_fund_movement, by an actor it was not granted to, or against its movement rules."""


class FundChoiceNeeded(UnbalancedJournalEntry):
    """An entry for a record that came from outside with a line no fund choice covers
    (peer_lines.check_outside_record). ``fix`` tells the reader what to choose: the
    document's fund and the numbers of the lines that have none of their own."""
    status_code = 409

    def __init__(self, record: str, source: dict, lines: list[int]):
        super().__init__("posting.fund.choice_needed", record=record, source=source)
        self.fix = {"kind": "funds", "document": True, "lines": lines}


_chart: ChartAccess | None = None


def register_chart(access: ChartAccess) -> None:
    """Accept ``access`` only when every one of its functions is code of the bundled,
    content-verified accounting module."""
    from celerp.modules.loader import first_party_owner

    global _chart
    for fn in (access.lock_accounts, access.list_accounts, access.add_account,
               access.guard_journal_change):
        code = getattr(fn, "__code__", None)
        if code is None or first_party_owner(Path(code.co_filename)) != CHART_MODULE:
            raise UntrustedChartError(
                "Only the bundled accounting module can provide the chart of accounts.")
    _chart = access


def chart_access() -> ChartAccess | None:
    """The registered chart, or None when the accounting module is not running."""
    from celerp.modules.loader import load_errors

    if _chart is None or CHART_MODULE in load_errors():
        return None
    return _chart


async def lock_accounts(session: AsyncSession, company_id, codes) -> dict[str, dict] | None:
    """The company's accounts among ``codes``, held as read until the transaction ends.

    Each value is {"code", "account_type", "is_active", "has_children"}; a code the
    chart does not hold is absent. None when the accounting module is not running.
    """
    chart = chart_access()
    if chart is None:
        return None
    return await chart.lock_accounts(session, company_id, set(codes))


async def bank_currencies(session: AsyncSession, company_id) -> dict[str, str]:
    """{chart code: currency} of every bank account; empty when the accounting module is
    not running."""
    chart = chart_access()
    if chart is None:
        return {}
    return {a["code"]: a["currency"] for a in await chart.list_accounts(session, company_id) if a.get("currency")}


async def add_account(session: AsyncSession, company_id, code: str, name: str, account_type: str) -> None:
    """Add a top-level account to the company's chart, for any module to post to.

    The chart checks it as an account added in Settings: a blank or over-long code, a
    blank name, a type the reports cannot sign, or a code already in use is refused
    with an HTTPException carrying a keyed refusal. Refused too while the accounting
    module is not running, since there is no chart to add to. The caller commits.
    """
    chart = chart_access()
    if chart is None:
        raise HTTPException(status_code=409, detail=refusal(
            "chart.accounting_not_running",
            "Accounting is not running, so no account can be added to the chart of accounts."))
    await chart.add_account(session, company_id, code=code, name=name, account_type=account_type)


async def prepare_journal_entry(session: AsyncSession, company_id, data: dict, *, restored: bool = False,
                                actor_id=None, entity_id=None) -> None:
    """Check every line's account and write its immutable role snapshot in place.

    A line that names roles was chosen for them: its account must be the role's
    current target (then active and of a compatible type) or an account that has
    served the role before (then it only has to exist, as when a payment settles a
    receivable recognized before a remap). A line naming no roles is classified by
    every role whose target or scope holds its account; an explicit empty list stays
    deliberately unclassified. Before the company has posting roles at all (a company
    still being migrated), such a line is left without a snapshot, so readers classify
    it by the scopes the company's roles are given later. A *restored* copy of an entry
    (an unvoid) keeps each line's snapshot as the original recorded it, so a line that
    recorded none stays without one, never classified by today's roles. Then the fund
    rules (check_fund_rules), judged on the entry's content and on ``actor_id``, the user
    the event is emitted by, for the entry ``entity_id``.
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
            raise HTTPException(status_code=422, detail=refusal(
                "posting.destination.not_in_chart",
                f"Account {code} is not in the chart of accounts.", code=code))
        roles = entry.get("account_roles")
        if roles is None:
            if SCHEMA_KEY in settings and not restored:
                entry["account_roles"] = roles_for_account(settings, code)
            continue
        for role in roles:
            if not is_role(role):
                raise HTTPException(status_code=422, detail=unknown_role(role))
            if current.get(role) == code:
                problems = target_problems([role], current, accounts)
                if problems:
                    raise PostingRoleError([problems[role]])
            elif code not in scope_codes(settings, role):
                label = ROLE_LABELS[AccountRole(role)].lower()
                raise HTTPException(
                    status_code=422,
                    detail=refusal(
                        "posting.account_never_had_role",
                        f"Account {code} has never been the {label} account.",
                        code=code, role=label),
                )
    await check_fund_rules(session, company_id, data, entries, accounts,
                           restored=restored, actor_id=actor_id, entity_id=entity_id)


FUNDS_ENABLED = "funds_enabled"  # company setting: whether journal lines carry funds


def funds_enabled(settings: dict) -> bool:
    return bool(settings.get(FUNDS_ENABLED))


async def funds_by_id(session: AsyncSession, company_id, ids=None) -> dict[str, dict]:
    """The company's funds (their projected state), or those among ``ids``."""
    from celerp.models.projections import Projection

    query = select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "fund")
    if ids is not None:
        query = query.where(Projection.entity_id.in_(sorted(ids)))
    return {entity_id: state or {} for entity_id, state in await session.execute(query)}


async def hold_fund_ledger(session: AsyncSession, company_id, entity_id=None, actor_id=None) -> dict:
    """Lock the company, read its settings under that lock and, when it has funds turned
    on, hold its fund ledger once in this transaction (lock_fund_ledger): exclusively for
    the entry a movement grant is open for (``entity_id`` emitted by ``actor_id``), whose
    cap reads fund balances, shared for every other posting; return the settings. A
    posting calls it before it locks any other row."""
    from celerp.services.company_lock import lock_company, lock_fund_ledger, noted

    await lock_company(session, company_id)
    settings = await session.scalar(select(Company.settings).where(Company.id == company_id)) or {}
    if funds_enabled(settings) and not any(c == str(company_id) for c, _ in noted(session, "funds")):
        await lock_fund_ledger(session, company_id, shared=_open_grant(company_id, entity_id, actor_id) is None)
    return settings


FUND_MOVEMENT_TYPES = frozenset({"fund_transfer", "fund_release", "fund_reallocation", "fund_opening"})
YEAR_CLOSE = "year_close"
# Account types whose lines are what a fund holds or owes (rule 4's BS measure); income
# and expense lines are neither.
_BALANCE_SHEET_TYPES = frozenset({"asset", "liability", "equity"})


@dataclass
class _MovementGrant:
    """One movement's permission to post: the company, the entity post_fund_movement minted
    and the user acting. One mutable object, so every task copying the context shares it
    and the first entry that matches spends it for all of them."""
    company_id: str
    entity_id: str
    actor_id: str | None
    used: bool = False
    authority: tuple[str, dict] | None = None  # the actor's role and settings (hold_movement_actor)


_movement_grant: ContextVar[_MovementGrant | None] = ContextVar("fund_movement_grant", default=None)


def _actor_key(actor_id) -> str | None:
    return str(actor_id) if actor_id is not None else None


@asynccontextmanager
async def fund_movement_grant(company_id, entity_id, actor_id):
    """Let exactly one movement-shaped entry post: the entry ``entity_id`` of ``company_id``
    emitted by ``actor_id``. Only post_fund_movement enters it; nothing an entry carries can."""
    grant = _MovementGrant(str(company_id), str(entity_id), _actor_key(actor_id))
    token = _movement_grant.set(grant)
    try:
        yield
    finally:
        grant.used = True
        _movement_grant.reset(token)


def _open_grant(company_id, entity_id, actor_id) -> _MovementGrant | None:
    grant = _movement_grant.get()
    if grant is None or grant.used or (grant.company_id, grant.entity_id, grant.actor_id) != (
            str(company_id), str(entity_id), _actor_key(actor_id)):
        return None
    return grant


async def hold_movement_actor(session: AsyncSession, company_id, entity_id, actor_id) -> None:
    """When a movement grant is open for this entry, read the acting user's role under the
    company lock now, before the posting locks any other row, so the membership row keeps
    its place in the lock order. emit_event calls it right after hold_fund_ledger."""
    from celerp.services.permissions import read_authority

    grant = _open_grant(company_id, entity_id, actor_id)
    if grant is not None and actor_id is not None:
        grant.authority = await read_authority(session, company_id, actor_id)


def fund_measures(entries: list[dict], accounts: dict[str, dict], net_assets: set[str]) -> tuple[dict, dict]:
    """Per fund (None for the unrestricted group): NA, the credits minus debits on the
    net-assets accounts, and BS, the debits minus credits on every other balance-sheet
    account, that is, the fund's net change in what it holds."""
    from celerp.services.money import to_decimal

    na: dict[str | None, Decimal] = {}
    bs: dict[str | None, Decimal] = {}
    for e in entries:
        fund_id = e.get("fund_id") or None
        amount = to_decimal(e.get("debit") or 0) - to_decimal(e.get("credit") or 0)
        if e["account"] in net_assets:
            na[fund_id] = na.get(fund_id, Decimal(0)) - amount
        elif (accounts.get(e["account"]) or {}).get("account_type") in _BALANCE_SHEET_TYPES:
            bs[fund_id] = bs.get(fund_id, Decimal(0)) + amount
    return na, bs


def _movement_shape(data: dict, entries: list[dict], accounts: dict[str, dict], settings: dict,
                    funds: dict[str, dict], restricted_na: set[str], retained: set[str]) -> str | None:
    """Which movement shape the entry has, judged on its content alone, or None:
    (a) a restricted net assets line; (b) a fund movement or year close type; (c) the net
    assets of two or more funds change; (d) value leaves a restricted fund and arrives in
    another. (a) and (b) are judged whenever the company has the restricted net assets
    account, even with funds turned off."""
    from celerp.services.money import round_money

    currency = str(settings.get("currency") or "USD")
    if role_map(settings).get(AccountRole.RESTRICTED_NET_ASSETS):
        if any(e["account"] in restricted_na for e in entries):
            return "a"
        if data.get("je_type") in FUND_MOVEMENT_TYPES or data.get("je_type") == YEAR_CLOSE:
            return "b"
    na, bs = fund_measures(entries, accounts, restricted_na | retained)
    if sum(1 for v in na.values() if round_money(v, currency)) >= 2:
        return "c"
    senders = [f for f, v in bs.items() if round_money(v, currency) < 0 and (funds.get(f or "") or {}).get("restricted")]
    if senders and any(round_money(v, currency) > 0 for f, v in bs.items() if f not in senders):
        return "d"
    return None


def _moving_fund(entries: list[dict], funds: dict[str, dict]) -> str:
    """The fund a movement refusal names: a restricted fund on the entry, else any fund,
    else the unrestricted group."""
    named = [funds[e["fund_id"]] for e in entries if e.get("fund_id") in funds]
    chosen = next((f for f in named if f.get("restricted")), named[0] if named else None)
    return chosen["name"] if chosen else unrestricted_label()


async def _authorize_movement(session: AsyncSession, company_id, data: dict, entries: list[dict],
                              funds: dict[str, dict], na: dict, *, actor_id, entity_id) -> None:
    """A movement-shaped entry posts only inside the grant for this entity and actor, from a
    user whose role, read under the company lock (hold_movement_actor), may manage accounting.
    A reallocation, and a reversal that takes money out of a restricted fund, also needs
    reallocate_restricted_funds; reversing a year close does not, as each fund's results
    return to that same fund. The actor is recorded on the entry as ``authorized_by``."""
    from celerp.services.permissions import missing_permission_refusal, role_has_permission

    fund = _moving_fund(entries, funds)
    grant = _open_grant(company_id, entity_id, actor_id)
    if grant is None:
        raise RestrictedFundMovement("posting.fund.movement_outside", fund=fund)
    grant.used = True
    if actor_id is None or grant.authority is None:
        raise RestrictedFundMovement("posting.fund.movement_needs_user", fund=fund)
    role, authority = grant.authority
    needed = ["manage_accounting"]
    if data.get("je_type") == "fund_reallocation" or (data.get("reverses") and data.get("je_type") != YEAR_CLOSE and any(
            v < 0 and _restricted(funds, f) for f, v in na.items())):
        needed.append("reallocate_restricted_funds")
    for permission in needed:
        if not role_has_permission(authority, role, permission):
            raise RestrictedFundMovement(
                "posting.fund.movement_permission", fund=fund,
                permission=missing_permission_refusal(permission)["params"]["label"])
    data["authorized_by"] = str(actor_id)


def _restricted(funds: dict[str, dict], fund_id) -> bool:
    return bool((funds.get(fund_id or "") or {}).get("restricted"))


def _fund_name(funds: dict[str, dict], fund_id) -> str:
    return funds[fund_id]["name"] if fund_id in funds else (fund_id or unrestricted_label())


async def _fund_day_changes(session: AsyncSession, company_id) -> dict[str, dict[tuple[str | None, str], Decimal]]:
    """Each day's posted change, debit less credit, per (fund, account), None for the
    unrestricted group. It reads the journal as the ledger does (ledger_rows.journal_rows),
    so an entry voided out of a locked period still counts on its own day and its reversal
    on reversed_on. A line counts in a fund only when it names a fund of this company."""
    from celerp.services.ledger_rows import journal_rows
    from celerp.services.money import to_decimal

    funds = await funds_by_id(session, company_id)
    days: dict[str, dict[tuple[str | None, str], Decimal]] = {}
    for _, state, day in await journal_rows(session, company_id):
        changes = days.setdefault(day, {})
        for line in state.get("entries") or ():
            if isinstance(line, dict) and line.get("account"):
                key = (line.get("fund_id") if line.get("fund_id") in funds else None, line["account"])
                changes[key] = (changes.get(key, Decimal(0))
                                + to_decimal(line.get("debit") or 0) - to_decimal(line.get("credit") or 0))
    return days


async def fund_account_balances(session: AsyncSession, company_id, *, as_of: str | None = None,
                                ) -> dict[str | None, dict[str, Decimal]]:
    """Each fund's (None: the unrestricted group's) non-zero posted balance, debit less
    credit, by account, counting the entries dated on or before ``as_of`` when given."""
    sums: dict[tuple[str | None, str], Decimal] = {}
    for day, changes in (await _fund_day_changes(session, company_id)).items():
        if not (as_of and day > as_of):
            for key, amount in changes.items():
                sums[key] = sums.get(key, Decimal(0)) + amount
    held: dict[str | None, dict[str, Decimal]] = {}
    for (fund, code), amount in sums.items():
        if amount:
            held.setdefault(fund, {})[code] = amount
    return held


async def fund_balance_range(session: AsyncSession, company_id, *, as_of: str,
                             ) -> dict[tuple[str | None, str], tuple[Decimal, Decimal]]:
    """Per (fund, account): the least and the greatest balance, debit less credit, it carries
    from ``as_of`` on (its balance that day, then after each later day), so what a fund gives
    on a date is what it still holds once every later entry has posted."""
    days = await _fund_day_changes(session, company_id)
    running: dict[tuple[str | None, str], Decimal] = {}
    for day in sorted(d for d in days if d <= as_of):
        for key, amount in days[day].items():
            running[key] = running.get(key, Decimal(0)) + amount
    span = {key: (amount, amount) for key, amount in running.items()}
    for day in sorted(d for d in days if d > as_of):
        for key, amount in days[day].items():
            running[key] = balance = running.get(key, Decimal(0)) + amount
            low, high = span.get(key, (Decimal(0), Decimal(0)))
            span[key] = (min(low, balance), max(high, balance))
    return span


async def pooled_split(session: AsyncSession, company_id, account: str, amount: Decimal, currency: str, *,
                       as_of: str, allocation: list[tuple[str | None, Decimal]] | None = None,
                       ) -> list[tuple[str | None, Decimal]]:
    """Each fund's share (None: the unrestricted group's) of ``amount`` moved off the pooled
    ``account`` (rule B): pro rata to what each holds there on ``as_of``, none above what it
    holds, the unrestricted group taking any amount beyond what the funds hold together. An
    explicit ``allocation`` replaces the split: each share at most what its fund holds there,
    adding up to ``amount``. What a fund holds there is the least it holds from ``as_of`` on
    (fund_balance_range). Funds off: the unrestricted group moves it all."""
    from celerp.services.money import allocate_pro_rata, round_money

    settings = await session.scalar(select(Company.settings).where(Company.id == company_id)) or {}
    if not funds_enabled(settings):
        if allocation:
            raise HTTPException(status_code=422, detail=catalog_refusal("fund.not_enabled"))
        return [(None, amount)]
    held = {fund: round_money(low, currency)
            for (fund, code), (low, _) in (await fund_balance_range(session, company_id, as_of=as_of)).items()
            if code == account}
    if allocation is not None:
        funds = await funds_by_id(session, company_id)
        for fund, share in allocation:
            if share > held.get(fund, Decimal(0)):
                raise HTTPException(status_code=422, detail=catalog_refusal(
                    "posting.fund.movement_exceeds_balance", fund=_fund_name(funds, fund), code=account,
                    available=str(max(held.get(fund, Decimal(0)), Decimal(0))), amount=str(round_money(share, currency)),
                    currency=currency))
        allocated = round_money(sum((share for _, share in allocation), Decimal(0)), currency)
        if allocated != amount:
            raise HTTPException(status_code=422, detail=catalog_refusal(
                "fund.transfer_allocation_total", allocated=str(allocated), amount=str(amount), currency=currency))
        return [(fund, share) for fund, share in allocation if share]
    holders = sorted((fund for fund, balance in held.items() if balance > 0), key=lambda f: f or "")
    covered = sum((held[fund] for fund in holders), Decimal(0))
    if covered >= amount:
        return [(fund, share) for fund, share in zip(holders, allocate_pro_rata(
            amount, [held[fund] for fund in holders], currency)) if share]
    shares = {fund: held[fund] for fund in holders}
    shares[None] = shares.get(None, Decimal(0)) + amount - covered
    return list(shares.items())


def split_lines_by_fund(entries: list[dict], shares: list[tuple[str | None, Decimal]], base: str) -> list[dict]:
    """``entries`` written once per fund, each line carrying that fund's part of its amounts
    in proportion to ``shares``, so every fund's lines balance as the whole entry did."""
    from celerp.services.money import allocate_pro_rata, to_decimal, to_stored_float

    weights = [share for _, share in shares]
    parts: list[list[dict]] = [[] for _ in shares]
    for line in entries:
        split = {field: allocate_pro_rata(to_decimal(line[field]), weights, currency)
                 for field, currency in (("debit", base), ("credit", base),
                                         ("fx_debit", line.get("fx_currency")), ("fx_credit", line.get("fx_currency")))
                 if line.get(field)}
        for i, (fund, _) in enumerate(shares):
            parts[i].append({**line, **{field: to_stored_float(amounts[i]) for field, amounts in split.items()},
                             **({"fund_id": fund} if fund else {})})
    return [line for part in parts for line in part]


# How fund_legs gives a line its fund, besides a basis of document lines.
CONTROL = "control"  # the receivable or payable: each fund's balancing figure
HEADER = "header"    # the document's own fund (a line no document line stands behind)


def fund_legs(entries: list[dict], bases: list, funds: Mapping[int, str | None], header_fund: str | None,
              base: str) -> list[dict]:
    """A document's entry with each line carried by the funds of the document lines behind it
    (attribution rule LG), in the order given.

    ``bases[i]`` says where ``entries[i]`` belongs: a mapping {document line n (1-based):
    weight} splits it over those lines' funds (``funds`` gives each line's fund, None for no
    fund) in proportion to the weights, every leg naming its ``source_lines``; ``HEADER`` gives
    it ``header_fund``; ``CONTROL`` writes it once per fund as the figure that balances that
    fund's other legs, with that fund's lines as its source; None leaves it without a fund (a
    stock or cost line). So every fund balances on its own as the whole entry did."""
    from celerp.services.money import allocate_pro_rata, to_decimal, to_stored_float

    legs: list[dict | None] = []
    control: dict | None = None
    for line, basis in zip(entries, bases):
        if basis == CONTROL:
            control = line
            legs.append(None)
        elif basis == HEADER:
            legs.append({**line, "source_lines": [], **({"fund_id": header_fund} if header_fund else {})})
        elif not basis:
            legs.append(line)
        else:
            groups: dict[str | None, tuple[Decimal, list[int]]] = {}
            for n, weight in sorted(basis.items()):
                total, lines = groups.get(funds.get(n), (Decimal(0), []))
                groups[funds.get(n)] = (total + max(to_decimal(weight), Decimal(0)), [*lines, n])
            shares = [(fund, weight if any(w for w, _ in groups.values()) else Decimal(1))
                      for fund, (weight, _) in groups.items()]
            for leg, (fund, (_, lines)) in zip(split_lines_by_fund([line], shares, base), groups.items()):
                legs.append({**leg, "source_lines": lines})
    if control is None:
        return [leg for leg in legs if leg is not None]

    net: dict[str | None, Decimal] = {}
    sources: dict[str | None, set[int]] = {}
    for leg in legs:
        if leg is not None:
            fund = leg.get("fund_id")
            net[fund] = net.get(fund, Decimal(0)) + to_decimal(leg.get("debit") or 0) - to_decimal(leg.get("credit") or 0)
            sources.setdefault(fund, set()).update(leg.get("source_lines") or [])
    owed = {fund: -amount for fund, amount in net.items() if amount}
    fx = {field: allocate_pro_rata(to_decimal(control[field]), [abs(a) for a in owed.values()],
                                   control.get("fx_currency"))
          for field in ("fx_debit", "fx_credit") if control.get(field) and owed}
    split = [{**{k: v for k, v in control.items() if k not in ("fx_debit", "fx_credit", "fund_id")},
              "debit": to_stored_float(max(amount, Decimal(0))), "credit": to_stored_float(max(-amount, Decimal(0))),
              **{field: to_stored_float(abs(parts[i])) for field, parts in fx.items()},
              "source_lines": sorted(sources[fund]), **({"fund_id": fund} if fund else {})}
             for i, (fund, amount) in enumerate(owed.items())]
    at = legs.index(None)
    return [leg for leg in legs[:at] + split + legs[at + 1:] if leg is not None]


def _movement_template(funds: dict[str, dict], fund_id) -> RestrictedFundMovement:
    return RestrictedFundMovement("posting.fund.movement_template", fund=_fund_name(funds, fund_id))


async def _check_movement(session: AsyncSession, company_id, data: dict, entries: list[dict],
                          accounts: dict[str, dict], settings: dict, funds: dict[str, dict],
                          net_assets: set[str]) -> None:
    """The template and the cap a movement meets, once _authorize_movement let it post.

    Only a fund movement type or a year close moves money. A year close carries no
    balance-sheet line but net assets. Every other movement carries no income or expense
    line (receivables and payables only in an opening), and on each account it only
    moves the balance between funds (the funds' changes add up to zero). A transfer,
    release or reallocation has one fund giving net assets and one receiving them, the
    original orientation read reversed on a reversal: a transfer between two unrestricted
    funds, a release from a restricted fund to an unrestricted one, a reallocation between
    restricted funds of one purpose with a reason and a donor authorization. An opening
    assigns the unrestricted group's balances to funds (a reversal gives them back) and
    leaves no restricted fund's net assets negative. A fund never gives more of an account
    than it holds on it from the entry's date on (fund_balance_range), nor gives the
    opposite side of it. A release
    names the purpose spending it pays for (_release_backing)."""
    from celerp.services.money import round_money, to_decimal

    je_type = data.get("je_type")
    if je_type not in FUND_MOVEMENT_TYPES and je_type != YEAR_CLOSE:
        raise RestrictedFundMovement("posting.fund.movement_outside", fund=_moving_fund(entries, funds))
    currency = str(settings.get("currency") or "USD")
    reverses = bool(data.get("reverses"))
    moved = [e for e in entries if e["account"] not in net_assets]
    if je_type == YEAR_CLOSE:
        for e in moved:
            if (accounts.get(e["account"]) or {}).get("account_type") in _BALANCE_SHEET_TYPES:
                raise _movement_template(funds, e.get("fund_id"))
        return

    parties = set().union(*(scope_codes(settings, role) for role in (AccountRole.RECEIVABLE, AccountRole.PAYABLE)))
    delta: dict[str, dict[str | None, Decimal]] = {}
    for e in moved:
        if (accounts.get(e["account"]) or {}).get("account_type") not in _BALANCE_SHEET_TYPES:
            raise RestrictedFundMovement("posting.fund.movement_income_expense", code=e["account"])
        if e["account"] in parties and je_type != "fund_opening":
            raise _movement_template(funds, e.get("fund_id"))
        by_fund = delta.setdefault(e["account"], {})
        fund_id = e.get("fund_id") or None
        by_fund[fund_id] = by_fund.get(fund_id, Decimal(0)) + to_decimal(e.get("debit") or 0) - to_decimal(e.get("credit") or 0)
    for code, by_fund in delta.items():
        by_fund = {f: v for f, v in by_fund.items() if round_money(v, currency)}
        delta[code] = by_fund
        if round_money(sum(by_fund.values(), Decimal(0)), currency):
            raise _movement_template(funds, next(iter(by_fund), None))

    na, _ = fund_measures(entries, accounts, net_assets)
    na = {f: v for f, v in na.items() if round_money(v, currency)}
    if je_type == "fund_opening":
        for code, by_fund in delta.items():
            if not by_fund.get(None) or any(v * by_fund[None] > 0 for f, v in by_fund.items() if f is not None):
                raise _movement_template(funds, next(iter(by_fund), None))
        if not reverses:
            for fund_id, v in na.items():
                if v < 0 and _restricted(funds, fund_id):
                    raise RestrictedFundMovement("posting.fund.opening_restricted_negative", fund=_fund_name(funds, fund_id))
        givers = {f for by_fund in delta.values() for f in by_fund if (f is not None) == reverses}
    else:
        senders = [f for f, v in na.items() if v < 0]
        receivers = [f for f, v in na.items() if v > 0]
        involved = {e.get("fund_id") or None for e in entries}
        if len(senders) != 1 or len(receivers) != 1 or involved != {senders[0], receivers[0]}:
            raise _movement_template(funds, (senders or receivers or [None])[0])
        source, target = (receivers[0], senders[0]) if reverses else (senders[0], receivers[0])
        if je_type == "fund_transfer":
            allowed = not _restricted(funds, source) and not _restricted(funds, target)
        elif je_type == "fund_release":
            allowed = _restricted(funds, source) and not _restricted(funds, target)
        else:
            allowed = _restricted(funds, source) and _restricted(funds, target) and (
                str(funds[source].get("purpose") or "").strip() == str(funds[target].get("purpose") or "").strip())
        if not allowed:
            raise _movement_template(funds, source)
        if je_type == "fund_reallocation" and not (
                str(data.get("reason") or "").strip() and (reverses or str(data.get("donor_authorization") or "").strip())):
            raise RestrictedFundMovement("posting.fund.reallocation_needs_authorization", fund=_fund_name(funds, source))
        givers = {senders[0]}
        if je_type == "fund_release" and not reverses:
            await _release_backing(session, company_id, data, round_money(na[target], currency), funds,
                                   _fund_name(funds, source), currency)

    span = await fund_balance_range(session, company_id, as_of=str(data.get("ts") or "")[:10] or "9999-12-31")
    for code, by_fund in sorted(delta.items()):
        for fund_id in sorted((f for f in by_fund if f in givers), key=lambda f: f or ""):
            change = by_fund[fund_id]
            low, high = span.get((fund_id, code), (Decimal(0), Decimal(0)))
            available = max(round_money(low if change < 0 else -high, currency), Decimal(0))
            if available < abs(change):
                raise RestrictedFundMovement(
                    "posting.fund.movement_exceeds_balance", fund=_fund_name(funds, fund_id), code=code,
                    available=str(round_money(available, currency)), amount=str(round_money(abs(change), currency)),
                    currency=currency)


async def _release_backing(session: AsyncSession, company_id, data: dict, amount: Decimal,
                          funds: dict[str, dict], fund: str, currency: str) -> None:
    """A release of ``amount`` from ``fund`` names, in ``data["backing"]``, the purpose
    spending it pays for: posted lines on expense accounts, charged to no fund, adding up
    to the amount, each line released no further than its own amount less what the
    releases still standing (not reversed) already took from it."""
    from celerp.models.projections import Projection
    from celerp.services.journal_reversals import standing_reversals
    from celerp.services.money import round_money, to_decimal

    backing = data.get("backing") or []
    missing = RestrictedFundMovement("posting.fund.release_backing_missing", fund=fund)
    if not isinstance(backing, list) or not backing:
        raise missing
    claims: dict[tuple[str, int], Decimal] = {}
    for item in backing:
        if not isinstance(item, dict) or not isinstance(item.get("line_index"), int):
            raise missing
        key = (str(item.get("je_id") or ""), item["line_index"])
        claims[key] = claims.get(key, Decimal(0)) + to_decimal(item.get("amount") or 0)
    if any(v <= 0 for v in claims.values()) or round_money(sum(claims.values(), Decimal(0)) - amount, currency):
        raise missing

    rows = await session.execute(select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "journal_entry",
        Projection.entity_id.in_(sorted({je for je, _ in claims}))))
    states = {str(eid): state or {} for eid, state in rows}
    lines = {}
    for je_id, index in claims:
        entries = states.get(je_id, {}).get("entries") or []
        if states.get(je_id, {}).get("status") != "posted" or not 0 <= index < len(entries):
            raise missing
        lines[(je_id, index)] = entries[index]
    accounts = await lock_accounts(session, company_id, {line.get("account") for line in lines.values()}) or {}
    for line in lines.values():
        if (accounts.get(line.get("account")) or {}).get("account_type") != "expense":
            raise missing
        if line.get("fund_id"):
            raise RestrictedFundMovement("posting.fund.release_backing_charged", fund=fund)

    taken: dict[tuple[str, int], Decimal] = {}
    releases = await session.execute(select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "journal_entry",
        Projection.state["je_type"].as_string() == "fund_release"))
    for eid, state in releases:
        state = state or {}
        if state.get("status") != "posted" or state.get("reverses") or await standing_reversals(session, company_id, str(eid)):
            continue
        for item in state.get("backing") or ():
            key = (str(item.get("je_id")), item.get("line_index"))
            if key in claims:
                taken[key] = taken.get(key, Decimal(0)) + to_decimal(item.get("amount") or 0)
    for key, claim in claims.items():
        line = lines[key]
        spent = to_decimal(line.get("debit") or 0) - to_decimal(line.get("credit") or 0)
        if round_money(claim + taken.get(key, Decimal(0)) - spent, currency) > 0:
            raise RestrictedFundMovement("posting.fund.release_backing_used", fund=fund, code=line.get("account"))


async def check_fund_void(session: AsyncSession, company_id, entity_id: str) -> None:
    """A movement between funds is undone by its reversal, never voided: refuse a void of a
    stored entry that has a movement shape (_movement_shape). Every void path passes here
    (events.engine.emit_event)."""
    from celerp.models.projections import Projection

    state = await session.scalar(select(Projection.state).where(
        Projection.company_id == company_id, Projection.entity_id == entity_id)) or {}
    entries = [e for e in (state.get("entries") or []) if isinstance(e, dict) and e.get("account")]
    settings = await session.scalar(select(Company.settings).where(Company.id == company_id)) or {}
    if not entries or not (funds_enabled(settings) or role_map(settings).get(AccountRole.RESTRICTED_NET_ASSETS)):
        return
    funds = await funds_by_id(session, company_id, {e["fund_id"] for e in entries if e.get("fund_id")})
    accounts = await lock_accounts(session, company_id, {e["account"] for e in entries}) or {}
    restricted_na = set(scope_codes(settings, AccountRole.RESTRICTED_NET_ASSETS))
    retained = set(scope_codes(settings, AccountRole.RETAINED_EARNINGS))
    if _movement_shape(state, entries, accounts, settings, funds, restricted_na, retained):
        raise HTTPException(status_code=422, detail=catalog_refusal(
            "posting.fund.movement_void", fund=_moving_fund(entries, funds)))


def stock_codes(settings: dict) -> set[str]:
    """The accounts of stock and its cost (STOCK_ROLES), which never carry a restricted fund."""
    return set().union(*(scope_codes(settings, role) for role in STOCK_ROLES))


def _check_fund_lines(settings: dict, entries: list[dict], named: set[str], funds: dict[str, dict],
                      restricted_na: set[str], retained: set[str], *, new: bool) -> None:
    """Rules 1, 2, 3 and 7 of check_fund_rules, for a company with funds turned on."""
    from celerp.services.money import round_money, to_decimal

    for fund_id in sorted(named):
        fund = funds.get(fund_id)
        if fund is None:
            raise HTTPException(status_code=422, detail=catalog_refusal("posting.fund.unknown", fund=fund_id))
        if fund.get("archived") and new:
            raise HTTPException(status_code=422, detail=catalog_refusal("posting.fund.archived", fund=fund["name"]))

    currency = str(settings.get("currency") or "USD")
    net: dict[str | None, Decimal] = {}
    for e in entries:
        fund_id = e.get("fund_id") or None
        net[fund_id] = net.get(fund_id, Decimal(0)) + to_decimal(e.get("debit") or 0) - to_decimal(e.get("credit") or 0)
    for fund_id, difference in sorted(net.items(), key=lambda kv: (kv[0] is None, kv[0] or "")):
        if round_money(difference, currency):
            raise UnbalancedJournalEntry(
                "posting.fund.unbalanced", fund=funds[fund_id]["name"] if fund_id else unrestricted_label(),
                difference=str(round_money(abs(difference), currency)), currency=currency)

    stock = stock_codes(settings)
    for e in entries:
        fund = funds.get(e.get("fund_id") or "")
        restricted = bool(fund and fund.get("restricted"))
        if e["account"] in restricted_na and not restricted:
            raise HTTPException(status_code=422, detail=catalog_refusal(
                "posting.fund.restricted_net_assets_needs_fund", code=e["account"]))
        if e["account"] in retained and restricted:
            raise HTTPException(status_code=422, detail=catalog_refusal(
                "posting.fund.retained_earnings_restricted", code=e["account"], fund=fund["name"]))
        if e["account"] in stock and restricted:
            raise HTTPException(status_code=422, detail=catalog_refusal(
                "posting.fund.stock_unrestricted", code=e["account"], fund=fund["name"]))


async def check_fund_rules(session: AsyncSession, company_id, data: dict, entries: list[dict],
                           accounts: dict[str, dict] | None, *, restored: bool = False,
                           actor_id=None, entity_id=None) -> None:
    """The fund rules every journal entry meets, whoever produced it.

    A company that has funds turned on holds its fund ledger (hold_fund_ledger) and reads the setting under the company lock, so a fund cap or turning funds off
    never interleaves with a posting. Then: (1) each fund named exists, and an archived
    one takes no new posting, though a reversal or a restored entry still lands; (2) each
    fund balances on its own, the lines without a fund counting as one; (3) a restricted
    net assets line carries a restricted fund and a retained earnings line never does;
    (7) a line on a stock or stock cost account (STOCK_ROLES) never carries a restricted
    fund; (4, 5, 6) a movement-shaped entry (_movement_shape), restored copies included,
    posts only as _authorize_movement allows and meets its type's template and cap
    (_check_movement). With funds off, a new line carrying a fund
    is refused, unless the entry came from outside (its ``origin``), whose lines are kept as
    sent (peer_lines), and only the movement shapes are judged. With funds on, a new entry for
    a record or journal that came from outside is judged first by rule R
    (peer_lines.check_outside_record): every line it posts is covered by a fund choice."""
    named = {e["fund_id"] for e in entries if e.get("fund_id")}
    new = not (restored or data.get("reverses"))
    settings = await hold_fund_ledger(session, company_id)
    if not funds_enabled(settings) and named and new and not data.get("origin"):
        raise HTTPException(status_code=422, detail=catalog_refusal("posting.fund.funds_off"))
    if not funds_enabled(settings) and not role_map(settings).get(AccountRole.RESTRICTED_NET_ASSETS):
        return
    funds = await funds_by_id(session, company_id, named) if named else {}
    restricted_na = set(scope_codes(settings, AccountRole.RESTRICTED_NET_ASSETS))
    retained = set(scope_codes(settings, AccountRole.RETAINED_EARNINGS))
    if funds_enabled(settings):
        if new:
            await peer_lines.check_outside_record(session, company_id, data, entries, settings)
        _check_fund_lines(settings, entries, named, funds, restricted_na, retained, new=new)

    if _movement_shape(data, entries, accounts or {}, settings, funds, restricted_na, retained):
        na, _ = fund_measures(entries, accounts or {}, restricted_na | retained)
        await _authorize_movement(session, company_id, data, entries, funds, na, actor_id=actor_id, entity_id=entity_id)
        await _check_movement(session, company_id, data, entries, accounts or {}, settings, funds, restricted_na | retained)


def unrestricted_label() -> str:
    """How the lines carrying no fund are named, in the reader's language."""
    from ui.i18n import t

    return t("posting.fund.unrestricted")


async def require_destinations(session: AsyncSession, company_id, codes) -> dict[str, dict] | None:
    """Refuse a new posting a user directed to any of ``codes`` unless each is an account
    in the chart, active, with nothing under it (a header only sums the accounts below it).

    The accounts stay as read until the transaction ends (the company lock), so switching one off or
    putting an account under it waits for the posting, and a posting that waited for such
    a change sees it and is refused. Returns the locked accounts, for the caller's own
    rule on their type; None when the accounting module is not running, so there is no
    chart to check against. Only a destination the user picks for a new posting is
    checked: reversing or settling what the books hold posts through the accounts it was
    recorded on, even if they are now inactive.
    """
    accounts = await lock_accounts(session, company_id, codes)
    if accounts is None:
        return None
    for code in sorted(set(codes)):
        problem = _destination_problem(accounts.get(code))
        if problem is not None:
            raise HTTPException(status_code=422, detail=refusal(
                f"posting.destination.{problem}", _DESTINATION_PROBLEMS[problem].format(code=code), code=code))
    return accounts


_DESTINATION_PROBLEMS = {
    "not_in_chart": "Account {code} is not in the chart of accounts.",
    "inactive": "Account {code} is inactive. Choose an active account.",
    "header": "Account {code} is a header account. Choose one of the accounts under it.",
}

_INCOME_ACCOUNT_PROBLEMS = {
    "not_in_chart": "{item} is sold on account {code}, which is not in the chart of accounts.",
    "inactive": "{item} is sold on account {code}, which is inactive. Choose an active revenue account for it.",
    "header": "{item} is sold on account {code}, which is a header account. Choose one of the accounts under it.",
    "not_revenue": "{item} is sold on account {code}, which is not a revenue account. Choose a revenue account for it.",
}


def _destination_problem(account: dict | None) -> str | None:
    """Why ``require_destinations`` refuses an account (a key of _DESTINATION_PROBLEMS),
    or None when a new posting may go to it."""
    if account is None:
        return "not_in_chart"
    if not account["is_active"]:
        return "inactive"
    if account["has_children"]:
        return "header"
    return None


CATEGORY_INCOME_ACCOUNTS = "category_income_accounts"  # company setting {category key: account code}


def income_account_of(settings: dict, item: dict | None) -> str | None:
    """The account a sale of ``item`` (its state) is credited to: the item's own income
    account, else its category's, else None for the sales revenue account."""
    if not item:
        return None
    return item.get(INCOME_ACCOUNT_FIELD) or (settings.get(CATEGORY_INCOME_ACCOUNTS) or {}).get(
        str(item.get("category") or "")) or None


async def require_income_account(session: AsyncSession, company_id, code: str, *, item: str | None = None) -> None:
    """Refuse ``code`` as the account a sale is credited to: a destination
    ``require_destinations`` accepts whose type is revenue. Saving an item's or a
    category's income account checks it plainly; finalizing a sale checks it again
    for each line, naming the ``item`` that line sells, since the account may have
    changed since it was saved. Nothing is checked while the accounting module is off."""
    accounts = await lock_accounts(session, company_id, {code})
    if accounts is None:
        return
    account = accounts.get(code)
    problem = _destination_problem(account)
    if problem is None and account["account_type"] not in ROLE_TYPES[AccountRole.SALES_REVENUE]:
        problem = "not_revenue"
    if problem is None:
        return
    if item is not None:
        detail = refusal(f"posting.income_account.{problem}",
                         _INCOME_ACCOUNT_PROBLEMS[problem].format(item=item, code=code), item=item, code=code)
    elif problem == "not_revenue":
        detail = refusal("posting.destination.not_revenue",
                         f"Account {code} is of type {account['account_type']}. Sales are credited to a revenue account.",
                         code=code, type=account["account_type"])
    else:
        detail = refusal(f"posting.destination.{problem}", _DESTINATION_PROBLEMS[problem].format(code=code), code=code)
    raise HTTPException(status_code=422, detail=detail)


async def require_line_destinations(session: AsyncSession, company_id, line_items) -> None:
    """Refuse a bill or purchase order whose lines name an account of their own that
    ``require_destinations`` would refuse: the bill posts each such line there."""
    await require_destinations(session, company_id, {
        li["account_code"] for li in line_items or [] if isinstance(li, dict) and li.get("account_code")})


async def require_settlement_account(session: AsyncSession, company_id, code: str) -> None:
    """Refuse a new payment or refund through ``code`` unless it can hold money: a
    destination ``require_destinations`` accepts that is an asset account, such as a bank
    or a clearing account.

    Only a new settlement is checked. Giving back money already recorded posts
    through the account it came in through, even if that account is now inactive.
    """
    accounts = await require_destinations(session, company_id, {code})
    account_type = (accounts or {}).get(code, {}).get("account_type")
    if accounts is not None and account_type != "asset":
        raise HTTPException(status_code=422, detail=refusal(
            "posting.destination.not_money",
            f"Account {code} is of type {account_type}. Money is paid or refunded through an asset "
            "account, such as a bank or a clearing account.", code=code, type=account_type))
