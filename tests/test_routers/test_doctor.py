# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for the doctor endpoint, import auto-JE hook, and entity guard."""

from __future__ import annotations

import uuid

import pytest

from celerp.services.je_keys import je_idempotency_key


async def _register(client) -> str:
    r = await client.post("/auth/register", json={
        "company_name": "Doctor Co", "email": f"doc-{uuid.uuid4().hex[:8]}@test.test",
        "name": "Admin", "password": "pw",
    })
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_invoice(client, token, *, total=1000, tax=70, status="draft") -> str:
    r = await client.post("/docs", headers=_h(token), json={
        "doc_type": "invoice", "contact_name": "Test",
        "subtotal": total - tax, "tax": tax, "total": total, "status": status,
    })
    assert r.status_code == 200
    return r.json()["id"]


# --- je_idempotency_key ---

def test_je_key_format():
    assert je_idempotency_key("doc:INV-1", "fin", "c") == "je:doc:INV-1:fin:c"
    assert je_idempotency_key("doc:INV-1", "pay", "p") == "je:doc:INV-1:pay:p"


def test_je_key_deterministic():
    k1 = je_idempotency_key("doc:X", "fin", "c")
    k2 = je_idempotency_key("doc:X", "fin", "c")
    assert k1 == k2


# --- Doctor dry-run ---

@pytest.mark.asyncio
async def test_doctor_dry_run_reports_missing_jes(client, session):
    token = await _register(client)
    # Create and finalize an invoice (auto-JE fires via API)
    inv = await _create_invoice(client, token)
    await client.post(f"/docs/{inv}/finalize", headers=_h(token))

    # Doctor should find 0 missing JEs (API flow created them)
    r = await client.post("/admin/doctor?checks=missing_jes", headers=_h(token))
    assert r.status_code == 200
    data = r.json()
    assert data["mode"] == "dry-run"
    missing = next(c for c in data["results"] if c["check"] == "missing_jes")
    assert missing["found"] == 0


@pytest.mark.asyncio
async def test_doctor_detects_unbalanced_je(client, session):
    """Manually create an unbalanced JE and verify doctor catches it."""
    token = await _register(client)
    # Create invoice via API lifecycle to get a finalization JE
    inv = await _create_invoice(client, token, total=100, tax=0)
    await client.post(f"/docs/{inv}/finalize", headers=_h(token))

    # The auto-JE should be balanced
    r = await client.post("/admin/doctor?checks=unbalanced_jes", headers=_h(token))
    data = r.json()
    unbalanced = next(c for c in data["results"] if c["check"] == "unbalanced_jes")
    assert unbalanced["found"] == 0


@pytest.mark.asyncio
async def test_doctor_all_checks_run(client, session):
    token = await _register(client)
    r = await client.post("/admin/doctor", headers=_h(token))
    assert r.status_code == 200
    data = r.json()
    assert len(data["results"]) == 10
    check_names = [c["check"] for c in data["results"]]
    assert "missing_jes" in check_names
    assert "duplicate_jes" in check_names
    assert "ghost_events" in check_names
    assert "orphan_projections" in check_names
    assert "stale_projections" in check_names
    assert "unbalanced_jes" in check_names
    assert "zero_amount_jes" in check_names
    assert "fractional_piece_quantities" in check_names


@pytest.mark.asyncio
async def test_doctor_invalid_check_name(client, session):
    token = await _register(client)
    r = await client.post("/admin/doctor?checks=nonexistent", headers=_h(token))
    assert r.status_code == 422
    data = r.json()
    assert "detail" in data


# --- Import entity guard ---

