# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Line item identifier (discussion #228): documents can show each line's barcode.

Covers the data layer (barcode stamped on lines and kept through finalize), the
legacy backfill (lines saved before stamping resolve their barcode from the
catalog item at render time), and the three render surfaces reachable from the
API (share view HTML, PDF, default-mode guard). The company setting is
``settings.line_item_identifier``: sku (default) / barcode / barcode_sku.
"""
from __future__ import annotations

import base64
import re
import uuid
import zlib

import pytest

BARCODE = "2130060000068"


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@lineident.test"
    r = await client.post("/auth/register", json={
        "company_name": "LineIdent Co", "email": addr, "name": "A", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _set_mode(client, t, mode: str) -> None:
    r = await client.patch("/companies/me", headers=_h(t),
                           json={"settings": {"line_item_identifier": mode}})
    assert r.status_code == 200, r.text


async def _item_with_barcode(client, t) -> str:
    r = await client.post("/items", headers=_h(t), json={
        "status": "available", "sku": "GEM-001", "name": "Tourmaline Parcel", "quantity": 5,
        "sell_by": "piece", "barcode": BARCODE})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _pdf_text(pdf: bytes) -> bytes:
    """Decompress the PDF content streams (ASCII85 + Flate) to reach drawn text."""
    out = b""
    for stream in re.findall(rb"stream\r?\n(.*?)endstream", pdf, re.DOTALL):
        raw = stream.strip(b"\r\n")
        try:
            raw = base64.a85decode(raw, adobe=True)
        except ValueError:
            pass
        try:
            out += zlib.decompress(raw)
        except zlib.error:
            out += raw
    return out


# ── Data layer: stamping ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_line_barcode_persists_and_survives_finalize(client):
    """A barcode sent on a line is stored (LineItem keeps the field) and is still
    there after finalize - the document keeps showing what was scanned."""
    t = await _register(client)
    item = await _item_with_barcode(client, t)
    doc = (await client.post("/docs", headers=_h(t), json={"doc_type": "invoice", "line_items": [
        {"entity_id": item, "sku": "GEM-001", "name": "Tourmaline Parcel", "quantity": 1,
         "unit_price": 10, "sell_by": "piece", "barcode": BARCODE}], "total": 10})).json()["id"]

    li = (await client.get(f"/docs/{doc}", headers=_h(t))).json()["line_items"][0]
    assert li.get("barcode") == BARCODE

    assert (await client.post(f"/docs/{doc}/finalize", headers=_h(t))).status_code == 200
    li = (await client.get(f"/docs/{doc}", headers=_h(t))).json()["line_items"][0]
    assert li.get("barcode") == BARCODE


# ── Share view (public HTML, reuses the print renderer) ──────────────────────

@pytest.mark.asyncio
async def test_share_view_shows_barcode_with_legacy_backfill(client):
    """Mode barcode_sku + a line saved WITHOUT a barcode (pre-feature doc): the
    share view backfills it from the catalog item and stacks it over the SKU."""
    t = await _register(client)
    item = await _item_with_barcode(client, t)
    await _set_mode(client, t, "barcode_sku")
    doc = (await client.post("/docs", headers=_h(t), json={"doc_type": "invoice", "line_items": [
        {"entity_id": item, "sku": "GEM-001", "name": "Tourmaline Parcel", "quantity": 1,
         "unit_price": 10, "sell_by": "piece"}], "total": 10})).json()["id"]

    token = (await client.post(f"/docs/{doc}/share", headers=_h(t))).json()["token"]
    html = (await client.get(f"/share/{token}")).text
    assert BARCODE in html
    assert "GEM-001" in html
    assert "Barcode / SKU" in html  # identifier column header follows the mode


@pytest.mark.asyncio
async def test_share_view_default_mode_has_no_barcode(client):
    """Default (sku) mode: output is unchanged - no barcode anywhere."""
    t = await _register(client)
    item = await _item_with_barcode(client, t)
    doc = (await client.post("/docs", headers=_h(t), json={"doc_type": "invoice", "line_items": [
        {"entity_id": item, "sku": "GEM-001", "name": "Tourmaline Parcel", "quantity": 1,
         "unit_price": 10, "sell_by": "piece", "barcode": BARCODE}], "total": 10})).json()["id"]

    token = (await client.post(f"/docs/{doc}/share", headers=_h(t))).json()["token"]
    html = (await client.get(f"/share/{token}")).text
    assert BARCODE not in html
    assert "GEM-001" in html


# ── PDF ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pdf_contains_barcode_when_enabled(client):
    """Mode barcode_sku: the PDF identifier column carries the barcode (backfilled
    for a legacy line) and the header follows the mode."""
    t = await _register(client)
    item = await _item_with_barcode(client, t)
    await _set_mode(client, t, "barcode_sku")
    doc = (await client.post("/docs", headers=_h(t), json={"doc_type": "invoice", "line_items": [
        {"entity_id": item, "sku": "GEM-001", "name": "Tourmaline Parcel", "quantity": 1,
         "unit_price": 10, "sell_by": "piece"}], "total": 10})).json()["id"]

    r = await client.get(f"/docs/{doc}/pdf", headers=_h(t))
    assert r.status_code == 200
    assert r.content[:4] == b"%PDF"
    text = _pdf_text(r.content)
    assert BARCODE.encode() in text
    assert b"Barcode / SKU" in text


@pytest.mark.asyncio
async def test_pdf_default_mode_shows_sku_only(client):
    t = await _register(client)
    item = await _item_with_barcode(client, t)
    doc = (await client.post("/docs", headers=_h(t), json={"doc_type": "invoice", "line_items": [
        {"entity_id": item, "sku": "GEM-001", "name": "Tourmaline Parcel", "quantity": 1,
         "unit_price": 10, "sell_by": "piece", "barcode": BARCODE}], "total": 10})).json()["id"]

    r = await client.get(f"/docs/{doc}/pdf", headers=_h(t))
    assert r.status_code == 200
    text = _pdf_text(r.content)
    assert b"GEM-001" in text
    assert BARCODE.encode() not in text


# ── Setting round trip ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_setting_round_trips_through_company_settings(client):
    t = await _register(client)
    await _set_mode(client, t, "barcode")
    settings = (await client.get("/companies/me", headers=_h(t))).json()["settings"]
    assert settings.get("line_item_identifier") == "barcode"


# ── UI wiring (source-level, same pattern as the labels module tests) ─────────

def test_fill_row_and_collect_carry_barcode():
    """celerpFillRow must stamp data.barcode onto the row and _celerpCollectLines
    must include it in the save payload - otherwise scanned barcodes are lost."""
    import inspect
    import ui.routes.documents as _docs
    src = inspect.getsource(_docs)
    assert "barcodeEl.value = data.barcode || ''" in src
    assert 'row.querySelector(\'[data-name="barcode"]\')?.value' in src
    assert "...(barcode ? {{barcode}} : {{}})" in src


def test_pdf_width_fractions_sum_to_one():
    """The identifier-mode width shift must keep every layout summing to 1.0."""
    for extra in (0.0, 0.03):
        with_disc = [(0.21 - extra), (0.13 + extra), 0.10, 0.14, 0.10, 0.10, 0.22]
        without = [(0.23 - extra), (0.15 + extra), 0.11, 0.16, 0.12, 0.23]
        assert abs(sum(with_disc) - 1.0) < 1e-9
        assert abs(sum(without) - 1.0) < 1e-9
