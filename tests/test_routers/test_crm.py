# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import os

import pytest

_crm_skip = pytest.mark.skipif(
    not os.path.isdir(os.path.join(os.path.dirname(__file__), "..", "..", "premium_modules", "celerp-sales-funnel")),
    reason="celerp-sales-funnel not installed",
)


@pytest.mark.asyncio
async def test_crm_batch_import_contacts_idempotent(client):
    headers = await _headers(client)

    payload = {
        "records": [
            {
                "entity_id": "contact:qa-ada",
                "event_type": "crm.contact.created",
                "data": {"name": "Ada Lovelace", "email": "ada@example.com"},
                "source": "csv",
                "idempotency_key": "qa-crm-contact-ada-1",
            }
        ]
    }

    r1 = await client.post("/crm/contacts/import/batch", json=payload, headers=headers)
    assert r1.status_code == 200
    assert r1.json() == {"created": 1, "skipped": 0, "updated": 0, "errors": []}

    r2 = await client.post("/crm/contacts/import/batch", json=payload, headers=headers)
    assert r2.status_code == 200
    assert r2.json() == {"created": 0, "skipped": 1, "updated": 0, "errors": []}


async def _headers(client) -> dict:
    reg = await client.post(
        "/auth/register",
        json={"company_name": "Acme", "email": "admin@acme.com", "name": "Admin", "password": "pw"},
    )
    token = reg.json()["access_token"]
    companies = await client.get("/auth/my-companies", headers={"Authorization": f"Bearer {token}"})
    company_id = companies.json()["items"][0]["company_id"]
    return {"Authorization": f"Bearer {token}", "X-Company-Id": company_id}


@_crm_skip
@pytest.mark.asyncio
async def test_crm_contacts_deals_memos(client):
    headers = await _headers(client)

    r = await client.post("/crm/contacts", json={"name": "Bob"}, headers=headers)
    assert r.status_code == 200
    cid = r.json()["id"]

    r = await client.get("/crm/contacts", headers=headers)
    assert r.status_code == 200
    data = r.json()
    items = data["items"] if isinstance(data, dict) else data
    assert any(c["id"] == cid for c in items)

    r = await client.get(f"/crm/contacts/{cid}", headers=headers)
    assert r.status_code == 200

    r = await client.patch(
        f"/crm/contacts/{cid}",
        json={"fields_changed": {"email": {"old": None, "new": "b@c.com"}}},
        headers=headers,
    )
    assert r.status_code == 200

    r = await client.post(f"/crm/contacts/{cid}/tags", json={"tags": ["vip"]}, headers=headers)
    assert r.status_code == 200

    r = await client.post(f"/crm/contacts/{cid}/notes", json={"note": "Called and left voicemail"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["id"].startswith("note:")

    r = await client.post("/crm/deals", json={"name": "Deal1", "stage": "lead"}, headers=headers)
    assert r.status_code == 200
    did = r.json()["id"]

    r = await client.get("/crm/deals", headers=headers)
    assert r.status_code == 200
    assert any(d["id"] == did for d in r.json()["items"])

    r = await client.post("/crm/memos", json={"contact_id": cid, "notes": "Memo note"}, headers=headers)
    assert r.status_code == 200
    mid = r.json()["id"]

    r = await client.get("/crm/memos", headers=headers)
    assert r.status_code == 200
    assert any(m["id"] == mid for m in r.json()["items"])


@_crm_skip
@pytest.mark.asyncio
async def test_deal_get_patch_delete_reopen(client):
    """Full lifecycle: get, patch, delete (soft), reopen."""
    headers = await _headers(client)

    # Create
    r = await client.post("/crm/deals", json={"name": "Test Deal", "stage": "qualified", "value": 5000.0}, headers=headers)
    assert r.status_code == 200
    did = r.json()["id"]

    # Get single
    r = await client.get(f"/crm/deals/{did}", headers=headers)
    assert r.status_code == 200
    d = r.json()
    assert d["name"] == "Test Deal"
    assert d["stage"] == "qualified"

    # Patch
    r = await client.patch(f"/crm/deals/{did}", json={"name": "Renamed Deal", "value": 9999.0}, headers=headers)
    assert r.status_code == 200

    # Confirm patch via get
    r = await client.get(f"/crm/deals/{did}", headers=headers)
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed Deal"

    # Soft-delete
    r = await client.delete(f"/crm/deals/{did}", headers=headers)
    assert r.status_code == 200

    # Confirm deleted status
    r = await client.get(f"/crm/deals/{did}", headers=headers)
    assert r.status_code == 200
    assert r.json()["status"] == "deleted"

    # Reopen
    r = await client.post(f"/crm/deals/{did}/reopen", headers=headers)
    assert r.status_code == 200

    # Confirm reopened
    r = await client.get(f"/crm/deals/{did}", headers=headers)
    assert r.status_code == 200
    assert r.json()["status"] == "open"


@_crm_skip
@pytest.mark.asyncio
async def test_deal_won_reopen(client):
    """Won deal can be reopened."""
    headers = await _headers(client)

    r = await client.post("/crm/deals", json={"name": "Won Deal", "stage": "negotiation"}, headers=headers)
    did = r.json()["id"]

    await client.post(f"/crm/deals/{did}/won", headers=headers)

    r = await client.get(f"/crm/deals/{did}", headers=headers)
    assert r.json()["status"] == "won"

    await client.post(f"/crm/deals/{did}/reopen", headers=headers)

    r = await client.get(f"/crm/deals/{did}", headers=headers)
    assert r.json()["status"] == "open"


@_crm_skip
@pytest.mark.asyncio
async def test_deal_get_404(client):
    """GET unknown deal returns 404."""
    headers = await _headers(client)
    r = await client.get("/crm/deals/deal:does-not-exist", headers=headers)
    assert r.status_code == 404
