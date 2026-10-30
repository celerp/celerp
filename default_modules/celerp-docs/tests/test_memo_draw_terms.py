# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""What a draw writes and what it refuses: the target's document terms, the lines as the
document services store them, the order problems are reported in, the preview, and a draw
that fails part way leaving nothing behind."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, func, select

from celerp.projections.engine import ProjectionEngine
from celerp.models.ledger import LedgerEntry
from memo_draw_support import (
    also_keys, auth, auth_full, contact, draft, draw, drawn, drawn_lines, item, key_of, lot, memo_out, ok, owner,
    pay, revoke, service_line, state,
)


async def nothing_written(client, h, memo: str, lots: list[str], target: str | None, before: dict) -> None:
    """The memo, the target and every lot are as ``before`` recorded them."""
    assert (await state(client, h, memo))["version"] == before["memo"]
    if target:
        assert (await state(client, h, target))["version"] == before["target"]
    for i in lots:
        assert await owner(client, h, i) == ("memo_out", memo)


async def ledger_rows(session) -> int:
    return (await session.execute(select(func.count()).select_from(LedgerEntry))).scalar_one()


async def versions(client, h, memo: str, target: str | None) -> dict:
    return {"memo": (await state(client, h, memo))["version"],
            "target": (await state(client, h, target))["version"] if target else None}


# ── The target's document terms ───────────────────────────────────────────────

_DIFFERENT = {
    "other_percentage": ({"discount": 5, "discount_type": "percentage"}, {"discount": 10, "discount_type": "percentage"}),
    "doc_tax": ({"doc_taxes": [{"code": "VAT", "rate": 7}]}, {"doc_taxes": [{"code": "VAT", "rate": 10}]}),
    "header_rate": ({"tax_rate": 7}, {"tax_rate": 10}),
    "percentage_vs_fixed_left": ({"discount": 40, "discount_type": "flat"}, {"discount": 5, "discount_type": "percentage"}),
    "draft_unrated_vs_rated_memo": ({"tax_rate": 7}, {"tax": 20.0}),
    "draft_unrated_vs_unrated_memo": ({"tax": 35.0}, {"tax": 20.0}),
}


@pytest.mark.parametrize("case", list(_DIFFERENT))
async def test_draw_refuses_target_with_different_terms(client, session, case):
    h = await auth(session)
    memo_terms, draft_terms = _DIFFERENT[case]
    memo, lids, lots = await memo_out(client, h, [(2, 50.0)], lots_per_line=2, **memo_terms)
    pf = await draft(client, h, lines=[service_line()], **draft_terms)
    before = await versions(client, h, memo, pf)

    r = await draw(client, h, memo, line_ids=lids, target=pf)

    assert r.status_code == 409, r.text
    assert key_of(r) == "docs.draw_terms_differ"
    text = r.json()["detail"]["message"]
    assert "new invoice" in text
    if case.startswith("draft_unrated"):
        assert "a tax amount of 20.00 with no rate" in text
    await nothing_written(client, h, memo, lots[0], pf, before)


async def test_draw_terms_own_unrated_amount_kept_after_partial_return(client, session):
    """A draft whose tax amount came from a full draw of this memo, kept by a partial return,
    takes the redraw of the returned goods; its tax stays that amount, once."""
    h = await auth(session)
    memo, lids, _ = await memo_out(client, h, [(1, 100.0), (1, 100.0)], tax=35.0)
    pf = await draft(client, h)
    await drawn(client, h, memo, line_ids=lids, target=pf)
    first = drawn_lines(await state(client, h, pf), memo)[0]["line_id"]
    await ok(await client.post(f"/docs/{pf}/back-to-memo", headers=h,
                               json={"line_ids": [first], "idempotency_key": uuid.uuid4().hex}))

    assert await drawn(client, h, memo, line_ids=lids[:1], target=pf) == pf

    assert (await state(client, h, pf))["tax"] == pytest.approx(35.0)


