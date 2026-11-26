# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A document or List is kept only in a supported currency, whichever path writes it.

The rule lives at the event boundary (emit_event's doc/list branch), so the first test
writes the record directly, as any writer could; the others drive the writers that build
a document from outside data (import, batch import, a connected company, a migration and a
shop connector). An empty currency still means "no currency" and passes."""
from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from celerp.events.engine import emit_event
from celerp.models.projections import Projection
from test_migration_sinks import _no_attachments, _sink_context

pytestmark = pytest.mark.asyncio

_REFUSAL = "error.invalid_currency"


def _refused(exc: HTTPException) -> bool:
    return exc.status_code == 422 and exc.detail.get("message_key") == _REFUSAL


async def _emit(session, company_id, entity_id: str, event_type: str, data: dict):
    return await emit_event(session, company_id=company_id, entity_id=entity_id,
                            entity_type=entity_id.split(":")[0], event_type=event_type, data=data,
                            actor_id=None, location_id=None, source="test", idempotency_key=uuid.uuid4().hex)


@pytest.mark.parametrize("entity", ["doc", "list"])
async def test_the_boundary_keeps_every_document_and_list_in_a_supported_currency(client, session, entity):
    headers, context = await _sink_context(client, session, _no_attachments)
    cid = context.company_id
    created = {"doc": ("doc.created", {"doc_type": "invoice", "status": "draft", "line_items": []}),
               "list": ("list.created", {"list_type": "quotation", "status": "draft", "line_items": []})}
    event, base = created[entity]
    updated, patched = f"{entity}.updated", f"{entity}.patched"
    for bad in ("ZZZ", "usd", "ANG"):
        with pytest.raises(HTTPException) as refused:
            await _emit(session, cid, f"{entity}:{uuid.uuid4().hex[:8]}", event, {**base, "currency": bad})
        assert _refused(refused.value), (bad, refused.value.detail)
        await session.rollback()
    kept = f"{entity}:{uuid.uuid4().hex[:8]}"
    await _emit(session, cid, kept, event, {**base, "currency": "EUR"})
    for empty in ("", None):
        await _emit(session, cid, f"{entity}:{uuid.uuid4().hex[:8]}", event, {**base, "currency": empty})
    await session.commit()
    for bad in ("ZZZ", ["USD"], {"code": "USD"}, 7):
        with pytest.raises(HTTPException) as refused:
            await _emit(session, cid, kept, updated, {"fields_changed": {"currency": {"old": "EUR", "new": bad}}})
        assert _refused(refused.value), (bad, refused.value.detail)
        await session.rollback()
    with pytest.raises(HTTPException) as refused:
        await _emit(session, cid, kept, patched, {"currency": "ZZZ"})
    assert _refused(refused.value), refused.value.detail
    await session.rollback()
    await _emit(session, cid, kept, updated, {"fields_changed": {"currency": {"old": "EUR", "new": "GBP"}}})
    await session.commit()
    assert (await session.get(Projection, (cid, kept), populate_existing=True)).state["currency"] == "GBP"


def _import_record(currency) -> dict:
    eid = f"doc:{uuid.uuid4().hex[:8]}"
    return {"entity_id": eid, "event_type": "doc.created", "source": "test", "idempotency_key": eid,
            "data": {"doc_type": "invoice", "status": "draft", "currency": currency, "line_items": []}}


async def _stored_currency(session, company_id, entity_id):
    row = await session.get(Projection, (company_id, entity_id), populate_existing=True)
    return None if row is None else (row.state or {}).get("currency", "<absent>")


async def test_the_document_imports_refuse_an_unsupported_currency(client, session):
    headers, context = await _sink_context(client, session, _no_attachments)
    cid = context.company_id
    bad = _import_record("ZZZ")
    r = await client.post("/docs/import", headers=headers, json=bad)
    assert r.status_code == 422 and r.json()["detail"]["message_key"] == _REFUSAL, r.text
    assert await _stored_currency(session, cid, bad["entity_id"]) is None
    for ok in ("EUR", ""):
        good = _import_record(ok)
        r = await client.post("/docs/import", headers=headers, json=good)
        assert r.status_code == 200, r.text
        assert await _stored_currency(session, cid, good["entity_id"]) in (ok, None, "<absent>")

    bad, good = _import_record("ZZZ"), _import_record("EUR")
    r = await client.post("/docs/import/batch", headers=headers, json={"records": [bad, good]})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1 and len(r.json()["errors"]) == 1, r.text
    assert await _stored_currency(session, cid, bad["entity_id"]) is None
    assert await _stored_currency(session, cid, good["entity_id"]) == "EUR"


async def test_a_migration_refuses_a_document_in_an_unsupported_currency(client, session):
    from celerp.importers.schema import CIFDocument
    from celerp.importers.sinks import sink_for

    _headers, context = await _sink_context(client, session, _no_attachments)

    def invoice(ref: str, currency: str) -> CIFDocument:
        return CIFDocument(source_system="manager_io", source_type="SalesInvoice", source_external_id=ref,
                           doc_type="invoice", status="draft", ref=ref, currency=currency, exchange_rate=Decimal("2"), line_items=[],
                           total=Decimal("0"), amount_paid=Decimal("0"), amount_outstanding=Decimal("0"))

    result = await sink_for("documents").import_batch(context, [invoice("inv-bad", "ZZZ"), invoice("inv-ok", "EUR")])
    assert result.created == 1 and len(result.errors) == 1, result


async def test_a_connected_company_document_in_an_unsupported_currency_is_refused(client, session, monkeypatch):
    from celerp.services import peer_documents

    _headers, context = await _sink_context(client, session, _no_attachments)
    target = f"doc:{uuid.uuid4().hex[:8]}"

    async def claim(*args, **kwargs):
        return SimpleNamespace(replay=False, target_id=target, mapping=None, source_key="a", digest="b")

    monkeypatch.setattr(peer_documents, "claim_peer_revision", claim)
    grant = SimpleNamespace(company_id=context.company_id, source_company_id=uuid.uuid4(), label="Books")
    with pytest.raises(HTTPException) as refused:
        await peer_documents.receive_document(session, grant=grant, source_entity_id="doc:src", source_revision=1,
                                              data={"doc_type": "invoice", "status": "draft", "currency": "ZZZ",
                                                    "line_items": []})
    assert refused.value.status_code == 409, refused.value.detail
    await session.rollback()
    assert await _stored_currency(session, context.company_id, target) is None


@pytest.mark.usefixtures("connector_modules_running")
async def test_a_shop_order_in_an_unsupported_currency_fails_its_sync(session, monkeypatch):
    import celerp.connectors.upsert as u
    from test_helpers import own_sessions_on
    from test_services.test_connector_upsert_integration import _seed_company

    monkeypatch.setattr("celerp.db.SessionLocal", await own_sessions_on(session))
    cid = await _seed_company(session, "ShopCur")
    order = {"id": 9101, "name": "#2001", "financial_status": "paid", "currency": "ZZZ",
             "line_items": [{"title": "T", "quantity": 1, "price": "3.00"}], "total_price": "3.00"}
    with pytest.raises(HTTPException) as refused:
        await u.upsert_order_from_shopify(str(cid), order)
    assert _refused(refused.value), refused.value.detail
    rows = await session.scalar(text("SELECT count(*) FROM ledger WHERE company_id = :c AND idempotency_key LIKE :k"),
                                {"c": cid, "k": "shopify:order:9101:%"})
    assert rows == 0
    assert await u.upsert_order_from_shopify(str(cid), {**order, "id": 9102, "currency": "EUR"}) == "created"
