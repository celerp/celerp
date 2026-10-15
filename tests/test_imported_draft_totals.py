# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An imported draft whose file gives its lines but no totals takes its money from those
lines, by the rule a document created in the app follows, so finalizing it books what the
lines are worth, never nothing."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection
from test_issued_document_tax_side import _VAT, _company, _import

pytestmark = pytest.mark.asyncio


async def _booked(session, doc_id: str) -> dict[str, float]:
    """Debit less credit per account over the journal entries a document posted."""
    session.expire_all()
    rows = (await session.execute(select(Projection).where(Projection.entity_type == "journal_entry"))).scalars()
    out: dict[str, float] = {}
    for row in rows:
        if doc_id in row.entity_id and (row.state or {}).get("status") == "posted":
            for e in row.state.get("entries") or []:
                out[e["account"]] = round(out.get(e["account"], 0.0) + (e.get("debit") or 0) - (e.get("credit") or 0), 2)
    return {k: v for k, v in out.items() if v}


async def test_an_imported_draft_without_totals_takes_them_from_its_lines(client, session):
    h = await _company(client)
    eid, r = await _import(client, h, {
        "doc_type": "invoice", "status": "draft", "issue_date": "2026-10-01",
        "discount": 10.0, "shipping": 5.0,
        "line_items": [{"description": "Service", "quantity": 2, "unit_price": 50.0, "taxes": [dict(_VAT)]},
                       {"description": "Setup", "quantity": 1, "unit_price": 20.0}]})
    assert r.status_code == 200, r.text
    doc = (await client.get(f"/docs/{eid}", headers=h)).json()
    # 120 of lines, less 10 discount, 7% on the service's share of what is left, plus 5 shipping.
    assert (doc["subtotal"], doc["discount_amount"], doc["tax"], doc["total"]) == (120.0, 10.0, 6.42, 121.42), doc
    f = await client.post(f"/docs/{eid}/finalize", headers=h)
    assert f.status_code == 200, f.text
    booked = await _booked(session, eid)
    assert sum(v for v in booked.values() if v > 0) == 121.42, booked


async def test_an_imported_draft_keeps_the_totals_its_file_gives(client):
    h = await _company(client)
    eid, r = await _import(client, h, {
        "doc_type": "invoice", "status": "draft", "issue_date": "2026-10-01", "subtotal": 100.0, "tax": 7.0,
        "total": 107.0, "line_items": [{"description": "Service", "quantity": 1, "unit_price": 100.0,
                                        "line_total": 100.0, "taxes": [dict(_VAT)]}]})
    assert r.status_code == 200, r.text
    doc = (await client.get(f"/docs/{eid}", headers=h)).json()
    assert (doc["subtotal"], doc["tax"], doc["total"]) == (100.0, 7.0, 107.0), doc
