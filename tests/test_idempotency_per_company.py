# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Regression: ledger idempotency is per-company, not global.

Two companies with overlapping doc numbers (per-company numbering resets) emit
auto-JEs with identical idempotency keys. The unique constraint must be scoped
to (company_id, idempotency_key), and emit_event must dedup per-company without
rolling back the caller's whole transaction on a collision.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from celerp.events.engine import emit_event
from celerp.models.company import Company


async def _seed_company(session, name: str) -> uuid.UUID:
    cid = uuid.uuid4()
    session.add(Company(id=cid, name=name, slug=f"{name.lower()}-{cid.hex[:8]}", settings={}))
    await session.flush()
    return cid


async def _emit_finalize_like(session, company_id: uuid.UUID, ref: str):
    """Mimic finalize: a doc.finalized event followed by an auto-JE whose
    idempotency key is derived from the (per-company) doc ref."""
    doc_id = f"doc:{ref}"
    fin = await emit_event(
        session, company_id=company_id, entity_id=doc_id, entity_type="doc",
        event_type="doc.finalized", data={"ref_id": ref},
        actor_id=None, location_id=None, source="test",
        idempotency_key=str(uuid.uuid4()), metadata_={},
    )
    je = await emit_event(
        session, company_id=company_id, entity_id=f"je:auto:{doc_id}:fin",
        entity_type="journal_entry", event_type="acc.journal_entry.created",
        data={"memo": "auto", "entries": []},
        actor_id=None, location_id=None, source="auto_je",
        idempotency_key=f"je:{doc_id}:invoice.finalized:c",  # NOT company-scoped
        metadata_={},
    )
    return fin, je


@pytest.mark.asyncio
async def test_two_companies_same_doc_number_both_finalize(session):
    """Both companies must finalize doc 'PF-2606-0008' even though the JE key collides."""
    ref = "PF-2606-0008"
    company_a = await _seed_company(session, "CoA")
    company_b = await _seed_company(session, "CoB")

    fin_a, je_a = await _emit_finalize_like(session, company_a, ref)
    await session.commit()

    # Company B reuses the same doc number → same JE idempotency key.
    fin_b, je_b = await _emit_finalize_like(session, company_b, ref)
    await session.commit()

    # Both companies' doc.finalized events persisted (B's was NOT rolled back).
    for cid in (company_a, company_b):
        cnt = await session.scalar(text(
            "SELECT count(*) FROM ledger WHERE company_id = :c AND event_type = 'doc.finalized'"
        ), {"c": cid})
        assert cnt == 1, f"company {cid} missing its doc.finalized event (count={cnt})"

    # The two JEs are distinct rows (different companies, same key).
    assert je_a.id != je_b.id
    assert je_a.company_id == company_a
    assert je_b.company_id == company_b


@pytest.mark.asyncio
async def test_duplicate_key_same_company_dedups_without_nuking_txn(session):
    """A genuine duplicate within one company dedups to the existing row AND does
    not roll back a sibling event emitted earlier in the same transaction."""
    ref = "PF-2606-0009"
    cid = await _seed_company(session, "CoDup")

    # First finalize commits the JE key.
    await _emit_finalize_like(session, cid, ref)
    await session.commit()

    # A second transaction: emit a fresh doc.finalized, then re-emit the SAME JE
    # key (duplicate). The dup must dedup to the existing JE without discarding
    # the new doc.finalized.
    new_fin = await emit_event(
        session, company_id=cid, entity_id=f"doc:{ref}", entity_type="doc",
        event_type="doc.finalized", data={"ref_id": ref},
        actor_id=None, location_id=None, source="test",
        idempotency_key=str(uuid.uuid4()), metadata_={},
    )
    dup_je = await emit_event(
        session, company_id=cid, entity_id=f"je:auto:doc:{ref}:fin",
        entity_type="journal_entry", event_type="acc.journal_entry.created",
        data={"memo": "auto", "entries": []},
        actor_id=None, location_id=None, source="auto_je",
        idempotency_key=f"je:doc:{ref}:invoice.finalized:c",  # duplicate
        metadata_={},
    )
    await session.commit()

    # The sibling doc.updated survived the dedup.
    got = await session.scalar(text("SELECT id FROM ledger WHERE id = :i"), {"i": new_fin.id})
    assert got == new_fin.id, "sibling event was rolled back by the dedup"
    # The dup returned the pre-existing JE.
    assert dup_je.company_id == cid