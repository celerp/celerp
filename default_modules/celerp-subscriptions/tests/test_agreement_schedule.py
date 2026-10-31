# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Agreement schedules: one document per period, billed on its bill date, the oldest first.

A period is claimed once, by the agreement's own schedule operations; documents never
write a period, and a one-off invoice for the same contact leaves the schedule alone."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from agreement_kit import (
    agreement, bill_early, billed_docs, claim_events, freeze_day, generate_due, member,
    next_bill_date, register, schedule, template,
)

pytestmark = pytest.mark.asyncio


async def _skip(client, hd, eid, k, reason="Office closed"):
    return await client.post(f"/subscriptions/{eid}/periods/{k}/skip", headers=hd, json={"reason": reason})


async def test_generate_due_refuses_before_bill_date(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-03-15")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-04-01")
    r = await generate_due(client, hd, eid)
    assert r.status_code == 409 and "2026-04-01" in r.json()["detail"]
    assert await billed_docs(session, eid) == []
    assert await next_bill_date(client, hd, eid) == "2026-04-01"


async def test_generate_due_bills_oldest_unbilled_period(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-03-15")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-02-01")
    first = await generate_due(client, hd, eid)
    assert first.status_code == 200 and first.json()["period_index"] == 0
    second = await generate_due(client, hd, eid)
    assert second.json()["period_index"] == 1
    docs = await billed_docs(session, eid)
    assert [(d["period_index"], d["service_start"], d["service_end"]) for d in docs] == [
        (0, "2026-02-01", "2026-02-28"), (1, "2026-03-01", "2026-03-31")]
    assert all(li.get("period_index") == d["period_index"] and li.get("source_agreement") == eid
               for d in docs for li in d["line_items"])
    assert await next_bill_date(client, hd, eid) == "2026-04-01"


async def test_bill_early_consumes_next_period_then_due_returns_same_invoice(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-03-15")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-04-01")
    early = await bill_early(client, hd, eid, "early-1")
    assert early.status_code == 200 and early.json()["period_index"] == 0
    again = await bill_early(client, hd, eid, "early-1")
    assert again.json()["doc_id"] == early.json()["doc_id"]
    later = await bill_early(client, hd, eid, "early-2", period_index=2)
    assert later.status_code == 409  # only the oldest unbilled period can be billed early
    freeze_day(monkeypatch, "2026-04-01")
    due = await generate_due(client, hd, eid)
    assert due.status_code == 200 and due.json()["doc_id"] == early.json()["doc_id"]
    assert len(await billed_docs(session, eid)) == 1
    missing = await client.post(f"/subscriptions/{eid}/bill-early", headers=hd, json={})
    assert missing.status_code == 422


async def test_same_period_two_keys_one_invoice(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-03-15")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-03-01")
    a = await generate_due(client, hd, eid, key="key-a")
    b = await generate_due(client, hd, eid, key="key-b")
    assert a.json()["doc_id"] == b.json()["doc_id"]
    assert len(await billed_docs(session, eid)) == 1
    assert await claim_events(session, eid) == [f"sub:{eid}:p0:invoice:r0"]


async def test_claim_survives_renumber_and_restore(client, session, monkeypatch):
    from sqlalchemy import select

    from celerp.models.projections import Projection
    from celerp.projections.engine import ProjectionEngine

    freeze_day(monkeypatch, "2026-03-15")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-03-01")
    doc_id = (await generate_due(client, hd, eid)).json()["doc_id"]
    assert (await client.post(f"/docs/{doc_id}/renumber", headers=hd, json={"ref_id": "RENUMBERED-1"})).status_code == 200
    cid = (await session.execute(select(Projection.company_id).where(Projection.entity_id == eid))).scalar_one()
    await ProjectionEngine.rebuild(session, cid)
    await session.commit()
    state = await template(client, hd, eid)
    assert state["periods"]["0"]["doc_id"] == doc_id and state["periods"]["0"]["state"] == "billed"
    assert (await generate_due(client, hd, eid)).json()["doc_id"] == doc_id
    assert len(await billed_docs(session, eid)) == 1


