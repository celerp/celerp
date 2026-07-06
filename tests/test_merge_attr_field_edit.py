# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Regression test: merging must not drop a category attribute that was set via a FIELD EDIT.

A field edit (`PATCH /items/{id}` with `fields_changed={"grade": ...}`) stores the attribute
**top-level** (`state["grade"]`), because `item.updated` writes generic fields to the top of item
state — only `pieces`/cost are normalized into `attributes`. The merge, however, resolves attribute
conflicts by scanning `attributes` **only**, so it never sees a field-edited value. The key is
therefore never processed at all, which drops it in BOTH cases:

- sources **conflict** (grade A vs B)  -> merged should be "Mixed", is dropped (None);
- sources **agree**    (grade A and A) -> merged should keep "A", is dropped (None).

These tests FAIL against the current merge (value dropped) and pass once the merge reads each
attribute from top-level OR `attributes` and includes schema/top-level keys in its candidate set.

Contrast with tests/test_routers/test_items.py::test_merge_dropdown_fields_use_value_or_mixed_never_sum,
which seeds the attribute *nested* in the create payload — the one path where the merge already sees
it — and so does not exercise this bug.
"""
from __future__ import annotations

import uuid

import pytest


async def _token(client) -> str:
    email = f"attr-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post(
        "/auth/register",
        json={"company_name": "Attr Co", "email": email, "name": "Owner", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _set_grade_schema(client, headers) -> None:
    """Give category 'DD' a 'grade' select attribute so a conflict must resolve to 'Mixed'."""
    r = await client.patch(
        "/companies/me",
        headers=headers,
        json={"settings": {"category_schemas": {"DD": [
            {"key": "grade", "label": "Grade", "type": "select",
             "options": ["A", "B", "C"], "editable": True, "required": False},
        ]}}},
    )
    assert r.status_code == 200, r.text


async def _mk_item_then_edit_grade(client, headers, sku: str, grade: str) -> str:
    """Create an item WITHOUT grade, then set grade via a FIELD EDIT so it lands TOP-LEVEL
    (state['grade']), exactly like normal UI usage — not nested under attributes."""
    r = await client.post(
        "/items",
        json={"sku": sku, "name": sku, "quantity": 1, "sell_by": "piece", "category": "DD"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    eid = r.json()["id"]
    r2 = await client.patch(
        f"/items/{eid}",
        json={"fields_changed": {"grade": {"old": None, "new": grade}}},
        headers=headers,
    )
    assert r2.status_code == 200, r2.text
    return eid


def _read_grade(item: dict):
    """Flattened read — grade may sit top-level or under attributes."""
    return (item.get("attributes") or {}).get("grade", item.get("grade"))


async def _merge(client, headers, sources: list[str], target: str) -> dict:
    r = await client.post(
        "/items/merge",
        json={"source_entity_ids": sources, "target_sku_from": target},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    merged = (await client.get(f"/items/{r.json()['id']}", headers=headers)).json()
    return merged


@pytest.mark.asyncio
async def test_merge_conflicting_field_edited_attr_becomes_mixed(client) -> None:
    """grade A vs grade B, both set via field edit -> merged grade must be 'Mixed' (issue: it is dropped)."""
    headers = {"Authorization": f"Bearer {await _token(client)}"}
    await _set_grade_schema(client, headers)
    a = await _mk_item_then_edit_grade(client, headers, "GA", "A")
    b = await _mk_item_then_edit_grade(client, headers, "GB", "B")

    # Sanity: each source really shows its grade (proves the value was set, just top-level).
    ia = (await client.get(f"/items/{a}", headers=headers)).json()
    ib = (await client.get(f"/items/{b}", headers=headers)).json()
    assert _read_grade(ia) == "A" and _read_grade(ib) == "B", (_read_grade(ia), _read_grade(ib))

    merged = await _merge(client, headers, [a, b], a)
    assert _read_grade(merged) == "Mixed", (
        f"conflicting field-edited attr must merge to 'Mixed', got {_read_grade(merged)!r}"
    )


@pytest.mark.asyncio
async def test_merge_agreeing_field_edited_attr_is_kept(client) -> None:
    """grade A and grade A, both set via field edit -> merged grade must stay 'A' (issue: it is dropped)."""
    headers = {"Authorization": f"Bearer {await _token(client)}"}
    await _set_grade_schema(client, headers)
    a = await _mk_item_then_edit_grade(client, headers, "GA", "A")
    b = await _mk_item_then_edit_grade(client, headers, "GB", "A")

    merged = await _merge(client, headers, [a, b], a)
    assert _read_grade(merged) == "A", (
        f"agreeing field-edited attr must be kept ('A'), got {_read_grade(merged)!r}"
    )