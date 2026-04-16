# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import uuid

import pytest

from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.services.auth import create_access_token


@pytest.mark.asyncio
async def test_manufacturing_import_batch_idempotency(client, session):
    company_id = uuid.uuid4()
    user_id = uuid.uuid4()

    session.add(Company(id=company_id, name="TestCo", slug="testco"))
    session.add(User(
        id=user_id,
        company_id=company_id,
        email="admin@test.co",
        name="Admin",
        auth_hash="x",
        role="admin",
        is_active=True,
    ))
    session.add(UserCompany(id=uuid.uuid4(), user_id=user_id, company_id=company_id, role="admin", is_active=True))
    await session.commit()

    token = create_access_token(subject=str(user_id), company_id=str(company_id), role="admin")
    headers = {"Authorization": f"Bearer {token}"}

    entity_id = "bom-test-" + uuid.uuid4().hex[:8]
    payload = {
        "records": [
            {
                "entity_id": entity_id,
                "event_type": "bom.created",
                "data": {"name": "Import BOM", "output_qty": 1.0, "components": []},
                "source": "import",
                "idempotency_key": "bom-created-" + entity_id,
            }
        ]
    }

    r1 = await client.post("/manufacturing/import/batch", headers=headers, json=payload)
    assert r1.status_code == 200, r1.text
    assert r1.json() == {"created": 1, "skipped": 0, "updated": 0, "errors": []}

    r2 = await client.post("/manufacturing/import/batch", headers=headers, json=payload)
    assert r2.status_code == 200, r2.text
    assert r2.json() == {"created": 0, "skipped": 1, "updated": 0, "errors": []}
