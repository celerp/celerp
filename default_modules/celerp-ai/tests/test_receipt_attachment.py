"""Confirmed AI receipt proposals retain the source file on the bill."""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

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
async def test_attach_receipt_is_canonical_company_scoped_and_replay_safe(monkeypatch):
    from celerp.ai import files
    from celerp.events import engine
    from celerp.services import attachments
    company_id, user_id, message_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    source = "ai_up_" + "a" * 32
    record = {"id": "prop_123", "file_id": source}
    result = {"data": {"id": "doc:bill-1"}, "ok": True}
    calls = []
    already = False

    def load(file_id, company, actor):
        assert (file_id, company, actor) == (source, company_id, user_id)
        calls.append("load")
        return b"receipt-image", {"filename": "receipt.png", "content_type": "image/png"}

    async def replay(session, company, key):
        assert company == company_id and key.startswith("ai:receipt:")
        return SimpleNamespace(event_type="doc.file_attached", entity_id="doc:bill-1") if already else None

    class Session:
        async def get(self, model, pk):
            assert pk == (company_id, "doc:bill-1")
            return SimpleNamespace(entity_type="doc", state={"doc_type": "bill"})

    class Files:
        async def file(self, content, filename, mime, *, att_id):
            assert (content, filename, mime) == (b"receipt-image", "receipt.png", "image/png")
            assert uuid.UUID(att_id)
            calls.append(("store", att_id))
            return {"id": att_id, "filename": filename, "mime": mime, "size": len(content)}

    @asynccontextmanager
    async def storage(session, company):
        assert company == company_id
        yield Files()

    async def attach(session, company, entity_type, entity_id, meta, actor, **kwargs):
        assert (company, entity_type, entity_id, actor) == (company_id, "doc", "doc:bill-1", user_id)
        assert kwargs["document_tag"] == "receipt"
        assert kwargs["idempotency_key"].startswith("ai:receipt:")
        calls.append(("attach", meta["id"]))

    monkeypatch.setattr(files, "load_file", load)
    monkeypatch.setattr(engine, "find_event_by_idempotency", replay)
    monkeypatch.setattr(attachments, "storing", storage)
    monkeypatch.setattr(attachments, "attach_file", attach)
    args = dict(session=Session(), company_id=company_id, user_id=user_id,
                message_id=message_id, record=record, result=result)
    await routes._attach_confirmed_bill_receipt(**args)
    assert len(calls) == 3 and calls[0] == "load"
    assert calls[1][1] == calls[2][1]
    already = True
    await routes._attach_confirmed_bill_receipt(**args)
    assert len(calls) == 3