async def test_generate_refuses_draft_paused_cancelled_ended(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-06-15")
    hd = await register(client)
    draft = await agreement(client, hd, start="2026-01-01", activate=False)
    paused = await agreement(client, hd, start="2026-01-01")
    await client.post(f"/subscriptions/{paused}/pause", headers=hd)
    cancelled = await agreement(client, hd, start="2026-01-01")
    await client.post(f"/subscriptions/{cancelled}/cancel", headers=hd)
    ended = await agreement(client, hd, start="2026-01-01")
    assert (await schedule(client, hd, ended, term_end="2026-02-15")).status_code == 200
    for _ in range(2):
        assert (await generate_due(client, hd, ended)).status_code == 200
    for eid, word in ((draft, "draft"), (paused, "paused"), (cancelled, "cancelled"), (ended, "ended")):
        r = await generate_due(client, hd, eid)
        assert r.status_code == 409 and word in r.json()["detail"].lower(), (word, r.text)
    for eid in (draft, paused, cancelled):
        assert await billed_docs(session, eid) == []


async def test_term_end_never_bills_post_term_period(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-06-15")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-01-01")
    assert (await schedule(client, hd, eid, term_end="2026-03-31")).status_code == 200
    for k in range(3):
        assert (await generate_due(client, hd, eid)).json()["period_index"] == k
    assert (await generate_due(client, hd, eid)).status_code == 409
    assert (await bill_early(client, hd, eid, "after-term")).status_code == 409
    assert len(await billed_docs(session, eid)) == 3
    short = await agreement(client, hd, start="2026-01-01")
    assert (await schedule(client, hd, short, term_end="2026-03-01")).status_code == 200
    for _ in range(3):
        await generate_due(client, hd, short)
    assert [d["period_index"] for d in await billed_docs(session, short)] == [0, 1]  # a period starting on term end does not exist


async def test_cancel_at_period_end_stops_after_current_period(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-03-15")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-01-01")
    for _ in range(3):
        await generate_due(client, hd, eid)
    assert (await schedule(client, hd, eid, cancel_at_period_end=True)).status_code == 200
    freeze_day(monkeypatch, "2026-05-15")
    assert (await generate_due(client, hd, eid)).json()["period_index"] == 2  # nothing after the current period
    assert len(await billed_docs(session, eid)) == 3
    assert await next_bill_date(client, hd, eid) is None
    assert (await schedule(client, hd, eid, cancel_at_period_end=False)).status_code == 200  # undone
    assert (await generate_due(client, hd, eid)).json()["period_index"] == 3


async def test_resume_lists_missed_periods_never_skips(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-01-15")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-01-01")
    await generate_due(client, hd, eid)
    await client.post(f"/subscriptions/{eid}/pause", headers=hd)
    freeze_day(monkeypatch, "2026-04-15")
    assert (await client.post(f"/subscriptions/{eid}/resume", headers=hd)).status_code == 200
    due = (await client.get("/subscriptions/due", headers=hd)).json()["items"]
    row = next(i for i in due if i["entity_id"] == eid)
    assert [p["period_index"] for p in row["periods"]] == [1, 2, 3]
    assert (await generate_due(client, hd, eid)).json()["period_index"] == 1


async def test_skip_period_recorded_and_unskip_restores(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-03-15")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-03-01")
    assert (await _skip(client, hd, eid, 0, reason="")).status_code == 422
    assert (await _skip(client, hd, eid, 0)).status_code == 200
    assert (await _skip(client, hd, eid, 0)).status_code == 200  # a repeat is a no-op
    entry = (await template(client, hd, eid))["periods"]["0"]
    assert entry["state"] == "skipped" and entry["reason"] == "Office closed" and entry["by"]
    assert (await generate_due(client, hd, eid)).status_code == 409
    assert await next_bill_date(client, hd, eid) == "2026-04-01"
    assert (await client.post(f"/subscriptions/{eid}/periods/0/unskip", headers=hd)).status_code == 200
    billed = await generate_due(client, hd, eid)
    assert billed.json()["period_index"] == 0
    refused = await _skip(client, hd, eid, 0)
    assert refused.status_code == 409 and billed.json()["ref_id"] in refused.json()["detail"]


async def test_release_period_only_after_void_rebills_once(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-03-15")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-03-01")
    first = (await generate_due(client, hd, eid)).json()
    reopen = await client.post(f"/subscriptions/{eid}/periods/0/reopen", headers=hd)
    assert reopen.status_code == 409 and first["ref_id"] in reopen.json()["detail"]
    assert (await client.post(f"/docs/{first['doc_id']}/void", headers=hd, json={"reason": "Wrong amount"})).status_code == 200
    assert (await client.post(f"/subscriptions/{eid}/periods/0/reopen", headers=hd)).status_code == 200
    again = (await generate_due(client, hd, eid)).json()
    assert again["period_index"] == 0 and again["doc_id"] != first["doc_id"]
    assert (await generate_due(client, hd, eid)).json()["doc_id"] == again["doc_id"]
    docs = await billed_docs(session, eid)
    assert len(docs) == 2 and [d["entity_id"] for d in docs if d["status"] != "void"] == [again["doc_id"]]
    assert await claim_events(session, eid) == [f"sub:{eid}:p0:invoice:r0", f"sub:{eid}:p0:invoice:r1"]


async def test_generate_all_due_preview_counts_match_run(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-03-15")
    hd = await register(client)
    due = [await agreement(client, hd, start="2026-03-01") for _ in range(2)]
    await agreement(client, hd, start="2026-04-01")
    preview = (await client.get("/subscriptions/due", headers=hd)).json()
    ids = [i["entity_id"] for i in preview["items"]]
    assert sorted(ids) == sorted(due)
    run = (await client.post("/subscriptions/generate-due", headers=hd, json={"ids": ids})).json()
    assert run["billed"] + run["refused"] == len(ids) == preview["total"]
    empty = (await client.get("/subscriptions/due", headers=hd)).json()
    assert empty["total"] == 0 and empty["next_bill_date"] == "2026-04-01"


async def test_generate_all_due_batch_bound_and_partial_failure(client, session, monkeypatch):
    from celerp_subscriptions import billing

    freeze_day(monkeypatch, "2026-03-15")
    hd = await register(client)
    ids = [await agreement(client, hd, start="2026-03-01") for _ in range(120)]
    broken = ids[7]
    real = billing.claim_period

    async def _claim(session, company_id, template_id, **kw):
        if template_id == broken:
            raise HTTPException(status_code=409, detail="Broken on purpose")
        return await real(session, company_id, template_id, **kw)

    monkeypatch.setattr(billing, "claim_period", _claim)
    over = await client.post("/subscriptions/generate-due", headers=hd, json={"ids": ids[:51]})
    assert over.status_code == 422
    billed = refused = 0
    for start in range(0, 120, 50):
        r = (await client.post("/subscriptions/generate-due", headers=hd, json={"ids": ids[start:start + 50]})).json()
        billed, refused = billed + r["billed"], refused + r["refused"]
        if broken in ids[start:start + 50]:
            row = next(x for x in r["results"] if x["entity_id"] == broken)
            assert not row["ok"] and "Broken on purpose" in row["error"]
    assert (billed, refused) == (119, 1)
    assert await billed_docs(session, broken) == []
    assert len(await billed_docs(session, ids[8])) == 1


async def test_arrears_bills_at_period_end(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-02-15")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-02-01", billing_timing="arrears")
    assert (await generate_due(client, hd, eid)).status_code == 409
    assert await next_bill_date(client, hd, eid) == "2026-03-01"
    freeze_day(monkeypatch, "2026-03-01")
    r = (await generate_due(client, hd, eid)).json()
    assert r["period_index"] == 0
    doc = (await billed_docs(session, eid))[0]
    assert (doc["service_start"], doc["service_end"]) == ("2026-02-01", "2026-02-28")


async def test_po_template_uses_period_engine_without_collection_or_deferral(client, session, monkeypatch):
    from celerp.models.ledger import LedgerEntry
    from sqlalchemy import func, select

    freeze_day(monkeypatch, "2026-03-15")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-03-01", doc_type="subscription_po")
    r = await generate_due(client, hd, eid)
    assert r.status_code == 200 and r.json()["period_index"] == 0
    doc = (await billed_docs(session, eid))[0]
    assert doc["doc_type"] == "purchase_order" and doc["source_agreement"] == eid
    assert not any(k in doc for k in ("revenue_schedule", "deferral"))
    collect = (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.idempotency_key.like(f"collect:{doc['entity_id']}:%")))).scalar_one()
    assert collect == 0
    assert (await claim_events(session, eid)) == [f"sub:{eid}:p0:purchase_order:r0"]
    assert (await schedule(client, hd, eid, generation_mode="auto_invoice_collect")).status_code == 422


