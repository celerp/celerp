# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Void sweeps cover every live auto-JE a doc can carry, backfill JEs included."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.events.engine import emit_event
from celerp.models.company import Company
from celerp.models.projections import Projection
from celerp.services.auto_je import (
    create_for_doc_unvoided,
    void_for_doc_finalized,
    void_for_doc_voided,
)


async def _seed_company(session) -> uuid.UUID:
    company_id = uuid.uuid4()
    session.add(Company(id=company_id, name="SweepCo", slug=f"sw-{company_id.hex[:8]}"))
    await session.flush()
    return company_id


def _fin_entries() -> list[dict]:
    return [
        {"account": "1120", "debit": 110.0, "credit": 0.0},
        {"account": "4100", "debit": 0.0, "credit": 100.0},
        {"account": "2120", "debit": 0.0, "credit": 10.0},
    ]


def _cogs_entries() -> list[dict]:
    return [
        {"account": "5100", "debit": 20.0, "credit": 0.0},
        {"account": "1130-P", "debit": 0.0, "credit": 20.0},
    ]


async def _emit_je(session, company_id, je_id: str, *, entries, ts=None,
                   void=False) -> None:
    data: dict = {"memo": "Auto JE", "entries": entries}
    if ts:
        data["ts"] = ts
    await emit_event(
        session, company_id=company_id, entity_id=je_id,
        entity_type="journal_entry", event_type="acc.journal_entry.created",
        data=data, actor_id=None, location_id=None, source="test",
        idempotency_key=str(uuid.uuid4()), metadata_={})
    if void:
        await emit_event(
            session, company_id=company_id, entity_id=je_id,
            entity_type="journal_entry", event_type="acc.journal_entry.voided",
            data={"reason": "test", "ts": ts}, actor_id=None, location_id=None,
            source="test", idempotency_key=str(uuid.uuid4()), metadata_={})


async def _je_states(session, company_id, doc_id: str) -> dict[str, dict]:
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id,
        Projection.entity_type == "journal_entry",
    ))).scalars().all()
    prefix = f"je:auto:{doc_id}:"
    return {r.entity_id: r.state for r in rows if r.entity_id.startswith(prefix)}


@pytest.mark.asyncio
async def test_void_after_backfill_voids_backfill_je(session):
    """Voiding a backfilled invoice voids the backfill JE alongside the finalize
    JE, and a later unvoid restores exactly one COGS leg."""
    company_id = await _seed_company(session)
    doc_id = "doc:INV-V1"
    await _emit_je(session, company_id, f"je:auto:{doc_id}:fin",
                   entries=_fin_entries(), ts="2024-06-01")
    await _emit_je(session, company_id, f"je:auto:{doc_id}:cogs-backfill",
                   entries=_cogs_entries(), ts="2024-06-01")
    await session.commit()

    await void_for_doc_voided(
        session, company_id=company_id, user_id=None, doc_id=doc_id)
    await session.commit()

    jes = await _je_states(session, company_id, doc_id)
    assert jes[f"je:auto:{doc_id}:fin"]["status"] == "void"
    assert jes[f"je:auto:{doc_id}:cogs-backfill"]["status"] == "void", (
        "void sweep left the backfill JE posted")

    await create_for_doc_unvoided(
        session, company_id=company_id, user_id=None, doc_id=doc_id)
    await session.commit()

    jes = await _je_states(session, company_id, doc_id)
    posted_5100 = [
        float(e.get("debit") or 0)
        for st in jes.values() if st.get("status") == "posted"
        for e in st.get("entries", [])
        if e.get("account") == "5100" and float(e.get("debit") or 0) > 0]
    assert posted_5100 == [20.0], (
        f"expected exactly one posted 5100 debit after unvoid, got {posted_5100}")


@pytest.mark.asyncio
async def test_revert_after_backfill_voids_both_jes(session):
    """Reverting a backfilled invoice to draft voids the finalize JE and the
    backfill JE; the sweep must not stop at the first hit."""
    company_id = await _seed_company(session)
    doc_id = "doc:INV-V2"
    await _emit_je(session, company_id, f"je:auto:{doc_id}:fin",
                   entries=_fin_entries(), ts="2024-06-01")
    await _emit_je(session, company_id, f"je:auto:{doc_id}:cogs-backfill",
                   entries=_cogs_entries(), ts="2024-06-01")
    await session.commit()

    await void_for_doc_finalized(
        session, company_id=company_id, user_id=None, doc_id=doc_id)
    await session.commit()

    jes = await _je_states(session, company_id, doc_id)
    assert jes[f"je:auto:{doc_id}:fin"]["status"] == "void"
    assert jes[f"je:auto:{doc_id}:cogs-backfill"]["status"] == "void", (
        "revert sweep stopped after the finalize JE")


@pytest.mark.asyncio
async def test_revert_after_unvoid_voids_the_restored_je(session):
    """Reverting a doc whose live finalize JE is the fin:unvoid restore leaves
    no posted finalize-family JE behind."""
    company_id = await _seed_company(session)
    doc_id = "doc:INV-V3"
    await _emit_je(session, company_id, f"je:auto:{doc_id}:fin",
                   entries=_fin_entries(), ts="2024-06-01", void=True)
    await _emit_je(session, company_id, f"je:auto:{doc_id}:fin:unvoid",
                   entries=_fin_entries(), ts="2024-06-02")
    await session.commit()

    await void_for_doc_finalized(
        session, company_id=company_id, user_id=None, doc_id=doc_id)
    await session.commit()

    jes = await _je_states(session, company_id, doc_id)
    posted = [eid for eid, st in jes.items() if st.get("status") == "posted"]
    assert posted == [], (
        f"revert left posted finalize-family JEs behind: {posted}")
