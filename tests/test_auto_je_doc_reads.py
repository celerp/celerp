# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A document's automatic entries are read by their ids, not by reading every entry.

Settling, voiding and re-posting a document read its own recognition entries and void
events. Selecting every journal entry of the company and keeping the document's in Python
made each settlement of an import cost what the whole ledger holds, so importing n
settlements read n squared rows. The read names the document's id prefix (escaped, so an id
holding ``_`` or ``%`` matches only itself) and loads only the document's rows.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import event

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services import auto_je
from test_money_boundary import _company

pytestmark = pytest.mark.asyncio

_DOCS = ("doc:a_1", "doc:aX1", "doc:a%1", "doc:b")


def _entry(company_id, entity_id: str) -> Projection:
    return Projection(company_id=company_id, entity_id=entity_id, entity_type="journal_entry",
                      state={"status": "posted", "entries": []}, version=1,
                      updated_at=datetime.now(timezone.utc))


def _void(company_id, entity_id: str) -> LedgerEntry:
    return LedgerEntry(company_id=company_id, entity_id=entity_id, entity_type="journal_entry",
                       event_type="acc.journal_entry.voided", data={}, source="test",
                       idempotency_key=f"void:{entity_id}:{uuid.uuid4().hex}")


def _loaded(model):
    """The ids of the ``model`` rows the session loads while the block runs."""
    ids: list[str] = []

    def load(target, context):
        ids.append(target.entity_id)

    event.listen(model, "load", load)
    return ids, lambda: event.remove(model, "load", load)


async def _ledger(session):
    auth = await _company(session, "USD")
    cid = auth["company_id"]
    ids = [f"je:auto:{doc}:{suffix}" for doc in _DOCS for suffix in ("fin", "bill", "pay:1")]
    for rows in ([_entry(cid, i) for i in ids], [_void(cid, i) for i in ids]):  # each table in its own transaction
        session.add_all(rows)
        await session.commit()
    session.expunge_all()
    return cid


async def test_a_documents_recognition_entries_load_only_its_own_rows(session):
    cid = await _ledger(session)
    ids, stop = _loaded(Projection)
    try:
        jes = await auto_je._doc_recognition_jes(session, cid, "doc:a_1")
    finally:
        stop()
    assert set(jes) == {"fin", "bill"}
    assert ids and all(i.startswith("je:auto:doc:a_1:") for i in ids), ids


async def test_a_documents_void_events_load_only_its_own_rows(session):
    cid = await _ledger(session)
    ids, stop = _loaded(LedgerEntry)
    try:
        events = await auto_je._doc_void_events(session, cid, "doc:a%1")
    finally:
        stop()
    assert {e.entity_id for e in events} == {f"je:auto:doc:a%1:{s}" for s in ("fin", "bill", "pay:1")}
    assert ids and all(i.startswith("je:auto:doc:a%1:") for i in ids), ids
