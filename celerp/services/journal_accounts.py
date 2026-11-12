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


class RestrictedFundMovement(UnbalancedJournalEntry):
    """A journal entry that moves money between funds other than through
    post_fund_movement, by an actor it was not granted to, or against its movement rules."""


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


async def hold_fund_ledger(session: AsyncSession, company_id) -> dict:
    """Lock the company, read its settings under that lock and, when it has funds turned
    on, hold its fund ledger shared (lock_fund_ledger) once in this transaction; return
    the settings. A posting calls it before it locks any other row."""
    from celerp.services.company_lock import lock_company, lock_fund_ledger, noted

    await lock_company(session, company_id)
    settings = await session.scalar(select(Company.settings).where(Company.id == company_id)) or {}
    if funds_enabled(settings) and not any(c == str(company_id) for c, _ in noted(session, "funds")):
        await lock_fund_ledger(session, company_id, shared=True)
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
                              funds: dict[str, dict], *, actor_id, entity_id) -> None:
    """A movement-shaped entry posts only inside the grant for this entity and actor, from a
    user whose role, read under the company lock (hold_movement_actor), may manage accounting. The actor
    is recorded on the entry as ``authorized_by``."""
    from celerp.services.permissions import missing_permission_refusal, role_has_permission

    fund = _moving_fund(entries, funds)
    grant = _open_grant(company_id, entity_id, actor_id)
    if grant is None:
        raise RestrictedFundMovement("posting.fund.movement_outside", fund=fund)
    grant.used = True
    if actor_id is None or grant.authority is None:
        raise RestrictedFundMovement("posting.fund.movement_needs_user", fund=fund)
    role, authority = grant.authority
    if not role_has_permission(authority, role, "manage_accounting"):
        raise RestrictedFundMovement(
            "posting.fund.movement_permission", fund=fund,
            permission=missing_permission_refusal("manage_accounting")["params"]["label"])
    data["authorized_by"] = str(actor_id)


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

    stock = set().union(*(scope_codes(settings, role) for role in STOCK_ROLES))
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

    A company that has funds turned on holds its fund ledger shared (lock_fund_ledger)
    and reads the setting under the company lock, so a fund cap or turning funds off
    never interleaves with a posting. Then: (1) each fund named exists, and an archived
    one takes no new posting, though a reversal or a restored entry still lands; (2) each
    fund balances on its own, the lines without a fund counting as one; (3) a restricted
    net assets line carries a restricted fund and a retained earnings line never does;
    (7) a line on a stock or stock cost account (STOCK_ROLES) never carries a restricted
    fund; (4, 6) a movement-shaped entry (_movement_shape), restored copies included,
    posts only as _authorize_movement allows. With funds off, a new line carrying a fund
    is refused, and only the movement shapes are judged."""
    named = {e["fund_id"] for e in entries if e.get("fund_id")}
    new = not (restored or data.get("reverses"))
    settings = await hold_fund_ledger(session, company_id)
    if not funds_enabled(settings) and named and new:
        raise HTTPException(status_code=422, detail=catalog_refusal("posting.fund.funds_off"))
    if not funds_enabled(settings) and not role_map(settings).get(AccountRole.RESTRICTED_NET_ASSETS):
        return
    funds = await funds_by_id(session, company_id, named) if named else {}
    restricted_na = set(scope_codes(settings, AccountRole.RESTRICTED_NET_ASSETS))
    retained = set(scope_codes(settings, AccountRole.RETAINED_EARNINGS))
    if funds_enabled(settings):
        _check_fund_lines(settings, entries, named, funds, restricted_na, retained, new=new)

    if _movement_shape(data, entries, accounts or {}, settings, funds, restricted_na, retained):
        await _authorize_movement(session, company_id, data, entries, funds, actor_id=actor_id, entity_id=entity_id)


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
