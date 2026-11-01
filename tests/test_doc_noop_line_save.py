# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Saving a document's lines unchanged records nothing.

The editor saves the lines before every action (Issue among them) and writes each row with the
editor's neutral values filled in: an empty SKU and unit, no line discount, a zero line rate,
splitting allowed, and a flat header discount of 0. A stored line that never had those fields
means the same thing, so such a save is not an edit: it records no update in the document's
history, and a refused Issue afterwards leaves the history as it was. A list's editor saves the
same way and is held to the same rule.
"""
from __future__ import annotations

import pytest

_LINE = {"description": "Fitting", "quantity": 1, "unit_price": 100.0, "taxes": []}


async def _get(client, auth, doc_id: str) -> dict:
    r = await client.get(f"/docs/{doc_id}", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()


async def _invoice(client, auth) -> dict:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "contact_id": "customer:1", "line_items": [dict(_LINE)]})
    assert r.status_code == 200, r.text
    return await _get(client, auth, r.json()["id"])


def _as_editor_sends(line: dict, **change) -> dict:
    """The row the editor posts for a stored line: every column it renders, neutral when empty."""
    return {"line_id": line["line_id"], "description": line["description"], "sku": "",
            "quantity": 1, "unit": "", "unit_price": 100, "discount_pct": 0, "tax_rate": 0,
            "taxes": [], "line_total": 100, "allow_splitting": True, **change}


async def _save(client, auth, doc: dict, lines: list[dict]):
    return await client.patch(f"/docs/{doc['id']}", headers=auth["headers"], json={
        "expected_version": doc["version"],
        "fields_changed": {"line_items": {"new": lines}, "discount": {"new": 0},
                           "discount_type": {"new": "flat"}, "doc_taxes": {"new": []}}})


@pytest.mark.asyncio
async def test_an_unchanged_editor_save_records_no_update(client, auth):
    doc = await _invoice(client, auth)
    r = await _save(client, auth, doc, [_as_editor_sends(doc["line_items"][0])])
    assert r.status_code == 200, r.text
    assert r.json()["event_id"] is None, r.json()
    assert (await _get(client, auth, doc["id"]))["version"] == doc["version"]


@pytest.mark.asyncio
async def test_a_real_change_in_the_same_shape_is_still_saved(client, auth):
    doc = await _invoice(client, auth)
    r = await _save(client, auth, doc, [_as_editor_sends(doc["line_items"][0], quantity=3, line_total=300)])
    assert r.status_code == 200, r.text
    assert r.json()["event_id"] is not None
    assert (await _get(client, auth, doc["id"]))["line_items"][0]["quantity"] == 3


@pytest.mark.asyncio
async def test_an_unchanged_list_save_records_no_update(client, auth):
    r = await client.post("/lists", headers=auth["headers"], json={
        "list_type": "quotation", "contact_id": "customer:1", "line_items": [dict(_LINE)]})
    assert r.status_code == 200, r.text
    lid = r.json()["id"]
    lst = (await client.get(f"/lists/{lid}", headers=auth["headers"])).json()
    r = await client.patch(f"/lists/{lid}", headers=auth["headers"], json={
        "expected_version": lst["version"],
        "fields_changed": {"line_items": {"new": [_as_editor_sends(lst["line_items"][0])]},
                           "discount_type": {"new": "flat"}}})
    assert r.status_code == 200, r.text
    assert r.json() == {"event_id": None, "version": lst["version"]}


@pytest.mark.asyncio
async def test_a_changed_discount_type_is_stored(client, auth):
    doc = await _invoice(client, auth)
    line = _as_editor_sends(doc["line_items"][0])
    for value, kind, total in ((10, "percentage", 90), (20, "percentage", 80), (25, "flat", 75)):
        r = await client.patch(f"/docs/{doc['id']}", headers=auth["headers"], json={
            "expected_version": doc["version"],
            "fields_changed": {"line_items": {"new": [line]}, "discount": {"old": None, "new": value},
                               "discount_type": {"old": doc.get("discount_type"), "new": kind},
                               "doc_taxes": {"new": []}}})
        assert r.status_code == 200, r.text
        doc = await _get(client, auth, doc["id"])
        assert doc.get("discount_type") == kind and float(doc["total"]) == total, doc