async def test_schedule_validation_and_mode_permissions(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-03-15")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-04-01")
    for bad in ({"billing_timing": "sideways"}, {"generation_mode": "bogus"}, {"catchup_periods": 13},
                {"catchup_periods": -1}, {"anchor_date": None},
                {"term_start": "2026-05-01", "term_end": "2026-04-01"}, {"frequency": "hourly"}):
        r = await schedule(client, hd, eid, **bad)
        assert r.status_code == 422, (bad, r.text)
    operator, _ = await member(client, session, hd, "operator")
    manager, manager_id = await member(client, session, hd, "manager")
    assert (await schedule(client, operator, eid, generation_mode="auto_invoice")).status_code == 403
    assert (await schedule(client, operator, eid, auto_send=True)).status_code == 403
    ok = await schedule(client, manager, eid, generation_mode="auto_invoice")
    assert ok.status_code == 200, ok.text
    state = await template(client, hd, eid)
    assert state["generation_mode"] == "auto_invoice"
    assert state["consents"]["generation"]["by"] == manager_id and state["consents"]["generation"]["at"]
    assert "sending" not in state["consents"]
    assert (await schedule(client, manager, eid, auto_send=True)).status_code == 200
    assert (await template(client, hd, eid))["consents"]["sending"]["by"] == manager_id
    assert (await schedule(client, manager, eid, generation_mode="auto_invoice_collect")).status_code == 403
    # Collection stays closed until the Connect service offers it.
    assert (await schedule(client, hd, eid, generation_mode="auto_invoice_collect")).status_code == 422


