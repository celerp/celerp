# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Item rate review: offer, never apply, the tax code a localization update gives items by HS code.

A pack's ``item_rate_table`` maps HS code prefixes to a tax code from a date (longest prefix wins).
When an applied update adds a row whose code differs from the one it supersedes, ``propose`` lists
the items whose ``hs_code`` now falls under that row and whose ``tax_codes`` are not that code.
Nothing changes an item until ``apply`` writes the checked ones, each as one ``item.updated`` whose
``fields_changed`` is ``{"tax_codes": {old, new}}``; ``undo`` writes the reverse for every item
not edited since. Items are read from their projection rows and written through the core event, so
this module never imports the inventory module.
"""
from __future__ import annotations

import datetime as dt
import re

from fastapi import HTTPException
from sqlalchemy import func, select

from celerp.accounting_roles import catalog_refusal
from celerp.events.engine import emit_event, find_event_by_idempotency
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.tax_regimes import get_regime

SOURCE = "rate_review"
# A proposal's marker: the last ledger entry it saw. An item with a later entry was edited since.
_MARK = re.compile(r"^r([0-9]{1,18})$")


def rate_row(table: list[dict], hs_code: str, day: str) -> dict | None:
    """The ``item_rate_table`` row in force for ``hs_code`` on ``day`` (ISO): the longest prefix
    the code starts with, its latest row from on or before ``day``; None when no row matches."""
    digits = "".join(ch for ch in hs_code or "" if ch.isdigit())
    rows = [r for r in table if digits.startswith(r["hs_prefix"]) and r["valid_from"] <= day]
    return max(rows, key=lambda r: (len(r["hs_prefix"]), r["valid_from"]), default=None)


def moved(before: list[dict], after: list[dict]) -> list[dict]:
    """Rows of ``after`` that ``before`` lacks and whose code differs from the row of ``before``
    in force for that prefix on the day the new row starts."""
    return [row for row in after if row not in before
            and (rate_row(before, row["hs_prefix"], row["valid_from"]) or {}).get("code_id") != row["code_id"]]


def _tables(operation: LedgerEntry) -> tuple[list[dict], list[dict], str | None]:
    was, left = operation.data["before"]["localization"] or {}, operation.data["after"]["localization"] or {}
    return was.get("item_rate_table") or [], left.get("item_rate_table") or [], left.get("country")


async def _review_target(session, company_id, operation_id: str) -> LedgerEntry:
    from celerp.localization.apply import operation
    target = await operation(session, company_id, operation_id)
    if target is None or target.data.get("undoes") is not None:
        raise HTTPException(status_code=404, detail=catalog_refusal("localization.no_operation"))
    return target


async def _candidates(session, company_id, target: LedgerEntry) -> dict[str, dict]:
    """``{item_id: proposal row}`` for every item the operation's moved rows reach today."""
    before, after, country = _tables(target)
    changed = moved(before, after)
    if not changed:
        return {}
    names = {code.get("id"): code["name"] for code in get_regime(country)["taxes"]}
    today = dt.date.today().isoformat()
    items = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "item",
    ).order_by(Projection.entity_id))).scalars().all()
    out = {}
    for item in items:
        state = item.state or {}
        row = rate_row(after, str(state.get("hs_code") or ""), today)
        new = [row["code_id"]] if row in changed else None
        if new and list(state.get("tax_codes") or []) != new:
            out[item.entity_id] = {"item_id": item.entity_id, "sku": state.get("sku"), "name": state.get("name"),
                                   "hs_code": state.get("hs_code"), "old": list(state.get("tax_codes") or []),
                                   "new": new, "code_name": names.get(row["code_id"], row["code_id"]),
                                   "valid_from": row["valid_from"]}
    return out


async def _last_entry(session, company_id) -> int:
    return (await session.execute(select(func.coalesce(func.max(LedgerEntry.id), 0)).where(
        LedgerEntry.company_id == company_id))).scalar_one()


async def _edited_since(session, company_id, item_ids, mark: int) -> set[str]:
    if not item_ids:
        return set()
    return set((await session.execute(select(LedgerEntry.entity_id).where(
        LedgerEntry.company_id == company_id, LedgerEntry.entity_id.in_(list(item_ids)),
        LedgerEntry.id > mark))).scalars().all())


async def propose(session, company_id, operation_id: str) -> dict:
    """``{"items": [proposal rows], "fingerprint"}``; reads only."""
    target = await _review_target(session, company_id, operation_id)
    items = await _candidates(session, company_id, target)
    return {"items": list(items.values()), "fingerprint": f"r{await _last_entry(session, company_id)}"}


async def _write(session, company_id, user_id, item_id: str, change: tuple[list, list], key: str,
                 operation_id: str, undoes: int | None) -> None:
    """One ``item.updated`` moving ``tax_codes`` from ``change[0]`` to ``change[1]``."""
    await emit_event(session, company_id=company_id, entity_id=item_id, entity_type="item",
                     event_type="item.updated", actor_id=user_id, location_id=None, source=SOURCE,
                     idempotency_key=key, metadata_={"operation_id": operation_id, "undoes": undoes},
                     data={"fields_changed": {"tax_codes": {"old": change[0], "new": change[1]}}})


async def apply(session, company_id, user_id, operation_id: str, item_ids: list[str], binding: str) -> list[str]:
    """Write the proposed code onto each checked item; returns the ids skipped because they were
    edited since the proposal. 422 on an id the proposal does not hold or a malformed binding."""
    target = await _review_target(session, company_id, operation_id)
    if (mark := _MARK.match(binding or "")) is None:
        raise HTTPException(status_code=422, detail=catalog_refusal("localization.rate_review_stale"))
    mark_id = int(mark.group(1))
    items = await _candidates(session, company_id, target)
    keys = {i: f"{SOURCE}:apply:{operation_id}:{mark_id}:{i}" for i in item_ids}
    done = {i for i in item_ids if await find_event_by_idempotency(session, company_id, keys[i]) is not None}
    edited = await _edited_since(session, company_id, set(item_ids) - done, mark_id)
    unknown = [i for i in item_ids if i not in items and i not in edited and i not in done]
    if unknown:
        raise HTTPException(status_code=422, detail=catalog_refusal("localization.rate_review_unknown_item"))
    for item_id in item_ids:
        if item_id in done or item_id in edited:
            continue
        row = items[item_id]
        await _write(session, company_id, user_id, item_id, (row["old"], row["new"]), keys[item_id], operation_id, None)
    return sorted(edited, key=item_ids.index)


async def undo(session, company_id, user_id, operation_id: str) -> list[str]:
    """Put back the codes the review of ``operation_id`` wrote on every item not edited since;
    returns the ids kept because they were."""
    await _review_target(session, company_id, operation_id)
    entries = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == company_id, LedgerEntry.event_type == "item.updated",
        LedgerEntry.source == SOURCE).order_by(LedgerEntry.id))).scalars().all()
    last: dict[str, LedgerEntry] = {}
    for entry in entries:
        if (entry.metadata_ or {}).get("operation_id") == operation_id:
            last[entry.entity_id] = entry
    written = {i: e for i, e in last.items() if (e.metadata_ or {}).get("undoes") is None}
    kept = []
    for item_id, entry in written.items():
        if await _edited_since(session, company_id, {item_id}, entry.id):
            kept.append(item_id)
            continue
        change = entry.data["fields_changed"]["tax_codes"]
        await _write(session, company_id, user_id, item_id, (change["new"], change["old"]),
                     f"{SOURCE}:undo:{entry.id}", operation_id, entry.id)
    return kept
