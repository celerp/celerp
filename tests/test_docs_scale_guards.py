# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""One oversized document or import must not degrade the system for everyone
else: the PDF render runs off the event loop, and the docs/lists batch import
endpoints bound one request's record count the same way the items importer does."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

import pytest

from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.models.projections import Projection
from celerp.services.auth import create_access_token


async def _company_admin(session):
    company_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session.add(Company(id=company_id, name="TestCo", slug="testco"))
    session.add(User(
        id=user_id,
        email=f"admin-{uuid.uuid4().hex[:8]}",  # synthetic; deliberately not email-shaped
        name="Admin",
        auth_hash="x",
        is_active=True,
    ))
    await session.flush()  # parents before membership for Postgres FK checks
    session.add(UserCompany(id=uuid.uuid4(), user_id=user_id, company_id=company_id, role="admin", is_active=True))
    await session.commit()
    token, _ = create_access_token(subject=str(user_id), company_id=str(company_id), role="admin")
    return company_id, {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_doc_pdf_renders_off_the_event_loop(client, session, monkeypatch):
    """The reportlab render is CPU-bound Python; run on the event-loop thread it
    stalls every concurrent request for the duration of a large document's
    render. The route must hand it to a worker thread."""
    company_id, headers = await _company_admin(session)
    entity_id = "doc-pdf-" + uuid.uuid4().hex[:8]
    session.add(Projection(
        company_id=company_id,
        entity_id=entity_id,
        entity_type="doc",
        state={"doc_type": "invoice", "status": "draft", "doc_number": "PDF-1", "line_items": []},
        version=1,
        updated_at=datetime.now(timezone.utc),
    ))
    await session.commit()

    render_thread: dict[str, int] = {}

    def fake_render(doc, company=None, import_url=None):
        render_thread["ident"] = threading.get_ident()
        return b"%PDF-1.4 fake"

    import celerp.output.pdf as pdf_mod
    monkeypatch.setattr(pdf_mod, "generate_document_pdf", fake_render)

    resp = await client.get(f"/docs/{entity_id}/pdf", headers=headers)
    assert resp.status_code == 200
    assert resp.content == b"%PDF-1.4 fake"
    assert render_thread["ident"] != threading.get_ident()


def _import_records(n: int) -> list[dict]:
    return [
        {
            "entity_id": f"doc-bulk-{i}",
            "event_type": "doc.created",
            "data": {
                "doc_type": "invoice",
                "status": "draft",
                "doc_number": f"BULK-{i}",
                "date": "2026-08-03",
                "contact_name": "Bulk Test",
                "currency": "USD",
                "total": 0,
                "line_items": [],
            },
            "source": "test",
            "idempotency_key": f"bulk-{i}",
        }
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_docs_batch_import_bounds_request_size(client, session):
    """The docs batch importer accepts at most 500 records per request, the same
    bound the items importer enforces; the import client already chunks at 500,
    so no legitimate request exceeds it. 501 records is a validation error, not
    an unbounded unit of work."""
    _, headers = await _company_admin(session)
    resp = await client.post(
        "/docs/import/batch",
        headers=headers,
        json={"records": _import_records(501)},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_lists_batch_import_bounds_request_size(client, session):
    """The lists batch importer shares the docs request model, so it carries the
    same 500-record bound."""
    _, headers = await _company_admin(session)
    records = _import_records(501)
    for r in records:
        r["event_type"] = "list.created"
        r["data"] = {"list_type": "packing", "status": "draft"}
    resp = await client.post(
        "/lists/import/batch",
        headers=headers,
        json={"records": records},
    )
    assert resp.status_code == 422
