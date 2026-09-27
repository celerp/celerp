# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Physical-code (barcode / RFID EPC) availability - one source of truth.

A barcode and an RFID / EPC are drawn from one physical-code namespace per company:
a value held in either slot of one item is not free for either slot of another.
The invariant is enforced where a write introduces a code, at the event boundary
(``emit_event``), by comparing the item's code set before and after the event. Only
codes the event newly introduces are checked, so a database that already holds a
duplicate (older data, imports, connectors) keeps working: unrelated edits to either
holder succeed, and moving one holder to a fresh code resolves the ambiguity.
Existing duplicates are reported by Doctor, never rewritten.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.inventory_codes import (
    PHYSICAL_CODE_RESOLVE_EXCLUDED_STATUSES,
    BarcodeConflictError,
    RfidEpcConflictError,
)
from celerp.models.company import Company
from celerp.models.projections import Projection

PHYSICAL_CODE_FIELDS = ("barcode", "rfid_epc")


async def lock_item_code_namespace(session: AsyncSession, company_id) -> None:
    """Serialize physical-code allocation and availability checks for a company.

    Two concurrent writers each read the same max sequence and mint the same next
    code, or both pass the availability check for the same value. Taking a row lock
    on the company makes the second writer wait for the first to commit, so it reads
    the committed state instead of colliding. The lock is held until the caller's
    transaction commits or rolls back.

    The mode is FOR NO KEY UPDATE, not FOR UPDATE. Every ledger insert takes an
    implicit foreign-key KEY SHARE lock on its company row and holds it to commit,
    so a plain FOR UPDATE here would have to upgrade past that share lock: two
    transactions that have each already emitted an event for the company both hold
    KEY SHARE and then block on each other's row lock, which PostgreSQL breaks by
    aborting one with a deadlock (40P01). FOR NO KEY UPDATE does not conflict with
    KEY SHARE, so the upgrade never happens, while it still conflicts with another
    FOR NO KEY UPDATE, keeping physical-code writers serialized for every module.

    Canonical lock order: when a transaction also needs item Projection row locks,
    take this namespace lock first, then lock the Projection rows. Never acquire this
    lock after SELECT ... FOR UPDATE on an item Projection.
    """
    await session.execute(
        select(Company.id).where(Company.id == company_id).with_for_update(key_share=True)
    )


async def code_in_use(
    session: AsyncSession, company_id, code, *, exclude_entity_id=None
) -> bool:
    """True when ``code`` already occupies EITHER physical-code slot of another item.

    One query over BOTH ``state ->> 'barcode'`` and ``state ->> 'rfid_epc'`` is the
    cross-field collision check. Callers hold ``lock_item_code_namespace`` so the
    read-then-write is serialized. ``exclude_entity_id`` skips one item's own row so
    re-asserting an item's current value is not read as a self-collision. Merged
    items still hold their codes here: a historical code is never re-issued.
    """
    if not code:
        return False
    value = str(code)
    query = select(Projection.entity_id).where(
        Projection.company_id == company_id,
        Projection.entity_type == "item",
        or_(
            Projection.state["barcode"].as_string() == value,
            Projection.state["rfid_epc"].as_string() == value,
        ),
    )
    if exclude_entity_id is not None:
        query = query.where(Projection.entity_id != exclude_entity_id)
    return (await session.execute(query)).first() is not None


def _codes(state: dict | None) -> dict[str, str]:
    """Map each physical code an item state resolves by to the first field holding it.

    An item in a status excluded from resolution (merged) resolves by nothing, so
    returning it to a live status introduces its codes again.
    """
    held: dict[str, str] = {}
    if str((state or {}).get("status") or "").lower() in PHYSICAL_CODE_RESOLVE_EXCLUDED_STATUSES:
        return held
    for field in PHYSICAL_CODE_FIELDS:
        value = (state or {}).get(field)
        if value not in (None, ""):
            held.setdefault(str(value), field)
    return held


async def assert_new_physical_codes_available(
    session: AsyncSession, company_id, entity_id, before: dict | None, after: dict | None
) -> None:
    """Reject a write that introduces a physical code another item already holds.

    Compares resolvable code SETS: a code present both before and after the write is
    not new, even if it moved between the barcode and RFID / EPC slots, so an item that
    already shares a code with another item can still be edited. A merged item holds
    no resolvable codes, so reactivating it checks its codes like new ones. Caller
    holds ``lock_item_code_namespace``.
    """
    previous = _codes(before)
    for code, field in _codes(after).items():
        if code in previous:
            continue
        if await code_in_use(session, company_id, code, exclude_entity_id=entity_id):
            if field == "rfid_epc":
                raise RfidEpcConflictError(code)
            raise BarcodeConflictError(code)


def physical_code_conflicts(items: Iterable[tuple[str, dict]]) -> list[dict]:
    """Group ``(entity_id, state)`` pairs into codes held by more than one resolvable item.

    Pure and read-only. Barcode and RFID / EPC share one namespace; an item holding the
    same value in both fields counts once. Merged items are excluded, matching the
    resolver. Returns ``[{"code", "items": [{"entity_id", "fields"}]}]`` sorted by code.
    """
    holders: dict[str, dict[str, list[str]]] = {}
    for entity_id, state in items:
        state = state or {}
        if str(state.get("status") or "").lower() in PHYSICAL_CODE_RESOLVE_EXCLUDED_STATUSES:
            continue
        for field in PHYSICAL_CODE_FIELDS:
            value = state.get(field)
            if value in (None, ""):
                continue
            holders.setdefault(str(value), {}).setdefault(entity_id, []).append(field)
    return [
        {
            "code": code,
            "items": [
                {"entity_id": entity_id, "fields": fields}
                for entity_id, fields in sorted(by_entity.items())
            ],
        }
        for code, by_entity in sorted(holders.items())
        if len(by_entity) > 1
    ]
