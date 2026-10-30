"""Peer draft documents reuse the canonical importer and must not bypass lifecycle."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from celerp.services import peer_documents as documents


def _grant():
    return SimpleNamespace(company_id=uuid.uuid4(), source_company_id=uuid.uuid4(), label="Books")


class _Session:
    async def get(self, model, identifier):
        from celerp.models.company import Company
        if model is Company:
            return SimpleNamespace(is_active=True, is_migration_staged=False, settings={}, name="Firm")
        return None

    async def execute(self, query):
        return SimpleNamespace(first=lambda: SimpleNamespace(id=uuid.uuid4(), role="owner"))

    async def scalar(self, query):
        return None  # no source revision held yet


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
    assert await documents.receive_document(
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
    assert await documents.receive_document(
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
    assert await documents.receive_document(
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
        await documents.receive_document(
            _Session(), grant=_grant(), source_entity_id="doc:a",
            source_revision=1, data={"doc_type": "invoice", "status": "draft",
                                     field: value})
    assert rejected.value.status_code == (409 if field == "amount_paid" else 422)


@pytest.mark.asyncio
async def test_missing_reference_is_not_accepted_as_success(monkeypatch):
    async def claim(*args, **kwargs):
        return SimpleNamespace(replay=False, target_id="doc:target")
    async def missing(*args, **kwargs):
        raise HTTPException(409, "Source document dependency is not synchronized")
    monkeypatch.setattr(documents, "claim_peer_revision", claim)
    monkeypatch.setattr(documents, "_map_identity", missing)
    with pytest.raises(HTTPException) as err:
        await documents.receive_document(
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
    assert await documents.receive_document(
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
    async def marked_sent(session, company_id, entity_id, user, payload, *, source):
        assert source == "connector" and payload.sent_to is None
        assert payload.idempotency_key.startswith("peer:doc-sent:")
        events.append(("sent", entity_id))
    monkeypatch.setattr(routes, "mark_doc_sent", marked_sent)
    monkeypatch.setattr(documents, "commit_peer_revision", commit)
    assert await documents.receive_document(
        HasDraft(), grant=_grant(), source_entity_id="doc:invoice",
        source_revision=3, data={"doc_type": "invoice", "status": "sent",
                                 "ref_id": "INV-10", "line_items": [
                                     {"name": "Service", "quantity": 1,
                                      "unit_price": 25}]}) == "updated"
    assert events == [("doc:target", False), ("sent", "doc:target"), ("revision", 3)]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,kind", [
    ("void", "invoice"), ("closed", "invoice"), ("final", "credit_note"),
    ("sent", "bill"), ("paid", "memo"),
])
async def test_unsafe_lifecycle_stays_deferred(monkeypatch, status, kind):
    async def claim(*args, **kwargs):
        return SimpleNamespace(replay=False, target_id="doc:target")
    monkeypatch.setattr(documents, "claim_peer_revision", claim)
    with pytest.raises(HTTPException) as e:
        await documents.receive_document(
            _Session(), grant=_grant(), source_entity_id="doc:source",
            source_revision=6,
            data={"doc_type": kind, "status": status})
    assert e.value.status_code == 409



@pytest.mark.asyncio
@pytest.mark.parametrize("status,kind,subject", [
    ("closed", "invoice", "A closed document"),
    ("final", "credit_note", "An issued credit note"),
    ("final", "consignment_in", "An issued consignment in"),
])
async def test_document_not_copied_says_to_enter_it_by_hand(monkeypatch, status, kind, subject):
    async def claim(*args, **kwargs):
        return SimpleNamespace(replay=False, target_id="doc:target")
    monkeypatch.setattr(documents, "claim_peer_revision", claim)
    with pytest.raises(HTTPException) as e:
        await documents.receive_document(
            _Session(), grant=_grant(), source_entity_id="doc:source",
            source_revision=6, data={"doc_type": kind, "status": status})
    assert e.value.status_code == 409
    assert e.value.detail == (subject + " is not copied automatically. "
                              "Enter it in the accountant's copy by hand.")


@pytest.mark.asyncio
async def test_paid_document_requires_payment_validation_before_revision_claim(monkeypatch):
    async def unexpected(*args, **kwargs):
        raise AssertionError("Claimed before validating missing cash")
    monkeypatch.setattr(documents, "claim_peer_revision", unexpected)
    with pytest.raises(HTTPException) as err:
        await documents.receive_document(
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
    async def place(session, *, codes, **kwargs):
        return {code: code for code in codes}
    monkeypatch.setattr(documents.peer_accounts, "place", place)
    with pytest.raises(HTTPException) as err:
        await documents.receive_document(
            _Session(), grant=_grant(), source_entity_id="doc:source", source_revision=3,
            data={"doc_type": "invoice", "status": "paid", "amount_paid": 30,
                  "total": 30, "payments": [{"index": 0, "amount": 30,
                      "payment_date": "2026-10-12", "bank_account": "1111"}]})
    assert err.value.status_code == 409
    assert actions == ["document", "payment"]



@pytest.mark.asyncio
async def test_peer_void_uses_canonical_operation_without_committing_receiver_transaction(monkeypatch):
    from celerp_docs import routes
    from celerp.models.projections import Projection

    grant = _grant()
    events = []
    class Issued(_Session):
        async def get(self, model, identifier):
            if model is Projection:
                return SimpleNamespace(entity_type="doc", state={
                    "doc_type": "invoice", "status": "final",
                    "currency": "USD", "total": 125,
                })
            return await super().get(model, identifier)

    async def claim(*args, **kwargs):
        return SimpleNamespace(replay=False, target_id="doc:target",
                               mapping=SimpleNamespace(), source_key="key")
    async def void_document(entity_id, body, company_id, user, session, *, commit):
        events.append((entity_id, str(company_id), body.idempotency_key, commit))
        assert user.id and body.reason == "Synchronized source void"
    async def commit_revision(*args, **kwargs):
        events.append(("revision", kwargs["source_revision"]))
    monkeypatch.setattr(documents, "claim_peer_revision", claim)
    monkeypatch.setattr(documents, "commit_peer_revision", commit_revision)
    monkeypatch.setattr(routes, "void_document", void_document)
    data = {"doc_type": "invoice", "status": "void", "total": 125,
            "amount_paid": 0, "currency": "USD"}
    assert await documents.receive_document(
        Issued(), grant=grant, source_entity_id="doc:source",
        source_revision=7, data=data) == "updated"
    assert events[0][0] == "doc:target" and events[0][-1] is False
    assert events[1] == ("revision", 7)


@pytest.mark.asyncio
@pytest.mark.parametrize("target_status,total", [
    ("draft", 125), ("final", 180),
])
async def test_peer_void_refuses_unissued_or_changed_receivable(monkeypatch, target_status, total):
    from celerp.models.projections import Projection

    class Existing(_Session):
        async def get(self, model, identifier):
            if model is Projection:
                return SimpleNamespace(entity_type="doc", state={
                    "doc_type": "invoice", "status": target_status,
                    "currency": "USD", "total": total,
                })
            return await super().get(model, identifier)
    async def claim(*args, **kwargs):
        return SimpleNamespace(replay=False, target_id="doc:target",
                               mapping=SimpleNamespace(), source_key="key")
    monkeypatch.setattr(documents, "claim_peer_revision", claim)
    with pytest.raises(HTTPException) as error:
        await documents.receive_document(
            Existing(), grant=_grant(), source_entity_id="doc:source",
            source_revision=3, data={"doc_type": "invoice", "status": "void",
                                     "currency": "USD", "total": 125, "amount_paid": 0})
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_canonical_void_route_still_commits_standard_api_requests(monkeypatch):
    from celerp_docs import routes
    events = []
    async def execute(entity_id, body, company_id, user, session, *, commit, role=None, settings=None):
        events.append((entity_id, commit))
        return {"event_id": 42}
    async def own_document(session, company_id, entity_id):
        return None
    monkeypatch.setattr(routes, "void_document", execute)
    monkeypatch.setattr(routes, "_refuse_synced", own_document)
    result = await routes.void_doc(
        "doc:test", routes.DocVoidBody(reason="User voided"),
        company_id="company", user=SimpleNamespace(id=uuid.uuid4()),
        session=_Session(),
    )
    assert result == {"event_id": 42}
    assert events == [("doc:test", True)]



@pytest.mark.asyncio
@pytest.mark.parametrize("line", [
    {"name": "Bad quantity", "quantity": float("nan"), "unit_price": 5},
    {"name": "Forged lot", "quantity": 1, "unit_price": 5,
     "attributes": {"reserved_quantity": 99}},
    {"name": "Bad price", "quantity": 1, "unit_price": float("inf")},
])
async def test_peer_document_line_uses_canonical_boundary_validation(monkeypatch, line):
    async def claim(*args, **kwargs):
        return SimpleNamespace(replay=False, target_id="doc:receiving")
    monkeypatch.setattr(documents, "claim_peer_revision", claim)
    with pytest.raises(HTTPException) as exc:
        await documents.receive_document(
            _Session(), grant=_grant(), source_entity_id="doc:unsafe-line",
            source_revision=1,
            data={"doc_type": "invoice", "status": "draft",
                  "line_items": [line], "currency": "USD"},
        )
    assert exc.value.status_code == 422



@pytest.mark.parametrize(("line", "expected"), [
    ({"name": "Consulting", "quantity": 1, "unit_price": 200}, False),
    ({"name": "Service", "item_id": "item:service", "inventory_type": "service",
      "quantity": 1, "unit_price": 200}, False),
    ({"name": "Stock", "item_id": "item:stock", "quantity": 1, "unit_price": 200}, True),
    ({"name": "Stock", "sku": "SKU-1", "quantity": 1, "unit_price": 200}, True),
])
def test_issued_stock_documents_require_canonical_stock_operations(line, expected):
    assert documents.requires_stock_movement({"line_items": [line]}) is expected


@pytest.mark.asyncio
async def test_canonical_sent_transition_can_participate_in_peer_transaction(monkeypatch):
    from celerp_docs import routes
    row = SimpleNamespace(state={
        "doc_type": "invoice", "status": "final",
        "line_items": [{"name": "Service", "quantity": 1, "unit_price": 10}],
    })
    class Session:
        async def commit(self):
            raise AssertionError("sending state during peer sync must not commit")
    emitted = []
    async def current(*args, **kwargs):
        assert kwargs.get("for_update") is True
        return row
    async def prior(*args, **kwargs):
        return None
    async def event(*args, **kwargs):
        emitted.append(kwargs)
        return SimpleNamespace(id=93)
    monkeypatch.setattr(routes, "_get_doc", current)
    monkeypatch.setattr(routes, "_earlier_run", prior)
    monkeypatch.setattr(routes, "emit_event", event)
    session = Session()
    payload = routes.DocSendBody(idempotency_key="source:invoice:sent:3")
    locked, entry, done = await routes.mark_doc_sent(
        session, str(uuid.uuid4()), "doc:recipient", SimpleNamespace(id=uuid.uuid4()),
        payload, source="connector",
    )
    assert locked is row and entry.id == 93 and done is None
    assert emitted[0]["source"] == "connector"
    assert emitted[0]["event_type"] == "doc.sent"
    assert emitted[0]["data"].get("sent_to") is None
    assert emitted[0]["idempotency_key"] == "source:invoice:sent:3"



@pytest.mark.asyncio
async def test_real_service_invoice_mirror_posts_once_in_second_company(client, session):
    from sqlalchemy import select, func
    from celerp.models.ledger import LedgerEntry
    from celerp.models.projections import Projection
    from celerp.services.peer_identity import new_target_identity
    from celerp.services.peer_documents import _FIELDS
    from test_payments_stripe import _register, _payable_invoice, _h, _company_id

    source_token = await _register(client)
    source_company = uuid.UUID(_company_id(source_token))
    source_doc, _ = await _payable_invoice(client, source_token)
    response = await client.get(f"/docs/{source_doc}", headers=_h(source_token))
    assert response.status_code == 200
    source_state = response.json()
    assert source_state["status"] == "final"
    source_row = await session.get(Projection, (source_company, source_doc),
                                   populate_existing=True)
    source_revision = source_row.version
    payload = {key: source_state[key] for key in _FIELDS if key in source_state}

    receiver = await client.post(
        "/companies", json={"name": "Independent Firm"}, headers=_h(source_token))
    assert receiver.status_code == 200, receiver.text
    receiver_token = receiver.json()["access_token"]
    target_company = uuid.UUID(_company_id(receiver_token))
    grant = SimpleNamespace(company_id=target_company, source_company_id=source_company, label="Books")
    assert await documents.receive_document(
        session, grant=grant, source_entity_id=source_doc,
        source_revision=source_revision, data=payload,
    ) == "created"
    await session.commit()
    target_doc = new_target_identity(source_company, "doc", source_doc)
    target = (await client.get(f"/docs/{target_doc}", headers=_h(receiver_token))).json()
    assert target["status"] == "final"
    assert target["total"] == source_state["total"]
    assert target["amount_outstanding"] == source_state["amount_outstanding"]
    before = (await session.scalar(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == target_company)))
    assert before > 0

    assert await documents.receive_document(
        session, grant=grant, source_entity_id=source_doc,
        source_revision=source_revision, data=payload,
    ) == "skipped"
    await session.commit()
    after = (await session.scalar(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == target_company)))
    assert after == before
    with pytest.raises(HTTPException) as conflicting:
        await documents.receive_document(
            session, grant=grant, source_entity_id=source_doc,
            source_revision=source_revision, data={**payload, "notes": "Changed receipt"},
        )
    assert conflicting.value.status_code == 409


async def _copy_held_bill(client, session):
    """An issued bill in a company copy, held under its source identity at the revision the
    copy was made from, as a firm holds a client's bill: (grant, bill id, revision, payload)."""
    from celerp.models.company_peer import CompanyPeerIdentity
    from celerp.models.projections import Projection
    from celerp.services.peer_documents import _FIELDS
    from celerp.services.peer_identity import _digest
    from test_payments_stripe import _register, _h, _company_id

    token = await _register(client)
    company = uuid.UUID(_company_id(token))
    created = await client.post("/docs", json={
        "doc_type": "bill", "contact_name": "Supplier",
        "line_items": [{"description": "Paper", "quantity": 2, "unit_price": 40.0}],
        "currency": "USD"}, headers=_h(token))
    assert created.status_code in (200, 201), created.text
    bill = created.json()["id"]
    assert (await client.post(f"/docs/{bill}/finalize", headers=_h(token))).status_code == 200
    state = (await client.get(f"/docs/{bill}", headers=_h(token))).json()
    assert state["status"] == "awaiting_payment"
    revision = (await session.get(Projection, (company, bill), populate_existing=True)).version
    grant = SimpleNamespace(company_id=company, source_company_id=uuid.uuid4(), label="Books")
    session.add(CompanyPeerIdentity(
        company_id=company, source_company_id=grant.source_company_id, entity_type="doc",
        source_entity_id=bill, source_key_hash=_digest(bill), target_entity_id=bill,
        last_revision=revision))
    await session.commit()
    payload = {key: state[key] for key in _FIELDS if key in state and key != "contact_id"}
    return grant, bill, revision, payload


