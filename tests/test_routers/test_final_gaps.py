# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Coverage gap cleanup: docs summary, refund, receive-PO branches, import errors,
events/engine apply_event, accounting batch error, subscriptions custom/generate,
crm contact search, share multipart/json-error, and tax_regimes.
"""
from __future__ import annotations

import json
import uuid
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _reg(client, name="GapCo") -> str:
    r = await client.post(
        "/auth/register",
        json={"company_name": name, "email": f"{uuid.uuid4().hex[:8]}@gap.test", "name": "Admin", "password": "pw"},
    )
    return r.json()["access_token"]


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


async def _doc(client, tok, **kw) -> str:
    defaults = {"doc_type": "invoice", "contact_id": "c1", "line_items": [], "subtotal": 10, "tax": 0, "total": 10}
    r = await client.post("/docs", headers=_h(tok), json={**defaults, **kw})
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# docs.py: get_doc_summary invoice branch (lines 180-189)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_doc_summary_invoice_branch(client):
    """GET /docs/summary with a finalized invoice hits the AR accumulator branch."""
    tok = await _reg(client, "SummaryCo")
    inv_id = await _doc(client, tok, total=100)
    await client.post(f"/docs/{inv_id}/send", headers=_h(tok), json={})
    await client.post(f"/docs/{inv_id}/finalize", headers=_h(tok))
    await client.post(f"/docs/{inv_id}/payment", headers=_h(tok), json={"amount": 40})

    r = await client.get("/docs/summary", headers=_h(tok))
    assert r.status_code == 200
    body = r.json()
    assert body["invoice_count"] >= 1
    assert body["ar_total"] >= 100
    assert body["ar_paid"] >= 40
    assert body["ar_outstanding"] >= 60


# ---------------------------------------------------------------------------
# docs.py: refund_payment success path (lines 422-428)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_doc_refund_success(client):
    """POST /docs/{id}/refund with valid amount succeeds (line 422-428)."""
    tok = await _reg(client, "RefundCo")
    inv_id = await _doc(client, tok, total=50)
    await client.post(f"/docs/{inv_id}/send", headers=_h(tok), json={})
    await client.post(f"/docs/{inv_id}/finalize", headers=_h(tok))
    await client.post(f"/docs/{inv_id}/payment", headers=_h(tok), json={"amount": 30})

    r = await client.post(f"/docs/{inv_id}/refund", headers=_h(tok), json={"amount": 20})
    assert r.status_code == 200
    assert "event_id" in r.json()


# ---------------------------------------------------------------------------
# docs.py: receive_po legacy total=0 path (line 484)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_receive_po_legacy_zero_total(client):
    """receive_po falls back to computing total from line_items when stored total is 0."""
    tok = await _reg(client, "POLegacyCo")
    # Create PO with total=0 (legacy) but has line_items in data
    r = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "purchase_order",
        "contact_id": "s1",
        "line_items": [{"description": "Widget", "quantity": 2, "unit_price": 25}],
        "subtotal": 0,
        "tax": 0,
        "total": 0,  # triggers legacy fallback
    })
    assert r.status_code == 200
    po_id = r.json()["id"]

    r2 = await client.post(f"/docs/{po_id}/receive", headers=_h(tok), json={
        "location_id": str(uuid.uuid4()),
        "received_items": [],
    })
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# docs.py: receive_po create new item from sku+name (line 506)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_receive_po_create_item_from_sku_name(client):
    """receive_po with no item_id but sku+name creates a new item (line 506)."""
    tok = await _reg(client, "PONewItemCo")
    r = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "purchase_order",
        "contact_id": "s2",
        "line_items": [],
        "subtotal": 20,
        "tax": 0,
        "total": 20,
    })
    po_id = r.json()["id"]

    r2 = await client.post(f"/docs/{po_id}/receive", headers=_h(tok), json={
        "location_id": str(uuid.uuid4()),
        "received_items": [{"po_line_index": 0, "sku": "SKU-NEW", "name": "New Widget", "quantity_received": 5}],
    })
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# docs.py: batch_import error capture path (lines 673-675)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_batch_import_docs_error_capture(client):
    """batch_import_docs captures per-record errors into .errors list (line 673-675)."""
    tok = await _reg(client, "BatchErrCo")
    r = await client.post("/docs/import/batch", headers=_h(tok), json={"records": [
        # Valid record
        {
            "entity_id": f"doc:batch-ok-{uuid.uuid4().hex[:6]}",
            "event_type": "doc.created",
            "data": {"doc_type": "invoice", "total": 5},
            "source": "test",
            "idempotency_key": f"ok-{uuid.uuid4().hex}",
        },
        # Bad record: unknown event_type triggers ValueError in emit_event
        {
            "entity_id": f"doc:batch-bad-{uuid.uuid4().hex[:6]}",
            "event_type": "doc.unknown_bad_type",
            "data": {},
            "source": "test",
            "idempotency_key": f"bad-{uuid.uuid4().hex}",
        },
    ]})
    assert r.status_code == 200
    body = r.json()
    assert body["created"] == 1
    assert len(body["errors"]) >= 1


# ---------------------------------------------------------------------------
# docs.py: import idempotency hit path (lines 545-560)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_import_doc_idempotency_hit(client):
    """POST /docs/import with same idempotency_key returns idempotency_hit=True."""
    tok = await _reg(client, "IdemCo")
    ikey = f"idem-{uuid.uuid4().hex}"
    entity_id = f"doc:idem-{uuid.uuid4().hex[:8]}"

    r1 = await client.post("/docs/import", headers=_h(tok), json={
        "entity_id": entity_id, "event_type": "doc.created",
        "data": {"doc_type": "invoice", "total": 1}, "source": "test", "idempotency_key": ikey,
    })
    assert r1.status_code == 200

    r2 = await client.post("/docs/import", headers=_h(tok), json={
        "entity_id": entity_id, "event_type": "doc.created",
        "data": {"doc_type": "invoice", "total": 1}, "source": "test", "idempotency_key": ikey,
    })
    assert r2.status_code == 200
    assert r2.json()["idempotency_hit"] is True


# ---------------------------------------------------------------------------
# docs.py: import existing entity different key → 409 (lines 548-560)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_import_doc_existing_entity_conflict(client):
    """POST /docs/import same entity_id but new key → 409."""
    tok = await _reg(client, "ConflictCo")
    entity_id = f"doc:conflict-{uuid.uuid4().hex[:8]}"

    r1 = await client.post("/docs/import", headers=_h(tok), json={
        "entity_id": entity_id, "event_type": "doc.created",
        "data": {"doc_type": "invoice", "total": 1}, "source": "test",
        "idempotency_key": f"k1-{uuid.uuid4().hex}",
    })
    assert r1.status_code == 200

    r2 = await client.post("/docs/import", headers=_h(tok), json={
        "entity_id": entity_id, "event_type": "doc.created",
        "data": {"doc_type": "invoice", "total": 1}, "source": "test",
        "idempotency_key": f"k2-{uuid.uuid4().hex}",
    })
    assert r2.status_code == 409


# ---------------------------------------------------------------------------
# events/engine.py: apply_event (line 15)
# ---------------------------------------------------------------------------

def test_events_engine_apply_event():
    """apply_event delegates to ProjectionEngine._apply (line 15)."""
    from celerp.events.engine import apply_event
    from celerp.models.ledger import LedgerEntry
    from unittest.mock import MagicMock

    entry = MagicMock(spec=LedgerEntry)
    entry.event_type = "item.created"
    entry.data = {"sku": "X", "name": "Test"}

    state = apply_event({}, entry)
    assert isinstance(state, dict)


# ---------------------------------------------------------------------------
# events/engine.py: IntegrityError + row is None → re-raise (line 34)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_events_engine_integrity_error_reraise():
    """emit_event re-raises IntegrityError when idempotency lookup returns None."""
    from sqlalchemy.exc import IntegrityError
    from celerp.events.engine import emit_event

    # Use the real session but force an IntegrityError by patching session.flush.
    # The rollback path then does execute(text(...)) which needs to return None.
    mock_result = MagicMock()
    mock_result.first.return_value = None

    mock_session = AsyncMock()
    mock_session.flush = AsyncMock(side_effect=IntegrityError("dup", {}, Exception()))
    mock_session.rollback = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.add = MagicMock()

    with pytest.raises(IntegrityError):
        await emit_event(
            mock_session,
            company_id="c1",
            entity_id="item:x",
            entity_type="item",
            event_type="item.created",
            data={"sku": "X", "name": "T"},
            actor_id=uuid.UUID(int=0),
            location_id=None,
            source="test",
            idempotency_key="ikey-unique",
            metadata_={},
        )


# ---------------------------------------------------------------------------
# accounting.py: batch import error capture (lines 268-270)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_accounting_batch_import_error_capture(client):
    """batch_import_accounting captures per-record errors (lines 268-270)."""
    tok = await _reg(client, "AccErrCo")
    r = await client.post("/accounting/import/batch", headers=_h(tok), json={"records": [
        {
            "entity_id": f"je:{uuid.uuid4().hex[:8]}",
            "event_type": "acc.journal_entry.bad_type",  # unknown → ValueError
            "data": {},
            "source": "test",
            "idempotency_key": f"acc-err-{uuid.uuid4().hex}",
        },
    ]})
    assert r.status_code == 200
    body = r.json()
    assert len(body["errors"]) >= 1


# ---------------------------------------------------------------------------
# subscriptions.py: custom frequency validation (lines 133, 135)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscription_invalid_doc_type_and_frequency(client):
    """create_subscription 422 on bad doc_type and bad frequency (lines 133-137)."""
    tok = await _reg(client, "SubValCo")

    r1 = await client.post("/subscriptions", headers=_h(tok), json={
        "name": "Sub1", "doc_type": "memo", "frequency": "monthly", "start_date": "2026-06-01",
    })
    assert r1.status_code == 422

    r2 = await client.post("/subscriptions", headers=_h(tok), json={
        "name": "Sub2", "doc_type": "invoice", "frequency": "fortnightly", "start_date": "2026-06-01",
    })
    assert r2.status_code == 422

    r3 = await client.post("/subscriptions", headers=_h(tok), json={
        "name": "Sub3", "doc_type": "invoice", "frequency": "custom", "start_date": "2026-06-01",
        # missing custom_interval_days
    })
    assert r3.status_code == 422


# ---------------------------------------------------------------------------
# subscriptions.py: batch import error capture (lines 331-333)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscriptions_batch_import_error_capture(client):
    """batch_import_subscriptions captures per-record errors (lines 331-333)."""
    tok = await _reg(client, "SubBatchErrCo")
    r = await client.post("/subscriptions/import/batch", headers=_h(tok), json={"records": [
        {
            "entity_id": f"sub:{uuid.uuid4()}",
            "event_type": "sub.bad_event_type",  # unknown → ValueError
            "data": {},
            "source": "test",
            "idempotency_key": f"sub-err-{uuid.uuid4().hex}",
        },
    ]})
    assert r.status_code == 200
    body = r.json()
    assert len(body["errors"]) >= 1


# ---------------------------------------------------------------------------
# subscriptions.py: generate with line_items computed total (lines 348, 379-380)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscription_generate_line_items_computed_total(client):
    """generate_now with line_items and total=0 computes total from items (line 379-380)."""
    tok = await _reg(client, "SubGenLiCo")
    r = await client.post("/subscriptions", headers=_h(tok), json={
        "name": "Monthly Widget Sub",
        "doc_type": "invoice",
        "frequency": "monthly",
        "start_date": "2026-06-01",
        "line_items": [{"description": "Widget", "quantity": 2, "unit_price": 15}],
        "tax": 0,
        "shipping": 0,
        "discount": 0,
    })
    assert r.status_code == 200
    sub_id = r.json()["id"]

    r2 = await client.post(f"/subscriptions/{sub_id}/generate", headers=_h(tok))
    assert r2.status_code == 200
    assert "doc_id" in r2.json()


# ---------------------------------------------------------------------------
# crm.py: contact list search filter (lines 120-121)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_crm_contact_list_search(client):
    """GET /crm/contacts?q=... filters by name/email/phone (lines 120-121)."""
    tok = await _reg(client, "CRMSearchCo")
    await client.post("/crm/contacts", headers=_h(tok), json={
        "name": "Zelda Unique", "email": "zelda@unique.test", "phone": "0001112222",
    })
    r = await client.get("/crm/contacts?q=zelda", headers=_h(tok))
    assert r.status_code == 200
    items = r.json()["items"]
    assert any("Zelda" in c.get("name", "") for c in items)

    # No match
    r2 = await client.get("/crm/contacts?q=xyznotfound99", headers=_h(tok))
    assert r2.status_code == 200
    assert r2.json()["items"] == []


# ---------------------------------------------------------------------------
# share.py: import-bundle multipart path (lines 229-237)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_share_import_bundle_multipart(client):
    """POST /docs/import-bundle with multipart/form-data parses bundle field (lines 229-237)."""
    tok = await _reg(client, "ShareMultiCo")
    bundle_data = json.dumps({
        "version": 1,
        "doc": {"doc_type": "invoice", "total": 5, "status": "draft", "line_items": []},
    })

    r = await client.post(
        "/docs/import-bundle",
        headers=_h(tok),
        files={"bundle": ("bundle.json", BytesIO(bundle_data.encode()), "application/json")},
        follow_redirects=False,
    )
    # Returns 302 redirect to the created doc (same as JSON path) — proves multipart branch hit
    assert r.status_code == 302
    assert r.headers.get("location", "").startswith("/docs/doc:rcv:")


@pytest.mark.asyncio
async def test_share_import_bundle_multipart_missing_field(client):
    """POST /docs/import-bundle multipart missing bundle field → 422 (line 232)."""
    tok = await _reg(client, "ShareMultiMissCo")
    r = await client.post(
        "/docs/import-bundle",
        headers=_h(tok),
        files={"other_field": ("f.txt", BytesIO(b"x"), "text/plain")},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_share_import_bundle_invalid_json_body(client):
    """POST /docs/import-bundle with non-JSON body → 422 (lines 237-238)."""
    tok = await _reg(client, "ShareBadJsonCo")
    r = await client.post(
        "/docs/import-bundle",
        headers={**_h(tok), "content-type": "application/json"},
        content=b"this is not json",
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# tax_regimes.py: line 217 (get_regime function)
# ---------------------------------------------------------------------------

def test_tax_regimes_get_regime_known_and_fallback():
    """get_regime returns known country regime and falls back for unknown (line 217)."""
    from celerp.tax_regimes import TAX_REGIMES, get_regime

    th = get_regime("TH")
    assert th["currency"] == "THB"
    assert len(th["taxes"]) > 0

    default = get_regime("XX")
    assert default == TAX_REGIMES["_default"]

    none_result = get_regime(None)
    assert none_result == TAX_REGIMES["_default"]


# ---------------------------------------------------------------------------
# db.py: lines 13-14 (create_all / engine bootstrap)
# ---------------------------------------------------------------------------

def test_db_engine_and_session_factory():
    """Import db module; engine and SessionLocal are properly constructed (lines 13-14)."""
    from celerp import db
    assert db.engine is not None
    assert db.SessionLocal is not None


# ---------------------------------------------------------------------------
# events/engine.py lines 42-45: pg_notify exception swallowed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_events_engine_pg_notify_exception_swallowed():
    """pg_notify Exception is swallowed and does not fail emission (lines 42-45)."""
    from celerp.events.engine import emit_event
    from celerp.projections.engine import ProjectionEngine

    mock_session = AsyncMock()
    mock_session.flush = AsyncMock()
    # Simulate postgresql dialect on session.bind
    mock_bind = MagicMock()
    mock_bind.dialect.name = "postgresql"
    mock_session.bind = mock_bind
    # pg_notify raises an exception
    mock_session.execute = AsyncMock(side_effect=Exception("pg_notify failed"))
    mock_session.add = MagicMock()

    added_entry = None

    def capture_add(entry):
        nonlocal added_entry
        added_entry = entry

    mock_session.add = capture_add

    with patch.object(ProjectionEngine, "apply_event", new_callable=AsyncMock) as mock_apply:
        result = await emit_event(
            mock_session,
            company_id="c1",
            entity_id="item:pg-test",
            entity_type="item",
            event_type="item.created",
            data={"sku": "PG", "name": "PGTest"},
            actor_id=uuid.UUID(int=0),
            location_id=None,
            source="test",
            idempotency_key=f"pg-{uuid.uuid4().hex}",
            metadata_={},
        )
    # apply_event was called despite pg_notify failing
    mock_apply.assert_called_once()
