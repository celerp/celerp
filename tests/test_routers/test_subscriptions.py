# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_subscription_generate_creates_invoice_with_lines_and_total(client):
    reg = await client.post(
        "/auth/register",
        json={"company_name": "Acme Inc", "email": "a@b.com", "name": "Admin", "password": "pw"},
    )
    token = reg.json()["access_token"]

    # Token embeds company_id; fetch it by listing my-companies.
    companies = await client.get("/auth/my-companies", headers={"Authorization": f"Bearer {token}"})
    assert companies.status_code == 200, companies.text
    company_id = companies.json()["items"][0]["company_id"]
    headers = {"Authorization": f"Bearer {token}", "X-Company-Id": company_id}

    # Create subscription with a single line item.
    sub = await client.post(
        "/subscriptions",
        headers=headers,
        json={
            "name": "Monthly retainer",
            "contact_id": None,
            "doc_type": "invoice",
            "frequency": "monthly",
            "start_date": "2026-01-01",
            "line_items": [{"description": "Service", "quantity": 2, "unit_price": 1000}],
            "shipping": 0,
            "discount": 0,
            "tax": 0,
        },
    )
    assert sub.status_code == 200, sub.text
    sub_id = sub.json()["id"]

    gen = await client.post(f"/subscriptions/{sub_id}/generate", headers=headers)
    assert gen.status_code == 200, gen.text
    doc_id = gen.json()["doc_id"]

    doc = await client.get(f"/docs/{doc_id}", headers=headers)
    assert doc.status_code == 200, doc.text
    body = doc.json()

    assert body.get("doc_type") == "invoice"
    assert body.get("status") == "draft"

    assert body.get("line_items")
    assert body["line_items"][0]["description"] == "Service"
    assert body["line_items"][0]["quantity"] == 2
    assert body["line_items"][0]["unit_price"] == 1000

    assert body.get("total") == 2000
    assert body.get("amount_outstanding") == 2000
