# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Funds: the funds setting, fund maintenance and movements between funds.

A fund is an entity (``fund:<id>``) projected from fund.created, fund.updated,
fund.archived and fund.unarchived. Journal lines name it in ``fund_id``; lines without
one are the unrestricted group. What a posting may do with funds is judged at the
journal boundary (journal_accounts.check_fund_rules). Every change here that reads fund
balances holds the fund ledger exclusively (lock_fund_ledger), so no posting commits
between the check and the change. Money moves between funds only through
post_fund_movement, the one caller of the movement grant.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import AccountRole, catalog_refusal
from celerp.db import get_session
from celerp.events.engine import emit_event
from celerp.models.company import Company
from celerp.models.projections import Projection
from celerp.services.account_roles import role_map, set_role
from celerp.services.auth import get_current_company_id, get_current_user
from celerp.services.company_lock import lock_fund_ledger, locked_company
from celerp.services.company_settings import record_change
from celerp.schemas.numbers import PositiveFloat
from celerp.services.je_keys import je_idempotency_key
from celerp.services.journal_accounts import (
    FUND_MOVEMENT_TYPES, FUNDS_ENABLED, fund_account_balances, fund_movement_grant, funds_by_id, funds_enabled,
    lock_accounts,
)
from celerp.services.money import round_money, to_decimal, to_stored_float
from celerp.services.peer_lines import (
    CONNECTION_DEFAULTS, checked, default_active, default_key, outside_waiting, records_carrying,
)
from celerp.services.permissions import read_authority, require_permission

router = APIRouter(dependencies=[Depends(get_current_user)])

# The account Celerp adds for restricted net assets when funds are first turned on and
# the role has none, under the equity header of the seeded chart.
RESTRICTED_NET_ASSETS_ACCOUNT = ("3500", "Restricted Net Assets", "3000")


class FundsSettingIn(BaseModel):
    enabled: bool


class FundIn(BaseModel):
    request_key: str
    name: str
    restricted: bool = False
    purpose: str | None = None


class FundPatch(BaseModel):
    name: str | None = None
    restricted: bool | None = None
    purpose: str | None = None


def _view(fund_id: str, state: dict) -> dict:
    return {"id": fund_id, "name": state["name"], "restricted": bool(state.get("restricted")),
            "purpose": state.get("purpose"), "archived": bool(state.get("archived"))}


async def fund_balances(session: AsyncSession, company_id) -> dict[str, dict[str, Decimal]]:
    """Each fund's non-zero posted balance (debit less credit) by account."""
    return {fund: held for fund, held in (await fund_account_balances(session, company_id)).items() if fund}


async def _refuse_held(session: AsyncSession, company_id, fund_ids=None) -> None:
    """Refuse while any of ``fund_ids`` (every fund when None) holds a balance, naming
    each fund and the accounts it holds them on."""
    held = await fund_balances(session, company_id)
    held = {f: accounts for f, accounts in held.items() if fund_ids is None or f in fund_ids}
    if held:
        funds = await funds_by_id(session, company_id, held)
        named = "; ".join(f"{funds.get(f, {}).get('name', f)}: {', '.join(sorted(accounts))}"
                          for f, accounts in sorted(held.items(), key=lambda kv: funds.get(kv[0], {}).get("name", kv[0])))
        raise HTTPException(status_code=422, detail=catalog_refusal("fund.balance_held", funds=named))


async def _has_postings(session: AsyncSession, company_id, fund_id: str) -> bool:
    rows = await session.execute(select(Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "journal_entry"))
    return any(isinstance(line, dict) and line.get("fund_id") == fund_id
               for (state,) in rows for line in (state or {}).get("entries") or ())


def _name(raw: str | None) -> str:
    name = (raw or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail=catalog_refusal("fund.name_required"))
    return name


async def _refuse_taken(session: AsyncSession, company_id, name: str, *, other_than: str | None = None) -> None:
    for fund_id, state in (await funds_by_id(session, company_id)).items():
        if fund_id != other_than and str(state.get("name", "")).casefold() == name.casefold():
            raise HTTPException(status_code=422, detail=catalog_refusal("fund.name_taken", name=name))


