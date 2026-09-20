# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Canonical entity/tenant invariants on APIs exposed to the AI assistant."""
from __future__ import annotations

import uuid

import pytest

from test_helpers import perm_setup


async def _other_company_location(client, headers: dict) -> str:
    created = await client.post(
        "/companies", json={"name": f"Other {uuid.uuid4().hex[:8]}"}, headers=headers,
    )
    assert created.status_code == 200, created.text
    other = {"Authorization": f"Bearer {created.json()['access_token']}"}
    loc = await client.post(
        "/companies/me/locations",
        json={"name": "Other Warehouse", "type": "warehouse", "address": None, "is_default": True},
        headers=other,
    )
    assert loc.status_code == 200, loc.text
    return loc.json()["id"]


@pytest.mark.asyncio
async def test_document_contact_must_be_live_contact_projection(client, session):
    setup = await perm_setup(client, session)
    headers = setup["admin_h"]
    item_id = setup["item_id"]

    created = await client.post(
        "/docs", json={"doc_type": "invoice", "contact_id": item_id}, headers=headers,
    )
    assert created.status_code == 422, created.text
    assert "Contact" in str(created.json()["detail"])

    doc = await client.post("/docs", json={"doc_type": "invoice"}, headers=headers)
    assert doc.status_code == 200, doc.text
    patched = await client.patch(
        f"/docs/{doc.json()['id']}",
        json={"fields_changed": {"contact_id": {"old": None, "new": item_id}}},
        headers=headers,
    )
    assert patched.status_code == 422, patched.text
    assert "Contact" in str(patched.json()["detail"])


@pytest.mark.asyncio
async def test_item_patch_requires_item_and_company_owned_location(client, session):
    setup = await perm_setup(client, session)
    headers = setup["admin_h"]
    foreign_location = await _other_company_location(client, headers)

    created = await client.post(
        "/crm/contacts", json={"name": "Not an item"}, headers=headers,
    )
    assert created.status_code == 200, created.text
    wrong_type = await client.patch(
        f"/items/{created.json()['id']}",
        json={"fields_changed": {"name": {"old": "Not an item", "new": "Still not an item"}}},
        headers=headers,
    )
    assert wrong_type.status_code == 404, wrong_type.text

    create_foreign = await client.post(
        "/items",
        json={
            "sku": "FOREIGN-LOC-CREATE", "name": "Wrong location", "sell_by": "piece",
            "quantity": 1, "location_id": foreign_location,
        },
        headers=headers,
    )
    assert create_foreign.status_code == 422, create_foreign.text

    patch_foreign = await client.patch(
        f"/items/{setup['item_id']}",
        json={"fields_changed": {"location_id": {"old": setup["location_id"], "new": foreign_location}}},
        headers=headers,
    )
    assert patch_foreign.status_code == 422, patch_foreign.text
