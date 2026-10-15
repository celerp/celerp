"""Peer file copies reuse the company attachment writer and retry each file independently."""
from __future__ import annotations

import hashlib
import io
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile

from celerp.services import peer_attachments as inbound
from celerp.services import peer_attachment_sync as outbound


def _file(raw=b"PDF", *, name="receipt.pdf"):
    return UploadFile(
        file=io.BytesIO(raw), filename=name,
        headers=Headers({"content-type": "application/pdf"}),
    )


@pytest.mark.asyncio
async def test_file_receiver_replay_and_changed_identity_are_fail_closed(monkeypatch):
    from celerp.services import attachments

    cid, source = uuid.uuid4(), uuid.uuid4()
    grant = SimpleNamespace(company_id=cid, source_company_id=source)
    target_doc = "doc:destination"
    written = {}
    stored = []
    class Files:
        async def file(self, raw, filename, mime, *, att_id):
            stored.append((raw, filename, mime, att_id))
            return {"id": att_id, "filename": filename, "mime": mime,
                    "size": len(raw), "url": "/file/1"}
    @asynccontextmanager
    async def storage(session, company):
        assert company == cid
        yield Files()
    async def mapping(*args, **kwargs):
        return target_doc
    async def lookup(session, company, key):
        assert company == cid
        return written.get(key)
    async def record(session, company, entity_type, entity_id, meta, actor_id,
                     *, source, idempotency_key, metadata_, **kwargs):
        assert (company, entity_type, entity_id, source) == (cid, "doc", target_doc, "connector")
        assert meta["id"] == stored[-1][-1]
        entry = SimpleNamespace(event_type="doc.file_attached", entity_id=target_doc,
                                metadata_=metadata_)
        written[idempotency_key] = entry
        return entry
    class Session:
        async def get(self, model, identity, **kwargs):
            return SimpleNamespace(entity_type="doc")
    monkeypatch.setattr(inbound, "storing", storage)
    monkeypatch.setattr(inbound, "peer_identity", mapping)
    monkeypatch.setattr(inbound, "find_event_by_idempotency", lookup)
    monkeypatch.setattr(attachments, "attach_file", record)

    fields = dict(grant=grant, source_doc_id="doc:source", source_file_id="file:a",
                  sha256_hex=hashlib.sha256(b"PDF").hexdigest())
    assert await inbound.receive_attachment(Session(), file=_file(), **fields) == "created"
    assert await inbound.receive_attachment(Session(), file=_file(), **fields) == "skipped"
    assert len(stored) == 1
    with pytest.raises(HTTPException) as changed:
        await inbound.receive_attachment(Session(), file=_file(name="other.pdf"), **fields)
    assert changed.value.status_code == 409
    with pytest.raises(HTTPException) as damaged:
        await inbound.receive_attachment(Session(), file=_file(b"DIFFERENT"), **fields)
    assert damaged.value.status_code == 409


@pytest.mark.asyncio
async def test_attachment_sender_sends_only_company_file_with_proof(monkeypatch):
    from celerp.connectors.base import ConnectorContext
    from celerp.connectors import sync_runner
    from celerp.connectors import celerp as peer

    cid = uuid.uuid4()
    sends = []
    async def watermark(*args):
        assert args[-1] == "attachments_out"
        return None
    async def page(company_id, after, since, entity):
        assert company_id == cid and entity == "doc"
        if after:
            return []
        return [("doc:source", 2, {
            "files": [{"id": "f1", "url": "/attachments/source/1.pdf",
                       "filename": "receipt.pdf", "mime": "application/pdf"}],
        })]
    async def read(company, url, max_bytes):
        assert company == cid and url.endswith("1.pdf")
        return b"PDF"
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["public_only"] and kwargs["timeout"] >= 30
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def post(self, url, **kwargs):
            assert url == "https://firm.example/company-peers/attachments"
            sends.append(kwargs)
            return SimpleNamespace(status_code=200, json=lambda: {"status": "created"})
    monkeypatch.setattr(sync_runner, "_last_success_watermark", watermark)
    monkeypatch.setattr(peer, "_read_projection_page", page)
    monkeypatch.setattr(outbound, "read_company_file", read)
    from celerp.connectors import http
    monkeypatch.setattr(http, "RateLimitedClient", Client)
    ctx = ConnectorContext(company_id=str(cid), store_handle="https://firm.example",
                           access_token="cp1.test")
    result = await outbound.sync_document_attachments(peer.CelerpConnector(), ctx)
    assert result.ok and result.created == 1
    assert sends[0]["data"]["sha256_hex"] == hashlib.sha256(b"PDF").hexdigest()
    assert sends[0]["files"]["file"] == ("receipt.pdf", b"PDF", "application/pdf")
    assert sends[0]["headers"]["Authorization"] == "Peer cp1.test"
