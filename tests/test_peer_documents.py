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
        from celerp.models.company import Company
        if model is Company:
            return SimpleNamespace(is_active=True, is_migration_staged=False, settings={})
        return None

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
async def test_issued_document_reuses_importer_and_preserves_revision_replay(monkeypatch):
    source = {"doc_type": "invoice", "status": "final", "ref_id": "INV-3"}
    async def claim(*args, **kwargs):
        return SimpleNamespace(replay=True)
    monkeypatch.setattr(documents, "claim_peer_revision", claim)
    assert await documents.receive_draft_document(
        _Session(), grant=_grant(), source_entity_id="doc:a",
        source_revision=4, data=source) == "skipped"
    from celerp_docs import import_service
    imports = []
    commits = []
    async def changed(*args, **kwargs):
        return SimpleNamespace(replay=False, target_id="doc:target")
    async def imported(session, company_id, user, role, settings, records, **kwargs):
        imports.append(records[0].data.copy())
        return SimpleNamespace(records=[SimpleNamespace(status="created")])
    async def commit(*args, **kwargs):
        commits.append(kwargs["source_revision"])
    monkeypatch.setattr(documents, "claim_peer_revision", changed)
    monkeypatch.setattr(import_service, "import_doc_records", imported)
    monkeypatch.setattr(documents, "commit_peer_revision", commit)
    assert await documents.receive_draft_document(
        _Session(), grant=_grant(), source_entity_id="doc:a",
        source_revision=5, data=source) == "created"
    assert imports[0]["status"] == "final" and commits == [5]
    assert "payments" not in imports[0]


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



@pytest.mark.asyncio
async def test_issued_bill_uses_canonical_record_now_without_synthesized_payment(monkeypatch):
    from celerp_docs import import_service

    calls = []
    async def claim(*args, **kwargs):
        return SimpleNamespace(replay=False, target_id="doc:target")
    async def imported(session, company_id, user, role, settings, records, **kwargs):
        calls.append(records[0].data)
        return SimpleNamespace(records=[SimpleNamespace(status="created")])
    async def commit(*args, **kwargs):
        return None
    monkeypatch.setattr(documents, "claim_peer_revision", claim)
    monkeypatch.setattr(import_service, "import_doc_records", imported)
    monkeypatch.setattr(documents, "commit_peer_revision", commit)
    payload = {"doc_type": "bill", "status": "final", "ref_id": "BILL-42",
               "currency": "USD", "line_items": [{"name": "Service", "quantity": 1,
                                                   "unit_price": 50}]}
    assert await documents.receive_draft_document(
        _Session(), grant=_grant(), source_entity_id="doc:bill",
        source_revision=8, data=payload) == "created"
    assert calls[0]["status"] == "final"
    assert calls[0]["import_treatment"] == "record_now"
    assert "payments" not in calls[0] and "received_items" not in calls[0]


@pytest.mark.asyncio
async def test_issued_transition_finalizes_existing_draft_using_canonical_service(monkeypatch):
    from celerp_docs import import_service, routes
    from celerp.models.projections import Projection
    class HasDraft(_Session):
        async def get(self, model, identifier):
            if model is Projection:
                return SimpleNamespace(state={"status": "draft"})
            return await super().get(model, identifier)

    async def claim(*args, **kwargs):
        return SimpleNamespace(replay=False, target_id="doc:target")
    async def imported(*args, **kwargs):
        (record,) = args[5]
        assert record.data["status"] == "draft"
        return SimpleNamespace(records=[SimpleNamespace(status="updated")])
    events = []
    async def finalize(entity_id, company_id, user, session, *, commit):
        events.append((entity_id, commit))
    async def commit(*args, **kwargs):
        events.append(("revision", kwargs["source_revision"]))
    monkeypatch.setattr(documents, "claim_peer_revision", claim)
    monkeypatch.setattr(import_service, "import_doc_records", imported)
    monkeypatch.setattr(routes, "finalize_document", finalize)
    monkeypatch.setattr(documents, "commit_peer_revision", commit)
    assert await documents.receive_draft_document(
        HasDraft(), grant=_grant(), source_entity_id="doc:invoice",
        source_revision=3, data={"doc_type": "invoice", "status": "sent",
                                 "ref_id": "INV-10", "line_items": [
                                     {"name": "Service", "quantity": 1,
                                      "unit_price": 25}]}) == "updated"
    assert events == [("doc:target", False), ("revision", 3)]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,kind", [
    ("void", "invoice"), ("closed", "invoice"), ("final", "credit_note"),
    ("received", "bill"), ("paid", "memo"),
])
async def test_unsafe_lifecycle_stays_deferred(monkeypatch, status, kind):
    async def claim(*args, **kwargs):
        return SimpleNamespace(replay=False, target_id="doc:target")
    monkeypatch.setattr(documents, "claim_peer_revision", claim)
    with pytest.raises(HTTPException) as e:
        await documents.receive_draft_document(
            _Session(), grant=_grant(), source_entity_id="doc:source",
            source_revision=6,
            data={"doc_type": kind, "status": status})
    assert e.value.status_code == 409



@pytest.mark.asyncio
async def test_paid_document_requires_payment_validation_before_revision_claim(monkeypatch):
    async def unexpected(*args, **kwargs):
        raise AssertionError("Claimed before validating missing cash")
    monkeypatch.setattr(documents, "claim_peer_revision", unexpected)
    with pytest.raises(HTTPException) as err:
        await documents.receive_draft_document(
            _Session(), grant=_grant(), source_entity_id="doc:source",
            source_revision=3, data={"doc_type": "invoice", "status": "paid",
                                     "amount_paid": 30, "payments": []})
    assert err.value.status_code == 409


@pytest.mark.asyncio
async def test_payment_posting_failure_prevents_revision_acknowledgement(monkeypatch):
    from celerp_docs import import_service
    from celerp.services import peer_payments
    actions = []
    async def claim(*args, **kwargs):
        return SimpleNamespace(replay=False, target_id="doc:target")
    async def imported(*args, **kwargs):
        assert "payments" not in args[5][0].data
        actions.append("document")
        return SimpleNamespace(records=[SimpleNamespace(status="created")])
    async def payment(*args, **kwargs):
        actions.append("payment")
        raise HTTPException(409, "Account not ready")
    async def commit(*args, **kwargs):
        actions.append("revision")
    monkeypatch.setattr(documents, "claim_peer_revision", claim)
    monkeypatch.setattr(import_service, "import_doc_records", imported)
    monkeypatch.setattr(peer_payments, "apply_source_payments", payment)
    monkeypatch.setattr(documents, "commit_peer_revision", commit)
    with pytest.raises(HTTPException) as err:
        await documents.receive_draft_document(
            _Session(), grant=_grant(), source_entity_id="doc:source", source_revision=3,
            data={"doc_type": "invoice", "status": "paid", "amount_paid": 30,
                  "total": 30, "payments": [{"index": 0, "amount": 30,
                      "payment_date": "2026-10-12", "bank_account": "1111"}]})
    assert err.value.status_code == 409
    assert actions == ["document", "payment"]
