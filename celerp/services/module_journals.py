# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""How Celerp's own modules post their journals.

A bundled module registers the kinds of journal it posts (operation types) while
modules start, each with the permission keys a user needs to post it. It then posts
and reverses through this interface, which checks, for every journal:

- the caller is code of the module it names (third-party modules cannot post here);
- the operation type is registered, and the user holds its keys in the company;
- the company uses the module and accounting, except for a correction a linked
  document makes while the module is off;
- every line to a new destination passes the chart's destination rule, and a line
  moving what the module recognized earlier stays within the module's own posted
  balance on that account;
- the journal points at a record of the module's own in this company;
- the same key with the same request returns the journal already posted, and the same
  key asking for anything else is refused.

Journals are ``je:mod:<module>:<key>``. Only the module that posted one reverses it.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select

from celerp.accounting_roles import refusal
from celerp.models.projections import Projection
from celerp.services.app_paths import is_app_local_path
from celerp.services.company_lock import lock_projections
from celerp.services.je_keys import je_void_data
from celerp.services.journal_accounts import UnbalancedJournalEntry, lock_accounts, require_destinations
from celerp.services.permissions import is_permission_key, locked_authority

_ACCOUNTING = "celerp-accounting"
_KEY = re.compile(r"[A-Za-z0-9:._-]+")


@dataclass(frozen=True)
class JournalLine:
    """One line. A line that ``moves_recognized`` takes back what was put on
    ``account`` before (the old side of a reclass): by the module's own journals, or,
    with ``recognized_in``, by that posted journal (a bill line the module took over) or,
    as ``ledger_key(day)``, by the ledger to that day (a balance brought in at a cutover).
    Any other line is a new destination and must be an account the chart accepts for
    new postings."""
    account: str
    debit: Decimal = Decimal(0)
    credit: Decimal = Decimal(0)
    role: str | None = None
    moves_recognized: bool = False
    recognized_in: str | None = None


@dataclass(frozen=True)
class JournalSource:
    """The module record a journal belongs to, the in-app page showing it, and the
    label the journal keeps for it (read even while the module is off). ``tags`` are
    named markers a report reads back, e.g. the prior years a correction restates."""
    entity_id: str
    href: str
    label: str
    tags: dict | None = None

    def fields(self) -> dict:
        """The source's fields; an untagged source carries no tags key."""
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass(frozen=True)
class PostedJournal:
    je_id: str
    existing: bool


@dataclass(frozen=True)
class ReversedJournal:
    je_id: str
    existing: bool


@dataclass(frozen=True)
class OperationType:
    keys: tuple[str, ...]
    source_prefix: str
    document_correction: bool = False


@dataclass
class _Registry:
    types: dict[tuple[str, str], OperationType] = field(default_factory=dict)
    frozen: bool = False


_registry = _Registry()


def _caller_module(depth: int) -> str | None:
    """The bundled module whose code called the public function ``depth`` frames up."""
    from celerp.modules.loader import first_party_owner

    return first_party_owner(Path(sys._getframe(depth + 1).f_code.co_filename))


def register_operation_types(module: str, types: dict[str, tuple[str, ...]], *,
                             source_prefix: str, document_corrections=()) -> None:
    """Register the journals ``module`` posts: operation type to the permission keys a
    user needs for it. ``source_prefix`` is the start of every entity id the module's
    journals point at; ``document_corrections`` names the types a linked document posts
    while the module is off. Only the module's own code registers, only while modules
    start, and a type registered twice must say the same thing both times."""
    if _caller_module(1) != module:
        raise ValueError("Only Celerp's own modules register journal operation types, each as itself.")
    if _registry.frozen:
        raise ValueError("Journal operation types are registered while modules start, not after modules are ready.")
    if not source_prefix:
        raise ValueError("Journal operation types need the prefix of the records they point at.")
    unknown = set(document_corrections) - set(types)
    if unknown:
        raise ValueError(f"Document corrections name unregistered types: {sorted(unknown)}")
    staged = {}
    for name, keys in types.items():
        keys = tuple(keys)
        if not keys or not all(is_permission_key(k) for k in keys):
            raise ValueError(f"Operation type {name!r} must name one or more permission keys.")
        entry = OperationType(keys, source_prefix, name in document_corrections)
        if _registry.types.get((module, name), entry) != entry:
            raise ValueError(f"Operation type {name!r} is already registered differently.")
        staged[(module, name)] = entry
    _registry.types.update(staged)


def open_operation_types() -> None:
    """Open registration: called as modules start, each time the process starts them. A
    type registered by an earlier start stays, and registering it again must agree."""
    _registry.frozen = False


def freeze_operation_types() -> None:
    """Close registration: called once every module has started."""
    _registry.frozen = True