@pytest.mark.asyncio
async def test_an_unchanged_issued_bill_the_copy_holds_is_taken_at_a_later_revision(client, session):
    grant, bill, revision, payload = await _copy_held_bill(client, session)
    assert await documents.receive_document(
        session, grant=grant, source_entity_id=bill, source_revision=revision + 1,
        data=payload) == "skipped"


@pytest.mark.asyncio
async def test_a_change_to_an_issued_bill_the_copy_holds_tells_the_client_what_happens(client, session):
    grant, bill, revision, payload = await _copy_held_bill(client, session)
    with pytest.raises(HTTPException) as refused:
        await documents.receive_document(
            session, grant=grant, source_entity_id=bill, source_revision=revision + 1,
            data={**payload, "line_items": [{"description": "Paper", "quantity": 3,
                                             "unit_price": 40.0}]})
    assert refused.value.status_code == 409
    assert refused.value.detail == documents.ISSUED_IN_COPY
    assert "doc:" not in refused.value.detail and "Draft" not in refused.value.detail


@pytest.mark.asyncio
async def test_a_receiving_refusal_does_not_name_the_receiving_record(monkeypatch):
    from celerp_docs import import_service

    async def claim(*args, **kwargs):
        return SimpleNamespace(replay=False, target_id="doc:target")
    async def imported(*args, **kwargs):
        return SimpleNamespace(records=[SimpleNamespace(
            status="failed", message="doc:target: The books are locked to 2026-09-30.")])
    monkeypatch.setattr(documents, "claim_peer_revision", claim)
    monkeypatch.setattr(import_service, "import_doc_records", imported)
    with pytest.raises(HTTPException) as refused:
        await documents.receive_document(
            _Session(), grant=_grant(), source_entity_id="doc:source", source_revision=3,
            data={"doc_type": "invoice", "status": "draft", "currency": "USD"})
    assert refused.value.detail == "The books are locked to 2026-09-30."


