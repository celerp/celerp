# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Accounts between a client company and its accountant.

Every journal (both ways) and every document the client sends carries the accounts its
lines name, keyed by the sender's code, with their parents: id, code, name, type, parent,
currency (the bank account's, else the books'), cash flow section, the posting roles the
account is the target of, and ``yours``, the receiver's account the sender has linked it
to. The receiver places each account in this order:

1. a posting-role account posts to the receiver's account for the role;
2. an account linked by its id posts to the linked account, whatever either is named;
3. ``yours`` links to that account;
4. an account with the same code, type, currency and name links to it; another type,
   another currency, another name, or a code linked to another account holds the record;
5. an account the receiver does not have is added with the record, parents first.

A firm company that was not opened from a copy of the client holds every account that is
not an exact match as waiting for its review, until the firm confirms the review. Every
hold is decided before anything is written, so a held record writes nothing; a record and
the accounts it adds commit together, in the caller's transaction.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import POSTABLE_ROLES, ROLE_LABELS, AccountRole, is_role
from celerp.importers.results import failure_reason
from celerp.models.accounting import UserCompany
from celerp.models.company import Company
from celerp.models.company_peer import CompanyPeerAccount, CompanyPeerAttention, CompanyPeerGrant
from celerp.services import peer_attention
from celerp.services.account_roles import role_map
from celerp.services.company_lock import locked_company
from celerp.services.journal_accounts import chart_access
from celerp.services.money import books_currency

# Where a company sees the accounts sync held, added, or is waiting for it to review.
ACCOUNTS_PAGE = "/accounting/sync-accounts"
POSTING_ACCOUNTS = "/settings/accounting#posting-accounts"
# The connected companies a firm company has reviewed the accounts of.
REVIEWED_KEY = "peer_accounts_reviewed"
ACCOUNT = "account"
_MAX_ACCOUNTS = 1000


class AccountHold(HTTPException):
    """A record that cannot post until one of its accounts is fixed; ``fix`` says how
    and travels with the record's attention item."""

    def __init__(self, reason: str, fix: dict):
        super().__init__(409, reason)
        self.fix = fix