def _refuse(status: int, key: str, message: str, **params):
    return HTTPException(status_code=status, detail=refusal(key, message, **params))


def _je_id(module: str, key: str) -> str:
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        raise _refuse(422, "module_journal.key", "A journal key may hold only letters, digits and : . _ -")
    return f"je:mod:{module}:{key}"


def _check_caller(module: str) -> None:
    if _caller_module(2) != module:
        raise _refuse(403, "module_journal.caller",
                      "Only the module's own code posts or reverses its journals.", module=module)


def _operation(module: str, operation: str) -> OperationType:
    found = _registry.types.get((module, operation))
    if found is None:
        raise _refuse(422, "module_journal.unknown_operation",
                      f"{operation} is not a journal this module posts.", operation=operation)
    return found


async def _authorize(session, company_id, user_id, module: str, op: OperationType) -> None:
    from celerp.modules.registry import uses_module

    _, settings = await locked_authority(session, company_id, user_id, op.keys)
    needed = [_ACCOUNTING] if op.document_correction else [module, _ACCOUNTING]
    off = [m for m in needed if not uses_module(settings, m)]
    if off:
        raise _refuse(409, "module_journal.module_off",
                      "This company does not use the module that posts this journal.", module=off[0])


async def _check_source(session, company_id, op: OperationType, source: JournalSource) -> None:
    ok = (source.entity_id.startswith(op.source_prefix) and is_app_local_path(source.href)
          and await session.get(Projection, {"company_id": company_id, "entity_id": source.entity_id}) is not None)
    if not ok:
        raise _refuse(422, "module_journal.source",
                      "A module journal points at one of the module's own records in this company.")


def _amount(value) -> Decimal:
    return Decimal(str(value or 0))


LEDGER_PREFIX = "ledger:"


def ledger_key(day: str) -> str:
    """The ``recognized_in`` of a line taking back what the ledger held on its account
    on ``day``, whoever posted it there (an opening balance, a bill)."""
    return f"{LEDGER_PREFIX}{day}"


async def recognized_balances(session, company_id, module: str,
                              keys: set[tuple[str, str | None]]) -> dict[tuple[str, str | None], Decimal]:
    """Debit minus credit recognized on each (account, recognizing journal) key: with no
    journal, what the module's posted journals put there themselves; with one, what that
    posted journal holds there plus what the module's journals already moved from it; with
    ``ledger_key(day)``, what every other posted journal dated on or before ``day`` holds
    there, less what the module's journals moved from the ledger to that day or from
    those journals."""
    named = {je for _, je in keys if je and not je.startswith(LEDGER_PREFIX)}
    days = {je.removeprefix(LEDGER_PREFIX) for _, je in keys if je and je.startswith(LEDGER_PREFIX)}
    query = select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id,
        Projection.entity_type == "journal_entry",
        Projection.state["status"].as_string() == "posted")
    if not days:
        query = query.where(Projection.entity_id.startswith(f"je:mod:{module}:", autoescape=True) | Projection.entity_id.in_(named))
    rows = (await session.execute(query)).all()
    dated = {eid: str(state.get("ts") or "")[:10] for eid, state in rows}
    balances = dict.fromkeys(keys, Decimal(0))

    def add(key, entry):
        if key in balances:
            balances[key] += _amount(entry.get("debit")) - _amount(entry.get("credit"))

    def ledger_day(own: bool, entity_id: str, source: str | None) -> str:
        """The date an entry stands in the ledger from, for the ledger keys ("" for none)."""
        if not own:
            return dated[entity_id]
        if source and source.startswith(LEDGER_PREFIX):
            return source.removeprefix(LEDGER_PREFIX)
        return dated.get(source, "") if source else ""

    for entity_id, state in rows:
        own = entity_id.startswith(f"je:mod:{module}:")
        for entry in state.get("entries") or []:
            account, source = entry.get("account"), entry.get("recognized_in")
            if own:
                add((account, source), entry)
            elif entity_id in named:
                add((account, entity_id), entry)
            since = ledger_day(own, entity_id, source)
            for day in days:
                if since and since <= day and not (own and source == ledger_key(day)):
                    add((account, ledger_key(day)), entry)
    return balances


