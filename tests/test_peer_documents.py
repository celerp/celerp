"""Peer draft documents reuse the canonical importer and must not bypass lifecycle."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from celerp.services import peer_documents as documents


def _grant():
    return SimpleNamespace(company_id=uuid.uuid4(), source_company_id=uuid.uuid4())


class _Session:
    async def get(self, model, identifier):
        return SimpleNamespace(is_active=True, is_migration_staged=False, settings={})

    async def execute(self, query):
        return SimpleNamespace(first=lambda: SimpleNamespace(id=uuid.uuid4(), role="owner"))


@pytest.mark.asyncio
async def test_draft_document_uses_existing_importer_and_maps_dependencies(monkeypatch):
    from celerp_docs import import_service
    grant = _grant()
    target = "doc:target"
    received = []
    commits = []
    async def claim(*args, **kwargs):
        return SimpleNamespace(replay=False, target_id=target, mapping=None,
                               source_key="a", digest="b")
    async def identity(session, grant, entity_type, source_id):
        return f"{entity_type}:mapped"
    async def imported(session, company_id, user, role, settings, records, **kwargs):
        assert company_id == grant.company_id
        assert user.id and role == "owner" and kwargs == {"upsert": True, "post_ledger": True}
        received.extend(records)
        return SimpleNamespace(records=[SimpleNamespace(status="created")])
    async def commit(*args, **kwargs):
        commits.append(kwargs["source_revision"])
    monkeypatch.setattr(documents, "claim_peer_revision", claim)
    monkeypatch.setattr(documents, "_map_identity", identity)
    monkeypatch.setattr(import_service, "import_doc_records", imported)
    monkeypatch.setattr(documents, "commit_peer_revision", commit)
    payload = {
        "doc_type": "invoice", "status": "draft", "ref_id": "PF-14",
        "contact_id": "contact:source", "line_items": [
            {"name": "Widget", "item_id": "item:source", "quantity": 1, "unit_price": 15},
        ],
        "currency": "USD",
    }
    assert await documents.receive_draft_document(
        _Session(), grant=grant, source_entity_id="doc:source",
        source_revision=3, data=payload) == "created"
    assert commits == [3]
    assert received[0].entity_id == target
    assert received[0].data["contact_id"] == "contact:mapped"
    assert received[0].data["line_items"][0]["item_id"] == "item:mapped"
    assert received[0].data["status"] == "draft"
    assert received[0].source == "connector"


@pytest.mark.asyncio
async def test_issued_document_requires_lifecycle_unless_restored_revision_is_unchanged(monkeypatch):
    source = {"doc_type": "invoice", "status": "final", "ref_id": "INV-3"}
    async def claim(*args, **kwargs):
        return SimpleNamespace(replay=True)
    monkeypatch.setattr(documents, "claim_peer_revision", claim)
    assert await documents.receive_draft_document(
        _Session(), grant=_grant(), source_entity_id="doc:a",
        source_revision=4, data=source) == "skipped"
    async def changed(*args, **kwargs):
        return SimpleNamespace(replay=False)
    monkeypatch.setattr(documents, "claim_peer_revision", changed)
    with pytest.raises(HTTPException) as rejected:
        await documents.receive_draft_document(
            _Session(), grant=_grant(), source_entity_id="doc:a",
            source_revision=5, data=source)
    assert rejected.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("payments", [{"amount": 100}]),
    ("received_items", [{"quantity_received": 3}]),
    ("amount_paid", 100),
    ("created_by", "forged"),
])
async def test_document_receiver_refuses_unapproved_fields_before_claim(monkeypatch, field, value):
    async def unexpected(*args, **kwargs):
        raise AssertionError("Invalid document reached peer revision claim")
    monkeypatch.setattr(documents, "claim_peer_revision", unexpected)
    with pytest.raises(HTTPException) as rejected:
        await documents.receive_draft_document(
            _Session(), grant=_grant(), source_entity_id="doc:a",
            source_revision=1, data={"doc_type": "invoice", "status": "draft",
                                     field: value})
    assert rejected.value.status_code == 422


@pytest.mark.asyncio
async def test_missing_reference_is_not_accepted_as_success(monkeypatch):
    async def claim(*args, **kwargs):
        return SimpleNamespace(replay=False, target_id="doc:target")
    async def missing(*args, **kwargs):
        raise HTTPException(409, "Source document dependency is not synchronized")
    monkeypatch.setattr(documents, "claim_peer_revision", claim)
    monkeypatch.setattr(documents, "_map_identity", missing)
    with pytest.raises(HTTPException) as err:
        await documents.receive_draft_document(
            _Session(), grant=_grant(), source_entity_id="doc:source",
            source_revision=1,
            data={"doc_type": "invoice", "status": "draft",
                  "contact_id": "contact:missing"})
    assert err.value.status_code == 409
