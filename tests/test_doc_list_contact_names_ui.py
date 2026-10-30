# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The invoice list a contact's money cards open, and the payments list, show the contact's
name, never its id, for documents that name their contact by id only (as demo and imported
documents do)."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

import pytest

from celerp.models.projections import Projection
from celerp.services.business_time import business_today
from test_helpers import mark_running

pytestmark = pytest.mark.asyncio


async def _seed(session, company_id, entity_id: str, entity_type: str, state: dict) -> None:
    session.add(Projection(company_id=company_id, entity_id=entity_id, entity_type=entity_type, version=1,
                           updated_at=datetime.now(timezone.utc), state=state))
    await session.commit()


def _cells(html: str) -> list[str]:
    return [c.strip() for c in re.findall(r"<td[^>]*>([^<]*)</td>", html)]


async def test_lists_show_the_contact_s_name_for_documents_that_hold_only_its_id(owner_ui, session, monkeypatch):
    mark_running(monkeypatch, "celerp-docs")
    company_id = uuid.UUID((await owner_ui.api.get("/companies/me")).json()["id"])
    today = business_today(None).isoformat()
    contact = f"contact:{uuid.uuid4()}"
    await _seed(session, company_id, contact, "contact", {"name": "Harbour Supply Co", "contact_type": "customer"})
    for n, paid in ((1, 25), (2, 0)):
        await _seed(session, company_id, f"doc:by-id-{n}", "doc", {
            "doc_type": "invoice", "status": "partial" if paid else "final", "doc_number": f"INV-ID-{n}",
            "currency": "USD", "contact_id": contact, "line_items": [], "total": 100, "amount_outstanding": 100 - paid,
            "issue_date": today, "due_date": today,
            "payments": [{"amount": paid, "payment_date": today, "method": "cash", "status": "active", "index": 0}] if paid else []})

    for url in ("/docs?type=invoice", f"/docs?type=invoice&all_issued=1&contact_id={contact}"):
        r = await owner_ui.get(url)
        assert r.status_code == 200, r.text
        cells = _cells(r.text)
        assert cells.count("Harbour Supply Co") == 2, url
        assert "INV-ID-1" in r.text and "INV-ID-2" in r.text, url
        assert not any(c.startswith("contact:") for c in cells), url

    r = await owner_ui.get("/payments")
    assert r.status_code == 200, r.text
    cells = _cells(r.text)
    assert "Harbour Supply Co" in cells
    assert not any(c.startswith("contact:") for c in cells)
    # The contact filter matches the name the list shows.
    r = await owner_ui.get("/payments?contact=harbour")
    assert "INV-ID-1" in r.text
