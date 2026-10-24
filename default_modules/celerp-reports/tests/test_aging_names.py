# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""AR and AP aging name each customer and supplier by the contact's name, never its id."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from celerp.models.projections import Projection

pytestmark = pytest.mark.asyncio


async def _company(client) -> tuple[dict, uuid.UUID]:
    r = await client.post("/auth/register", json={"company_name": "Aging Names Co", "email": f"an-{uuid.uuid4().hex[:8]}@rep.test",
                                                  "name": "Admin", "password": "validpass1"})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    return headers, uuid.UUID((await client.get("/companies/me", headers=headers)).json()["id"])


async def _seed(session, company_id, entity_id: str, entity_type: str, state: dict) -> None:
    session.add(Projection(company_id=company_id, entity_id=entity_id, entity_type=entity_type, version=1,
                           updated_at=datetime.now(timezone.utc), state=state))
    await session.commit()


async def _open_doc(session, company_id, doc_id: str, doc_type: str, contact_id: str) -> None:
    """An issued document awaiting payment that names its contact by id only, as imported
    and demo documents can."""
    await _seed(session, company_id, doc_id, "doc", {
        "doc_type": doc_type, "status": "final", "doc_number": doc_id, "currency": "USD", "contact_id": contact_id,
        "line_items": [], "total": 40, "amount_outstanding": 40, "due_date": "2026-01-01"})


@pytest.mark.parametrize("path,doc_type,party", [("/reports/ar-aging", "invoice", "customer"),
                                                ("/reports/ap-aging", "bill", "supplier")])
async def test_aging_names_the_contact_and_never_shows_its_id(client, session, path, doc_type, party):
    headers, company_id = await _company(client)
    known, gone = f"contact:{uuid.uuid4()}", f"contact:{uuid.uuid4()}"
    await _seed(session, company_id, known, "contact", {"name": "Harbour Supply Co", "contact_type": "both"})
    await _open_doc(session, company_id, "doc:named", doc_type, known)
    await _open_doc(session, company_id, "doc:unknown", doc_type, gone)

    r = await client.get(path, headers=headers)
    assert r.status_code == 200, r.text
    names = {line[f"{party}_id"]: line[f"{party}_name"] for line in r.json()["lines"]}
    assert names == {known: "Harbour Supply Co", gone: "--"}
    assert all(not name.startswith("contact:") for name in names.values())
