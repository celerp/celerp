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


@pytest.mark.asyncio
async def test_line_page_patch_deletes_middle_row(client):
    """Delete a row in the MIDDLE of the covered window. After the delete the surviving
    tail rows shift up one position, so the incoming page no longer sits id-for-id over
    the stored rows: incoming[1] carries item:2 while stored[1] still holds item:2's
    old neighbour item:1. A positional id comparison reads that legitimate shift as a
    concurrent edit and rejects the save 409; the slice-splice must accept it, because
    the window it replaces is the whole loaded slice, not a row-by-row overwrite.

    Observable: the save succeeds and the persisted array is exactly the post-delete
    set, read back through the real API - never a status-code-only assertion."""
    t = await _register(client)
    q = await _quotation(client, t)
    lines = [{"item_id": f"item:{i}", "sku": f"SKU{i}", "description": f"Item {i}",
              "quantity": 1, "unit_price": 1.0} for i in range(3)]
    v = await _set_lines(client, t, q, lines)
    before = (await _state(client, t, q))["line_items"]
    assert len(before) == 3

    # The user deletes item:1 (the middle row). The editor resubmits the surviving rows
    # [item:0, item:2] and the loaded window length (original_count == 3).
    page = [dict(before[0]), dict(before[2])]
    r = await client.patch(f"/lists/{q}/line-page", headers=_h(t),
                           json={"line_items": page, "offset": 0,
                                 "original_count": len(before), "expected_version": v})
    assert r.status_code == 200, (
        f"deleting a middle row is a legitimate edit within the loaded window, not a "
        f"concurrent-edit conflict; got {r.status_code}: {r.text}")

    after = (await _state(client, t, q))["line_items"]
    assert _ids(after) == ["item:0", "item:2"], (
        f"the persisted array must be exactly the post-delete set; got {_ids(after)}")
    assert "item:1" not in _ids(after), "the deleted middle row's identity must be gone"


@pytest.mark.asyncio
async def test_line_page_patch_window_cannot_exceed_loaded_array(client):
    """original_count is the length of the window the client loaded, so it can never
    reach past the end of the stored array. A page that claims a window running beyond
    len(stored) would splice away rows the client never loaded and cannot have edited.

    Prod hazard: offset 0 + a large original_count + a one-row page collapses the whole
    list to that single row. The write must be rejected, or at worst leave the
    off-window rows intact - it must NEVER silently drop rows past the loaded window.

    Observable: the rows beyond the claimed window survive, read back through the real
    API."""
    t = await _register(client)
    q = await _quotation(client, t)
    lines = [{"item_id": f"item:{i}", "sku": f"SKU{i}", "description": f"Item {i}",
              "quantity": 1, "unit_price": 1.0} for i in range(3)]
    v = await _set_lines(client, t, q, lines)
    before = (await _state(client, t, q))["line_items"]
    assert len(before) == 3

    # A window that claims to cover 999 rows starting at 0, replaced by a single row.
    page = [dict(before[0])]
    r = await client.patch(f"/lists/{q}/line-page", headers=_h(t),
                           json={"line_items": page, "offset": 0,
                                 "original_count": 999, "expected_version": v})

    after = (await _state(client, t, q))["line_items"]
    if r.status_code == 200:
        assert _ids(after) == ["item:0", "item:1", "item:2"], (
            f"a window past the loaded array must not drop rows the client never "
            f"loaded; persisted identities were {_ids(after)}")
    else:
        assert 400 <= r.status_code < 500, r.text
        assert after == before, (
            f"a rejected over-wide window must write nothing; got {_ids(after)}")


@pytest.mark.asyncio
async def test_line_page_patch_window_cannot_exceed_page_limit(client):
    """A page fetch returns at most the page cap (100 rows), so the client can never have
    loaded a window wider than that. A claimed original_count above the cap - even one that
    still fits INSIDE the stored array - is a forged window that would splice away rows
    beyond the page the client actually read.

    Prod hazard the end-of-array bounds check misses: 150 stored rows, offset 0,
    original_count 149, a one-row page. The window fits the array (149 <= 150) so a pure
    bounds check lets it through, and the splice collapses 148 rows the client could never
    have loaded. The write must be rejected and every off-page row must survive.

    Observable: all 150 rows survive, read back through the real API."""
    t = await _register(client)
    q = await _quotation(client, t)
    lines = [{"item_id": f"item:{i}", "sku": f"SKU{i}", "description": f"Item {i}",
              "quantity": 1, "unit_price": 1.0} for i in range(150)]
    v = await _set_lines(client, t, q, lines)
    before = (await _state(client, t, q))["line_items"]
    assert len(before) == 150

    # A window claiming 149 loaded rows - well past the 100-row page cap - replaced by one row.
    # 149 <= 150, so the end-of-array bounds check alone would accept this and drop 148 rows.
    page = [dict(before[0])]
    r = await client.patch(f"/lists/{q}/line-page", headers=_h(t),
                           json={"line_items": page, "offset": 0,
                                 "original_count": 149, "expected_version": v})

    after = (await _state(client, t, q))["line_items"]
    if r.status_code == 200:
        assert len(after) == 150, (
            f"a window wider than the 100-row page cap must not drop rows the client never "
            f"loaded; persisted {len(after)} rows, identities {_ids(after)}")
    else:
        assert 400 <= r.status_code < 500, r.text
        assert _ids(after) == _ids(before), (
            f"a rejected over-wide window must write nothing; persisted {len(after)} rows")