def _purpose(restricted: bool, raw: str | None) -> str | None:
    purpose = (raw or "").strip() or None
    if restricted and purpose is None:
        raise HTTPException(status_code=422, detail=catalog_refusal("fund.purpose_required"))
    return purpose


async def _emit(session: AsyncSession, company_id, fund_id: str, event_type: str, data: dict, user, key: str):
    return await emit_event(session, company_id=company_id, entity_id=fund_id, entity_type="fund",
                            event_type=event_type, data=data, actor_id=user.id, location_id=None,
                            source="api", idempotency_key=key, metadata_={})


async def _fund(session: AsyncSession, company_id, fund_id: str) -> dict:
    state = (await funds_by_id(session, company_id, {fund_id})).get(fund_id)
    if state is None:
        raise HTTPException(status_code=404, detail=catalog_refusal("fund.not_found"))
    return state


async def _seed_restricted_net_assets(session: AsyncSession, company_id, settings: dict, user) -> None:
    """Point the restricted net assets role at an account the first time funds go on:
    the seeded account when the chart has it, else one Celerp adds."""
    role = AccountRole.RESTRICTED_NET_ASSETS.value
    if role_map(settings).get(role):
        return
    code, name, parent = RESTRICTED_NET_ASSETS_ACCOUNT
    if code not in (await lock_accounts(session, company_id, {code}) or {}):
        from celerp_accounting.import_service import add_posting_account
        await add_posting_account(session, company_id, code=code, name=name, account_type="equity", parent_code=parent)
    await set_role(session, company_id, role, code, user.id)


@router.get("/funds")
async def list_funds(
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
    _: None = require_permission("view_financial_reports"),
) -> dict:
    funds = await funds_by_id(session, company_id)
    return {"items": sorted((_view(f, s) for f, s in funds.items()), key=lambda v: v["name"].casefold())}


@router.get("/funds/waiting")
async def list_waiting_for_funds(
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
    _: None = require_permission("manage_accounting"),
) -> dict:
    """The outside records waiting for a fund choice (peer_lines.outside_waiting): the count
    the dashboard shows, the held items Sync accounts posts and the documents to choose for."""
    return await outside_waiting(session, company_id)


