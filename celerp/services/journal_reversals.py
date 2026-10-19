# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A journal entry's effect is cancelled at most once.

An entry is cancelled by voiding it or by a reversal (an entry whose ``reverses`` names
it) that still stands: posted, and not itself cancelled. Undoing a reversal reverses the
reversal, so the chain original, reversal, undo, ... alternates. Every void and every
reversal passes through ``require_cancelled_once`` (events.engine.emit_event), which
judges the whole chain as it would stand after the write, under the company lock, so
two writers racing on one chain are judged one after the other.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.projections import Projection
from ui.i18n import t


async def _states(session: AsyncSession, company_id, ids) -> dict[str, dict]:
    rows = (await session.execute(select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "journal_entry",
        Projection.entity_id.in_(list(ids))).execution_options(populate_existing=True))).all()
    return {str(eid): dict(state or {}) for eid, state in rows}


async def _chain(session: AsyncSession, company_id, je_id: str) -> dict[str, dict]:
    """Every entry in ``je_id``'s chain: what it reverses up to the first original, and
    every reversal below each of those."""
    nodes: dict[str, dict] = {}
    current = je_id
    while current and current not in nodes:
        found = await _states(session, company_id, [current])
        if current not in found:
            break
        nodes[current] = found[current]
        current = found[current].get("reverses")
    frontier = set(nodes)
    while frontier:
        rows = (await session.execute(select(Projection.entity_id, Projection.state).where(
            Projection.company_id == company_id, Projection.entity_type == "journal_entry",
            Projection.state["reverses"].as_string().in_(list(frontier)),
        ).execution_options(populate_existing=True))).all()
        fresh = {str(eid): dict(state or {}) for eid, state in rows if str(eid) not in nodes}
        nodes.update(fresh)
        frontier = set(fresh)
    return nodes


async def standing_reversals(session: AsyncSession, company_id, je_id: str) -> list[tuple[str, dict]]:
    """The reversals of ``je_id`` that still stand, with their states."""
    nodes = await _chain(session, company_id, je_id)
    return [(eid, nodes[eid]) for eid in _standing_reversals(nodes, je_id)]


def _standing_reversals(nodes: dict[str, dict], je_id: str) -> list[str]:
    """The reversals of ``je_id`` that still stand."""
    return [eid for eid, state in nodes.items()
            if state.get("reverses") == je_id and state.get("status") == "posted"
            and not _standing_reversals(nodes, eid)]


def _named(state: dict, fallback: str) -> dict:
    return {"memo": state.get("memo") or fallback, "date": str(state.get("ts") or "")[:10]}


async def require_cancelled_once(session: AsyncSession, company_id, entity_id: str, *,
                                 void: bool = False, reverses: str | None = None) -> None:
    """Refuse a void of ``entity_id`` (``void``) or a new reversal ``entity_id`` of
    ``reverses`` that would leave any entry in the chain cancelled twice: void with a
    reversal still standing, or reversed by two that stand. A write the chain already
    holds (a retry) is judged as it stands."""
    from celerp.services.company_lock import lock_company

    if void:
        current = (await _states(session, company_id, [entity_id])).get(entity_id)
        if current is None or current.get("je_type") != "manual":
            return  # only a manual entry is ever reversed
    await lock_company(session, company_id)
    nodes = await _chain(session, company_id, reverses or entity_id)
    if void:
        if entity_id not in nodes:
            return
        nodes[entity_id] = {**nodes[entity_id], "status": "void"}
    elif entity_id not in nodes:
        nodes[entity_id] = {"status": "posted", "reverses": reverses}
    for eid, state in nodes.items():
        standing = _standing_reversals(nodes, eid)
        if len(standing) + (state.get("status") == "void") <= 1:
            continue
        if void and eid == entity_id and standing:
            first = nodes[standing[0]]
            raise HTTPException(status_code=422, detail=t(
                "acct.err_je_already_reversed", **_named(first, standing[0])))
        raise HTTPException(status_code=422, detail=t("acct.err_je_cancelled_twice", **_named(state, eid)))