async def _check_accounts(session, company_id, module: str, lines: list[JournalLine]) -> None:
    new = {ln.account for ln in lines if not ln.moves_recognized}
    moving = [ln for ln in lines if ln.moves_recognized]
    if await lock_accounts(session, company_id, set()) is None:
        raise _refuse(409, "module_journal.accounting_off",
                      "Accounting is not running, so no journal can be posted.")
    await require_destinations(session, company_id, new)
    if not moving:
        return
    codes = {ln.account for ln in moving}
    accounts = await lock_accounts(session, company_id, codes)
    keys = {(ln.account, ln.recognized_in) for ln in moving}
    balances = await recognized_balances(session, company_id, module, keys)
    for code, je in sorted(keys, key=lambda k: (k[0], k[1] or "")):
        if code not in accounts:
            raise _refuse(422, "posting.destination.not_in_chart",
                          f"Account {code} is not in the chart of accounts.", code=code)
        net = sum((_amount(ln.debit) - _amount(ln.credit) for ln in moving
                   if (ln.account, ln.recognized_in) == (code, je)), Decimal(0))
        held = balances[(code, je)]
        if (net < 0 and held + net < 0) or (net > 0 and held + net > 0):
            raise _refuse(422, "module_journal.moves_more_than_recognized",
                          f"This moves more off account {code} than was recorded there.", code=code)


def _intent_hash(operation: str, date: str, lines: list[JournalLine], source: JournalSource, intent: dict) -> str:
    body = {"operation": operation, "date": date, "source": source.fields(), "intent": intent,
            "lines": [{**asdict(ln), "debit": str(_amount(ln.debit)), "credit": str(_amount(ln.credit))}
                      for ln in lines]}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


async def post_journal(session, *, company_id, user_id, module: str, key: str, operation: str,
                       date: str, memo: str, lines: list[JournalLine], source: JournalSource,
                       intent: dict) -> PostedJournal:
    """Post one journal of ``module`` under ``key``, or return the one already posted
    there for the same request. A key whose journal was voided is spent: asking again is
    refused, so a caller posting afresh uses a new key."""
    _check_caller(module)
    je_id = _je_id(module, key)
    op = _operation(module, operation)
    await _authorize(session, company_id, user_id, module, op)
    await _check_source(session, company_id, op, source)
    intent_hash = _intent_hash(operation, date, lines, source, intent)
    existing = (await lock_projections(session, company_id, [je_id])).get(je_id)
    if existing is not None:
        if existing.state.get("intent_hash") != intent_hash:
            raise _refuse(409, "module_journal.key_reused",
                          "This operation was already recorded with different details.")
        if existing.state.get("status") != "posted":
            raise _refuse(409, "module_journal.key_voided",
                          "The journal for this operation was voided. Record the operation again to post a new one.")
        return PostedJournal(je_id, True)
    await _check_accounts(session, company_id, module, lines)
    entries = []
    for ln in lines:
        entry = {"account": ln.account, "debit": _amount(ln.debit), "credit": _amount(ln.credit)}
        if ln.role:
            entry["account_roles"] = [ln.role]
        if ln.recognized_in:
            entry["recognized_in"] = ln.recognized_in
        entries.append(entry)
    from celerp.services.auto_je import _emit_auto_posted_je

    try:
        await _emit_auto_posted_je(
            session, company_id=company_id, user_id=user_id, je_id=je_id,
            idem_create=f"{je_id}:c", idem_posted=f"{je_id}:p", memo=memo, entries=entries,
            metadata_={"module": module, "operation": operation}, ts=date,
            extra={"source": {"module": module, **source.fields()}, "intent_hash": intent_hash})
    except UnbalancedJournalEntry as exc:
        raise _refuse(422, "module_journal.unbalanced",
                      "The journal does not balance once rounded to the company currency.") from exc
    return PostedJournal(je_id, False)


async def reverse_journal(session, *, company_id, user_id, module: str, key: str, operation: str,
                          reason: str, reversed_on: str | None = None) -> ReversedJournal:
    """Void ``module``'s journal under ``key``; voiding it again returns it as it is."""
    _check_caller(module)
    je_id = _je_id(module, key)
    op = _operation(module, operation)
    await _authorize(session, company_id, user_id, module, op)
    row = (await lock_projections(session, company_id, [je_id])).get(je_id)
    if row is None:
        raise _refuse(404, "module_journal.not_found", "There is no such journal to reverse.")
    if row.state.get("status") == "void":
        return ReversedJournal(je_id, True)
    from celerp.events.engine import emit_event

    data = je_void_data(reason, row.state)
    if reversed_on:
        data["reversed_on"] = reversed_on
    await emit_event(
        session, company_id=company_id, entity_id=je_id, entity_type="journal_entry",
        event_type="acc.journal_entry.voided", data=data, actor_id=user_id, location_id=None,
        source="auto_je", idempotency_key=f"{je_id}:v",
        metadata_={"module": module, "operation": operation})
    return ReversedJournal(je_id, False)


async def journal_status(session, *, company_id, module: str, key: str) -> str | None:
    """The status of ``module``'s journal under ``key``, or None when it was never posted."""
    _check_caller(module)
    row = await session.get(Projection, {"company_id": company_id, "entity_id": _je_id(module, key)})
    return row.state.get("status") if row is not None else None
