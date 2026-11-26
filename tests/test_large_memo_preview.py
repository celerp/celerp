# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Set as shipped on a large memo previews without writing or locking anything.

The preview plans the shipment by the same rule the confirm runs, then stops: it carves no lot,
writes no event and locks no row, so a preview of a hundred lines never holds the company's
stock while other work waits. The confirm takes the locks, runs the action again and refuses
when its outcome differs from the preview's binding.

The records are the gemstones demo company's, with imported stones up to a hundred lines."""
from __future__ import annotations

import re
import uuid

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from test_item_less_line_actions import _act, _doc, _gems, _ids, _line, _ok

pytestmark = pytest.mark.asyncio

LINES = 100


async def _large_memo(client, n: int = LINES):
    """A finalized memo of ``n`` stones of the gemstones demo company, imported stones making up
    what the demo stock lacks."""
    g = await _gems(client)

    def stones():
        return [s for s, i in g.items.items() if i.get("inventory_type") != "service" and float(i.get("quantity") or 0) > 0]
    if (need := n - len(stones())) > 0:
        await _ok(await client.post("/items/import/batch", headers=g.h, json={"records": [
            {"entity_id": f"item:{uuid.uuid4()}", "event_type": "item.created", "source": "import",
             "idempotency_key": f"import:stone:{k}",
             "data": {"sku": f"IMP-{k:04d}", "name": f"Imported stone {k}", "category": "Sapphire", "quantity": 1,
                      "cost_total": 500, "retail_price": 1000, "status": "available", "sell_by": "piece"}}
            for k in range(need)]}))
        g.items = {i["sku"]: i for i in (await _ok(await client.get("/items", headers=g.h, params={"limit": 500})))["items"]}
    memo = await _doc(client, g, [_line(g, s) for s in stones()[:n]], doc_type="memo")
    return g, memo, await _ids(client, g, memo)


class _Statements:
    """The SQL statements run while it is open."""

    def __enter__(self):
        self.seen: list[str] = []
        event.listen(Engine, "before_cursor_execute", self._seen)
        return self

    def _seen(self, _conn, _cursor, statement, *_args):
        self.seen.append(re.sub(r"\s+", " ", statement).strip())

    def __exit__(self, *_exc):
        event.remove(Engine, "before_cursor_execute", self._seen)


async def test_the_preview_of_a_large_memo_writes_and_locks_nothing_and_its_confirm_ships_it(client):
    g, memo, lids = await _large_memo(client)
    body = {"line_ids": lids, "idempotency_key": uuid.uuid4().hex}
    with _Statements() as sql:
        preview = await _ok(await client.post(f"/docs/{memo}/fulfill-lines", headers=g.h, json={**body, "dry_run": True}))
    assert len(preview["acted"]) > 80, preview
    writes = [s[:90] for s in sql.seen if re.match(r"(INSERT|UPDATE|DELETE)\b", s) or re.search(r"\bFOR (NO KEY )?UPDATE\b", s)
              or "pg_advisory" in s or "pg_notify" in s]
    assert writes == [], writes
    real = await _ok(await client.post(f"/docs/{memo}/fulfill-lines", headers=g.h, json={**body, "binding": preview["binding"]}))
    assert real["acted"] == preview["acted"] and real["binding"] == preview["binding"]


async def test_a_confirm_after_its_lines_changed_refuses_and_ships_nothing(client):
    g, memo, lids = await _large_memo(client, 12)
    preview = await _ok(await client.post(f"/docs/{memo}/fulfill-lines", headers=g.h, json={
        "line_ids": lids, "idempotency_key": uuid.uuid4().hex, "dry_run": True}))
    await _ok(await _act(client, g, memo, "fulfill-lines", lids[:1]))
    late = await client.post(f"/docs/{memo}/fulfill-lines", headers=g.h, json={
        "line_ids": lids, "idempotency_key": uuid.uuid4().hex, "binding": preview["binding"]})
    assert late.status_code == 409 and late.json()["detail"]["message_key"] == "lines.preview_differs", late.text
    state = await _ok(await client.get(f"/docs/{memo}", headers=g.h))
    assert state.get("fulfillment_status") == "partial", state.get("fulfillment_status")
