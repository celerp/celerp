# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A document line names one item.

A line can link its item under ``item_id`` or ``entity_id``. A line that carries both with
different values names two items at once: the line rules would judge one while finalize
and fulfil move the other. Every writer refuses such a line, naming it, and stores nothing.
"""
from __future__ import annotations

import pytest

from test_cost_restatement import _item, _state
from stock_books import assert_settled


async def _invoice(client, auth, lines: list[dict]):
    return await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "contact_id": "customer:1", "line_items": lines})


def _line(item_id: str, **extra) -> dict:
    return {"item_id": item_id, "name": "Lot", "quantity": 1, "unit_price": 200.0, **extra}


@pytest.mark.asyncio
async def test_a_new_line_naming_two_items_is_refused(client, session, auth):
    a = await _item(client, auth, 50.0)
    b = await _item(client, auth, 70.0)
    r = await _invoice(client, auth, [_line(a, entity_id=b)])
    assert r.status_code == 422, r.text
    assert "two different items" in r.text


@pytest.mark.asyncio
async def test_an_edit_that_gives_a_line_a_second_item_is_refused_and_changes_nothing(client, session, auth):
    """Red before: the edit was stored, finalize relieved the other item's cost and the
    books stopped matching the stock."""
    a = await _item(client, auth, 50.0)
    b = await _item(client, auth, 70.0)
    r = await _invoice(client, auth, [_line(a)])
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    stored = (await _state(session, auth, doc_id))["line_items"]
    r = await client.patch(f"/docs/{doc_id}", headers=auth["headers"], json={
        "fields_changed": {"line_items": {"old": stored, "new": [dict(stored[0], entity_id=b)]}}})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "conflicting_reference"
    await session.rollback()
    assert (await _state(session, auth, doc_id))["line_items"] == stored
    for path, body in ((f"/docs/{doc_id}/finalize", {}), (f"/docs/{doc_id}/fulfill-lines", {"line_entity_ids": [a]})):
        r = await client.post(path, headers=auth["headers"], json=body)
        assert r.status_code == 200, r.text
    assert (await _state(session, auth, a))["status"] == "sold"
    assert (await _state(session, auth, b))["status"] == "available"
    await assert_settled(client, session, auth)


@pytest.mark.asyncio
async def test_a_line_carrying_the_same_item_under_both_keys_is_accepted(client, session, auth):
    a = await _item(client, auth, 50.0)
    r = await _invoice(client, auth, [_line(a, entity_id=a)])
    assert r.status_code == 200, r.text
