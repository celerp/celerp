"""Confirmed AI receipt proposals retain the source file on the bill."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from celerp_ai import routes


@pytest.mark.asyncio
async def test_bill_proposal_does_not_leave_transient_upload_id_in_notes():
    class Lookups:
        async def contact_id(self, vendor, warnings):
            return "contact:vendor"
        async def item_id(self, description):
            return None
    entry = {"file_id": "ai_up_" + "a" * 32, "filename": "invoice.png"}
    extraction = {"vendor_name": "Vendor", "document_kind": "receipt",
                  "line_items": [{"description": "Parts", "quantity": 2, "unit_price": 10}],
                  "total": 20, "reference": "REF123"}
    records, summary = await routes._bill_proposals(
        Lookups(), entry, extraction, datetime.now(timezone.utc))
    (proposal,) = [r for r in records if r["name"] == routes._DOCS_CREATE]
    body = proposal["arguments"]["body"]
    assert proposal["file_id"] == entry["file_id"]
    assert body["notes"] == "Vendor reference: REF123"
    assert entry["file_id"] not in str(body)
    assert "invoice.png" in summary


@pytest.mark.asyncio
async def test_attach_receipt_is_canonical_company_scoped_and_replay_safe(monkeypatch, session, auth):
    """The receipt lands on the bill as an ordinary file attachment, written under the
    company lock (so the request's access is judged again first), once per proposal."""
    from sqlalchemy import func, select
    import mutation_observer
    from celerp.ai import files
    from celerp.models.ledger import LedgerEntry
    from celerp.models.projections import Projection
    from celerp.services.permissions import authorize_request
    company_id, user_id, message_id = auth["company_id"], auth["user_id"], uuid.uuid4()
    source = "ai_up_" + "a" * 32
    record = {"id": "prop_123", "file_id": source}
    result = {"data": {"id": "doc:bill-1"}, "ok": True}

    def load(file_id, company, actor):
        assert (file_id, company, actor) == (source, company_id, user_id)
        return b"receipt-image", {"filename": "receipt.png", "content_type": "image/png"}

    monkeypatch.setattr(files, "load_file", load)
    session.add(Projection(company_id=company_id, entity_id="doc:bill-1", entity_type="doc",
                           state={"doc_type": "bill", "attachments": []}, version=1,
                           updated_at=datetime.now(timezone.utc)))
    await session.commit()
    args = dict(session=session, company_id=company_id, user_id=user_id,
                message_id=message_id, record=record, result=result)
    with mutation_observer.serving("POST", "/ai/conversations/{conversation_id}/confirm-all"):
        authorize_request(session, company_id, user_id, "admin")
        await routes._attach_confirmed_bill_receipt(**args)
        await routes._attach_confirmed_bill_receipt(**args)
    assert [f.kind for f in mutation_observer.CENSUS.findings] == []
    attached = await session.scalar(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == company_id, LedgerEntry.event_type == "doc.file_attached",
        LedgerEntry.entity_id == "doc:bill-1"))
    assert attached == 1
