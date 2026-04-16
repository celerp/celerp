# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Tests for celerp-labels module: CRUD, custom dims, PDF generation, barcode/QR, positions."""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _headers(client: AsyncClient, suffix: str = "") -> dict:
    uid = suffix or uuid.uuid4().hex[:8]
    r = await client.post(
        "/auth/register",
        json={
            "email": f"labels_{uid}@example.com",
            "password": "pass1234",
            "name": "Label Tester",
            "company_name": f"LabelCo_{uid}",
        },
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ── Template CRUD ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_template_create_and_list(client: AsyncClient):
    headers = await _headers(client)

    r = await client.post(
        "/api/labels/templates",
        json={"name": "My Tag", "format": "40x30mm", "copies": 2},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["name"] == "My Tag"
    assert data["copies"] == 2
    tid = data["id"]

    r2 = await client.get("/api/labels/templates", headers=headers)
    assert r2.status_code == 200
    items = r2.json()["items"]
    assert any(t["id"] == tid for t in items)


@pytest.mark.asyncio
async def test_template_get_single(client: AsyncClient):
    headers = await _headers(client)

    r = await client.post(
        "/api/labels/templates",
        json={"name": "SingleGet", "format": "62x29mm"},
        headers=headers,
    )
    assert r.status_code == 201
    tid = r.json()["id"]

    r2 = await client.get(f"/api/labels/templates/{tid}", headers=headers)
    assert r2.status_code == 200
    assert r2.json()["id"] == tid
    assert r2.json()["name"] == "SingleGet"


@pytest.mark.asyncio
async def test_template_update(client: AsyncClient):
    headers = await _headers(client)

    r = await client.post(
        "/api/labels/templates",
        json={"name": "Old Name", "format": "40x30mm"},
        headers=headers,
    )
    assert r.status_code == 201
    tid = r.json()["id"]

    r2 = await client.put(
        f"/api/labels/templates/{tid}",
        json={"name": "New Name", "copies": 5},
        headers=headers,
    )
    assert r2.status_code == 200
    data = r2.json()
    assert data["name"] == "New Name"
    assert data["copies"] == 5


@pytest.mark.asyncio
async def test_template_delete(client: AsyncClient):
    headers = await _headers(client)

    r = await client.post(
        "/api/labels/templates",
        json={"name": "ToDelete"},
        headers=headers,
    )
    assert r.status_code == 201
    tid = r.json()["id"]

    r2 = await client.delete(f"/api/labels/templates/{tid}", headers=headers)
    assert r2.status_code == 204

    r3 = await client.get(f"/api/labels/templates/{tid}", headers=headers)
    assert r3.status_code == 404


@pytest.mark.asyncio
async def test_template_not_found(client: AsyncClient):
    headers = await _headers(client)
    fake_id = str(uuid.uuid4())
    r = await client.get(f"/api/labels/templates/{fake_id}", headers=headers)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_company_isolation(client: AsyncClient):
    """Templates are scoped per company."""
    # Bootstrap first company
    r_boot = await client.post(
        "/auth/register",
        json={
            "email": "labels_iso_a@example.com",
            "password": "pass1234",
            "name": "Iso A",
            "company_name": "IsoCoA",
        },
    )
    assert r_boot.status_code == 200, r_boot.text
    h1 = {"Authorization": f"Bearer {r_boot.json()['access_token']}"}

    # Create second company via admin endpoint
    r_b = await client.post("/companies", json={"name": "IsoCoB"}, headers=h1)
    assert r_b.status_code == 200, r_b.text
    h2 = {"Authorization": f"Bearer {r_b.json()['access_token']}"}

    r = await client.post(
        "/api/labels/templates",
        json={"name": "Company A Template"},
        headers=h1,
    )
    assert r.status_code == 201
    tid = r.json()["id"]

    # Company B should not see company A's template
    r2 = await client.get(f"/api/labels/templates/{tid}", headers=h2)
    assert r2.status_code == 404


# ── Custom dimensions ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_custom_dimensions_stored_and_retrieved(client: AsyncClient):
    headers = await _headers(client)

    r = await client.post(
        "/api/labels/templates",
        json={
            "name": "Custom Size",
            "format": "custom",
            "width_mm": 75.5,
            "height_mm": 25.0,
        },
        headers=headers,
    )
    assert r.status_code == 201
    data = r.json()
    assert data["format"] == "custom"
    assert data["width_mm"] == 75.5
    assert data["height_mm"] == 25.0
    tid = data["id"]

    r2 = await client.get(f"/api/labels/templates/{tid}", headers=headers)
    assert r2.status_code == 200
    fetched = r2.json()
    assert fetched["width_mm"] == 75.5
    assert fetched["height_mm"] == 25.0


@pytest.mark.asyncio
async def test_custom_dimensions_update(client: AsyncClient):
    headers = await _headers(client)

    r = await client.post(
        "/api/labels/templates",
        json={"name": "DimUpdate", "format": "40x30mm"},
        headers=headers,
    )
    assert r.status_code == 201
    tid = r.json()["id"]

    r2 = await client.put(
        f"/api/labels/templates/{tid}",
        json={"format": "custom", "width_mm": 100.0, "height_mm": 50.0},
        headers=headers,
    )
    assert r2.status_code == 200
    assert r2.json()["width_mm"] == 100.0
    assert r2.json()["height_mm"] == 50.0


# ── Field position data round-trips ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_field_positions_roundtrip(client: AsyncClient):
    headers = await _headers(client)

    fields = [
        {"key": "name", "label": "Name", "type": "text", "x": 2.0, "y": 3.5, "fontSize": 8.0, "bold": True},
        {"key": "sku", "label": "SKU", "type": "text", "x": 2.0, "y": 12.0},
        {"key": "barcode", "label": "Barcode", "type": "barcode", "x": 2.0, "y": 18.0},
        {"key": "qr_val", "label": "QR", "type": "qr"},
    ]

    r = await client.post(
        "/api/labels/templates",
        json={"name": "PositionTest", "fields": fields},
        headers=headers,
    )
    assert r.status_code == 201
    tid = r.json()["id"]

    r2 = await client.get(f"/api/labels/templates/{tid}", headers=headers)
    assert r2.status_code == 200
    saved_fields = r2.json()["fields"]
    assert len(saved_fields) == 4

    f0 = saved_fields[0]
    assert f0["key"] == "name"
    assert f0["x"] == 2.0
    assert f0["y"] == 3.5
    assert f0["fontSize"] == 8.0
    assert f0["bold"] is True
    assert f0["type"] == "text"

    f2 = saved_fields[2]
    assert f2["type"] == "barcode"

    f3 = saved_fields[3]
    assert f3["type"] == "qr"


# ── PDF generation ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_print_returns_pdf(client: AsyncClient):
    headers = await _headers(client)

    r = await client.post(
        "/api/labels/templates",
        json={"name": "PrintTest", "format": "40x30mm", "copies": 1},
        headers=headers,
    )
    assert r.status_code == 201
    tid = r.json()["id"]

    r2 = await client.post(
        f"/api/labels/print/test-item-001?template_id={tid}",
        headers=headers,
    )
    assert r2.status_code == 200
    assert "pdf" in r2.headers["content-type"]
    pdf_bytes = r2.content
    assert len(pdf_bytes) > 20
    assert pdf_bytes[:4] == b"%PDF"


@pytest.mark.asyncio
async def test_bulk_print_returns_pdf(client: AsyncClient):
    headers = await _headers(client)

    r = await client.post(
        "/api/labels/templates",
        json={"name": "BulkTest", "format": "40x30mm"},
        headers=headers,
    )
    assert r.status_code == 201
    tid = r.json()["id"]

    r2 = await client.post(
        "/api/labels/bulk-print",
        json={"entity_ids": ["item-a", "item-b"], "template_id": tid},
        headers=headers,
    )
    assert r2.status_code == 200
    assert "pdf" in r2.headers["content-type"]
    assert r2.content[:4] == b"%PDF"


@pytest.mark.asyncio
async def test_print_without_template_uses_default(client: AsyncClient):
    """Print with no template falls back to built-in default and still returns PDF."""
    headers = await _headers(client)
    r = await client.post("/api/labels/print/orphan-item", headers=headers)
    assert r.status_code == 200
    assert "pdf" in r.headers["content-type"]


# ── Barcode / QR render size check ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_barcode_field_renders_larger_pdf(client: AsyncClient):
    """A template with a barcode field should produce a larger PDF than a text-only one."""
    headers = await _headers(client)

    r_text = await client.post(
        "/api/labels/templates",
        json={
            "name": "TextOnly",
            "format": "100x50mm",
            "fields": [{"key": "name", "label": "Name", "type": "text"}],
        },
        headers=headers,
    )
    assert r_text.status_code == 201
    t_text_id = r_text.json()["id"]

    r_bc = await client.post(
        "/api/labels/templates",
        json={
            "name": "WithBarcode",
            "format": "100x50mm",
            "fields": [{"key": "sku", "label": "SKU", "type": "barcode"}],
        },
        headers=headers,
    )
    assert r_bc.status_code == 201
    t_bc_id = r_bc.json()["id"]

    pdf_text = (await client.post(
        f"/api/labels/print/test-abc?template_id={t_text_id}", headers=headers
    )).content

    pdf_bc = (await client.post(
        f"/api/labels/print/test-abc?template_id={t_bc_id}", headers=headers
    )).content

    assert pdf_bc[:4] == b"%PDF"
    # Barcode PDF should be noticeably bigger than plain-text PDF
    assert len(pdf_bc) > len(pdf_text)
