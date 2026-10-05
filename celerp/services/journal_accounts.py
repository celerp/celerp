# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""The one boundary every journal line passes before it enters the ledger.

emit_event calls ``prepare_journal_entry`` for every acc.journal_entry.created,
whoever produced it: automatic postings, manual journals, imports, bank
reconciliation, repairs. Replay applies stored events directly and never comes
back through here, so history is never revalidated or rewritten.

The chart of accounts belongs to the accounting module. When its API starts it
registers a ``ChartAccess`` here (``register_chart``): reading and share-locking
account rows, listing the chart, and adding an account. Core never touches its
table. Only the bundled accounting module can register; with nothing registered,
or the module stopped, accounting is not running and only the role snapshot is
written.

Any module may post journal entries, and adds the accounts they post to through
``add_account``, which the registered chart checks as Settings does.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import (
    ROLE_LABELS,
    SCHEMA_KEY,
    AccountRole,
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
    "is_active", "has_children"}}, share-locked; list_accounts(session, company_id) ->
    the chart's rows; add_account(session, company_id, *, code, name, account_type).
    """

    lock_accounts: Callable[..., Awaitable[dict[str, dict]]]
    list_accounts: Callable[..., Awaitable[list[dict]]]
    add_account: Callable[..., Awaitable[None]]


class UntrustedChartError(ValueError):
    """A chart offered by code that is not the bundled accounting module."""


_chart: ChartAccess | None = None


def register_chart(access: ChartAccess) -> None:
    """Accept ``access`` only when every one of its functions is code of the bundled,
    content-verified accounting module."""
    from celerp.modules.loader import first_party_owner

    global _chart
    for fn in (access.lock_accounts, access.list_accounts, access.add_account):
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
    """The company's accounts among ``codes``, share-locked until the transaction ends.

    Each value is {"code", "account_type", "is_active", "has_children"}; a code the
    chart does not hold is absent. None when the accounting module is not running.
    """
    chart = chart_access()
    if chart is None:
        return None
    return await chart.lock_accounts(session, company_id, set(codes))


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
                raise HTTPException(status_code=422, detail=unknown_role(role))
            if current.get(role) == code:
                problems = target_problems([role], current, accounts)
                if problems:
                    raise PostingRoleError([problems[role]])
            elif code not in scope_codes(settings, role):
                raise HTTPException(
                    status_code=422,
                    detail=f"Account {code} has never been the {ROLE_LABELS[AccountRole(role)].lower()} account.",
                )


async def require_destinations(session: AsyncSession, company_id, codes) -> dict[str, dict] | None:
    """Refuse a new posting a user directed to any of ``codes`` unless each is an account
    in the chart, active, with nothing under it (a header only sums the accounts below it).

    The accounts stay share-locked until the transaction ends, so switching one off or
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
        account = accounts.get(code)
        if account is None:
            detail = refusal("posting.destination.not_in_chart",
                             f"Account {code} is not in the chart of accounts.", code=code)
        elif not account["is_active"]:
            detail = refusal("posting.destination.inactive",
                             f"Account {code} is inactive. Choose an active account.", code=code)
        elif account["has_children"]:
            detail = refusal("posting.destination.header",
                             f"Account {code} is a header account. Choose one of the accounts under it.", code=code)
        else:
            continue
        raise HTTPException(status_code=422, detail=detail)
    return accounts


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
