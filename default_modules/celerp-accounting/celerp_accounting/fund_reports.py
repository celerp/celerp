# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The fund reports: the fund balance sheet, fund activity and the fund reconciliation.

Each reads the posted journal as the ledger does (ledger_rows.journal_rows). A line counts in
a fund when it names a fund of this company (journal_accounts.line_fund); every other line is
the unrestricted group, keyed ``unrestricted`` in each report's per-fund mappings. A fund's net
assets are what it holds less what it owes: its debits less credits on asset and liability
accounts. Read only; nothing here posts or locks.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services.auth import get_current_company_id, get_current_user
from celerp.services.business_time import company_timezone
from celerp.services.journal_accounts import (
    FUND_MOVEMENT_TYPES, YEAR_CLOSE, fund_account_balances, fund_day_changes, funds_by_id, line_change, line_fund,
)
from celerp.services.ledger_rows import journal_rows
from celerp.services.money import to_stored_float
from celerp.services.peer_lines import UNRESTRICTED, leg_funds
from celerp.services.permissions import require_permission
from celerp_accounting.models import Account
from celerp_accounting.routes import _line_amounts, _require_date_range, _require_iso_date
from ui.i18n import t

router = APIRouter(dependencies=[Depends(get_current_user)])

_HOLDINGS = frozenset({"asset", "liability"})
# Where an equity line of each movement type lands in fund activity; an equity line of any
# other entry (a contribution or a drawing) is an addition to net assets, read as received.
_MOVEMENT_COLUMN = {"fund_release": "released", "fund_transfer": "transferred",
                    "fund_reallocation": "transferred", "fund_opening": "transferred"}
_COLUMNS = ("opening", "received", "spent", "released", "transferred", "closing")
FUND_CHANGE_EVENTS = ("doc.fund_changed", "acc.journal_entry.fund_changed")


def _key(fund_id: str | None) -> str:
    return fund_id or UNRESTRICTED


async def _account_types(session: AsyncSession, company_id) -> dict[str, Account]:
    return {a.code: a for a in (await session.scalars(select(Account).where(Account.company_id == company_id)))}


def _columns(funds: dict[str, dict], seen) -> list[dict]:
    """Every fund still active or holding something, by name, then the unrestricted group."""
    shown = sorted((f for f, s in funds.items() if not s.get("archived") or f in seen),
                   key=lambda f: str(funds[f]["name"]).casefold())
    return [*({"id": f, "name": funds[f]["name"], "restricted": bool(funds[f].get("restricted"))} for f in shown),
            {"id": UNRESTRICTED, "name": t("label.unrestricted"), "restricted": False}]


def _net_assets(held: dict[str, Decimal], accounts: dict[str, Account]) -> Decimal:
    return sum((amount for code, amount in held.items()
                if code in accounts and accounts[code].account_type in _HOLDINGS), Decimal(0))


async def fund_balance_sheet(session: AsyncSession, company_id, *, as_of: str | None = None) -> dict:
    """Each asset and liability account split by fund (liabilities credit-normal, as on the
    balance sheet), each fund's net assets, and the restricted net assets apart from the rest."""
    funds = await funds_by_id(session, company_id)
    held = await fund_account_balances(session, company_id, as_of=as_of)
    accounts = await _account_types(session, company_id)
    columns = _columns(funds, held)
    rows = []
    for code in sorted({c for amounts in held.values() for c in amounts}):
        account = accounts.get(code)
        if account is None or account.account_type not in _HOLDINGS:
            continue
        sign = -1 if account.account_type == "liability" else 1
        amounts = {_key(f): to_stored_float(sign * by[code]) for f, by in held.items() if by.get(code)}
        rows.append({"code": code, "name": account.name, "account_type": account.account_type,
                     "amounts": amounts, "total": to_stored_float(sum((sign * by.get(code, Decimal(0))
                                                                       for by in held.values()), Decimal(0)))})
    restricted = Decimal(0)
    for column in columns:
        net = _net_assets(held.get(None if column["id"] == UNRESTRICTED else column["id"], {}), accounts)
        column["net_assets"] = to_stored_float(net)
        restricted += net if column["restricted"] else 0
    total = sum((_net_assets(by, accounts) for by in held.values()), Decimal(0))
    return {"as_of": as_of, "funds": columns, "accounts": rows,
            "restricted_net_assets": to_stored_float(restricted),
            "available_net_assets": to_stored_float(total - restricted)}


