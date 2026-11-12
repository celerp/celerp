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


async def prepare_journal_entry(session: AsyncSession, company_id, data: dict, *, restored: bool = False) -> None:
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
    recorded none stays without one, never classified by today's roles.
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
    await check_fund_rules(session, company_id, data, entries, restored=restored)


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


async def check_fund_rules(session: AsyncSession, company_id, data: dict, entries: list[dict], *,
                           restored: bool = False) -> None:
    """The fund rules every journal entry meets, whoever produced it.

    A company that has funds turned on holds its fund ledger shared (lock_fund_ledger)
    and reads the setting under the company lock, so a fund cap or turning funds off
    never interleaves with a posting. Then: (1) each fund named exists, and an archived
    one takes no new posting, though a reversal or a restored entry still lands; (2) each
    fund balances on its own, the lines without a fund counting as one; (3) a restricted
    net assets line carries a restricted fund and a retained earnings line never does;
    (7) a line on a stock or stock cost account (STOCK_ROLES) never carries a restricted fund.
    With funds off, a new line carrying a fund is refused and nothing else is checked."""
    from celerp.services.money import round_money, to_decimal

    named = {e["fund_id"] for e in entries if e.get("fund_id")}
    new = not (restored or data.get("reverses"))
    settings = await hold_fund_ledger(session, company_id)
    if not funds_enabled(settings):
        if named and new:
            raise HTTPException(status_code=422, detail=catalog_refusal("posting.fund.funds_off"))
        return
    funds = await funds_by_id(session, company_id, named)
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

    restricted_na = set(scope_codes(settings, AccountRole.RESTRICTED_NET_ASSETS))
    retained = set(scope_codes(settings, AccountRole.RETAINED_EARNINGS))
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
