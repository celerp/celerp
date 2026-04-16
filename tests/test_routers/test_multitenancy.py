# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import base64
import json

import pytest


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


async def _bootstrap(client) -> tuple[str, dict]:
    r = await client.post(
        "/auth/register",
        json={"company_name": "Company A", "email": "a@example.com", "name": "Admin", "password": "pw"},
    )
    assert r.status_code == 200
    token = r.json()["access_token"]
    return token, {"Authorization": f"Bearer {token}"}


async def _create_company(client, bootstrap_headers: dict, name: str) -> tuple[str, dict]:
    r = await client.post("/companies", json={"name": name}, headers=bootstrap_headers)
    assert r.status_code == 200
    token = r.json()["access_token"]
    return token, {"Authorization": f"Bearer {token}"}


def _items_from_list_response(payload: object) -> list[dict]:
    if isinstance(payload, dict) and "items" in payload:
        return payload["items"]
    assert isinstance(payload, list)
    return payload


@pytest.mark.asyncio
async def test_cross_company_item_isolation(client):
    _, headers_a_boot = await _bootstrap(client)
    _, headers_b = await _create_company(client, headers_a_boot, "Company B")

    r = await client.post(
        "/items",
        json={"sku": "SKU-A-1", "name": "A item", "quantity": 1, "sell_by": "piece"},
        headers=headers_a_boot,
    )
    assert r.status_code == 200
    item_id = r.json()["id"]

    r = await client.get("/items", headers=headers_b)
    assert r.status_code == 200
    items = _items_from_list_response(r.json())
    assert all(x.get("id") != item_id for x in items)


@pytest.mark.asyncio
async def test_cross_company_doc_isolation_and_write_isolation(client):
    _, headers_a_boot = await _bootstrap(client)
    _, headers_b = await _create_company(client, headers_a_boot, "Company B")

    r = await client.post(
        "/docs",
        json={
            "doc_type": "invoice",
            "ref_id": "INV-B-1",
            "currency": "THB",
            "customer_name": "Cust",
            "line_items": [{"description": "x", "qty": 1, "unit_price": 10.0}],
            "subtotal": 10.0,
            "total": 10.0,
        },
        headers=headers_b,
    )
    assert r.status_code == 200
    doc_id = r.json()["id"]

    r = await client.get("/docs", headers=headers_a_boot)
    assert r.status_code == 200
    docs = _items_from_list_response(r.json())
    assert all(x.get("id") != doc_id for x in docs)

    r = await client.post(f"/docs/{doc_id}/finalize", headers=headers_a_boot)
    assert r.status_code in (404, 403)

    r = await client.post(f"/docs/{doc_id}/void", json={"reason": "nope"}, headers=headers_a_boot)
    assert r.status_code in (404, 403)


@pytest.mark.asyncio
async def test_cross_company_contact_isolation(client):
    _, headers_a_boot = await _bootstrap(client)
    _, headers_b = await _create_company(client, headers_a_boot, "Company B")

    r = await client.post("/crm/contacts", json={"name": "Alice"}, headers=headers_a_boot)
    assert r.status_code == 200
    contact_id = r.json()["id"]

    r = await client.get("/crm/contacts", headers=headers_b)
    assert r.status_code == 200
    contacts = _items_from_list_response(r.json())
    assert all(x.get("id") != contact_id for x in contacts)


@pytest.mark.asyncio
async def test_cross_company_list_isolation(client):
    _, headers_a_boot = await _bootstrap(client)
    _, headers_b = await _create_company(client, headers_a_boot, "Company B")

    r = await client.post(
        "/lists",
        json={"list_type": "quote", "ref_id": "L-A-1", "customer_name": "Cust", "total": 1.0},
        headers=headers_a_boot,
    )
    assert r.status_code == 200
    list_id = r.json()["id"]

    r = await client.get("/lists", headers=headers_b)
    assert r.status_code == 200
    items = _items_from_list_response(r.json())
    assert all(x.get("id") != list_id for x in items)


@pytest.mark.asyncio
async def test_jwt_company_id_tampering_header_does_not_escalate(client):
    token_a, headers_a_boot = await _bootstrap(client)
    _, headers_b = await _create_company(client, headers_a_boot, "Company B")

    r = await client.post(
        "/items",
        json={"sku": "SKU-B-ONLY", "name": "B item", "quantity": 1, "sell_by": "piece"},
        headers=headers_b,
    )
    assert r.status_code == 200
    item_b_id = r.json()["id"]

    # API doesn't use X-Company-Id currently; ensure extra header doesn't allow reading company B.
    headers_tampered = {"Authorization": f"Bearer {token_a}", "X-Company-Id": "tampered"}
    r = await client.get("/items", headers=headers_tampered)
    assert r.status_code == 200
    items = _items_from_list_response(r.json())
    assert all(x.get("id") != item_b_id for x in items)


@pytest.mark.asyncio
async def test_x_company_id_header_enforcement_or_token_fallback(client):
    token_a, headers_a_boot = await _bootstrap(client)

    r = await client.post(
        "/items",
        json={"sku": "SKU-A-ONLY", "name": "A item", "quantity": 1, "sell_by": "piece"},
        headers=headers_a_boot,
    )
    assert r.status_code == 200
    item_id = r.json()["id"]

    headers_no_company = {"Authorization": f"Bearer {token_a}"}
    r = await client.get("/items", headers=headers_no_company)
    assert r.status_code in (200, 400)
    if r.status_code == 200:
        items = _items_from_list_response(r.json())
        assert any(x.get("id") == item_id for x in items)


@pytest.mark.asyncio
async def test_invalid_token_company_id_claim_rejected(client):
    token_a, _ = await _bootstrap(client)

    header_b64, payload_b64, sig_b64 = token_a.split(".")
    payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "==").decode())
    payload["company_id"] = "00000000-0000-0000-0000-000000000000"
    forged = ".".join([header_b64, _b64url(json.dumps(payload).encode()), sig_b64])

    r = await client.get("/items", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401
