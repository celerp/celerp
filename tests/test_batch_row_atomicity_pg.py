# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Each row of a batch import is written whole or not at all.

A batch import reports a bad row and carries on with the rest. A row whose
second write fails (an issued document whose accounting entry is refused) must
leave nothing behind: not the document, not half an entry. The other rows are
written, and the counts and errors say exactly what happened. Real PostgreSQL.
"""

from __future__ import annotations

import types
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.models.accounting import UserCompany
from celerp.models.company import Company, Location, User
from celerp_docs import routes as docs

pytestmark = pytest.mark.asyncio

_LOCKED_THROUGH = "2026-01-31"


async def _seed(factory, settings: dict | None = None) -> tuple[uuid.UUID, types.SimpleNamespace]:
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="Rows", slug=f"rows-{company_id.hex[:8]}",
                      settings={"lock_date": _LOCKED_THROUGH, **(settings or {})}))
        s.add(User(id=user_id, email=f"a-{user_id.hex[:8]}@example.test", name="Admin",
                   auth_hash="x", is_active=True))
        await s.flush()
        s.add(Location(id=uuid.uuid4(), company_id=company_id, name="Main", type="warehouse", is_default=True))
        s.add(UserCompany(user_id=user_id, company_id=company_id, role="admin", is_active=True))
        await s.commit()
    return company_id, types.SimpleNamespace(id=user_id)


def _invoice(ref: str, **extra) -> dict:
    """An issued invoice snapshot, dated after the locked period."""
    data = {"doc_type": "invoice", "ref_id": ref, "status": "sent", "issue_date": "2026-03-01",
            "line_items": [], "subtotal": 100, "tax": 0, "total": 100, **extra}
    return {"entity_id": f"doc:{ref}", "event_type": "doc.created", "data": data,
            "source": "import", "idempotency_key": f"imp:{uuid.uuid4().hex}"}


async def _ledger(engine, company_id) -> list[tuple[str, str]]:
    async with engine.connect() as conn:
        return [tuple(r) for r in (await conn.execute(text(
            "SELECT entity_id, event_type FROM ledger WHERE company_id = :c ORDER BY id"),
            {"c": company_id})).all()]


async def _entities(engine, company_id) -> list[str]:
    async with engine.connect() as conn:
        return list((await conn.execute(text(
            "SELECT entity_id FROM projections WHERE company_id = :c ORDER BY entity_id"),
            {"c": company_id})).scalars().all())


async def _batch_docs(factory, company_id, user, records, upsert: bool = False):
    async with factory() as s:
        return await docs.batch_import_docs(
            docs.DocBatchImportRequest(records=records, upsert=upsert),
            company_id=company_id, _=None, __=None, user=user, session=s)


async def test_a_document_whose_entry_is_refused_is_not_imported(committed_engine):
    """The entry of row 1 falls in the locked period (it posts on finalized_at), so the
    whole row is refused; row 2 is imported with its entry."""
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user = await _seed(factory)
    refused, good = _invoice("INV-A", finalized_at="2026-01-15"), _invoice("INV-B")

    result = await _batch_docs(factory, company_id, user, [refused, good])

    ledger = await _ledger(committed_engine, company_id)
    assert not [r for r in ledger if "INV-A" in r[0]], ledger
    assert (result.created, result.skipped, result.updated) == (1, 0, 0)
    assert len(result.errors) == 1
    assert result.errors[0].startswith("doc:INV-A: Period is locked"), result.errors
    assert ("doc:INV-B", "doc.created") in ledger
    assert ("je:auto:doc:INV-B:fin", "acc.journal_entry.posted") in ledger
    assert await _entities(committed_engine, company_id) == ["doc:INV-B", "je:auto:doc:INV-B:fin"]


async def test_a_refused_row_does_not_block_a_corrected_row_for_the_same_document(committed_engine):
    """After row 1 is rolled back, the document does not exist, so row 2 (the same
    document, corrected) creates it rather than being skipped as already imported."""
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user = await _seed(factory)
    refused = _invoice("INV-C", finalized_at="2026-01-15")
    corrected = {**_invoice("INV-C"), "idempotency_key": f"imp:{uuid.uuid4().hex}"}

    result = await _batch_docs(factory, company_id, user, [refused, corrected])

    assert (result.created, result.skipped) == (1, 0), result
    assert len(result.errors) == 1 and result.errors[0].startswith("doc:INV-C: Period is locked")
    assert await _entities(committed_engine, company_id) == ["doc:INV-C", "je:auto:doc:INV-C:fin"]


async def test_an_import_update_is_left_for_the_caller_to_commit(committed_engine):
    """The document import commits nothing itself, so its caller can keep or discard
    the whole batch. An update row used to commit in the middle of the batch."""
    from celerp_docs import import_service

    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user = await _seed(factory)
    draft = _invoice("INV-D", status="draft", customer_note="first")
    assert (await _batch_docs(factory, company_id, user, [draft])).created == 1

    edited = {**draft, "data": {**draft["data"], "customer_note": "second"}}
    async with factory() as s:
        outcome = await import_service.import_doc_records(
            s, company_id, user, "admin", {}, [docs.DocImportRecord(**edited)], upsert=True)
        assert [r.status for r in outcome.records] == ["updated"]
        await s.rollback()

    async with committed_engine.connect() as conn:
        note = (await conn.execute(text(
            "SELECT state::jsonb ->> 'customer_note' FROM projections "
            "WHERE company_id = :c AND entity_id = 'doc:INV-D'"), {"c": company_id})).scalar_one()
    assert note == "first"