def _movement_view(entity_id: str, state: dict, day: str, funds: dict, accounts: dict) -> dict:
    amounts: dict[str, Decimal] = {}
    for line in state.get("entries") or ():
        account = accounts.get(line.get("account")) if isinstance(line, dict) else None
        if account is not None and account.account_type in _HOLDINGS:
            key = _key(line_fund(funds, line))
            amounts[key] = amounts.get(key, Decimal(0)) + line_change(line)
    return {"entity_id": entity_id, "je_type": state.get("je_type"), "date": day, "memo": state.get("memo"),
            "reason": state.get("reason"), "reverses": state.get("reverses"), "reversed_by": None,
            "amounts": {k: to_stored_float(v) for k, v in amounts.items() if v}}


async def fund_changes(session: AsyncSession, company_id, *, date_from: str | None = None,
                       date_to: str | None = None) -> list[dict]:
    """Every Change fund, newest first, dated on the company's business day: the record, its
    reason, who made it, the entries it voided and the copies it posted in their place."""
    events = (await session.scalars(select(LedgerEntry).where(
        LedgerEntry.company_id == company_id, LedgerEntry.event_type.in_(FUND_CHANGE_EVENTS),
    ).order_by(LedgerEntry.id.desc()))).all()
    zone = await company_timezone(session, company_id)
    out = []
    for event in events:
        day = event.ts.astimezone(zone).date().isoformat()
        if (date_from and day < date_from) or (date_to and day > date_to):
            continue
        copies = ((event.data or {}).get("result") or {}).get("copies") or {}
        out.append({"record_id": event.entity_id, "date": day, "reason": (event.data or {}).get("reason"),
                    "actor_id": str(event.actor_id) if event.actor_id else None,
                    "voided": list(copies), "posted": list(copies.values())})
    return out


async def fund_activity(session: AsyncSession, company_id, *, date_from: str | None = None,
                        date_to: str | None = None) -> dict:
    """Per fund over the period: net assets at its start (opening) and end (closing), and
    between them what income added (received), costs took (spent), releases from
    restriction moved (released) and transfers, reallocations and opening assignments moved
    (transferred), each signed for the fund. A year close moves nothing between these and is
    left out. The period's movements are listed newest first, each naming its reversal, then
    the period's fund changes."""
    funds = await funds_by_id(session, company_id)
    accounts = await _account_types(session, company_id)
    figures: dict[str, dict[str, Decimal]] = {}
    movements: dict[str, dict] = {}
    reversed_by: dict[str, str] = {}
    for row_id, state, day in await journal_rows(session, company_id):
        je_type = state.get("je_type")
        if je_type in FUND_MOVEMENT_TYPES and state.get("reverses"):
            reversed_by[state["reverses"]] = row_id
        if date_to and day > date_to:
            continue
        before = bool(date_from and day < date_from)
        if je_type in FUND_MOVEMENT_TYPES and not before:
            movements[row_id] = _movement_view(row_id, state, day, funds, accounts)
        for line in state.get("entries") or ():
            account = accounts.get(line.get("account")) if isinstance(line, dict) else None
            if account is None:
                continue
            fund = figures.setdefault(_key(line_fund(funds, line)), dict.fromkeys(_COLUMNS, Decimal(0)))
            change = line_change(line)
            if account.account_type in _HOLDINGS:
                fund["closing"] += change
                if before:
                    fund["opening"] += change
            elif before or je_type == YEAR_CLOSE:
                continue
            elif account.account_type in ("expense", "cogs"):
                fund["spent"] += change
            elif account.account_type == "equity":
                fund[_MOVEMENT_COLUMN.get(je_type, "received")] -= change
            else:
                fund["received"] -= change
    columns = _columns(funds, figures)
    for column in columns:
        values = figures.get(column["id"], dict.fromkeys(_COLUMNS, Decimal(0)))
        column.update({c: to_stored_float(values[c]) for c in _COLUMNS})
    for entity_id, view in movements.items():
        view["reversed_by"] = reversed_by.get(entity_id)
    return {"date_from": date_from, "date_to": date_to, "funds": columns,
            "movements": sorted(movements.values(), key=lambda m: (m["date"], m["entity_id"]), reverse=True),
            "fund_changes": await fund_changes(session, company_id, date_from=date_from, date_to=date_to)}


