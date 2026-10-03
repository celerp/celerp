# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Work orders made when an invoice is posted cover only what Demand Planning says is short.

Finished stock is held as lots under the product, and runs already in progress will add to
it; both count before anything new is made, exactly as on the Demand Planning board.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection
from mfg_runs import product, set_settings
from test_cost_restatement import _item, _state, auth, ids  # noqa: F401  (fixtures)

pytestmark = pytest.mark.asyncio


async def _made_in_stock(client, auth, fg: str, qty: float) -> None:
    """``qty`` of ``fg`` built and completed: a lot under the product, the product itself at 0."""
    r = await client.post(f"/manufacturing/items/{fg}/build", headers=auth["headers"],
                          json={"quantity": qty, "complete": True})
    assert r.status_code == 200, r.text


async def _in_progress(client, auth, fg: str, qty: float) -> None:
    r = await client.post(f"/manufacturing/items/{fg}/build", headers=auth["headers"], json={"quantity": qty})
    assert r.status_code == 200, r.text


async def _post_invoice(client, auth, fg: str, qty: float) -> str:
    r = await client.post("/docs", headers=auth["headers"], json={"doc_type": "invoice", "total": 0, "line_items": [
        {"item_id": fg, "sku": "FG", "name": "FG", "quantity": qty, "unit_price": 100}]})
    assert r.status_code in (200, 201), r.text
    doc = r.json()["id"]
    r = await client.post(f"/docs/{doc}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return doc


async def _runs_for_doc(session, auth, doc: str) -> list[dict]:
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "mfg_order"))).scalars().all()
    return [r.state for r in rows if (r.state or {}).get("source_doc_id") == doc]


async def _setup(client, session, auth, *, auto_complete: bool = False) -> tuple[str, str]:
    raw = await _item(client, auth, 1000.0, qty=100)
    fg = await product(client, auth, [(raw, 1)])
    await set_settings(session, auth, manufacturing={"auto_create_work_orders": True,
                                                     "auto_complete_work_orders": auto_complete})
    return raw, fg


def _made(runs: list[dict]) -> list[float]:
    return [float(r["expected_outputs"][0]["quantity"]) for r in runs]


async def test_lots_on_hand_cover_the_order(client, session, auth):
    raw, fg = await _setup(client, session, auth)
    await _made_in_stock(client, auth, fg, 10)

    doc = await _post_invoice(client, auth, fg, 5)

    assert await _runs_for_doc(session, auth, doc) == []


async def test_lots_on_hand_partly_cover_the_order(client, session, auth):
    raw, fg = await _setup(client, session, auth)
    await _made_in_stock(client, auth, fg, 3)

    doc = await _post_invoice(client, auth, fg, 5)

    assert _made(await _runs_for_doc(session, auth, doc)) == [2.0]


async def test_production_in_progress_covers_the_order(client, session, auth):
    raw, fg = await _setup(client, session, auth)
    await _in_progress(client, auth, fg, 5)

    doc = await _post_invoice(client, auth, fg, 5)

    assert await _runs_for_doc(session, auth, doc) == []


async def test_on_hand_and_in_progress_together(client, session, auth):
    raw, fg = await _setup(client, session, auth)
    await _made_in_stock(client, auth, fg, 2)
    await _in_progress(client, auth, fg, 2)

    doc = await _post_invoice(client, auth, fg, 5)

    assert _made(await _runs_for_doc(session, auth, doc)) == [1.0]


async def test_auto_complete_uses_components_only_for_the_shortfall(client, session, auth):
    raw, fg = await _setup(client, session, auth, auto_complete=True)
    await _made_in_stock(client, auth, fg, 4)
    assert float((await _state(session, auth, raw))["quantity"]) == 96

    doc = await _post_invoice(client, auth, fg, 5)

    runs = await _runs_for_doc(session, auth, doc)
    assert _made(runs) == [1.0] and runs[0]["status"] == "completed"
    assert float((await _state(session, auth, raw))["quantity"]) == 95