async def test_draw_accepts_draft_with_other_line_taxes_same_header_terms(client, session):
    h = await auth(session)
    terms = {"discount": 5, "discount_type": "percentage"}
    memo, lids, _ = await memo_out(client, h, [(2, 25.0)], lots_per_line=2,
                                   line_extra={"discount_pct": 10, "taxes": [{"code": "VAT", "rate": 7}]}, **terms)
    own = service_line(40.0, taxes=[{"code": "GST", "rate": 10}])
    pf = await draft(client, h, lines=[own], **terms)
    stored_own = [li for li in (await state(client, h, pf))["line_items"]]

    assert await drawn(client, h, memo, {lids[0]: 1}, target=pf) == pf

    doc = await state(client, h, pf)
    line = drawn_lines(doc, memo)[0]
    assert (line["unit_price"], line["discount_pct"]) == (25.0, 10)
    assert [t["rate"] for t in line["taxes"]] == [7]
    kept = [li for li in doc["line_items"] if not li.get("drawn_from")]
    assert [(li["unit_price"], li["line_total"], li["taxes"]) for li in kept] == \
        [(li["unit_price"], li["line_total"], li["taxes"]) for li in stored_own]


async def test_draw_writes_through_document_services(client, session):
    a = await auth_full(session)
    h = a["headers"]
    memo, lids, _ = await memo_out(client, h, [(4, 25.0)], lots_per_line=4)
    pf = await draft(client, h, lines=[service_line()])
    await revoke(session, a["company_id"], "set_sales_doc_prices")

    new = await state(client, h, await drawn(client, h, memo, {lids[0]: 1}))
    onto = await state(client, h, await drawn(client, h, memo, {lids[0]: 1}, target=pf))

    assert new["ref_id"].startswith("PF")
    for doc in (new, onto):
        line = drawn_lines(doc, memo)[0]
        assert (line["unit_price"], line["line_total"]) == (25.0, 25.0)
        assert doc["subtotal"] == pytest.approx(sum(li["line_total"] for li in doc["line_items"]))


# ── Fidelity of both document services (P2 item 1 table) ──────────────────────