def _mirrors(rows: list[tuple[str, dict, str]], funds: dict) -> list[dict]:
    """Pairs of entries, one of them manual, whose lines are each other's with debit and credit
    swapped, same accounts and amounts, on different funds: each is valid alone, together they
    move value between funds, so the pair is listed for review. An entry posted for a record
    (a document's payment or refund) is never the manual one."""
    def lines(state, swap):
        return tuple(sorted((str(line.get("account")), line_change(line) * (-1 if swap else 1))
                            for line in state.get("entries") or () if isinstance(line, dict)))

    def fund_set(state):
        return frozenset(line_fund(funds, line) for line in state.get("entries") or () if isinstance(line, dict))

    by_lines: dict[tuple, list[tuple[str, dict]]] = {}
    for row_id, state, _ in rows:
        by_lines.setdefault(lines(state, False), []).append((row_id, state))
    pairs: dict[frozenset, dict] = {}
    for row_id, state, _ in rows:
        if state.get("record_id") or state.get("je_type") not in (None, "manual"):
            continue
        for other_id, other in by_lines.get(lines(state, True), ()):
            if (other_id != row_id and fund_set(other) != fund_set(state)
                    and row_id != other.get("reverses") and other_id != state.get("reverses")):
                pairs.setdefault(frozenset({row_id, other_id}), {"entries": sorted([other_id, row_id])})
    return list(pairs.values())


async def _attribution(session: AsyncSession, company_id, rows: list[tuple[str, dict, str]], funds: dict,
                       accounts: dict) -> list[dict]:
    """Each entry posted for a document whose lines carry a fund, compared with those lines
    (check_fund_attribution): a leg on another fund than the document line it names, or an
    income or cost line of no fund and no document line where the document carries a fund."""
    records = sorted({str(state["record_id"]) for _, state, _ in rows if state.get("record_id")})
    docs = {}
    for start in range(0, len(records), 500):
        docs.update({p.entity_id: p.state or {} for p in (await session.scalars(select(Projection).where(
            Projection.company_id == company_id, Projection.entity_type == "doc",
            Projection.entity_id.in_(records[start:start + 500])))).all()})
    out = []
    for row_id, state, _ in rows:
        doc = docs.get(str(state.get("record_id") or ""))
        if not doc:
            continue
        line_funds, _header = leg_funds(doc)
        carried = sorted((n, f) for n, f in line_funds.items() if f in funds)
        if not carried:
            continue
        found: dict[int | None, dict] = {}
        for line in state.get("entries") or ():
            if not isinstance(line, dict):
                continue
            posted = line_fund(funds, line)
            named = line.get("source_lines") or ()
            for n in named:
                expected = line_funds.get(int(n))
                if expected != posted:
                    found.setdefault(int(n), {"line": int(n), "posted_fund": posted, "line_fund": expected})
            account = accounts.get(line.get("account"))
            if (not named and posted is None and account is not None
                    and account.account_type in ("revenue", "expense")):
                n, fund = carried[0]
                found.setdefault(None, {"line": n, "posted_fund": None, "line_fund": fund})
        out += [{"entity_id": row_id, "record_id": state["record_id"], "doc_type": doc.get("doc_type"), **mismatch}
                for mismatch in found.values()]
    return out