async def test_generate_requires_finalize_permission(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-03-15")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-03-01")
    viewer, _ = await member(client, session, hd, "viewer")
    assert (await generate_due(client, viewer, eid)).status_code == 403
    assert (await bill_early(client, viewer, eid, "v-1")).status_code == 403
    assert (await client.post("/subscriptions/generate-due", headers=viewer, json={"ids": [eid]})).status_code == 403
    assert await billed_docs(session, eid) == []


async def test_offcycle_invoice_does_not_advance_agreement(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-03-15")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-03-01")
    await generate_due(client, hd, eid)
    before = await template(client, hd, eid)
    r = await client.post("/docs", headers=hd, json={
        "doc_type": "invoice", "contact_id": "contact:test-001",
        "line_items": [{"description": "Extra visit", "quantity": 1, "unit_price": 40.0}]})
    assert r.status_code in (200, 201), r.text
    one_off = r.json()["id"]
    assert (await client.post(f"/docs/{one_off}/finalize", headers=hd)).status_code == 200
    after = await template(client, hd, eid)
    assert after["periods"] == before["periods"]
    assert await next_bill_date(client, hd, eid) == "2026-04-01"
    assert (await client.post(f"/docs/{one_off}/void", headers=hd, json={"reason": "Not needed"})).status_code == 200
    assert (await template(client, hd, eid))["periods"] == before["periods"]
    assert len(await billed_docs(session, eid)) == 1


@pytest.mark.parametrize("field,value", [("source_agreement", "doc:SUB-1"), ("period_index", 3),
                                         ("service_start", "2026-01-01"), ("service_end", "2026-01-31")])
async def test_client_cannot_set_period_fields_on_documents(client, field, value):
    hd = await register(client)
    line = {"description": "Visit", "quantity": 1, "unit_price": 40.0}
    r = await client.post("/docs", headers=hd, json={"doc_type": "invoice", "line_items": [line], field: value})
    assert r.status_code == 422 and field in r.text
    r = await client.post("/docs", headers=hd, json={"doc_type": "invoice", "line_items": [line]})
    doc_id = r.json()["id"]
    p = await client.patch(f"/docs/{doc_id}", headers=hd, json={field: value})
    assert p.status_code == 422 and field in p.text


async def test_anchor_editable_only_before_first_claim(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-03-15")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-04-01")
    assert (await schedule(client, hd, eid, anchor_date="2026-03-10")).status_code == 200
    assert await next_bill_date(client, hd, eid) == "2026-03-10"
    await generate_due(client, hd, eid)
    r = await schedule(client, hd, eid, anchor_date="2026-03-20")
    assert r.status_code == 409 and "skip" in r.json()["detail"].lower()
    assert await next_bill_date(client, hd, eid) == "2026-04-10"