async def _demo_fixture(client):
    """The fidelity harness's fixture: a demo company, a customer on Wholesale in GBP, and a memo
    to it in EUR at 1.1 on Retail at an agreed 25.00 with a 10% line discount and 7% VAT, a 5%
    header discount and shipping 12, issued with its four units out."""
    r = await client.post("/auth/register", json={
        "company_name": "Demo Co", "email": f"fid-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Owner", "password": "pwvalid1"})
    h = {"Authorization": f"Bearer {(await ok(r))['access_token']}"}
    await ok(await client.post("/companies/me/demo/reseed", headers=h))
    rows = await ok(await client.get("/items", headers=h, params={"q": "DEMO-001", "limit": 50}))
    rows = rows.get("items", rows) if isinstance(rows, dict) else rows
    stock = next(i for i in rows if i.get("sku") == "DEMO-001" and i.get("status") in (None, "available"))
    cid = await contact(client, h, "Store A", currency="GBP")
    await ok(await client.patch(f"/crm/contacts/{cid}", headers=h, json={"fields_changed": {
        "price_list": {"new": "Wholesale"}, "payment_terms": {"new": "Net 30"}}}))
    header = {"contact_id": cid, "currency": "EUR", "conversion_rate": 1.1, "price_list": "Retail",
              "payment_terms": "Net 15", "discount_type": "percentage", "discount": 5}
    memo = (await ok(await client.post("/docs", headers=h, json={
        "doc_type": "memo", **header, "shipping": 12, "line_items": [{
            "item_id": stock["id"], "sku": "DEMO-001", "description": "[DEMO] Sample Item", "quantity": 4,
            "unit_price": 25.0, "discount_pct": 10, "sell_by": "piece", "taxes": [{"code": "VAT", "rate": 7}]}]})))["id"]
    await ok(await client.post(f"/docs/{memo}/finalize", headers=h))
    lid = (await state(client, h, memo))["line_items"][0]["line_id"]
    await ok(await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_ids": [lid]}))
    return h, memo, lid, header


@pytest.mark.parametrize("target", ["new_invoice", "existing_draft"])
async def test_draw_fidelity_both_services(client, session, target):
    h, memo, lid, header = await _demo_fixture(client)
    m = await state(client, h, memo)
    memo_line = m["line_items"][0]
    pf = None
    if target == "existing_draft":
        pf = await draft(client, h, lines=[service_line(30.0)], **header)
        own_before = (await state(client, h, pf))["line_items"]

    doc = await state(client, h, await drawn(client, h, memo, {lid: 2}, target=pf))

    line = drawn_lines(doc, memo)[0]
    assert line["line_id"] != memo_line["line_id"]
    assert line["quantity"] == 2
    assert (line["unit_price"], line["discount_pct"]) == (25.0, 10)
    assert line["line_total"] == pytest.approx(45.0)
    assert [t["rate"] for t in line["taxes"]] == [7]
    # The 5% header discount applies to every line of either invoice: 45 x 0.95 x 7%.
    assert line["taxes"][0]["amount"] == pytest.approx(2.99)
    assert (line["description"], line["sku"], line["sell_by"]) == ("[DEMO] Sample Item", "DEMO-001", "piece")
    assert "entity_id" not in line
    assert await owner(client, h, line["item_id"]) == ("memo_out", doc["id"])
    assert line["drawn_from"]["memo_id"] == memo
    assert line["drawn_from"]["memo_line_id"] == memo_line["line_id"]
    assert line["drawn_from"]["memo_number"] == m["ref_id"]
    assert line["drawn_from"]["draw_id"]
    for k in ("contact_id", "currency", "conversion_rate", "price_list", "payment_terms", "discount_type", "discount"):
        assert doc[k] == header[k], k
    assert doc["shipping"] == pytest.approx(12.0)
    assert not doc.get("source_memo_id")
    assert doc["subtotal"] == pytest.approx(sum(li["line_total"] for li in doc["line_items"]))
    if pf is not None:
        kept = [li for li in doc["line_items"] if not li.get("drawn_from")]
        assert [(li["unit_price"], li["line_total"]) for li in kept] == \
            [(li["unit_price"], li["line_total"]) for li in own_before]


# ── Atomicity ─────────────────────────────────────────────────────────────────

async def test_draw_unsplittable_lot_refused_atomically(client, session):
    h = await auth(session)
    memo, lids, lots = await memo_out(client, h, [(10, 10.0)])
    await ok(await client.patch(f"/items/{lots[0][0]}", headers=h,
                                json={"fields_changed": {"allow_splitting": {"old": True, "new": False}}}))
    before = await versions(client, h, memo, None)
    qty = (await item(client, h, lots[0][0]))["quantity"]

    r = await draw(client, h, memo, {lids[0]: 4})

    assert r.status_code == 409, r.text
    assert key_of(r) == "lots.splitting_off"
    await nothing_written(client, h, memo, lots[0], None, before)
    assert (await item(client, h, lots[0][0]))["quantity"] == qty
    assert not (await state(client, h, memo)).get("invoiced_on")


async def test_draw_failure_midway_changes_nothing(client, session, monkeypatch):
    h = await auth(session)
    memo, lids, lots = await memo_out(client, h, [(3, 10.0)], lots_per_line=3)
    before = await versions(client, h, memo, None)
    rows = await ledger_rows(session)
    real, stamps = ProjectionEngine._apply, []

    def failing(state_, event_type, data):
        if event_type == "item.status.set" and (data or {}).get("source_doc_id") not in (None, memo):
            stamps.append(data)
            if len(stamps) == 2:
                raise RuntimeError("injected failure after the first stamp")
        return real(state_, event_type, data)

    monkeypatch.setattr(ProjectionEngine, "_apply", staticmethod(failing))
    try:
        r = await draw(client, h, memo, line_ids=lids)
        assert r.status_code >= 500, r.text
    except RuntimeError:
        pass
    monkeypatch.setattr(ProjectionEngine, "_apply", staticmethod(real))
    # The app's request session closes on the failure, discarding what it had not committed;
    # the tests share one session, so it is discarded here.
    await session.rollback()

    assert len(stamps) == 2
    await nothing_written(client, h, memo, lots[0], None, before)
    assert not (await state(client, h, memo)).get("invoiced_on")
    assert await ledger_rows(session) == rows


async def test_draw_unattributable_lot_refused(client, session):
    """Two memo lines of one SKU and a lot whose shipment record is gone: nothing says which
    line it went out on, so the draw refuses before writing."""
    h = await auth(session)
    sku = f"MD-{uuid.uuid4().hex[:6]}"
    first, loose, second = [await lot(client, h, sku, 1) for _ in range(3)]
    memo = (await ok(await client.post("/docs", headers=h, json={"doc_type": "memo", "line_items": [
        {"item_id": first, "sku": sku, "description": sku, "quantity": 2, "unit_price": 10.0, "sell_by": "piece"},
        {"item_id": second, "sku": sku, "description": sku, "quantity": 1, "unit_price": 10.0, "sell_by": "piece"},
    ]})))["id"]
    await ok(await client.post(f"/docs/{memo}/finalize", headers=h))
    lids = [li["line_id"] for li in (await state(client, h, memo))["line_items"]]
    await ok(await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_ids": lids}))
    assert await owner(client, h, loose) == ("memo_out", memo)
    await session.execute(delete(LedgerEntry).where(
        LedgerEntry.entity_id == loose, LedgerEntry.event_type == "item.fulfilled"))
    await session.commit()
    before = await versions(client, h, memo, None)

    r = await draw(client, h, memo, line_ids=lids[:1])

    assert r.status_code == 409, r.text
    await nothing_written(client, h, memo, [first, loose, second], None, before)


# ── One validation order and the preview ──────────────────────────────────────

async def _paid_unrated_memo(client, h, *, full: bool):
    memo, lids, lots = await memo_out(client, h, [(2, 100.0), (2, 50.0)], lots_per_line=2, tax=35.0)
    await ok(await pay(client, h, memo, (await state(client, h, memo))["total"] if full else 50.0))
    return memo, lids, lots


@pytest.mark.parametrize("full", [False, True])
async def test_draw_validation_order_reports_every_problem(client, session, full):
    h = await auth(session)
    memo, lids, lots = await _paid_unrated_memo(client, h, full=full)
    if full:
        assert (await state(client, h, memo))["status"] == "paid"
    before = await versions(client, h, memo, None)
    order = ["docs.draw_memo_has_payment", "docs.draw_quantity_more_than_out", "docs.draw_unrated_tax_partial"]

    r = await draw(client, h, memo, {lids[0]: 5})
    assert r.status_code == 409, r.text
    assert [key_of(r), *also_keys(r)] == order
    preview = await ok(await draw(client, h, memo, {lids[0]: 5}, dry_run=True))
    assert [p["message_key"] for p in preview["problems"]] == order
    await nothing_written(client, h, memo, lots[0] + lots[1], None, before)

    v = (await state(client, h, memo))["version"]
    rate = await client.post(f"/docs/{memo}/tax-rate", headers=h, json={"tax_rate": 7, "expected_version": v})
    assert key_of(rate) == "docs.memo_rate_paid", rate.text
    assert not also_keys(rate)
    for p in (await state(client, h, memo)).get("payments") or []:
        if p.get("status", "active") == "active" and p.get("amount", 0) > 0:
            await ok(await client.post(f"/docs/{memo}/void-payment", headers=h, json={"payment_index": p["index"]}))
    v = (await state(client, h, memo))["version"]
    await ok(await client.post(f"/docs/{memo}/tax-rate", headers=h, json={"tax_rate": 7, "expected_version": v}))


async def test_draw_validation_order_status_refusal_alone(client, session):
    h = await auth(session)
    memo, lids, _ = await memo_out(client, h, [(2, 100.0)], lots_per_line=2, tax=35.0)
    await ok(await pay(client, h, memo, 50.0))
    await ok(await client.post(f"/docs/{memo}/set-available", headers=h, json={"line_ids": lids}))
    await ok(await client.post(f"/docs/{memo}/close", headers=h, json={}))

    r = await draw(client, h, memo, {lids[0]: 5})

    assert key_of(r) == "documents.err_memo_issue_first", r.text
    assert not also_keys(r)


@pytest.mark.parametrize("target", ["new_invoice", "existing_draft"])
async def test_draw_preview_matches_stored(client, session, target):
    h = await auth(session)
    memo, lids, lots = await memo_out(client, h, [(3, 40.0), (2, 25.0)], lots_per_line=1, shipping=50.0,
                                      discount=100.0, discount_type="flat", line_extra={"tax_rate": 7})
    pf = await draft(client, h, lines=[service_line()], shipping=20.0) if target == "existing_draft" else None
    pick = {lids[0]: 2, lids[1]: 1}
    before = await versions(client, h, memo, pf)
    rows = await ledger_rows(session)

    preview = await ok(await draw(client, h, memo, pick, target=pf, dry_run=True))

    await nothing_written(client, h, memo, lots[0] + lots[1], pf, before)
    assert await ledger_rows(session) == rows
    assert preview["problems"] == []
    inv = await drawn(client, h, memo, pick, target=pf,
                      expected_versions={"memo": preview["memo_version"], "target": preview["target_version"]})
    doc, m = await state(client, h, inv), await state(client, h, memo)
    stored = drawn_lines(doc, memo)
    assert [(p["memo_line_id"], p["quantity"], p["line_total"]) for p in preview["lines"]] == \
        [(li["drawn_from"]["memo_line_id"], li["quantity"], li["line_total"]) for li in stored]
    take = m["invoiced_on"][-1]
    assert preview["fixed_taken"] == {"shipping": take["shipping"], "discount": take["discount"], "tax": take["tax"]}
    assert preview["tax"] == pytest.approx(doc["tax"])
    for k in ("subtotal", "discount_amount", "tax", "total"):
        assert preview["totals_after"][k] == pytest.approx(doc[k]), k
    assert preview["left_on_memo"]["fixed"] == m["memo_fixed_amounts"]


@pytest.mark.parametrize("change", ["other_draw", "target_edit", "target_finalize"])
async def test_draw_preview_stale_refused(client, session, change):
    h = await auth(session)
    memo, lids, lots = await memo_out(client, h, [(4, 10.0)], lots_per_line=4)
    pf = await draft(client, h, lines=[service_line()])
    preview = await ok(await draw(client, h, memo, {lids[0]: 1}, target=pf, dry_run=True))
    seen = {"memo": preview["memo_version"], "target": preview["target_version"]}

    if change == "other_draw":
        await drawn(client, h, memo, {lids[0]: 1})
    elif change == "target_edit":
        doc = await state(client, h, pf)
        await ok(await client.patch(f"/docs/{pf}", headers=h, json={"fields_changed": {"notes": {"new": "changed"}}, "expected_version": doc["version"]}))
    else:
        await ok(await client.post(f"/docs/{pf}/finalize", headers=h))
    after = await versions(client, h, memo, pf)

    r = await draw(client, h, memo, {lids[0]: 1}, target=pf, expected_versions=seen)

    assert r.status_code == 409, r.text
    assert key_of(r) == "docs.draw_preview_stale"
    text = r.json()["detail"]["message"]
    m, t = await state(client, h, memo), await state(client, h, pf)
    assert m["ref_id"] in text and t["ref_id"] in text
    if change == "target_finalize":
        assert "docs.draw_target_not_draft" in also_keys(r)
        return
    assert (m["version"], t["version"]) == (after["memo"], after["target"])
    fresh = await ok(await draw(client, h, memo, {lids[0]: 1}, target=pf, dry_run=True))
    await drawn(client, h, memo, {lids[0]: 1}, target=pf,
                expected_versions={"memo": fresh["memo_version"], "target": fresh["target_version"]})