async def check_fund_reconciliation(session: AsyncSession, company_id, *, as_of: str | None = None) -> dict:
    """The funds and the unrestricted group added up against the company's own ledger reading,
    every account on every date to ``as_of``; each restricted fund overdrawn on an account
    (below zero on an asset, a debit balance on a liability) or with net assets below zero;
    cross-fund mirror pairs; the fund changes; and entries posted on other funds than their
    document's lines. Balanced is the first check alone: the rest are listed for review."""
    funds = await funds_by_id(session, company_id)
    accounts = await _account_types(session, company_id)
    rows = [(i, s, d) for i, s, d in await journal_rows(session, company_id) if not (as_of and d > as_of)]
    company: dict[str, dict[str, Decimal]] = {}
    for _, state, day in rows:
        for line in state.get("entries") or ():
            amounts = _line_amounts(line) if isinstance(line, dict) and line.get("account") else None
            if amounts is not None:
                on_day = company.setdefault(day, {})
                on_day[line["account"]] = on_day.get(line["account"], Decimal(0)) + amounts[0] - amounts[1]
    by_fund = await fund_day_changes(session, company_id)
    running_company: dict[str, Decimal] = {}
    running_funds: dict[str, Decimal] = {}
    differences: dict[str, dict] = {}
    for day in sorted(set(company) | {d for d in by_fund if not (as_of and d > as_of)}):
        for code, amount in company.get(day, {}).items():
            running_company[code] = running_company.get(code, Decimal(0)) + amount
        for (_, code), amount in by_fund.get(day, {}).items():
            running_funds[code] = running_funds.get(code, Decimal(0)) + amount
        for code in set(running_company) | set(running_funds):
            ledger, summed = running_company.get(code, Decimal(0)), running_funds.get(code, Decimal(0))
            if ledger != summed and code not in differences:
                differences[code] = {"date": day, "account": code, "company_total": to_stored_float(ledger),
                                     "funds_total": to_stored_float(summed)}
    held = await fund_account_balances(session, company_id, as_of=as_of)
    overdrawn, negative = [], []
    for fund_id in sorted((f for f in held if f and funds.get(f, {}).get("restricted")),
                          key=lambda f: str(funds[f]["name"]).casefold()):
        for code, balance in sorted(held[fund_id].items()):
            kind = accounts[code].account_type if code in accounts else None
            if (kind == "asset" and balance < 0) or (kind == "liability" and balance > 0):
                overdrawn.append({"fund_id": fund_id, "fund": funds[fund_id]["name"], "account": code,
                                  "balance": to_stored_float(balance)})
        net = _net_assets(held[fund_id], accounts)
        if net < 0:
            negative.append({"fund_id": fund_id, "fund": funds[fund_id]["name"], "net_assets": to_stored_float(net)})
    posted = [(i, s, d) for i, s, d in rows if not i.endswith(":reversal")]
    return {"as_of": as_of, "balanced": not differences, "differences": list(differences.values()),
            "overdrawn": overdrawn, "negative_net_assets": negative, "mirrors": _mirrors(posted, funds),
            "fund_changes": await fund_changes(session, company_id, date_to=as_of),
            "attribution": await _attribution(session, company_id, posted, funds, accounts)}


@router.get("/funds/balance-sheet")
async def get_fund_balance_sheet(
    as_of: str | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
    _: None = require_permission("view_financial_reports"),
) -> dict:
    _require_iso_date(as_of, t("acct.field_as_of_date"))
    return await fund_balance_sheet(session, company_id, as_of=as_of)


@router.get("/funds/activity")
async def get_fund_activity(
    date_from: str | None = None,
    date_to: str | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
    _: None = require_permission("view_financial_reports"),
) -> dict:
    _require_date_range(date_from, date_to)
    return await fund_activity(session, company_id, date_from=date_from, date_to=date_to)


@router.get("/funds/reconciliation")
async def get_fund_reconciliation(
    as_of: str | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
    _: None = require_permission("view_financial_reports"),
) -> dict:
    _require_iso_date(as_of, t("acct.field_as_of_date"))
    return await check_fund_reconciliation(session, company_id, as_of=as_of)