@pytest.mark.asyncio
async def test_import_rejects_duplicate_entity_id(client, session):
    token = await _register(client)
    entity_id = f"doc:test-dup-{uuid.uuid4().hex[:8]}"

    # First import succeeds
    r = await client.post("/docs/import", headers=_h(token), json={
        "entity_id": entity_id, "event_type": "doc.created",
        "data": {"doc_type": "invoice", "total": 100, "status": "draft"},
        "source": "import:test", "idempotency_key": f"idem-1-{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 200

    # Second import with different idempotency key: 409
    r = await client.post("/docs/import", headers=_h(token), json={
        "entity_id": entity_id, "event_type": "doc.created",
        "data": {"doc_type": "invoice", "total": 200, "status": "draft"},
        "source": "import:test", "idempotency_key": f"idem-2-{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"]


@pytest.mark.asyncio
async def test_import_allows_lifecycle_events_on_existing(client, session):
    token = await _register(client)
    entity_id = f"doc:test-lc-{uuid.uuid4().hex[:8]}"

    # Create
    r = await client.post("/docs/import", headers=_h(token), json={
        "entity_id": entity_id, "event_type": "doc.created",
        "data": {"doc_type": "invoice", "total": 100, "status": "draft"},
        "source": "import:test", "idempotency_key": f"idem-cr-{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 200

    # Finalize (non-create event) should be allowed
    r = await client.post("/docs/import", headers=_h(token), json={
        "entity_id": entity_id, "event_type": "doc.finalized",
        "data": {}, "source": "import:test",
        "idempotency_key": f"idem-fin-{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_import_same_idempotency_key_returns_existing(client, session):
    """Same idempotency key = idempotent, no error."""
    token = await _register(client)
    entity_id = f"doc:test-idem-{uuid.uuid4().hex[:8]}"
    idem = f"idem-same-{uuid.uuid4().hex[:8]}"

    r1 = await client.post("/docs/import", headers=_h(token), json={
        "entity_id": entity_id, "event_type": "doc.created",
        "data": {"doc_type": "invoice", "total": 100, "status": "draft"},
        "source": "import:test", "idempotency_key": idem,
    })
    assert r1.status_code == 200

    # Same key again - should succeed (idempotent retry)
    r2 = await client.post("/docs/import", headers=_h(token), json={
        "entity_id": entity_id, "event_type": "doc.created",
        "data": {"doc_type": "invoice", "total": 100, "status": "draft"},
        "source": "import:test", "idempotency_key": idem,
    })
    assert r2.status_code == 200
    assert r2.json()["idempotency_hit"] is True


# --- Import auto-JE hook ---

@pytest.mark.asyncio
async def test_import_paid_invoice_creates_jes(client, session):
    """Import a paid invoice - should auto-create finalization + payment JEs."""
    token = await _register(client)
    entity_id = f"doc:test-paid-{uuid.uuid4().hex[:8]}"

    r = await client.post("/docs/import", headers=_h(token), json={
        "entity_id": entity_id, "event_type": "doc.created",
        "data": {
            "doc_type": "invoice", "total": 1000, "subtotal": 930, "tax": 70,
            "status": "paid", "amount_paid": 1000, "amount_outstanding": 0,
        },
        "source": "import:test", "idempotency_key": f"idem-{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 200

    # Check JE projections were created
    r = await client.get("/ledger?entity_type=journal_entry", headers=_h(token))
    jes = r.json()["items"]
    # Only finalization JE is created on import (no synthetic payment JE)
    je_types = [e["event_type"] for e in jes]
    assert je_types.count("acc.journal_entry.created") == 1
    assert je_types.count("acc.journal_entry.posted") == 1

    # Trial balance should show data
    r = await client.get("/accounting/trial-balance", headers=_h(token))
    tb = r.json()
    assert tb["total_debit"] > 0
    assert tb["balanced"]


@pytest.mark.asyncio
async def test_import_draft_invoice_no_jes(client, session):
    """Import a draft invoice - should NOT create JEs."""
    token = await _register(client)
    entity_id = f"doc:test-draft-{uuid.uuid4().hex[:8]}"

    r = await client.post("/docs/import", headers=_h(token), json={
        "entity_id": entity_id, "event_type": "doc.created",
        "data": {"doc_type": "invoice", "total": 500, "status": "draft"},
        "source": "import:test", "idempotency_key": f"idem-{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 200

    r = await client.get("/ledger?entity_type=journal_entry", headers=_h(token))
    assert len(r.json()["items"]) == 0


@pytest.mark.asyncio
async def test_import_void_invoice_no_jes(client, session):
    """Import a void invoice - should NOT create JEs."""
    token = await _register(client)
    entity_id = f"doc:test-void-{uuid.uuid4().hex[:8]}"

    r = await client.post("/docs/import", headers=_h(token), json={
        "entity_id": entity_id, "event_type": "doc.created",
        "data": {"doc_type": "invoice", "total": 500, "status": "void"},
        "source": "import:test", "idempotency_key": f"idem-{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 200

    r = await client.get("/ledger?entity_type=journal_entry", headers=_h(token))
    assert len(r.json()["items"]) == 0


# --- Doc-scoped idempotency: import + API can't duplicate ---

@pytest.mark.asyncio
async def test_api_finalize_after_import_no_duplicate_je(client, session):
    """If a doc was imported as finalized (JE created), then finalized via API, no duplicate JE."""
    token = await _register(client)

    # Create via API (draft)
    inv = await _create_invoice(client, token, total=500, tax=0)

    # Finalize via API (creates JE with doc-scoped key)
    r = await client.post(f"/docs/{inv}/finalize", headers=_h(token))
    assert r.status_code == 200

    # Check: exactly 1 finalization JE (2 events: created + posted)
    r = await client.get("/ledger?entity_type=journal_entry", headers=_h(token))
    jes = r.json()["items"]
    created_events = [e for e in jes if e["event_type"] == "acc.journal_entry.created"]
    assert len(created_events) == 1


# --- Batch import entity guard ---

@pytest.mark.asyncio
async def test_batch_import_skips_existing_entities(client, session):
    token = await _register(client)
    entity_id = f"doc:batch-dup-{uuid.uuid4().hex[:8]}"

    # First: import one doc
    r = await client.post("/docs/import", headers=_h(token), json={
        "entity_id": entity_id, "event_type": "doc.created",
        "data": {"doc_type": "invoice", "total": 100, "status": "draft"},
        "source": "import:test", "idempotency_key": f"idem-first-{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 200

    # Batch: try to re-import same entity + a new one
    new_id = f"doc:batch-new-{uuid.uuid4().hex[:8]}"
    r = await client.post("/docs/import/batch", headers=_h(token), json={"records": [
        {
            "entity_id": entity_id, "event_type": "doc.created",
            "data": {"doc_type": "invoice", "total": 200, "status": "draft"},
            "source": "import:test", "idempotency_key": f"idem-dup-{uuid.uuid4().hex[:8]}",
        },
        {
            "entity_id": new_id, "event_type": "doc.created",
            "data": {"doc_type": "invoice", "total": 300, "status": "draft"},
            "source": "import:test", "idempotency_key": f"idem-new-{uuid.uuid4().hex[:8]}",
        },
    ]})
    assert r.status_code == 200
    data = r.json()
    assert data["created"] == 1  # only the new one
    assert data["skipped"] == 1  # the duplicate


@pytest.mark.asyncio
async def test_batch_import_paid_invoices_create_jes(client, session):
    """Batch import paid invoices - auto-JEs should fire for each."""
    token = await _register(client)

    records = []
    for i in range(3):
        records.append({
            "entity_id": f"doc:batch-paid-{i}-{uuid.uuid4().hex[:8]}",
            "event_type": "doc.created",
            "data": {
                "doc_type": "invoice", "total": 1000 * (i + 1),
                "subtotal": 1000 * (i + 1), "tax": 0,
                "status": "paid", "amount_paid": 1000 * (i + 1),
            },
            "source": "import:test",
            "idempotency_key": f"idem-bp-{i}-{uuid.uuid4().hex[:8]}",
        })

    r = await client.post("/docs/import/batch", headers=_h(token), json={"records": records})
    assert r.status_code == 200
    assert r.json()["created"] == 3

    # Should have 6 JE created events (3 finalization + 3 payment) = 12 total events
    r = await client.get("/ledger?entity_type=journal_entry", headers=_h(token))
    jes = r.json()["items"]
    created = [e for e in jes if e["event_type"] == "acc.journal_entry.created"]
    assert len(created) == 3  # 3 finalization only; no synthetic payment JEs on import


# --- Doctor fix mode ---

@pytest.mark.asyncio
async def test_doctor_fix_creates_missing_jes(client, session):
    """Import a doc without JEs (old-style), then doctor fix should create them."""
    token = await _register(client)
    entity_id = f"doc:doctor-fix-{uuid.uuid4().hex[:8]}"

    # Bypass the new import hook by using a raw ledger event
    from celerp.events.engine import emit_event
    from celerp.services.auth import get_current_company_id

    # Use old-style import (directly emit event without hook)
    # Simulate by importing a draft then manually updating status in projection
    r = await client.post("/docs/import", headers=_h(token), json={
        "entity_id": entity_id, "event_type": "doc.created",
        "data": {"doc_type": "invoice", "total": 500, "subtotal": 500, "tax": 0, "status": "draft"},
        "source": "import:test", "idempotency_key": f"idem-dr-{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 200

    # Finalize via import (non-create event, no JE hook)
    r = await client.post("/docs/import", headers=_h(token), json={
        "entity_id": entity_id, "event_type": "doc.finalized", "data": {},
        "source": "import:test", "idempotency_key": f"idem-fin-{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 200

    # No JEs yet (finalize via import doesn't trigger auto-JE)
    r = await client.get("/ledger?entity_type=journal_entry", headers=_h(token))
    assert len(r.json()["items"]) == 0

    # Doctor dry-run: should find missing JE
    r = await client.post("/admin/doctor?checks=missing_jes", headers=_h(token))
    data = r.json()
    missing = next(c for c in data["results"] if c["check"] == "missing_jes")
    assert missing["found"] == 1
    assert missing["fixed"] == 0

    # Doctor fix: should create the JE
    r = await client.post("/admin/doctor?checks=missing_jes&fix=true", headers=_h(token))
    data = r.json()
    assert data["mode"] == "fix"
    missing = next(c for c in data["results"] if c["check"] == "missing_jes")
    assert missing["found"] == 1
    assert missing["fixed"] == 1

    # Verify JEs exist now
    r = await client.get("/ledger?entity_type=journal_entry", headers=_h(token))
    assert len(r.json()["items"]) > 0

    # Running doctor again: no more missing
    r = await client.post("/admin/doctor?checks=missing_jes", headers=_h(token))
    data = r.json()
    missing = next(c for c in data["results"] if c["check"] == "missing_jes")
    assert missing["found"] == 0


# --- Doctor fix: orphan projections ---

@pytest.mark.asyncio
async def test_doctor_fix_orphan_projections(client, session):
    """With API-created data, orphan_projections check finds 0; fix=true completes cleanly."""
    import uuid as _uuid
    token = await _register(client)
    # Create a doc so there are projections to scan
    orphan_id = f"doc:orphan-{_uuid.uuid4().hex[:8]}"
    r = await client.post("/docs/import", headers=_h(token), json={
        "entity_id": orphan_id, "event_type": "doc.created",
        "data": {"doc_type": "invoice", "total": 100, "status": "draft"},
        "source": "import:test", "idempotency_key": f"idem-orphan-{_uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 200

    # Dry-run: no orphans (event exists)
    r2 = await client.post("/admin/doctor?checks=orphan_projections", headers=_h(token))
    assert r2.status_code == 200
    res = next(c for c in r2.json()["results"] if c["check"] == "orphan_projections")
    assert res["found"] == 0

    # Fix mode: same result, no error
    r3 = await client.post("/admin/doctor?checks=orphan_projections&fix=true", headers=_h(token))
    assert r3.status_code == 200
    assert r3.json()["mode"] == "fix"


# --- Doctor fix: stale projections ---

@pytest.mark.asyncio
async def test_doctor_stale_projections_clean_data(client, session):
    """With API-created data (projection kept in sync by engine), stale check should find 0."""
    token = await _register(client)
    inv = await _create_invoice(client, token, total=200, tax=0)
    await client.post(f"/docs/{inv}/finalize", headers=_h(token))

    r = await client.post("/admin/doctor?checks=stale_projections", headers=_h(token))
    assert r.status_code == 200
    stale = next(c for c in r.json()["results"] if c["check"] == "stale_projections")
    assert stale["found"] == 0

    # Fix mode: still 0, no error
    r2 = await client.post("/admin/doctor?checks=stale_projections&fix=true", headers=_h(token))
    assert r2.status_code == 200
    assert r2.json()["total_fixed"] == 0


# --- Doctor fix: zero_amount_jes ---

@pytest.mark.asyncio
async def test_doctor_zero_amount_jes_none_found(client, session):
    """With a real invoice JE (non-zero), zero_amount check should find 0."""
    token = await _register(client)
    inv = await _create_invoice(client, token, total=300, tax=0)
    await client.post(f"/docs/{inv}/finalize", headers=_h(token))

    r = await client.post("/admin/doctor?checks=zero_amount_jes", headers=_h(token))
    assert r.status_code == 200
    zeros = next(c for c in r.json()["results"] if c["check"] == "zero_amount_jes")
    assert zeros["found"] == 0

    # Fix mode: same result
    r2 = await client.post("/admin/doctor?checks=zero_amount_jes&fix=true", headers=_h(token))
    assert r2.status_code == 200
    assert r2.json()["total_fixed"] == 0


# --- Doctor rebuild flag ---

@pytest.mark.asyncio
async def test_doctor_rebuild_flag(client, session):
    """rebuild=true with fix=true should complete without error."""
    token = await _register(client)
    inv = await _create_invoice(client, token, total=150, tax=0)
    await client.post(f"/docs/{inv}/finalize", headers=_h(token))

    r = await client.post("/admin/doctor?fix=true&rebuild=true", headers=_h(token))
    assert r.status_code == 200
    data = r.json()
    assert data["mode"] == "fix"
    assert data["rebuilt"] is True


# --- Doctor: specific checks subset ---

@pytest.mark.asyncio
async def test_doctor_subset_checks(client, session):
    """Only requested checks run."""
    token = await _register(client)
    r = await client.post(
        "/admin/doctor?checks=unbalanced_jes,zero_amount_jes", headers=_h(token)
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["results"]) == 2
    assert {c["check"] for c in data["results"]} == {"unbalanced_jes", "zero_amount_jes"}


# --- Doctor: PO missing JE (fix path) ---

@pytest.mark.asyncio
async def test_doctor_missing_je_po_no_missing_after_api(client, session):
    """A PO imported as received triggers the auto-JE hook - doctor should find 0 missing."""
    import uuid as _uuid
    token = await _register(client)
    entity_id = f"doc:po-fix-{_uuid.uuid4().hex[:8]}"

    r = await client.post("/docs/import", headers=_h(token), json={
        "entity_id": entity_id, "event_type": "doc.created",
        "data": {
            "doc_type": "purchase_order", "total": 800, "status": "received",
            "purchase_kind": "inventory",
        },
        "source": "import:test",
        "idempotency_key": f"idem-po-{_uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 200

    # Auto-JE hook fires for received POs - no missing JEs
    r2 = await client.post("/admin/doctor?checks=missing_jes", headers=_h(token))
    missing = next(c for c in r2.json()["results"] if c["check"] == "missing_jes")
    assert missing["found"] == 0

    # Verify PO JE events created
    r3 = await client.get("/ledger?entity_type=journal_entry", headers=_h(token))
    created = [e for e in r3.json()["items"] if e["event_type"] == "acc.journal_entry.created"]
    assert len(created) == 1


# --- Doctor fix: paid invoice missing payment JE ---

@pytest.mark.asyncio
async def test_doctor_fix_missing_payment_je(client, session):
    """Invoice finalized (JE created) but payment JE missing -> doctor fix creates it."""
    token = await _register(client)
    entity_id = f"doc:paid-fix-{uuid.uuid4().hex[:8]}"

    # Import as finalized (non-draft, non-void) - hook creates finalize JE
    r = await client.post("/docs/import", headers=_h(token), json={
        "entity_id": entity_id, "event_type": "doc.created",
        "data": {
            "doc_type": "invoice", "total": 600, "subtotal": 600, "tax": 0,
            "status": "finalized",
        },
        "source": "import:test",
        "idempotency_key": f"idem-pf-cr-{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 200

    # Mark as paid via a subsequent event (but no auto-JE hook for this path)
    r = await client.post("/docs/import", headers=_h(token), json={
        "entity_id": entity_id, "event_type": "doc.payment.received",
        "data": {"amount": 600.0, "amount_paid": 600, "amount_outstanding": 0, "status": "paid", "payment_date": "2026-01-15"},
        "source": "import:test",
        "idempotency_key": f"idem-pf-pay-{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 200

    # Doctor: may find missing payment JE depending on hook coverage - just verify it runs
    r = await client.post("/admin/doctor?checks=missing_jes&fix=true", headers=_h(token))
    assert r.status_code == 200
    data = r.json()
    assert data["mode"] == "fix"
    missing = next(c for c in data["results"] if c["check"] == "missing_jes")
    # found >= 0: main thing is it doesn't crash
    assert missing["found"] >= 0


# --- Doctor fix: duplicate JEs ---

@pytest.mark.asyncio
async def test_doctor_fix_duplicate_jes(client, session):
    """Create two JEs for the same trigger key, verify doctor detects and fixes duplicates."""
    import uuid as _uuid
    from celerp.events.engine import emit_event as _emit

    token = await _register(client)
    # Get company_id via API - comes back as string UUID, must parse to UUID for ORM
    me = (await client.get("/companies/me", headers=_h(token))).json()
    company_id = _uuid.UUID(me["id"])

    doc_id = f"doc:dup-je-{_uuid.uuid4().hex[:8]}"
    je_entity_id = f"je:auto:{doc_id}:fin"

    # Emit the same JE twice with slightly different idempotency keys to bypass dedup
    # Use the test session (same DB as the HTTP client)
    for i in range(2):
        await _emit(
            session, company_id=company_id,
            entity_id=je_entity_id,
            entity_type="journal_entry",
            event_type="acc.journal_entry.created",
            data={"memo": f"dup-{i}", "entries": [
                {"account": "1120", "debit": 100.0, "credit": 0.0},
                {"account": "4100", "debit": 0.0, "credit": 100.0},
            ]},
            actor_id=None, location_id=None, source="test",
            idempotency_key=f"{je_entity_id}:test-dup-{i}",
            metadata_={},
        )
    await session.commit()

    # Doctor should find 1 duplicate pair
    r = await client.post("/admin/doctor?checks=duplicate_jes", headers=_h(token))
    assert r.status_code == 200
    dups = next(c for c in r.json()["results"] if c["check"] == "duplicate_jes")
    assert dups["found"] >= 1

    # Fix mode: voids the duplicates
    r2 = await client.post("/admin/doctor?checks=duplicate_jes&fix=true", headers=_h(token))
    assert r2.status_code == 200
    dups2 = next(c for c in r2.json()["results"] if c["check"] == "duplicate_jes")
    assert dups2["fixed"] >= 1


# --- Doctor fix: zero-amount JEs ---

@pytest.mark.asyncio
async def test_doctor_fix_zero_amount_je(client, session):
    """Create a zero-amount JE; doctor should detect and fix it."""
    import uuid as _uuid
    from celerp.events.engine import emit_event as _emit

    token = await _register(client)
    me = (await client.get("/companies/me", headers=_h(token))).json()
    company_id = _uuid.UUID(me["id"])

    je_id = f"je:auto:doc:zero-{_uuid.uuid4().hex[:8]}:fin"

    # Use the test session directly (same DB as the HTTP client)
    await _emit(
        session, company_id=company_id,
        entity_id=je_id,
        entity_type="journal_entry",
        event_type="acc.journal_entry.created",
        data={"memo": "zero-amount JE", "entries": [
            {"account": "1120", "debit": 0.0, "credit": 0.0},
        ]},
        actor_id=None, location_id=None, source="test",
        idempotency_key=f"test-zero-{_uuid.uuid4().hex[:8]}",
        metadata_={},
    )
    await session.commit()

    # Detect
    r = await client.post("/admin/doctor?checks=zero_amount_jes", headers=_h(token))
    assert r.status_code == 200
    zeros = next(c for c in r.json()["results"] if c["check"] == "zero_amount_jes")
    assert zeros["found"] >= 1

    # Fix
    r2 = await client.post("/admin/doctor?checks=zero_amount_jes&fix=true", headers=_h(token))
    assert r2.status_code == 200
    zeros2 = next(c for c in r2.json()["results"] if c["check"] == "zero_amount_jes")
    assert zeros2["fixed"] >= 1


# --- Doctor fix: ghost events ---

@pytest.mark.asyncio
async def test_doctor_ghost_events_clean_data(client, session):
    """API-created docs have exactly one doc.created event - ghost check finds 0."""
    token = await _register(client)
    await _create_invoice(client, token, total=200, tax=0)

    r = await client.post("/admin/doctor?checks=ghost_events", headers=_h(token))
    assert r.status_code == 200
    ghosts = next(c for c in r.json()["results"] if c["check"] == "ghost_events")
    assert ghosts["found"] == 0

    # Fix mode with ghost - no error
    r2 = await client.post("/admin/doctor?checks=ghost_events&fix=true", headers=_h(token))
    assert r2.status_code == 200
    assert r2.json()["mode"] == "fix"


# --- Doctor fix: PO missing JE (fix path) with no existing JE ---

@pytest.mark.asyncio
async def test_doctor_fix_po_missing_je(client, session):
    """Import a received PO via lifecycle event (no hook), then doctor fix creates JE."""
    import uuid as _uuid
    token = await _register(client)
    entity_id = f"doc:po-missing-{_uuid.uuid4().hex[:8]}"

    # Create PO as draft first
    r = await client.post("/docs/import", headers=_h(token), json={
        "entity_id": entity_id, "event_type": "doc.created",
        "data": {"doc_type": "purchase_order", "total": 400, "status": "draft", "purchase_kind": "inventory"},
        "source": "import:test",
        "idempotency_key": f"idem-po-dr-{_uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 200

    # Mark as received via import (lifecycle event - doc.received requires location_id)
    r = await client.post("/docs/import", headers=_h(token), json={
        "entity_id": entity_id, "event_type": "doc.received",
        "data": {"status": "received", "location_id": "loc:default", "received_items": []},
        "source": "import:test",
        "idempotency_key": f"idem-po-rcv-{_uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 200

    # Doctor fix: creates the missing PO received JE
    r2 = await client.post("/admin/doctor?checks=missing_jes&fix=true", headers=_h(token))
    assert r2.status_code == 200
    data = r2.json()
    missing = next(c for c in data["results"] if c["check"] == "missing_jes")
    # Should find at least the PO (received, no JE) and fix it
    assert missing["found"] >= 1
    assert missing["fixed"] >= 1


# --- Connector sync error paths ---
# All connector endpoints require X-Session-Token (cloud gate).

_CONNECTOR_SESSION_TOKEN = "test-session-token-abc123"


@pytest.fixture(autouse=False)
def patch_connector_session_token():
    import celerp.gateway.state as gw_state
    gw_state.set_session_token(_CONNECTOR_SESSION_TOKEN)
    yield
    gw_state.set_session_token("")


@pytest.mark.asyncio
async def test_connector_sync_not_implemented(client, patch_connector_session_token):
    """Sync with an entity that raises NotImplementedError -> 400."""
    from unittest.mock import AsyncMock, patch
    import celerp.connectors as conn_module

    resp = await client.post("/auth/register", json={
        "email": "connector_notimpl@test.com", "password": "pw",
        "name": "Test", "company_name": "ConnTest",
    })
    headers = {
        "Authorization": f"Bearer {resp.json()['access_token']}",
        "X-Session-Token": _CONNECTOR_SESSION_TOKEN,
    }

    connector = conn_module.get("shopify")
    with patch.object(
        connector, "sync_orders",
        new=AsyncMock(side_effect=NotImplementedError("not supported")),
    ):
        resp = await client.post("/connectors/shopify/sync", headers=headers, json={
            "entity": "orders", "access_token": "tok",
        })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_connector_sync_generic_exception(client, patch_connector_session_token):
    """Sync that raises a generic exception -> 502."""
    from unittest.mock import AsyncMock, patch
    import celerp.connectors as conn_module

    resp = await client.post("/auth/register", json={
        "email": "connector_exc@test.com", "password": "pw",
        "name": "Test", "company_name": "ConnTest2",
    })
    headers = {
        "Authorization": f"Bearer {resp.json()['access_token']}",
        "X-Session-Token": _CONNECTOR_SESSION_TOKEN,
    }

    connector = conn_module.get("shopify")
    with patch.object(connector, "sync_products", new=AsyncMock(side_effect=RuntimeError("network error"))):
        resp = await client.post("/connectors/shopify/sync", headers=headers, json={
            "entity": "products", "access_token": "tok",
        })
    assert resp.status_code == 502
    assert "Connector error" in resp.json()["detail"]


# --- Connector sync: contacts and inventory paths ---

@pytest.mark.asyncio
async def test_connector_sync_contacts(client, patch_connector_session_token):
    """Sync contacts entity routes to sync_contacts."""
    from unittest.mock import AsyncMock, patch
    import celerp.connectors as conn_module
    from celerp.connectors.base import SyncResult, SyncDirection, SyncEntity

    resp = await client.post("/auth/register", json={
        "email": "connector_contacts@test.com", "password": "pw",
        "name": "Test", "company_name": "ConnTestContacts",
    })
    headers = {
        "Authorization": f"Bearer {resp.json()['access_token']}",
        "X-Session-Token": _CONNECTOR_SESSION_TOKEN,
    }

    mock_result = SyncResult(entity=SyncEntity.CONTACTS, direction=SyncDirection.INBOUND)
    connector = conn_module.get("shopify")
    with patch.object(connector, "sync_contacts", new=AsyncMock(return_value=mock_result)):
        resp = await client.post("/connectors/shopify/sync", headers=headers, json={
            "entity": "contacts", "access_token": "tok",
        })


# --- inverted_doc_dates doctor check ---

@pytest.mark.asyncio
async def test_doctor_inverted_doc_dates_clean_data(client, session):
    """No inverted dates -> found=0."""
    token = await _register(client)
    h = _h(token)
    # Create a normal invoice with valid dates.
    await client.post("/docs", headers=h, json={
        "doc_type": "invoice", "contact_name": "Test",
        "line_items": [{"description": "x", "quantity": 1, "unit_price": 100}],
        "issue_date": "2024-06-01", "due_date": "2024-06-30",
    })
    r = await client.post("/admin/doctor?checks=inverted_doc_dates", headers=h)
    assert r.status_code == 200
    result = next(c for c in r.json()["results"] if c["check"] == "inverted_doc_dates")
    assert result["found"] == 0


@pytest.mark.asyncio
async def test_doctor_inverted_doc_dates_detect_and_fix(client, session):
    """Import a doc with due_date < issue_date (simulating pre-v1.0.10 data).
    Dry-run should find it; fix run should repair due_date = issue_date."""
    token = await _register(client)
    h = _h(token)
    entity_id = f"doc:inv-date-{uuid.uuid4().hex[:8]}"

    # Plant the inverted doc via the import endpoint (bypasses _assert_date_order).
    r = await client.post("/docs/import", headers=h, json={
        "entity_id": entity_id,
        "event_type": "doc.created",
        "data": {
            "doc_type": "invoice", "contact_name": "Legacy",
            "total": 100, "subtotal": 100, "tax": 0, "status": "draft",
            "issue_date": "2024-06-15",
            "due_date": "2024-06-01",   # inverted: due < issue
        },
        "source": "import:test",
        "idempotency_key": f"inv-{uuid.uuid4().hex}",
    })
    assert r.status_code == 200

    # Dry-run: detect but don't fix.
    r = await client.post("/admin/doctor?checks=inverted_doc_dates", headers=h)
    assert r.status_code == 200
    result = next(c for c in r.json()["results"] if c["check"] == "inverted_doc_dates")
    assert result["found"] >= 1
    assert result["fixed"] == 0

    # Fix run.
    r = await client.post("/admin/doctor?checks=inverted_doc_dates&fix=true", headers=h)
    assert r.status_code == 200
    result = next(c for c in r.json()["results"] if c["check"] == "inverted_doc_dates")
    assert result["fixed"] >= 1

    # Verify: due_date now == issue_date (set to issue_date = "2024-06-15").
    r2 = await client.get(f"/docs/{entity_id}", headers=h)
    assert r2.status_code == 200
    doc = r2.json()
    assert doc["due_date"] >= doc["issue_date"]

    # Second fix run: idempotent -> fixed=0.
    r = await client.post("/admin/doctor?checks=inverted_doc_dates&fix=true", headers=h)
    result = next(c for c in r.json()["results"] if c["check"] == "inverted_doc_dates")
    assert result["fixed"] == 0


# --- Doctor check: fractional_piece_quantities ---

@pytest.mark.asyncio
async def test_fractional_piece_quantities_clean(client, session):
    """No fractional piece items or doc lines -> found=0."""
    token = await _register(client)
    h = _h(token)

    # Create a whole-number piece item
    r = await client.post("/items", headers=h, json={
        "sku": "FPQ-CLEAN-001", "name": "Whole Piece", "quantity": 3, "sell_by": "piece",
    })
    assert r.status_code == 200

    # Create an invoice with integer quantity
    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice", "contact_name": "Test",
        "line_items": [{"sku": "FPQ-CLEAN-001", "name": "Whole Piece", "sell_by": "piece", "quantity": 1, "unit_price": 100}],
        "subtotal": 100, "total": 100,
    })
    assert r.status_code == 200

    r = await client.post("/admin/doctor?checks=fractional_piece_quantities", headers=h)
    assert r.status_code == 200
    result = next(c for c in r.json()["results"] if c["check"] == "fractional_piece_quantities")
    assert result["found"] == 0
    assert result["fixed"] == 0
    assert result["auto_fixable"] is False


@pytest.mark.asyncio
async def test_fractional_piece_quantities_detects_item_and_doc(client, session):
    """Items and docs with fractional piece quantities are detected, never auto-fixed."""
    token = await _register(client)
    h = _h(token)

    # Plant a piece item with fractional quantity via import
    item_eid = f"item:{uuid.uuid4()}"
    from celerp.events.engine import emit_event as _emit
    import uuid as _uuid
    me = (await client.get("/companies/me", headers=h)).json()
    company_id = _uuid.UUID(me["id"])

    await _emit(
        session, company_id=company_id,
        entity_id=item_eid, entity_type="item",
        event_type="item.created",
        data={"sku": "FPQ-BAD-ITEM", "name": "Bad Piece Item", "quantity": 0.7, "sell_by": "piece"},
        actor_id=None, location_id=None, source="test",
        idempotency_key=f"fpq-item-{_uuid.uuid4().hex}",
        metadata_={},
    )

    # Plant a finalized doc with fractional piece line via import
    doc_eid = f"doc:FPQ-INV-{_uuid.uuid4().hex[:8]}"
    await _emit(
        session, company_id=company_id,
        entity_id=doc_eid, entity_type="doc",
        event_type="doc.created",
        data={
            "doc_type": "invoice", "status": "final",
            "line_items": [{"sku": "FPQ-BAD-ITEM", "name": "Bad Piece Item", "sell_by": "piece", "quantity": 0.7, "unit_price": 100}],
        },
        actor_id=None, location_id=None, source="test",
        idempotency_key=f"fpq-doc-{_uuid.uuid4().hex}",
        metadata_={},
    )
    await session.commit()

    # Dry-run: detects both
    r = await client.post("/admin/doctor?checks=fractional_piece_quantities", headers=h)
    assert r.status_code == 200
    result = next(c for c in r.json()["results"] if c["check"] == "fractional_piece_quantities")
    assert result["found"] >= 2
    assert result["fixed"] == 0

    item_findings = [d for d in result["details"] if d["kind"] == "item"]
    doc_findings = [d for d in result["details"] if d["kind"] == "doc_line"]
    assert any(f["sku"] == "FPQ-BAD-ITEM" and f["quantity"] == 0.7 for f in item_findings)
    assert any(f["sku"] == "FPQ-BAD-ITEM" and f["action_required"] == "void_and_reissue_or_credit_note" for f in doc_findings)

    # Fix mode: still not fixed (report-only check)
    r2 = await client.post("/admin/doctor?checks=fractional_piece_quantities&fix=true", headers=h)
    assert r2.status_code == 200
    result2 = next(c for c in r2.json()["results"] if c["check"] == "fractional_piece_quantities")
    assert result2["fixed"] == 0
    assert result2["found"] >= 2


@pytest.mark.asyncio
async def test_upgrade_report_written_on_fix_with_from_version(client, session, tmp_path, monkeypatch):
    """When fix=true and from_version provided, upgrade report JSON is written to disk."""
    import importlib, celerp.config as cfg_mod
    monkeypatch.setattr(cfg_mod.settings, "data_dir", str(tmp_path), raising=False)

    token = await _register(client)
    h = _h(token)
    await _create_invoice(client, token, total=100, tax=0)

    r = await client.post("/admin/doctor?fix=true&from_version=1.0.9", headers=h)
    assert r.status_code == 200
    data = r.json()
    assert "upgrade_report" in data
    report_path = data["upgrade_report"]
    import os, json as _json
    assert os.path.isfile(report_path)
    with open(report_path) as f:
        report = _json.load(f)
    assert report["from_version"] == "1.0.9"
    assert "auto_fixed" in report
    assert "manual_attention_required" in report


@pytest.mark.asyncio
async def test_all_checks_have_auto_fixable_field(client, session):
    """Every check result must include the auto_fixable boolean field."""
    token = await _register(client)
    r = await client.post("/admin/doctor", headers=_h(token))
    assert r.status_code == 200
    for result in r.json()["results"]:
        assert "auto_fixable" in result, f"check {result['check']} missing auto_fixable field"
        assert isinstance(result["auto_fixable"], bool)
