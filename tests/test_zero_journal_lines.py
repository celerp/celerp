# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A journal line that moves nothing is never posted, and never shown.

An invoice with no tax posts no VAT Payable line, and an invoice with tax posts its VAT
line exactly once. Entries an older release stored with a line of no debit and no credit
read on every journal view without that line, the stored entry keeps every line it was
posted with, and the totals stay what they were.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.events.engine import emit_event
from celerp.models.projections import Projection
from celerp.services import auto_je
from test_cost_restatement import _state

pytestmark = pytest.mark.asyncio

_VAT = {"code": "Output VAT", "rate": 7.0, "order": 0}


async def _company(client) -> tuple[dict, uuid.UUID]:
    r = await client.post("/auth/register", json={
        "company_name": "Zero Lines Co", "email": f"zl-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Admin", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.patch("/companies/me/taxes", headers=h, json={"taxes": [
        {"name": "Output VAT", "rate": 7.0, "tax_type": "sales", "is_default": False}]})
    assert r.status_code == 200, r.text
    return h, uuid.UUID((await client.get("/companies/me", headers=h)).json()["id"])


async def _finalized_invoice_entry(client, session, h, taxes: list) -> list[dict]:
    r = await client.post("/docs", headers=h, json={"doc_type": "invoice", "line_items": [
        {"description": "Service", "quantity": 1, "unit_price": 1000.0, "taxes": taxes}]})
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    assert (await client.post(f"/docs/{doc_id}/finalize", headers=h)).status_code == 200
    session.expire_all()
    row = (await session.execute(select(Projection).where(
        Projection.entity_id.like(f"je:auto:{doc_id}:fin%")))).scalar_one()
    return row.state["entries"]


async def test_an_invoice_without_tax_posts_no_vat_line(client, session):
    h, _ = await _company(client)
    lines = await _finalized_invoice_entry(client, session, h, [])
    assert [l["account"] for l in lines] == ["1120", "4100"]
    assert all(l["debit"] or l["credit"] for l in lines)


async def test_a_taxed_invoice_posts_its_vat_line_exactly_once(client, session):
    h, _ = await _company(client)
    lines = await _finalized_invoice_entry(client, session, h, [_VAT])
    vat = [l for l in lines if l["account"] == "2120"]
    assert len(vat) == 1
    assert (vat[0]["debit"], vat[0]["credit"]) == (0.0, 70.0)
    assert sum(l["debit"] for l in lines) == sum(l["credit"] for l in lines) == 1070.0


async def test_no_automatic_entry_writes_a_line_that_moves_nothing(client, session):
    """The posting boundary drops a line that is zero once rounded, so no producer can
    write one, and the lines that move money post as they were built."""
    _, company_id = await _company(client)
    je_id = f"je:auto:zero-{uuid.uuid4().hex[:8]}"
    await auto_je._emit_auto_posted_je(
        session, company_id=company_id, user_id=None, je_id=je_id, idem_create=f"{je_id}:c",
        idem_posted=f"{je_id}:p", memo="Zero line", ts="2026-10-01", metadata_={},
        entries=[{"account": "1120", "debit": 10.0, "credit": 0.0},
                 {"account": "4100", "debit": 0.0, "credit": 10.0},
                 {"account": "2120", "debit": 0.0, "credit": 0.001}])
    await session.commit()
    state = await _state(session, {"company_id": company_id}, je_id)
    assert [(l["account"], l["debit"], l["credit"]) for l in state["entries"]] == [
        ("1120", 10.0, 0.0), ("4100", 0.0, 10.0)]
    assert state["status"] == "posted"


def _rows_for(node, je_id: str):
    """Every row a journal view returns for one entry: the entry's own lines in the journals,
    the entry's row in a ledger."""
    if isinstance(node, dict):
        if node.get("je_id") == je_id:
            yield from node["lines"] if isinstance(node.get("lines"), list) else [node]
            return
        for value in node.values():
            yield from _rows_for(value, je_id)
    elif isinstance(node, list):
        for value in node:
            yield from _rows_for(value, je_id)


async def test_a_stored_line_that_moves_nothing_is_not_shown_and_stays_stored(client, session):
    h, company_id = await _company(client)
    before = (await client.get("/accounting/journal", headers=h)).json()
    je_id = f"je:auto:doc:{uuid.uuid4()}:fin"
    stored = [{"account": "1120", "debit": 41.18, "credit": 0.0},
              {"account": "4100", "debit": 0.0, "credit": 41.18},
              {"account": "2120", "debit": 0.0, "credit": 0.0}]
    for event_type, data in (("acc.journal_entry.created",
                              {"memo": "Older invoice", "ts": "2026-08-31", "entries": stored}),
                             ("acc.journal_entry.posted", {"ts": "2026-08-31"})):
        await emit_event(
            session, company_id=company_id, entity_id=je_id, entity_type="journal_entry",
            event_type=event_type, actor_id=None, location_id=None, source="auto_je",
            idempotency_key=f"{je_id}:{event_type}", metadata_={}, data=data)
    await session.commit()

    for path in ("/accounting/journal", "/accounting/extended-journal", "/accounting/ledger/2120",
                 "/accounting/general-ledger?include_lines=true"):
        r = await client.get(path, headers=h)
        assert r.status_code == 200, r.text
        rows = list(_rows_for(r.json(), je_id))
        assert all(row.get("debit") or row.get("credit") for row in rows), (path, rows)
        if "ledger/2120" not in path:
            assert rows, path
    journal = (await client.get("/accounting/journal", headers=h)).json()
    assert round(journal["total_debit"] - before["total_debit"], 2) == 41.18
    assert journal["total_debit"] == journal["total_credit"]
    assert (await _state(session, {"company_id": company_id}, je_id))["entries"] == stored