async def test_generate_all_due_rejects_over_cap_and_foreign_ids(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-03-15")
    import uuid
    from test_helpers import company_auth

    hd = await register(client)
    other = (await company_auth(session, uuid.uuid4(), uuid.uuid4()))["headers"]
    foreign = await agreement(client, other, start="2026-03-01")
    mine = await agreement(client, hd, start="2026-03-01")
    assert (await client.post("/subscriptions/generate-due", headers=hd,
                              json={"ids": [mine] * 51})).status_code == 422
    r = (await client.post("/subscriptions/generate-due", headers=hd,
                           json={"ids": [mine, foreign, "doc:SUB-NOPE"]})).json()
    rows = {x["entity_id"]: x for x in r["results"]}
    assert rows[mine]["ok"] and r["billed"] == 1
    assert rows[foreign]["error"] == rows["doc:SUB-NOPE"]["error"] == "Not found"
    assert await billed_docs(session, foreign) == []


async def test_catchup_run_refuses_items_outside_preview(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-06-15")
    hd = await register(client)
    behind = await agreement(client, hd, start="2026-01-01")
    assert (await schedule(client, hd, behind, generation_mode="auto_invoice", catchup_periods=1)).status_code == 200
    current = await agreement(client, hd, start="2026-06-01")
    assert (await schedule(client, hd, current, generation_mode="auto_invoice", catchup_periods=1)).status_code == 200
    skipped = await agreement(client, hd, start="2026-01-01")
    assert (await schedule(client, hd, skipped, generation_mode="auto_invoice")).status_code == 200
    await _skip(client, hd, skipped, 0)
    preview = (await client.get("/subscriptions/catch-up", headers=hd)).json()["items"]
    rows = {i["entity_id"]: [p["period_index"] for p in i["periods"]] for i in preview}
    assert rows[behind] == [0, 1, 2, 3, 4, 5] and current not in rows
    items = ([{"entity_id": behind, "period_index": 0}, {"entity_id": behind, "period_index": 0},
              {"entity_id": behind, "period_index": 4}, {"entity_id": current, "period_index": 0},
              {"entity_id": skipped, "period_index": 0}, {"entity_id": behind, "period_index": 1}]
             + [{"entity_id": behind, "period_index": 2}] * 50)
    r = (await client.post("/subscriptions/catch-up/run", headers=hd, json={"items": items})).json()
    ok = [x["ok"] for x in r["results"]]
    assert ok[:6] == [True, False, False, False, False, True]
    assert not any(ok[50:])  # above the batch cap: refused per item
    assert [d["period_index"] for d in await billed_docs(session, behind)] == [0, 1, 2]


async def test_history_review_empty_state(monkeypatch):
    from unittest.mock import AsyncMock, patch

    from httpx import ASGITransport, AsyncClient

    from test_helpers import make_test_token
    from ui.app import app as ui_app

    row = {"entity_id": "sub-1", "id": "sub-1", "doc_type": "subscription_invoice", "status": "active",
           "frequency": "monthly", "anchor_date": "2026-04-01", "next_bill_date": "2026-04-01",
           "periods": {}, "contact_name": "Alice", "line_items": []}
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as ui:
        ui.cookies.update({"celerp_token": make_test_token(role="owner")})
        with patch("ui.api_client.list_subscriptions", new=AsyncMock(return_value={"items": [row], "total": 1})):
            plain = await ui.get("/subscriptions?direction=sales")
        with patch("ui.api_client.list_subscriptions", new=AsyncMock(return_value={"items": [], "total": 0})):
            filtered = await ui.get("/subscriptions?direction=sales&review=1")
    assert plain.status_code == 200 and "need review" not in plain.text.lower()
    assert "No agreements need review" in filtered.text
    assert 'href="/subscriptions?direction=sales"' in filtered.text


async def test_period_list_first_use_shows_first_bill_date():
    from unittest.mock import AsyncMock, patch

    from httpx import ASGITransport, AsyncClient

    from test_helpers import make_test_token
    from ui.app import app as ui_app

    doc = {"entity_id": "sub-1", "id": "sub-1", "ref_id": "SUB-1", "doc_type": "subscription_invoice",
           "status": "active", "frequency": "monthly", "anchor_date": "2026-06-01", "billing_timing": "advance",
           "periods": {}, "contact_id": "ct:1", "contact_name": "Alice", "currency": "USD",
           "line_items": [{"description": "Service", "quantity": 1, "unit_price": 100}]}
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as ui:
        ui.cookies.update({"celerp_token": make_test_token(role="owner")})
        with (
            patch("ui.api_client.get_subscription", new=AsyncMock(return_value=doc)),
            patch("ui.api_client.get_company", new=AsyncMock(return_value={"name": "ACME", "currency": "USD", "timezone": "UTC"})),
            patch("ui.api_client.list_ledger", new=AsyncMock(return_value={"items": []})),
            patch("ui.api_client.get_price_lists", new=AsyncMock(return_value=[])),
            patch("ui.api_client.get_taxes", new=AsyncMock(return_value=[])),
            patch("ui.api_client.list_doc_notes", new=AsyncMock(return_value=[])),
        ):
            r = await ui.get("/subscriptions/sub-1")
    assert r.status_code == 200
    assert "First bill on" in r.text and "2026-06-01" in r.text
    assert "Generate Now" not in r.text and "Generate due" in r.text

