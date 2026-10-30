# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""What other parts of Celerp read from an invoice drawn from a memo: the draft list by
currency, unshipped demand, and cost of sales, which is booked only when the invoice is
finalized and only once per finalize."""
from __future__ import annotations

import pytest

from celerp.models.projections import Projection
from celerp.services.fulfill import outstanding_physical_lines
from memo_draw_support import auth, auth_full, back_to_memo, draft, drawn, drawn_lines, memo_out, ok, service_line, state

pytestmark = pytest.mark.asyncio


async def test_list_docs_filters_by_currency(client, session):
    h = await auth(session)
    ids = {cur: await draft(client, h, lines=[service_line(5.0)], currency=cur) for cur in ("USD", "EUR")}

    body = await ok(await client.get("/docs", headers=h, params={"doc_type": "invoice", "status": "draft", "currency": "EUR"}))

    rows = body if isinstance(body, list) else body.get("items") or body.get("docs") or []
    listed = {row.get("id") or row.get("entity_id") for row in rows}
    assert ids["EUR"] in listed
    assert ids["USD"] not in listed


async def test_drawn_lines_skip_outstanding_physical_lines(client, session):
    a = await auth_full(session)
    h = a["headers"]
    memo, lids, _ = await memo_out(client, h, [(2, 50.0)], lots_per_line=2)
    pf = await drawn(client, h, memo, {lids[0]: 1}, target=await draft(client, h, lines=[service_line()]))
    assert len(drawn_lines(await state(client, h, pf))) == 1

    session.expire_all()
    row = await session.get(Projection, {"company_id": a["company_id"], "entity_id": pf})
    lines = (await outstanding_physical_lines(session, a["company_id"], [row])).get(pf, [])

    assert [ln for ln in lines if ln.outstanding > 0] == []


async def _net_for(client, h, doc_id: str) -> dict[str, float]:
    """Net debit less credit per account over the live journal entries that name ``doc_id``."""
    led = (await ok(await client.get("/ledger?entity_type=journal_entry&limit=500", headers=h)))["items"]
    led = [e for e in led if doc_id in str(e.get("data"))]
    voided = {e["entity_id"] for e in led if str(e.get("event_type", "")).endswith(".voided")}
    net: dict[str, float] = {}
    for e in led:
        if not str(e.get("event_type", "")).endswith(".created") or e["entity_id"] in voided:
            continue
        for x in (e.get("data") or {}).get("entries", []):
            net[x["account"]] = round(net.get(x["account"], 0.0) + float(x.get("debit") or 0) - float(x.get("credit") or 0), 2)
    return {k: v for k, v in net.items() if abs(v) > 1e-9}


async def test_cogs_only_at_finalize_and_once_per_cycle(client, session):
    h = await auth(session)
    memo, lids, _ = await memo_out(client, h, [(3, 50.0)], lots_per_line=3)
    pf = await drawn(client, h, memo, {lids[0]: 2})
    assert await _net_for(client, h, pf) == {}

    back = drawn_lines(await state(client, h, pf))[0]["line_id"]
    await ok(await back_to_memo(client, h, pf, [back]))
    assert await _net_for(client, h, pf) == {}
    await drawn(client, h, memo, {lids[0]: 2}, target=pf)

    once = None
    for _ in range(3):
        await ok(await client.post(f"/docs/{pf}/finalize", headers=h))
        booked = await _net_for(client, h, pf)
        assert booked, "finalize books revenue and cost of sales"
        assert once is None or booked == once
        once = booked
        await ok(await client.post(f"/docs/{pf}/revert-to-draft", headers=h, json={}))
        assert await _net_for(client, h, pf) == {}
