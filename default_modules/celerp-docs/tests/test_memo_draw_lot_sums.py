# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""What a memo line has out is the exact sum of its lots in the line's unit. After Back to memo a
carat line holds 0.1 and 0.2 in two lots: it shows 0.3 out, and every way of invoicing the rest
(Invoice all, a draw naming no quantity, the 0.3 typed) takes it whole, the last draw billing
exactly what the memo line has not billed yet."""
from __future__ import annotations

import uuid

import pytest

from memo_draw_support import auth, carat_line_in_two_lots, draw, drawn_lines, ok, state

pytestmark = pytest.mark.asyncio


async def _invoice_rest(client, h, memo: str, lid: str, how: str) -> str:
    if how == "invoice_all":
        r = await client.post(f"/docs/{memo}/convert", headers=h, json={"idempotency_key": uuid.uuid4().hex})
    elif how == "default_quantity":
        r = await draw(client, h, memo, line_ids=[lid])
    else:
        r = await draw(client, h, memo, {lid: 0.3})
    return (await ok(r))["target_doc_id"]


@pytest.mark.parametrize("how", ["invoice_all", "default_quantity", "typed"])
async def test_the_rest_of_a_line_out_in_two_lots_invoices_whole(client, session, how):
    h = await auth(session)
    memo, lid, first = await carat_line_in_two_lots(client, h)
    line = (await state(client, h, memo))["line_items"][0]
    assert line["out_quantity"] == 0.3

    last = await _invoice_rest(client, h, memo, lid, how)

    billed = [li for inv in (first, last) for li in drawn_lines(await state(client, h, inv), memo)]
    assert sum(li["quantity"] for li in billed) == pytest.approx(0.6)
    assert round(sum(li["line_total"] for li in billed), 2) == line["line_total"] == 60.21
    assert not (await state(client, h, memo))["line_items"][0].get("out_quantity")