class _Account(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    account_type: str = Field(min_length=1, max_length=32)
    parent_code: str | None = Field(default=None, max_length=64)
    currency: str = Field(min_length=3, max_length=3)
    cash_flow_category: str | None = Field(default=None, max_length=32)
    roles: list[str] = Field(default_factory=list, max_length=64)
    yours: str | None = Field(default=None, max_length=64)


def parse(value) -> dict[str, dict]:
    """The accounts a record carries, keyed by the sender's code; refused when malformed."""
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > _MAX_ACCOUNTS:
        raise HTTPException(422, "Invalid accounts")
    try:
        out = {code: _Account.model_validate(a).model_dump() for code, a in value.items()}
    except ValidationError:
        raise HTTPException(422, "Invalid accounts") from None
    if any(code != a["code"] for code, a in out.items()) or len({a["id"] for a in out.values()}) != len(out):
        raise HTTPException(422, "Invalid accounts")
    return out


def label(account: dict) -> str:
    return f"{account['code']} {account['name']}"


def _currency(account: dict, books: str) -> str:
    return str(account.get("currency") or books).upper()


def _same_name(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


def _article(word: str) -> str:
    return ("an " if word[:1].lower() in "aeiou" else "a ") + word


def next_free(code: str, taken) -> str:
    """The first code after ``code`` the chart does not use: 6171 after 6170, else 6170-1."""
    if code.isdigit():
        n = int(code) + 1
        while str(n).zfill(len(code)) in taken:
            n += 1
        return str(n).zfill(len(code))
    n = 1
    while f"{code}-{n}" in taken:
        n += 1
    return f"{code}-{n}"


def _postable(roles) -> list[str]:
    return [r for r in roles if is_role(r) and AccountRole(r) in POSTABLE_ROLES]


async def links(session: AsyncSession, company_id: uuid.UUID, source_company_id: uuid.UUID) -> list[CompanyPeerAccount]:
    return list((await session.scalars(select(CompanyPeerAccount).where(
        CompanyPeerAccount.company_id == company_id,
        CompanyPeerAccount.source_company_id == source_company_id,
    ).order_by(CompanyPeerAccount.created_at))).all())


async def link(session: AsyncSession, *, company_id: uuid.UUID, source_company_id: uuid.UUID,
               other: dict, local: dict, how: str, added_from: str | None = None) -> None:
    """Link the other company's account to ``local``, replacing any earlier link of it."""
    values = dict(other_code=other["code"], other_name=other["name"], account_id=local["id"],
                  code=local["code"], how=how, added_from=added_from)
    await session.execute(pg_insert(CompanyPeerAccount).values(
        id=uuid.uuid4(), company_id=company_id, source_company_id=source_company_id,
        other_account_id=other["id"], **values,
    ).on_conflict_do_update(constraint="uq_company_peer_account_link", set_=values))


async def seed_copy(session: AsyncSession, *, target_company: uuid.UUID, source_company: uuid.UUID,
                    id_map: dict[str, str]) -> int:
    """Link every account of a copy to the account of the company it was copied from, so
    a firm that opened its client's copy has nothing to review."""
    chart = chart_access()
    if chart is None:
        return 0
    old_id = {new: old for old, new in id_map.items() if new != old}
    rows = await chart.list_accounts(session, target_company)
    for row in rows:
        await link(session, company_id=target_company, source_company_id=source_company,
                   other={"id": old_id.get(row["id"], row["id"]), "code": row["code"], "name": row["name"]},
                   local=row, how="copy")
    return len(rows)


# ---- the sender -----------------------------------------------------------------------

async def outgoing(session: AsyncSession, company_id: uuid.UUID, peer_company: uuid.UUID,
                   codes) -> dict[str, dict]:
    """The accounts ``codes`` name, with their parents, as the connected company reads them."""
    chart = chart_access()
    codes = [c for c in codes if isinstance(c, str) and c]
    if chart is None or not codes:
        return {}
    rows = {a["code"]: a for a in await chart.list_accounts(session, company_id)}
    company = await session.get(Company, company_id)
    settings = dict(company.settings or {}) if company is not None else {}
    books = books_currency(settings)
    roles: dict[str, list[str]] = {}
    for role, code in role_map(settings).items():
        if _postable([role]):
            roles.setdefault(code, []).append(role)
    yours: dict[str, str] = {}
    for row in await links(session, company_id, peer_company):
        if row.account_id not in yours or row.other_code == row.code:
            yours[row.account_id] = row.other_account_id
    out: dict[str, dict] = {}
    stack = [c for c in codes if c in rows]
    while stack:
        code = stack.pop()
        if code in out:
            continue
        a = rows[code]
        out[code] = {"id": a["id"], "code": code, "name": a["name"], "account_type": a["account_type"],
                     "parent_code": a["parent_code"], "currency": _currency(a, books),
                     "cash_flow_category": a["cash_flow_category"], "roles": sorted(roles.get(code, [])),
                     "yours": yours.get(a["id"])}
        if a["parent_code"] in rows:
            stack.append(a["parent_code"])
    return out


async def outgoing_for(company_id: uuid.UUID, peer_company: uuid.UUID, codes) -> dict[str, dict]:
    from celerp.db import get_session_ctx
    async with get_session_ctx() as session:
        return await outgoing(session, company_id, peer_company, codes)


# ---- the receiver ---------------------------------------------------------------------

@dataclass
class _Names:
    """How a hold names the two companies: ``receiver`` None is the reader's own company."""
    sender: str
    receiver: str | None

    @property
    def chart(self) -> str:
        return "your chart" if self.receiver is None else f"the chart of {self.receiver}"

    @property
    def who(self) -> str:
        return "you" if self.receiver is None else self.receiver


@dataclass
class _Plan:
    codes: dict[str, str] = field(default_factory=dict)
    links: list[tuple[dict, dict, str]] = field(default_factory=list)
    adds: list[dict] = field(default_factory=list)
    seen: list[tuple[CompanyPeerAccount, dict]] = field(default_factory=list)


def _fix(kind: str, other: dict, *, local: dict | None = None, suggested: str | None = None,
         add: bool = True, parent: str | None = None, **extra) -> dict:
    return {"kind": kind, "other": other, "local": local["code"] if local else None,
            "suggested": suggested, "add": add, "parent": parent, **extra}


async def review_pending(session: AsyncSession, company: Company, source_company_id: uuid.UUID) -> bool:
    """Whether the firm still has to review the client's accounts: it was not opened from
    a copy of the client and has not confirmed a review."""
    if str(source_company_id) in ((company.settings or {}).get(REVIEWED_KEY) or []):
        return False
    copied = await session.scalar(select(CompanyPeerAccount.id).where(
        CompanyPeerAccount.company_id == company.id,
        CompanyPeerAccount.source_company_id == source_company_id,
        CompanyPeerAccount.how == "copy").limit(1))
    return copied is None


def _local_parent(other: dict, accounts: dict, local: dict, by_id: dict, linked: dict) -> str | None:
    """The receiver's account the sender's parent of ``other`` already is, if any."""
    parent = accounts.get(other.get("parent_code") or "")
    if parent is None:
        return None
    row = linked.get(parent["id"])
    target = by_id.get(row.account_id) if row is not None else by_id.get(parent.get("yours") or "")
    if target is None:
        same = local.get(parent["code"])
        target = same if same is not None and same["account_type"] == parent["account_type"] else None
    return target["code"] if target is not None else None


async def place(session: AsyncSession, *, company_id: uuid.UUID, source_company_id: uuid.UUID,
                accounts: dict[str, dict], codes, sender: str, receiver: str | None,
                record: str, firm: bool) -> dict[str, str]:
    """Each of ``codes`` (the sender's) as the receiver's code, adding the accounts the
    receiver lacks; raises AccountHold, before writing anything, for a record that cannot
    post. An account lands only where its details (type, currency, role) agree with the
    receiver's account, or where an existing link of it points; a code the record carries
    no details for posts only by such a link. ``receiver`` names the
    receiving company in a hold's reason, None when it is read by the receiver itself;
    ``firm`` is True on the firm's side, where an unreviewed client's accounts wait."""
    codes = list(dict.fromkeys(codes))
    chart = chart_access()
    if chart is None:
        return {c: c for c in codes}
    names = _Names(sender, receiver)
    company = await session.get(Company, company_id)
    settings = dict(company.settings or {})
    books = books_currency(settings)
    local = {a["code"]: a for a in await chart.list_accounts(session, company_id)}
    by_id = {a["id"]: a for a in local.values()}
    linked = {row.other_account_id: row for row in await links(session, company_id, source_company_id)}
    taken = {row.code: row.other_account_id for row in linked.values()}
    by_other_code: dict[str, list[CompanyPeerAccount]] = {}
    for row in linked.values():
        by_other_code.setdefault(row.other_code, []).append(row)
    roles = role_map(settings)
    role_of = {c: r for r, c in roles.items() if _postable([r])}
    review = firm and await review_pending(session, company, source_company_id)
    plan, waiting = _Plan(), []
    for code in codes:
        other = accounts.get(code)
        if other is None:
            plan.codes[code] = _by_link(names, code, by_other_code.get(code) or [], by_id, record)
            continue
        parent = _local_parent(other, accounts, local, by_id, linked)
        role = next(iter(_postable(other["roles"])), None)
        if role is not None:
            role_label = ROLE_LABELS[AccountRole(role)]
            target = local.get(roles.get(role) or "")
            if target is None:
                has = "you have" if receiver is None else f"{receiver} has"
                raise AccountHold(
                    f"{label(other)} is the {role_label} account of {sender}, and {has} no "
                    f"{role_label} account chosen. Choose it in Settings > Accounting > Posting accounts.",
                    _fix("role", other, add=False, role=role, href=POSTING_ACCOUNTS))
            if _kind(target, books) != _kind(other, books):
                whose = "your" if receiver is None else f"{receiver}'s"
                raise AccountHold(
                    f"{label(other)} is the {role_label} account of {sender}, {_kind_text(other, books)}, "
                    f"and {whose} {role_label} account {label(target)} is {_kind_text(target, books)}. "
                    f"Choose one that matches in Settings > Accounting > Posting accounts.",
                    _fix("role", other, add=False, role=role, href=POSTING_ACCOUNTS))
            if not target["is_active"]:
                raise _archived(names, other, target)
            plan.codes[code] = target["code"]
            continue
        row = linked.get(other["id"])
        target = by_id.get(row.account_id) if row is not None else None
        how = None
        if target is not None:
            _differs(names, other, target, books, f"{label(target)}, where it posts in {names.chart},",
                     next_free(code, set(local)), parent)
            plan.seen.append((row, other))
        elif other["yours"] in by_id:
            target, how = by_id[other["yours"]], "match"
            _differs(names, other, target, books, f"{label(target)}, where {sender} posts it in {names.chart},",
                     next_free(code, set(local)), parent)
        else:
            same = local.get(code)
            exact = (same is not None and _kind(same, books) == _kind(other, books)
                     and _same_name(same["name"], other["name"]) and taken.get(code) in (None, other["id"])
                     and code not in role_of)
            if exact:
                target, how = same, "match"
            elif review:
                waiting.append(other)
                continue
            elif same is not None:
                suggested = next_free(code, set(local))
                _differs(names, other, same, books, f"{label(same)} in {names.chart}", suggested, parent)
                held_role = role_of.get(code)
                why = (f", the {ROLE_LABELS[AccountRole(held_role)]} account there, which {sender} does not "
                       f"post it as" if held_role else "")
                raise AccountHold(
                    f"{sender}'s account {label(other)} has the same code as {label(same)} in "
                    f"{names.chart}{why}. Use {label(same)} for it, or add it as {suggested}.",
                    _fix("collision", other, local=same, suggested=suggested, parent=parent))
            elif _currency(other, books) != books:
                raise AccountHold(
                    f"{sender}'s account {label(other)} is kept in {_currency(other, books)}, and "
                    f"{names.chart} has no account for it. Use an account kept in {_currency(other, books)}.",
                    _fix("currency", other, add=False))
            else:
                plan.adds.append(other)
                plan.codes[code] = code
                continue
        if not target["is_active"]:
            raise _archived(names, other, target)
        plan.codes[code] = target["code"]
        if how is not None:
            plan.links.append((other, target, how))
    if waiting:
        raise AccountHold(f"Waiting for {receiver or company.name} to review accounts.",
                          _fix("review", waiting[0], add=False, accounts=waiting))
    await _write(session, company_id, source_company_id, plan, accounts, local, by_id, linked,
                 sender=sender, record=record)
    return plan.codes


def _kind(account: dict, books: str) -> tuple[str, str]:
    return account["account_type"], _currency(account, books)


def _kind_text(account: dict, books: str) -> str:
    return f"{_article(account['account_type'])} account kept in {_currency(account, books)}"


def _differs(names: _Names, other: dict, target: dict, books: str, where: str, suggested: str,
             parent: str | None) -> None:
    """Hold ``other`` when ``target`` (``where`` names it) is another type of account, or
    kept in another currency."""
    if target["account_type"] != other["account_type"]:
        raise AccountHold(
            f"{names.sender}'s account {label(other)} is {_article(other['account_type'])} account, "
            f"and {where} is {_article(target['account_type'])} account. "
            f"Use another {other['account_type']} account, or add it as {suggested}.",
            _fix("type", other, local=target, suggested=suggested, parent=parent))
    if _currency(target, books) != _currency(other, books):
        raise AccountHold(
            f"{names.sender}'s account {label(other)} is kept in {_currency(other, books)}, and "
            f"{where} is kept in {_currency(target, books)}. "
            f"Use an account kept in {_currency(other, books)}.",
            _fix("currency", other, local=target, add=False))


def _by_link(names: _Names, code: str, rows: list[CompanyPeerAccount], by_id: dict, record: str) -> str:
    """The receiver's account for the sender's ``code`` when a record carries no details
    of it: the one account a link of that code points to, else the record is held."""
    target = by_id.get(rows[0].account_id) if len(rows) == 1 else None
    if target is None:
        raise HTTPException(409, f"{names.sender} sent {record} without the name, type and currency of "
                                 f"its account {code}, so it is not matched to an account in {names.chart}. "
                                 f"It posts when {names.sender} sends it again with them.")
    if not target["is_active"]:
        raise _archived(names, {"code": code, "name": rows[0].other_name}, target)
    return target["code"]


def _archived(names: _Names, other: dict, target: dict) -> AccountHold:
    return AccountHold(
        f"Account {label(target)}, which {names.who} archived, is where {names.sender}'s "
        f"{label(other)} posts. Unarchive it, or use another account.",
        _fix("archived", other, local=target))


async def _write(session: AsyncSession, company_id: uuid.UUID, source_company_id: uuid.UUID, plan: _Plan,
                 accounts: dict, local: dict, by_id: dict, linked: dict, *, sender: str, record: str) -> None:
    for row, other in plan.seen:
        row.other_code, row.other_name = other["code"], other["name"]  # as the other side names it now
    for other, target, how in plan.links:
        await link(session, company_id=company_id, source_company_id=source_company_id,
                   other=other, local=target, how=how)
    created: dict[str, dict] = {}

    async def add(other: dict) -> dict:
        if other["code"] in created:
            return created[other["code"]]
        parent = _local_parent(other, accounts, local, by_id, linked)
        sender_parent = accounts.get(other.get("parent_code") or "")
        if parent is None and sender_parent is not None and sender_parent["code"] not in local:
            parent = (await add(sender_parent))["code"]
        made, why = await create(session, company_id, other, other["code"], parent)
        created[other["code"]] = made
        local[made["code"]] = by_id[made["id"]] = made
        await link(session, company_id=company_id, source_company_id=source_company_id,
                   other=other, local=made, how="added", added_from=sender)
        await peer_attention.record(
            session, company_id=company_id, source_company_id=source_company_id,
            kind=peer_attention.ADDED, entity_type=ACCOUNT, source_entity_id=other["id"],
            label=label(made), reason=f"Added by sync from {sender} with {record}.",
            fix={"kind": "added", "code": made["code"]})
        params = dict(account=label(made), record=record, company=sender)
        if why is None:
            await _notify(session, company_id, "notice.peer_account_added", **params)
        else:
            await _notify(session, company_id, "notice.peer_account_added_top", **params,
                          type=other["account_type"], why=why)
        return made

    for other in plan.adds:
        await add(other)


async def create(session: AsyncSession, company_id: uuid.UUID, other: dict, code: str,
                 parent: str | None) -> tuple[dict, str | None]:
    """Add ``other`` to the chart as ``code``, under ``parent`` when the chart lets it sit
    there, else at the top of its type with the chart's reason."""
    chart = chart_access()
    fields = dict(code=code, name=other["name"], account_type=other["account_type"],
                  cash_flow_category=other.get("cash_flow_category"))
    why = None
    try:
        account_id = await chart.add_account(session, company_id, **fields, parent_code=parent)
    except HTTPException as exc:
        if parent is None or exc.status_code != 422:
            raise
        why = failure_reason(exc)
        account_id = await chart.add_account(session, company_id, **fields, parent_code=None)
    return {"id": account_id, "code": code, "name": other["name"], "account_type": other["account_type"],
            "is_active": True, "has_children": False, "currency": None,
            "parent_code": None if why else parent}, why


async def _notify(session: AsyncSession, company_id: uuid.UUID, key: str, **params) -> None:
    from celerp.services.peer_journals import _notify as notify
    await notify(session, company_id, key, action_url=ACCOUNTS_PAGE, **params)


# ---- fixes ----------------------------------------------------------------------------

def use_problem(other: dict, target: dict | None, code: str, books: str) -> str | None:
    """Why ``target`` cannot take the place of the other company's ``other``, or None."""
    if target is None:
        return f"Account {code} is not in the chart of accounts."
    if not target["is_active"]:
        return f"Account {code} is inactive. Choose an active account."
    if target["has_children"]:
        return f"Account {code} is a header account. Choose one of the accounts under it."
    if target["account_type"] != other["account_type"]:
        return (f"{label(other)} is {_article(other['account_type'])} account. "
                f"Use another {other['account_type']} account.")
    if _currency(target, books) != _currency(other, books):
        return (f"{label(other)} is kept in {_currency(other, books)}. "
                f"Use an account kept in {_currency(other, books)}.")
    return None


async def _item_to_change(session: AsyncSession, company_id: uuid.UUID, item_id,
                          closed: str) -> CompanyPeerAttention:
    """The open item a request changes, read with the company under its lock as the
    previous holder committed them, so two answers to one item never both apply."""
    try:
        key = uuid.UUID(str(item_id))
    except ValueError:
        raise HTTPException(404, closed) from None
    await locked_company(session, company_id)
    item = await session.get(CompanyPeerAttention, key, populate_existing=True)
    if item is None or item.company_id != company_id:
        raise HTTPException(404, closed)
    return item


async def held_item(session: AsyncSession, company_id: uuid.UUID, item_id) -> CompanyPeerAttention:
    item = await _item_to_change(session, company_id, item_id, "This item is not open.")
    if not item.fix:
        raise HTTPException(404, "This item is not open.")
    return item


async def use_account(session: AsyncSession, company_id: uuid.UUID, source_company_id: uuid.UUID,
                      other: dict, code: str) -> None:
    """Post the other company's ``other`` to the receiver's ``code`` from now on."""
    chart = chart_access()
    if chart is None:
        raise HTTPException(409, "Accounting is not running.")
    company = await session.get(Company, company_id)
    rows = {a["code"]: a for a in await chart.list_accounts(session, company_id)}
    problem = use_problem(other, rows.get(code), code, books_currency(company.settings or {}))
    if problem:
        raise HTTPException(422, problem)
    await link(session, company_id=company_id, source_company_id=source_company_id,
               other=other, local=rows[code], how="chosen")


async def add_account(session: AsyncSession, company_id: uuid.UUID, source_company_id: uuid.UUID,
                      other: dict, code: str, parent: str | None) -> dict:
    """Add the other company's ``other`` to the receiver's chart as ``code`` and post it there."""
    if chart_access() is None:
        raise HTTPException(409, "Accounting is not running.")
    company = await session.get(Company, company_id)
    if _currency(other, books_currency(company.settings or {})) != books_currency(company.settings or {}):
        raise HTTPException(422, f"{label(other)} is kept in {other['currency']}. Add a bank account kept in "
                                 f"{other['currency']} in Banking, then use it.")
    made, _ = await create(session, company_id, other, code, parent)
    await link(session, company_id=company_id, source_company_id=source_company_id,
               other=other, local=made, how="chosen")
    return made


async def fix_hold(session: AsyncSession, company_id: uuid.UUID, item: CompanyPeerAttention, *,
                   action: str, account: str | None = None, code: str | None = None) -> None:
    """Apply ``use`` or ``add`` to a held record's account; the record posts on its next run."""
    fix = item.fix or {}
    other = fix.get("other")
    if fix.get("kind") in ("role", "review", "added") or not isinstance(other, dict):
        raise HTTPException(422, "This item is not fixed here.")
    if action == "use" and account:
        await use_account(session, company_id, item.source_company_id, other, account)
    elif action == "add" and code and fix.get("add"):
        await add_account(session, company_id, item.source_company_id, other, code, fix.get("parent"))
    elif action == "add":
        raise HTTPException(422, f"{label(other)} cannot be added here. Use an account kept in {other['currency']}.")
    else:
        raise HTTPException(422, "Choose an account to use, or a code to add it as.")


async def holds(session: AsyncSession, company_id: uuid.UUID) -> list[dict]:
    """Every record of a connected company held here for an account, newest first."""
    rows = await peer_attention.open_items(session, [company_id])
    return [{"id": str(r.id), "source_company_id": str(r.source_company_id), "label": r.label,
             "reason": r.reason, "fix": r.fix, "at": r.created_at.isoformat()}
            for r in rows if r.kind == peer_attention.REFUSED and r.fix]


async def added(session: AsyncSession, company_id: uuid.UUID) -> list[dict]:
    """Every account sync added here that is not yet kept, archived or removed."""
    rows = await peer_attention.open_items(session, [company_id])
    return [{"id": str(r.id), "label": r.label, "reason": r.reason, "code": (r.fix or {}).get("code"),
             "at": r.created_at.isoformat()} for r in rows if r.kind == peer_attention.ADDED]


async def added_item(session: AsyncSession, company_id: uuid.UUID, item_id) -> CompanyPeerAttention:
    item = await _item_to_change(session, company_id, item_id, "This notice is closed.")
    if item.kind != peer_attention.ADDED:
        raise HTTPException(404, "This notice is closed.")
    return item


async def close_added(session: AsyncSession, item: CompanyPeerAttention, *, removed: bool) -> None:
    """Close an added account's notice; ``removed`` also drops its link, so the next
    record that needs the account adds it again."""
    if removed:
        await session.execute(delete(CompanyPeerAccount).where(
            CompanyPeerAccount.company_id == item.company_id,
            CompanyPeerAccount.source_company_id == item.source_company_id,
            CompanyPeerAccount.code == (item.fix or {}).get("code")))
    await session.delete(item)


async def elsewhere(session: AsyncSession, company_id: uuid.UUID, code: str, name: str) -> list[dict]:
    """How each connected company names the account linked to ``code``, where its code
    or name differs from this company's (names never travel after linking)."""
    rows = (await session.scalars(select(CompanyPeerAccount).where(
        CompanyPeerAccount.company_id == company_id, CompanyPeerAccount.code == code,
    ).order_by(CompanyPeerAccount.created_at))).all()
    out = []
    for row in rows:
        if row.other_code != code or not _same_name(row.other_name, name):
            out.append({"company": await _grant_label(session, company_id, row.source_company_id),
                        "code": row.other_code, "name": row.other_name})
    return out


# ---- the firm's review ----------------------------------------------------------------

async def _grant_label(session: AsyncSession, company_id: uuid.UUID, source: uuid.UUID) -> str | None:
    """The name the company gave the connected company on its newest key, if it holds one."""
    return await session.scalar(select(CompanyPeerGrant.label).where(
        CompanyPeerGrant.company_id == company_id, CompanyPeerGrant.source_company_id == source,
    ).order_by(CompanyPeerGrant.created_at.desc()).limit(1))


async def _client_label(session: AsyncSession, company_id: uuid.UUID, source: uuid.UUID) -> str:
    return await _grant_label(session, company_id, source) or str(source)


async def _previous(session: AsyncSession, company_id: uuid.UUID) -> dict[tuple[str, str], str]:
    """The firm's answers for its other clients: (their code, name) -> the code chosen."""
    owners = select(UserCompany.user_id).where(
        UserCompany.company_id == company_id, UserCompany.role == "owner", UserCompany.is_active.is_(True))
    siblings = select(UserCompany.company_id).where(
        UserCompany.user_id.in_(owners), UserCompany.company_id != company_id)
    rows = (await session.scalars(select(CompanyPeerAccount).where(
        CompanyPeerAccount.company_id.in_(siblings), CompanyPeerAccount.how == "chosen",
    ).order_by(CompanyPeerAccount.created_at))).all()
    return {(r.other_code, r.other_name.strip().casefold()): r.code for r in rows}


async def review(session: AsyncSession, company_id: uuid.UUID) -> list[dict]:
    """Each client whose accounts wait for the firm's review, with the accounts to review."""
    chart = chart_access()
    company = await session.get(Company, company_id)
    if chart is None or company is None:
        return []
    reviewed = set((company.settings or {}).get(REVIEWED_KEY) or [])
    books = books_currency(company.settings or {})
    local = {a["code"]: a for a in await chart.list_accounts(session, company_id)}
    previous = None
    waiting: dict[uuid.UUID, dict[str, dict]] = {}
    for item in await peer_attention.open_items(session, [company_id]):
        if item.kind == peer_attention.REFUSED and (item.fix or {}).get("kind") == "review" \
                and str(item.source_company_id) not in reviewed:
            for other in item.fix.get("accounts") or []:
                waiting.setdefault(item.source_company_id, {})[other["id"]] = other
    out = []
    for source, others in waiting.items():
        done = {row.other_account_id for row in await links(session, company_id, source)}
        rows = []
        for other in sorted(others.values(), key=lambda a: a["code"]):
            if other["id"] in done:
                continue
            if previous is None:
                previous = await _previous(session, company_id)
            same = local.get(other["code"])
            if same is None:
                why = "Only in the client's chart"
            elif same["account_type"] != other["account_type"]:
                why = "Another type here"
            elif _currency(same, books) != _currency(other, books):
                why = "Another currency here"
            else:
                why = f"Named {same['name']} here"
            before = previous.get((other["code"], other["name"].strip().casefold()))
            usable = before is not None and before in local and local[before]["is_active"]
            suggested = before if usable else (
                other["code"] if same is not None and use_problem(other, same, other["code"], books) is None else None)
            rows.append({"id": other["id"], "code": other["code"], "name": other["name"],
                         "account_type": other["account_type"], "why": why,
                         "suggested": suggested, "previous": usable,
                         "add_code": next_free(other["code"], set(local)) if same else other["code"]})
        if rows:
            out.append({"source_company_id": str(source),
                        "client": await _client_label(session, company_id, source), "accounts": rows})
    return out


async def answer_review(session: AsyncSession, company_id: uuid.UUID, source_company_id: uuid.UUID,
                        answers: list[dict], finish: bool) -> None:
    """Link or add each reviewed account; ``finish`` ends the review, after which the
    client's accounts are placed as any connected company's are, and each record that
    waited for it says it is fixed until the next sync places it."""
    await locked_company(session, company_id)
    pending = {row["id"]: row for c in await review(session, company_id)
               if c["source_company_id"] == str(source_company_id) for row in c["accounts"]}
    waited = [item for item in await peer_attention.open_items(session, [company_id])
              if item.source_company_id == source_company_id and (item.fix or {}).get("kind") == "review"]
    others = {a["id"]: a for item in waited for a in item.fix.get("accounts") or []}
    for answer in answers:
        other = others.get(answer.get("account"))
        if other is None or answer.get("account") not in pending:
            raise HTTPException(422, "That account is not waiting for review.")
        if answer.get("use"):
            await use_account(session, company_id, source_company_id, other, answer["use"])
        elif answer.get("add"):
            await add_account(session, company_id, source_company_id, other, answer["add"], None)
        else:
            raise HTTPException(422, f"Choose an account for {label(other)}, or a code to add it as.")
    if finish:
        company = await locked_company(session, company_id)
        settings = dict(company.settings or {})
        settings[REVIEWED_KEY] = sorted(set(settings.get(REVIEWED_KEY) or []) | {str(source_company_id)})
        company.settings = settings
        for item in waited:
            item.fix, item.reason = {**item.fix, "fixed": True}, "Accounts reviewed. It posts on the next sync."
