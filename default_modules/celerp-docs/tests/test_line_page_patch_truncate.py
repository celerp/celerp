# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Line-page PATCH must be a slice-splice, not a positional overwrite/append.

Prod incident: deleting a line inside a page (the incoming page carries fewer rows
than the stored window it covers) left the tail rows intact - the server overwrote
by position and never truncated, so deleted lines came back as phantom rows on
reload. The line-page path also skipped the draft-item guard the full save applies,
so a draft item could be smuggled onto a list through it.

These assert the observable persisted line_items after the write, read back through
the real API - never a SQL surface property."""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@lptrunc.test"
    r = await client.post("/auth/register", json={
        "company_name": "LinePage Co", "email": addr, "name": "A", "password": "pw"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _quotation(client, t) -> str:
    r = await client.post("/lists", headers=_h(t), json={"list_type": "quotation"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _state(client, t, list_id) -> dict:
    return (await client.get(f"/lists/{list_id}", headers=_h(t))).json()


async def _version(client, t, list_id) -> int:
    return (await client.get(f"/lists/{list_id}", headers=_h(t))).json()["version"]


async def _set_lines(client, t, list_id, lines: list[dict]) -> int:
    """Replace the whole line_items array (mandatory version pin) and return the new version."""
    v = await _version(client, t, list_id)
    r = await client.patch(f"/lists/{list_id}", headers=_h(t),
                           json={"fields_changed": {"line_items": {"new": lines}},
                                 "expected_version": v})
    assert r.status_code == 200, r.text
    return r.json()["version"]


def _ids(lines: list[dict]) -> list[str | None]:
    """The stable identity server-side uses for each line (item_id or entity_id)."""
    return [(li.get("item_id") or li.get("entity_id")) for li in lines]


@pytest.mark.asyncio
async def test_line_page_patch_truncates_on_shorter_page(client):
    """Delete the tail row of the covered window: the incoming page carries one FEWER
    row than the stored positions [offset:offset+len(window)] it represents. The
    persisted array must shrink by one and the deleted row's identity must be gone.

    The delete drops the LAST id-bearing row of the window so the surviving rows stay
    positionally aligned (no id-mismatch 409 fires); the red is purely the truncation
    behaviour, not a schema or version error."""
    t = await _register(client)
    q = await _quotation(client, t)
    lines = [{"item_id": f"item:{i}", "sku": f"SKU{i}", "description": f"Item {i}",
              "quantity": 1, "unit_price": 1.0} for i in range(3)]
    v = await _set_lines(client, t, q, lines)
    before = (await _state(client, t, q))["line_items"]
    assert len(before) == 3

    # The whole list is one page; the user deletes item:2 (the tail). The autosave resubmits the
    # surviving rows [item:0, item:1] and, as the real editor does, the length of the window it
    # loaded (original_count == 3) so the server knows the page shrank by one and truncates the tail.
    page = [dict(before[0]), dict(before[1])]
    r = await client.patch(f"/lists/{q}/line-page", headers=_h(t),
                           json={"line_items": page, "offset": 0,
                                 "original_count": len(before), "expected_version": v})
    assert r.status_code == 200, r.text

    after = (await _state(client, t, q))["line_items"]
    assert len(after) == 2, (
        f"deleting a line must shrink the persisted array to 2, got {len(after)}; "
        f"identities {_ids(after)} (the deleted item:2 survived as a phantom row)")
    assert _ids(after) == ["item:0", "item:1"], (
        f"surviving rows must be exactly the post-delete set; got {_ids(after)}")
    assert "item:2" not in _ids(after), "the deleted row's identity must be gone"


@pytest.mark.asyncio
async def test_line_page_patch_rejects_draft_item(client):
    """A line-page PATCH whose page carries a DRAFT item must not land that item on the
    list (the full save applies the draft-item guard; the line-page path must too).
    Observable: the draft's identity is absent from the persisted list, or the request
    is rejected 4xx and writes nothing."""
    t = await _register(client)
    q = await _quotation(client, t)

    # One real (available) starter line so the list has a stored window to patch.
    ok_item = (await client.post("/items", headers=_h(t), json={
        "status": "available", "sku": "OK-1", "name": "Widget",
        "quantity": 5, "sell_by": "piece"})).json()["id"]
    starter = {"item_id": ok_item, "sku": "OK-1", "description": "Widget",
               "quantity": 1, "unit_price": 10.0}
    v = await _set_lines(client, t, q, [starter])

    # A genuine DRAFT item: it is not stock yet and must never reach a list.
    draft_item = (await client.post("/items", headers=_h(t), json={
        "status": "draft", "sku": "DRAFT-1", "name": "Unfinished",
        "quantity": 0, "sell_by": "piece"})).json()["id"]

    before = (await _state(client, t, q))["line_items"]
    # Save a page that appends the draft line onto the list.
    page = [dict(before[0]),
            {"item_id": draft_item, "sku": "DRAFT-1", "description": "Unfinished",
             "quantity": 1, "unit_price": 10.0}]
    r = await client.patch(f"/lists/{q}/line-page", headers=_h(t),
                           json={"line_items": page, "offset": 0, "expected_version": v})

    after = (await _state(client, t, q))["line_items"]
    if r.status_code == 200:
        assert draft_item not in _ids(after), (
            f"a draft item must never persist on a list via the line-page path; "
            f"persisted identities were {_ids(after)}")
    else:
        assert 400 <= r.status_code < 500, r.text
        assert after == before, "a rejected draft save must write nothing"
        assert draft_item not in _ids(after)