@router.put("/funds/setting")
async def set_funds_setting(
    payload: FundsSettingIn,
    company_id: uuid.UUID = Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_accounting"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Turn funds on or off. On is refused while an unposted record carries fund fields
    sent from outside; off while any fund holds a balance on any account, or an unposted
    record holds a fund choice or a line's fund."""
    await lock_fund_ledger(session, company_id, shared=False)
    company = await locked_company(session, company_id)
    if funds_enabled(company.settings or {}) != payload.enabled:
        if payload.enabled:
            # Every fund key an unposted record carries at turn-on came from outside: funds
            # off keeps them as sent, and turning off waits for local choices to clear.
            if carried := await records_carrying(session, company_id, ("fund_choice", "fund_id")):
                raise HTTPException(status_code=422, detail=catalog_refusal(
                    "fund.turn_on_outside_keys", count=len(carried), records=", ".join(carried)))
            await _seed_restricted_net_assets(session, company_id, company.settings or {}, user)
            company = await locked_company(session, company_id)
        else:
            await _refuse_held(session, company_id)
            if chosen := await records_carrying(session, company_id, ("fund_choice", "fund_id")):
                raise HTTPException(status_code=422, detail=catalog_refusal(
                    "fund.turn_off_choices_held", records=", ".join(chosen)))
        settings = {**(company.settings or {}), FUNDS_ENABLED: payload.enabled}
        record_change(settings, FUNDS_ENABLED, user.id)
        company.settings = settings
        await session.commit()
    return {"enabled": payload.enabled}


class ConnectionDefaultIn(BaseModel):
    origin: str
    source_key: str
    value: str | None
    expected_version: int
    request_key: str


# The accounting apps whose documents arrive with origin accounting_system.
_ACCOUNTING_SYSTEMS = {"quickbooks": "QuickBooks", "xero": "Xero"}


async def _connections(session: AsyncSession, company_id) -> dict[tuple[str, str], str]:
    """Every connection records from outside arrive through here, each with its label: a
    connected company sending documents and a client sending journals (a live key this
    company issued), the accountant whose journals reached these books (the client keeps no
    key of its own for it) and QuickBooks or Xero."""
    from celerp.models.company_peer import CompanyPeerAttention, CompanyPeerGrant
    from celerp.models.connector_config import ConnectorConfig

    found: dict[tuple[str, str], str] = {}
    now = datetime.now(timezone.utc)
    for grant in (await session.scalars(select(CompanyPeerGrant).where(
            CompanyPeerGrant.company_id == company_id, CompanyPeerGrant.revoked_at.is_(None),
            CompanyPeerGrant.expires_at > now))).all():
        for origin, scope in (("connected_company", "documents"), ("client", "journals")):
            if scope in (grant.scopes or []):
                found.setdefault((origin, str(grant.source_company_id)), grant.label)
    for firm, name in (await session.execute(select(
            Projection.state["peer_origin"]["firm_company"].as_string(),
            Projection.state["peer_origin"]["firm"].as_string()).where(
            Projection.company_id == company_id, Projection.entity_type == "journal_entry",
            Projection.state["peer_origin"].as_string().is_not(None)).distinct())).all():
        found.setdefault(("accountant", firm), name or firm)
    for firm, fix in (await session.execute(select(CompanyPeerAttention.source_company_id, CompanyPeerAttention.fix).where(
            CompanyPeerAttention.company_id == company_id, CompanyPeerAttention.entity_type == "journals"))).all():
        found.setdefault(("accountant", str(firm)), (fix or {}).get("firm") or str(firm))
    for platform in (await session.scalars(select(ConnectorConfig.connector).where(
            ConnectorConfig.company_id == str(company_id),
            ConnectorConfig.connector.in_(list(_ACCOUNTING_SYSTEMS))))).all():
        found[("accounting_system", platform)] = _ACCOUNTING_SYSTEMS[platform]
    return found


async def _default_items(session: AsyncSession, company_id, settings: dict) -> list[dict]:
    stored = settings.get(CONNECTION_DEFAULTS) or {}
    funds = await funds_by_id(session, company_id)
    items = []
    for (origin, source_key), label in sorted((await _connections(session, company_id)).items()):
        entry = stored.get(default_key(origin, source_key)) or {}
        active = False
        if entry.get("fund_choice"):
            role, current = await read_authority(session, company_id, uuid.UUID(entry["set_by"]))
            active = default_active(entry, current, role, funds)
        items.append({"origin": origin, "source_key": source_key, "label": label,
                      "fund_choice": entry.get("fund_choice"), "set_by": entry.get("set_by"),
                      "set_at": entry.get("set_at"), "version": entry.get("version", 0), "active": active})
    return items


@router.get("/funds/connection-defaults")
async def list_connection_defaults(
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The standing fund of every connection records arrive through, read-only for a user
    without the fund permission; ``active`` is false while nothing is set or its setter can
    no longer choose funds."""
    company = await session.get(Company, company_id)
    return {"items": await _default_items(session, company_id, dict(company.settings or {}))}


@router.put("/funds/connection-defaults")
async def set_connection_default(
    payload: ConnectionDefaultIn,
    company_id: uuid.UUID = Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_accounting"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Set, change or clear (``value`` null) the fund records from one connection take at
    their first arrival. Refused from a stale copy (``expected_version``); a repeat of
    ``request_key`` answers with what it wrote."""
    await lock_fund_ledger(session, company_id, shared=False)
    company = await locked_company(session, company_id)
    settings = dict(company.settings or {})
    key = default_key(payload.origin, payload.source_key)
    stored = dict(settings.get(CONNECTION_DEFAULTS) or {})
    entry = stored.get(key) or {}
    if entry.get("request_key") != payload.request_key:
        if not funds_enabled(settings):
            raise HTTPException(status_code=422, detail=catalog_refusal("fund.not_enabled"))
        if (payload.origin, payload.source_key) not in await _connections(session, company_id):
            raise HTTPException(status_code=422, detail=catalog_refusal("fund.connection_unknown"))
        if payload.expected_version != entry.get("version", 0):
            raise HTTPException(status_code=409, detail={**catalog_refusal("fund.default_stale"),
                                                         "code": "stale_version"})
        if payload.value is not None:
            checked(await funds_by_id(session, company_id), payload.value, "posting.fund.document_choice_missing")
        entry = {"fund_choice": payload.value, "set_by": str(user.id),
                 "set_at": datetime.now(timezone.utc).isoformat(), "version": entry.get("version", 0) + 1,
                 "request_key": payload.request_key}
        company.settings = {**settings, CONNECTION_DEFAULTS: {**stored, key: entry}}
        await session.commit()
    return {k: entry.get(k) for k in ("fund_choice", "set_by", "set_at", "version")}


@router.post("/funds")
async def create_fund(
    payload: FundIn,
    company_id: uuid.UUID = Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_accounting"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Create a fund. Repeat-safe on ``request_key``: a repeat answers with the fund it made."""
    await lock_fund_ledger(session, company_id, shared=False)
    fund_id = f"fund:{uuid.uuid5(uuid.NAMESPACE_URL, f'{company_id}:{payload.request_key}')}"
    existing = (await funds_by_id(session, company_id, {fund_id})).get(fund_id)
    if existing is not None:
        return _view(fund_id, existing)
    company = await locked_company(session, company_id)
    if not funds_enabled(company.settings or {}):
        raise HTTPException(status_code=422, detail=catalog_refusal("fund.not_enabled"))
    name = _name(payload.name)
    await _refuse_taken(session, company_id, name)
    data = {"name": name, "restricted": payload.restricted, "purpose": _purpose(payload.restricted, payload.purpose)}
    await _emit(session, company_id, fund_id, "fund.created", data, user, f"fund:{fund_id}:created")
    await session.commit()
    return _view(fund_id, {**data, "archived": False})


@router.patch("/funds/{fund_id}")
async def update_fund(
    fund_id: str,
    payload: FundPatch,
    company_id: uuid.UUID = Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_accounting"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Rename a fund, or change what it is restricted to before anything is posted to it."""
    await lock_fund_ledger(session, company_id, shared=False)
    state = await _fund(session, company_id, fund_id)
    restricted = state.get("restricted") if payload.restricted is None else payload.restricted
    purpose = _purpose(bool(restricted), state.get("purpose") if payload.purpose is None else payload.purpose)
    changes: dict = {}
    if payload.name is not None and (name := _name(payload.name)) != state["name"]:
        await _refuse_taken(session, company_id, name, other_than=fund_id)
        changes["name"] = name
    if (restricted, purpose) != (state.get("restricted"), state.get("purpose")):
        if await _has_postings(session, company_id, fund_id):
            raise HTTPException(status_code=422, detail=catalog_refusal("fund.restriction_locked", fund=state["name"]))
        changes.update(restricted=restricted, purpose=purpose)
    if changes:
        await _emit(session, company_id, fund_id, "fund.updated", changes, user, f"fund:{fund_id}:{uuid.uuid4()}")
        await session.commit()
        state = {**state, **changes}
    return _view(fund_id, state)


async def _set_archived(session: AsyncSession, company_id, fund_id: str, archived: bool, user) -> dict:
    await lock_fund_ledger(session, company_id, shared=False)
    state = await _fund(session, company_id, fund_id)
    if bool(state.get("archived")) != archived:
        if archived:
            await _refuse_held(session, company_id, {fund_id})
        await _emit(session, company_id, fund_id, "fund.archived" if archived else "fund.unarchived", {}, user,
                    f"fund:{fund_id}:{uuid.uuid4()}")
        await session.commit()
    return _view(fund_id, {**state, "archived": archived})


@router.post("/funds/{fund_id}/archive")
async def archive_fund(
    fund_id: str,
    company_id: uuid.UUID = Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_accounting"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Archive a fund that holds nothing; it then takes no new posting."""
    return await _set_archived(session, company_id, fund_id, True, user)


@router.post("/funds/{fund_id}/unarchive")
async def unarchive_fund(
    fund_id: str,
    company_id: uuid.UUID = Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_accounting"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await _set_archived(session, company_id, fund_id, False, user)


class _Movement(BaseModel):
    request_key: str
    date: date
    reason: str = Field(min_length=1)


class TransferIn(_Movement):
    from_fund: str | None
    to_fund: str | None
    amount: PositiveFloat
    account: str | None = None


class BackingIn(BaseModel):
    je_id: str
    line_index: int
    amount: PositiveFloat


class ReleaseIn(_Movement):
    fund: str
    to_fund: str | None
    backing: list[BackingIn] = Field(min_length=1)
    account: str | None = None


class ReallocateIn(TransferIn):
    donor_authorization: str


class AssignmentIn(BaseModel):
    account: str
    fund: str | None
    amount: PositiveFloat


class OpeningIn(_Movement):
    assignments: list[AssignmentIn] = Field(min_length=1)


class ReverseIn(BaseModel):
    date: date
    reason: str = Field(min_length=1)


def _distinct(giver: str | None, receiver: str | None) -> None:
    if giver == receiver:
        raise HTTPException(status_code=422, detail=catalog_refusal("fund.movement_same_fund"))


async def _leg_lines(session: AsyncSession, company_id, settings: dict, legs) -> list[dict]:
    """The journal lines of moving each leg's ``amount`` of ``account`` from the fund
    ``giver`` to ``receiver`` (None: the unrestricted group): the account changes hands
    and each fund's net assets (restricted net assets for a restricted fund, retained
    earnings otherwise) take the other side."""
    roles = role_map(settings)
    currency = str(settings.get("currency") or "USD")
    funds = await funds_by_id(session, company_id)
    accounts = await lock_accounts(session, company_id, {account for _, _, account, _ in legs}) or {}

    def line(fund_id, account, amount):
        return {"account": account, "debit": to_stored_float(max(amount, Decimal(0))),
                "credit": to_stored_float(max(-amount, Decimal(0))), **({"fund_id": fund_id} if fund_id else {})}

    def net_assets(fund_id):
        restricted = (funds.get(fund_id or "") or {}).get("restricted")
        return roles.get(AccountRole.RESTRICTED_NET_ASSETS if restricted else AccountRole.RETAINED_EARNINGS)

    lines = []
    for giver, receiver, account, amount in legs:
        amount = round_money(to_decimal(amount), currency)
        if (accounts.get(account) or {}).get("account_type") in ("liability", "equity"):
            amount = -amount
        lines += [line(giver, net_assets(giver), amount), line(giver, account, -amount),
                  line(receiver, account, amount), line(receiver, net_assets(receiver), -amount)]
    return lines


async def post_fund_movement(session: AsyncSession, company_id, *, ts: date, reason: str, actor,
                             request_key: str | None = None, je_id: str | None = None, kind: str | None = None,
                             legs=(), entries: list[dict] | None = None, reverses: str | None = None,
                             details: dict | None = None) -> dict:
    """Post one movement between funds: ``kind`` built from ``legs`` (giver, receiver,
    account, amount) or from the prebuilt ``entries``, or the reversal of the movement
    ``reverses`` (of type ``kind`` when given, else any fund movement type). It holds the
    fund ledger exclusively and opens the movement grant for this entry and ``actor``
    only; the journal boundary judges the rest (journal_accounts.check_fund_rules). The
    entry is ``je_id``, else minted from ``request_key``; a repeat answers with the entry
    it posted. The caller commits, so the movement can share its transaction."""
    await lock_fund_ledger(session, company_id, shared=False)
    je_id = je_id or f"je:fund:{uuid.uuid5(uuid.NAMESPACE_URL, f'{company_id}:{request_key}')}"
    if await session.get(Projection, (company_id, je_id)) is not None:
        return {"je_id": je_id}
    company = await locked_company(session, company_id)
    if reverses is not None:
        original = await session.get(Projection, (company_id, reverses))
        state = original.state if original is not None and original.entity_type == "journal_entry" else {}
        original_kind = state.get("je_type") or kind  # a close posted before closes carried a type
        if original_kind not in ({kind} if kind else FUND_MOVEMENT_TYPES) or state.get("status") != "posted":
            raise HTTPException(status_code=404, detail=catalog_refusal("fund.movement_not_found"))
        kind = original_kind
        entries = [{"account": e["account"], "debit": e.get("credit") or 0, "credit": e.get("debit") or 0,
                    **({"fund_id": e["fund_id"]} if e.get("fund_id") else {})} for e in state.get("entries") or ()]
    elif entries is None:
        entries = await _leg_lines(session, company_id, company.settings or {}, legs)
    data = {"memo": reason, "ts": ts.isoformat(), "entries": entries, "je_type": kind, "status": "posted",
            "reason": reason, **({"reverses": reverses} if reverses else {}), **(details or {})}
    async with fund_movement_grant(company_id, je_id, actor):
        await emit_event(session, company_id=company_id, entity_id=je_id, entity_type="journal_entry",
                         event_type="acc.journal_entry.created", data=data, actor_id=actor, location_id=None,
                         source="api", idempotency_key=je_idempotency_key(je_id, kind, "c"), metadata_={})
    return {"je_id": je_id}


async def _posted(session: AsyncSession, movement) -> dict:
    """Commit a fund route's movement and answer with it."""
    posted = await movement
    await session.commit()
    return posted


async def _account(session: AsyncSession, company_id, chosen: str | None) -> str | None:
    """The account a movement moves: the one chosen, else the default deposit account."""
    if chosen:
        return chosen
    company = await locked_company(session, company_id)
    return role_map(company.settings or {}).get(AccountRole.DEFAULT_DEPOSIT)


@router.post("/funds/transfer")
async def transfer_between_funds(
    payload: TransferIn,
    company_id: uuid.UUID = Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_accounting"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Move money of an account between two unrestricted funds."""
    _distinct(payload.from_fund, payload.to_fund)
    account = await _account(session, company_id, payload.account)
    return await _posted(session, post_fund_movement(
        session, company_id, request_key=payload.request_key, ts=payload.date, reason=payload.reason, actor=user.id,
        kind="fund_transfer", legs=[(payload.from_fund, payload.to_fund, account, payload.amount)]))


@router.post("/funds/release")
async def release_from_restriction(
    payload: ReleaseIn,
    company_id: uuid.UUID = Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_accounting"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Release restricted money to an unrestricted fund for the purpose spending it paid for."""
    _distinct(payload.fund, payload.to_fund)
    account = await _account(session, company_id, payload.account)
    amount = sum((to_decimal(b.amount) for b in payload.backing), Decimal(0))
    return await _posted(session, post_fund_movement(
        session, company_id, request_key=payload.request_key, ts=payload.date, reason=payload.reason, actor=user.id,
        kind="fund_release", legs=[(payload.fund, payload.to_fund, account, amount)],
        details={"backing": [b.model_dump() for b in payload.backing]}))


@router.post("/funds/reallocate")
async def reallocate_restricted_funds(
    payload: ReallocateIn,
    company_id: uuid.UUID = Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_accounting"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Move restricted money to another restricted fund of the same purpose, with the donor's authorization."""
    _distinct(payload.from_fund, payload.to_fund)
    account = await _account(session, company_id, payload.account)
    return await _posted(session, post_fund_movement(
        session, company_id, request_key=payload.request_key, ts=payload.date, reason=payload.reason, actor=user.id,
        kind="fund_reallocation", legs=[(payload.from_fund, payload.to_fund, account, payload.amount)],
        details={"donor_authorization": payload.donor_authorization}))


@router.post("/funds/opening")
async def opening_balances_by_fund(
    payload: OpeningIn,
    company_id: uuid.UUID = Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_accounting"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Assign balances the unrestricted group holds to funds, as the books' opening by fund."""
    for assignment in payload.assignments:
        _distinct(None, assignment.fund)
    return await _posted(session, post_fund_movement(
        session, company_id, request_key=payload.request_key, ts=payload.date, reason=payload.reason, actor=user.id,
        kind="fund_opening", legs=[(None, a.fund, a.account, a.amount) for a in payload.assignments]))


@router.post("/funds/movements/{je_id}/reverse")
async def reverse_fund_movement(
    je_id: str,
    payload: ReverseIn,
    company_id: uuid.UUID = Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_accounting"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Undo a movement between funds by its reversal. Repeat-safe: reversing it again
    answers with the same reversal."""
    return await _posted(session, post_fund_movement(
        session, company_id, request_key=f"reverse:{je_id}", ts=payload.date, reason=payload.reason, actor=user.id,
        reverses=je_id))