def _applied_credit(**extra) -> dict:
    return {"index": 1, "amount": 30, "payment_date": "2026-10-12", "currency": "USD",
            "method": "credit_note", "source_doc_id": "doc:source-note", "paired_index": 0,
            "status": "active", **extra}


class _Held(_Session):
    """A receiving copy that already holds revision 5 of the source document, as a company
    copy with its source identities kept does."""
    async def scalar(self, query):
        return SimpleNamespace(source_entity_id="doc:source", last_revision=5, last_payload_hash=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", [4, 5])
async def test_a_revision_the_copy_already_holds_is_skipped_before_its_payload_is_checked(monkeypatch, revision):
    async def unexpected(*args, **kwargs):
        raise AssertionError("A held revision is not claimed again")
    monkeypatch.setattr(documents, "claim_peer_revision", unexpected)
    assert await documents.receive_document(
        _Held(), grant=_grant(), source_entity_id="doc:source", source_revision=revision,
        data={"doc_type": "invoice", "status": "paid", "currency": "USD", "total": 30,
              "amount_paid": 30, "payments": [_applied_credit()]}) == "skipped"


@pytest.mark.asyncio
async def test_a_held_revision_sent_again_with_other_content_is_refused(monkeypatch):
    class _Hashed(_Session):
        async def scalar(self, query):
            return SimpleNamespace(source_entity_id="doc:source", last_revision=5, last_payload_hash="0" * 64)
    with pytest.raises(HTTPException) as err:
        await documents.receive_document(
            _Hashed(), grant=_grant(), source_entity_id="doc:source", source_revision=5,
            data={"doc_type": "invoice", "status": "draft"})
    assert err.value.status_code == 409 and err.value.detail == "Source revision already used"
