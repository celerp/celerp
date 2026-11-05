# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An issued invoice keeps every line. Its issue posted its total, and a payment collects
that total, so a save that leaves a line out is refused and names Revert to Draft, which
voids the entry and posts the new total on the next issue. A line that shipped and came
back is refused the same way: no invoice is left holding goods none of its lines name."""
from __future__ import annotations

import pytest

from test_invoice_unshipped_books import _ok
from test_reissued_invoice_cogs import _invoice, _line_ids, _ship, _state, _stones

pytestmark = pytest.mark.asyncio


async def _drop_last(client, auth, doc: str):
    st = await _state(client, auth, doc)
    return await client.patch(f"/docs/{doc}", headers=auth["headers"], json={
        "fields_changed": {"line_items": {"old": st["line_items"], "new": st["line_items"][:-1]}},
        "expected_version": st["version"]})


async def _assert_kept(client, auth, doc: str, r) -> None:
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "docs.edit_locked"
    st = await _state(client, auth, doc)
    assert (len(st["line_items"]), st["total"]) == (2, 200.0)


@pytest.mark.parametrize("history", ["none", "returned"])
async def test_an_issued_invoice_refuses_a_save_that_leaves_a_line_out(client, session, auth, history):
    doc = await _invoice(client, auth, (await _stones(client, auth))[:2])
    if history == "returned":
        second = (await _line_ids(client, auth, doc))[1]
        assert (await _ship(client, auth, doc, [second])).status_code == 200
        await _ok(client, auth, f"/docs/{doc}/set-available", {"line_ids": [second]})
    await _assert_kept(client, auth, doc, await _drop_last(client, auth, doc))


async def test_revert_to_draft_is_the_way_to_remove_a_line(client, session, auth):
    doc = await _invoice(client, auth, (await _stones(client, auth))[:2])
    await _ok(client, auth, f"/docs/{doc}/revert-to-draft")
    r = await _drop_last(client, auth, doc)
    assert r.status_code == 200, r.text
    await _ok(client, auth, f"/docs/{doc}/finalize")
    st = await _state(client, auth, doc)
    assert (len(st["line_items"]), st["total"]) == (1, 100.0)
