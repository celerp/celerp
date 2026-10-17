# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Selling an asset through an invoice, and every change to the sale after it.

A contributed car (12,000 over 60 months, straight line, full month, from January 2026)
charges 200 a month. Runs post through March, so a sale on 20 May brings April's 200 up
to date and leaves a carrying amount of 11,200: sold for 10,000 that is a loss of 1,200.
The invoice credits the sale clearing account (1160) with the proceeds and the disposal
debits it with the same amount, so clearing nets to zero per sale.
"""
from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.models.ledger import LedgerEntry
from fa_fixtures import FA
from test_runs import _contributed, _lines, _lock, _managed_from, _movements, _notices, _owner_id, _run, _set_module

pytestmark = pytest.mark.asyncio

D = Decimal
CLEARING = "1160"
MODULE_JE = "je:mod:celerp-fixed-assets:"


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _sell(fa: FA, asset_id: str, on: str = "2026-05-20", price: str = "10000", headers=None) -> str:
    """Sell through a new draft invoice; returns the invoice id."""
    done = await fa.operate(f"/assets/{asset_id}/sell", {"date": on, "price": price, "contact_name": "Buyer Co"},
                            key=f"s-{uuid.uuid4().hex[:8]}", headers=headers)
    return done["invoice_id"]


async def _discard_refused(fa: FA, response):
    """The app shares the test's session, so a refused request's work is rolled back here,
    as closing the request's own session does in production."""
    if response.is_error:
        await fa.session.rollback()
    return response


async def _doc(fa: FA, doc_id: str, action: str, body: dict | None = None, headers=None):
    return await _discard_refused(
        fa, await fa.client.post(f"/docs/{doc_id}/{action}", headers=headers or fa.h, json=body or {}))


async def _finalize(fa: FA, doc_id: str, headers=None) -> None:
    await fa.ok(await _doc(fa, doc_id, "finalize", headers=headers))


async def _asset_line(fa: FA, invoice_id: str) -> dict:
    return next(li for li in (await fa.state(invoice_id))["line_items"] if li.get("asset_id"))


async def _credit_note(fa: FA, invoice_id: str, amount: str, *, on: str, returned: bool = False,
                       line: dict | None = None, tax_rate: float | None = None):
    """Create a credit note on ``invoice_id`` crediting ``amount`` of its asset line (or ``line``)."""
    source = line or await _asset_line(fa, invoice_id)
    item = {"name": source.get("name"), "quantity": 1, "unit_price": float(amount), "line_total": float(amount),
            "source_line_id": source["line_id"]}
    if source.get("asset_id"):
        item["asset_id"] = source["asset_id"]
    if returned:
        item["asset_returned"] = True
    if tax_rate is not None:
        item["tax_rate"] = tax_rate
    body = {"doc_type": "credit_note", "original_doc_id": invoice_id, "issue_date": on, "line_items": [item]}
    if tax_rate is None:
        body["total"] = float(amount)
    return await _discard_refused(fa, await fa.client.post("/docs", headers=fa.h, json=body))


async def _credit(fa: FA, invoice_id: str, amount: str, *, on: str = "2026-06-10", returned: bool = False,
                  **kw) -> str:
    """A finalized credit note; returns its id."""
    cn = (await fa.ok(await _credit_note(fa, invoice_id, amount, on=on, returned=returned, **kw)))["id"]
    await _finalize(fa, cn)
    return cn


async def _number(fa: FA, doc_id: str) -> str:
    state = await fa.state(doc_id)
    return state.get("doc_number") or state.get("ref_id") or doc_id


async def _clearing(fa: FA, asset_id: str) -> tuple[D, D]:
    """Clearing (debit less credit) on the documents' entries for the asset, and on the asset's
    own journals."""
    short = asset_id.removeprefix("fa:asset:")
    docs = mine = D(0)
    for je in await fa.entities("journal_entry"):
        if je.get("status") != "posted":
            continue
        ours = je["entity_id"].startswith(f"{MODULE_JE}asset:{short}:")
        net = sum((D(str(e.get("debit") or 0)) - D(str(e.get("credit") or 0))
                   for e in je.get("entries") or []
                   if e.get("account") == CLEARING and (ours or e.get("asset_id") == asset_id)), D(0))
        if ours:
            mine += net
        elif je["entity_id"].startswith("je:auto:"):
            docs += net
    return docs, mine


async def _assert_clearing(fa: FA, asset_id: str, outstanding: str) -> None:
    """Clearing nets to zero, and the asset's side holds the net proceeds less what was credited."""
    docs, mine = await _clearing(fa, asset_id)
    assert docs + mine == 0, (docs, mine)
    assert mine == D(outstanding), (docs, mine)


async def _sale_journals(fa: FA, asset_id: str) -> list[dict]:
    """The asset's posted journals touching clearing, in the order they were made (one test runs
    in one transaction, so their creation times are equal; the ledger's ids are not)."""
    short = asset_id.removeprefix("fa:asset:")
    found = [je for je in await fa.module_journals()
             if je["entity_id"].startswith(f"{MODULE_JE}asset:{short}:") and je.get("status") == "posted"
             and any(e["account"] == CLEARING for e in je["entries"])]
    first = dict((await fa.session.execute(
        select(LedgerEntry.entity_id, func.min(LedgerEntry.id))
        .where(LedgerEntry.entity_id.in_([je["entity_id"] for je in found]))
        .group_by(LedgerEntry.entity_id))).all())
    return sorted(found, key=lambda je: first[je["entity_id"]])


async def _doc_entry(fa: FA, doc_id: str) -> dict:
    """The document's live issue entry."""
    found = [je for je in await fa.entities("journal_entry")
             if je["entity_id"].startswith(f"je:auto:{doc_id}:") and je.get("status") == "posted"
             and ":fin" in je["entity_id"]]
    assert len(found) == 1, found
    return found[0]


async def _sale(fa: FA, asset_id: str) -> dict:
    return await fa.ok(await fa.get(f"/assets/{asset_id}/sale"))


async def _sold_car(fa: FA, price: str = "10000") -> tuple[str, str]:
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-03")
    inv = await _sell(fa, car, price=price)
    await _finalize(fa, inv)
    return car, inv


async def _patch(fa: FA, doc_id: str, lines: list[dict], **fields):
    """Edit a draft's lines (and any header fields) as the document page does."""
    changed = {"line_items": {"new": lines}, **{k: {"new": v} for k, v in fields.items()}}
    return await _discard_refused(fa, await fa.client.patch(f"/docs/{doc_id}", headers=fa.h,
                                                            json={"fields_changed": changed}))


async def _patch_lines(fa: FA, doc_id: str, lines: list[dict], **fields) -> None:
    r = await _patch(fa, doc_id, lines, **fields)
    assert r.status_code == 200, r.text


async def _assert_totals(fa: FA, doc_id: str) -> None:
    """The document's posted entry totals equal its sale totals, and its stored line
    allocations add up to its net in the books' currency."""
    from celerp.models.ledger import LedgerEntry
    from celerp.services.auto_je import _sale_totals

    doc = await fa.state(doc_id)
    total, tax, revenue = (D(str(v)) for v in _sale_totals(doc, "USD"))
    je = await _doc_entry(fa, doc_id)
    lines = _lines(je)
    receivable = lines["1120"]
    assert receivable[0] + receivable[1] == total, (lines, total)
    assert sum(lines.get("2120", (D(0), D(0))), D(0)) == tax, (lines, tax)
    created = (await fa.session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == fa.auth["company_id"], LedgerEntry.entity_id == je["entity_id"],
        LedgerEntry.event_type == "acc.journal_entry.created"))).scalars().first()
    allocations = (created.metadata_ or {}).get("line_allocations")
    assert allocations, created.metadata_
    assert sum((D(str(a["base"])) for a in allocations.values()), D(0)) == revenue, (allocations, revenue)


# ── S4: the sale invoice lifecycle ───────────────────────────────────────────


async def test_s4_draft_finalize_completes_sale_atomically(fa, monkeypatch):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-03")
    number = (await fa.asset(car))["number"]
    inv = await _sell(fa, car)
    draft = await fa.state(inv)
    assert draft["status"] == "draft" and draft["issue_date"] == "2026-05-20"
    line = await _asset_line(fa, inv)
    assert line["asset_id"] == car and number in line["name"] and not line.get("sku")
    got = await fa.asset(car)
    assert got["status"] == "active" and got["sale"]["state"] == "pending" and got["sale"]["invoice_id"] == inv
    preview = await _sale(fa, car)
    assert D(preview["proceeds"]) == D("10000") and D(preview["carrying_amount"]) == D("11200")
    assert D(preview["gain_or_loss"]) == D("-1200") and preview["journal"]["lines"]

    await _finalize(fa, inv)
    assert _lines(await _doc_entry(fa, inv)) == {"1120": (D("10000"), D(0)), CLEARING: (D(0), D("10000"))}
    [je] = await _sale_journals(fa, car)
    assert je["ts"][:10] == "2026-05-20"
    assert _lines(je) == {"6400": (D("200"), D(0)), "1220": (D("800"), D("200")), CLEARING: (D("10000"), D(0)),
                          "1210": (D(0), D("12000")), "6980": (D("1200"), D(0))}
    got = await fa.asset(car)
    assert got["status"] == "sold" and got["sale"]["state"] == "sold"
    assert got["disposal"]["date"] == "2026-05-20" and D(got["disposal"]["gain_or_loss"]) == D("-1200")
    await _assert_clearing(fa, car, "10000")
    await fa.ok(await _doc(fa, inv, "finalize"))  # finalizing again changes nothing
    assert len(await _sale_journals(fa, car)) == 1
    await fa.fold_matches(car)

    # A disposal that cannot post refuses the finalize: neither the invoice nor the disposal posts.
    from celerp_fixed_assets import disposal

    van = await _contributed(fa, name="Van")
    van_inv = await _sell(fa, van)

    def broken(*args, **kwargs):
        raise disposal.conflict("Disposal failed")

    monkeypatch.setattr(disposal, "_plan_asset", broken)
    refused = await _doc(fa, van_inv, "finalize")
    assert refused.status_code == 409 and "Disposal failed" in refused.json()["detail"]
    assert (await fa.state(van_inv))["status"] == "draft"
    assert not [je for je in await fa.entities("journal_entry") if je["entity_id"].startswith(f"je:auto:{van_inv}:")]
    assert not await _sale_journals(fa, van)
    assert (await fa.asset(van))["sale"]["state"] == "pending"


async def test_s4_void_and_revert_reverse_disposal_pending(fa):
    car, inv = await _sold_car(fa)
    number = (await fa.asset(car))["number"]
    await fa.ok(await _doc(fa, inv, "void"))
    got = await fa.asset(car)
    assert got["status"] == "active" and got["sale"]["state"] == "pending" and got.get("disposal") is None
    assert D(got["cost"]) == D("12000") and D(got["accumulated_depreciation"]) == D("600")
    assert not await _sale_journals(fa, car) or (await _clearing(fa, car))[1] == 0
    await _assert_clearing(fa, car, "0")
    invoice_number = await _number(fa, inv)
    assert any(f"Sale of {number} reversed with invoice {invoice_number}" in n.title for n in await _notices(fa))
    await fa.fold_matches(car)

    await fa.ok(await _doc(fa, inv, "unvoid"))
    got = await fa.asset(car)
    assert got["status"] == "sold" and D(got["disposal"]["gain_or_loss"]) == D("-1200")
    await _assert_clearing(fa, car, "10000")

    await fa.ok(await _doc(fa, inv, "revert-to-draft"))
    got = await fa.asset(car)
    assert got["status"] == "active" and got["sale"]["state"] == "pending"
    await _assert_clearing(fa, car, "0")
    await _finalize(fa, inv)
    assert (await fa.asset(car))["status"] == "sold"
    await _assert_clearing(fa, car, "10000")
    await fa.fold_matches(car)

    # The sale date locked: the void still reverses the disposal, on the first open day.
    sold = (await _sale_journals(fa, car))[-1]["entity_id"]
    await _lock(fa, "2026-05-31")
    await fa.ok(await _doc(fa, inv, "void"))
    got = await fa.asset(car)
    assert got["status"] == "active" and got["sale"]["state"] == "pending"
    await _assert_clearing(fa, car, "0")
    voided = await fa.state(sold)
    assert voided["status"] == "void" and voided["ts"][:10] == "2026-05-20" and voided["reversed_on"] > "2026-05-31"
    await fa.fold_matches(car)


async def test_s4_credit_note_adjusts_proceeds(fa):
    car, inv = await _sold_car(fa)
    made = await fa.operate(f"/assets/{car}/sale/credit", {"kind": "adjust", "amount": "2000", "date": "2026-06-10"},
                            key="adj-1")
    cn = made["credit_note_id"]
    note = await fa.state(cn)
    assert note["status"] == "draft" and note["original_doc_id"] == inv
    assert note["line_items"][0]["asset_id"] == car and not note["line_items"][0].get("asset_returned")
    await _finalize(fa, cn)
    assert _lines(await _doc_entry(fa, cn)) == {CLEARING: (D("2000"), D(0)), "1120": (D(0), D("2000"))}
    adjustment = (await _sale_journals(fa, car))[-1]
    assert adjustment["ts"][:10] == "2026-06-10"
    assert _lines(adjustment) == {"6980": (D("2000"), D(0)), CLEARING: (D(0), D("2000"))}
    sale = await _sale(fa, car)
    assert (D(sale["proceeds"]), D(sale["credited"]), D(sale["remaining"])) == (D("10000"), D("2000"), D("8000"))
    assert [c["id"] for c in sale["credit_notes"]] == [cn]
    assert D((await fa.asset(car))["disposal"]["gain_or_loss"]) == D("-3200")
    await _assert_clearing(fa, car, "8000")
    await fa.ok(await _doc(fa, cn, "void"))
    await _assert_clearing(fa, car, "10000")
    assert D((await _sale(fa, car))["credited"]) == 0


async def test_s4_partial_credit_return_remaining_reverse_return_then_depreciation(fa):
    car, inv = await _sold_car(fa)
    await _assert_clearing(fa, car, "10000")
    await _credit(fa, inv, "2000", on="2026-06-10")
    await _assert_clearing(fa, car, "8000")
    ret = await _credit(fa, inv, "8000", on="2026-07-05", returned=True)
    await _assert_clearing(fa, car, "0")
    back = (await _sale_journals(fa, car))[-1]
    assert back["ts"][:10] == "2026-07-05"
    assert _lines(back) == {"1210": (D("12000"), D(0)), "1220": (D(0), D("800")), CLEARING: (D(0), D("8000")),
                            "6980": (D(0), D("3200"))}
    got = await fa.asset(car)
    assert got["status"] == "active" and got["sale"]["state"] == "returned"
    assert D(got["cost"]) == D("12000") and D(got["accumulated_depreciation"]) == D("800")
    assert D(await fa.balance("6980")) == 0
    await fa.fold_matches(car)

    await _run(fa, "2026-07")
    assert D((await fa.asset(car))["accumulated_depreciation"]) == D("1400")

    await fa.ok(await _doc(fa, ret, "void"))
    again = (await _sale_journals(fa, car))[-1]
    assert again["ts"][:10] == "2026-05-20"
    assert _lines(again) == {"1220": (D("1400"), D(0)), "6400": (D(0), D("600")), CLEARING: (D("8000"), D(0)),
                             "1210": (D(0), D("12000")), "6980": (D("3200"), D(0))}
    moves = await _movements(fa, car)
    assert moves[-1]["periods"] == {"2026-05": "-200.00", "2026-06": "-200.00", "2026-07": "-200.00"}
    got = await fa.asset(car)
    assert got["status"] == "sold" and got["sale"]["state"] == "sold"
    await _assert_clearing(fa, car, "8000")
    await fa.fold_matches(car)


async def test_s4_credit_exceeding_remaining_and_duplicate_refused(fa):
    car, inv = await _sold_car(fa)
    number = (await fa.asset(car))["number"]
    first = await _credit(fa, inv, "2000")
    over = (await fa.ok(await _credit_note(fa, inv, "9000", on="2026-06-12")))["id"]
    refused = await _doc(fa, over, "finalize")
    assert refused.status_code == 409 and "Only 8,000" in refused.json()["detail"]
    assert number in refused.json()["detail"]
    assert (await fa.state(over))["status"] == "draft"
    await _credit(fa, inv, "8000", on="2026-06-15", returned=True)
    second = (await fa.ok(await _credit_note(fa, inv, "1", on="2026-06-16", returned=True)))["id"]
    refused = await _doc(fa, second, "finalize")
    assert refused.status_code == 409 and number in refused.json()["detail"]
    blocked = await _doc(fa, inv, "void")
    assert blocked.status_code == 409 and await _number(fa, first) in blocked.json()["detail"]["message"]
    assert (await fa.state(inv))["status"] != "void"


async def test_s4_operator_finalizes_and_voids_sale(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-03")
    number = (await fa.asset(car))["number"]
    operator = await fa.role_headers("operator")
    inv = await _sell(fa, car, headers=operator)
    await _finalize(fa, inv, headers=operator)
    assert (await fa.asset(car))["status"] == "sold"
    await fa.ok(await _doc(fa, inv, "void", headers=operator))
    assert (await fa.asset(car))["sale"]["state"] == "pending"

    from celerp.services.company_lock import locked_company

    company = await locked_company(fa.session, fa.auth["company_id"])
    grants = dict((company.settings or {}).get("role_grants") or {})
    grants["manage_fixed_assets"] = ["owner", "admin", "manager"]
    company.settings = {**(company.settings or {}), "role_grants": grants}
    await fa.session.commit()
    await fa.ok(await _doc(fa, inv, "unvoid"))  # the owner completes it again
    refused = await _doc(fa, inv, "void", headers=operator)
    assert refused.status_code == 403 and number in refused.json()["detail"]
    assert (await fa.asset(car))["status"] == "sold"
    await fa.ok(await _doc(fa, inv, "revert-to-draft"))
    refused = await _doc(fa, inv, "finalize", headers=operator)
    assert refused.status_code == 403 and number in refused.json()["detail"]
    assert (await fa.state(inv))["status"] == "draft"


async def test_s4_mixed_invoice_goods_only_credit_leaves_clearing(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-03")
    inv = await _sell(fa, car)
    line = await _asset_line(fa, inv)
    await _patch_lines(fa, inv, [line, {"name": "Delivery", "quantity": 1, "unit_price": 500.0}])
    await _finalize(fa, inv)
    assert _lines(await _doc_entry(fa, inv)) == {"1120": (D("10500"), D(0)), CLEARING: (D(0), D("10000")),
                                                 "4100": (D(0), D("500"))}
    await _assert_clearing(fa, car, "10000")
    goods = next(li for li in (await fa.state(inv))["line_items"] if not li.get("asset_id"))
    cn = await _credit(fa, inv, "500", line=goods)
    assert _lines(await _doc_entry(fa, cn)) == {"4100": (D("500"), D(0)), "1120": (D(0), D("500"))}
    await _assert_clearing(fa, car, "10000")
    assert D((await _sale(fa, car))["credited"]) == 0
    stray = {"name": "Car", "quantity": 1, "unit_price": 100.0, "line_total": 100.0, "asset_id": car,
             "source_line_id": "no-such-line"}
    refused = await _discard_refused(fa, await fa.client.post("/docs", headers=fa.h, json={
        "doc_type": "credit_note", "original_doc_id": inv, "line_items": [stray], "total": 100.0}))
    assert refused.status_code == 422 and "Car" in refused.json()["detail"]


async def test_s4_paid_sale_asset_returned(fa):
    car, inv = await _sold_car(fa)
    paid = await _doc(fa, inv, "payment", {"amount": 10000, "method": "cash", "payment_date": "2026-05-25",
                                           "bank_account": "1111"})
    assert paid.status_code == 200, paid.text
    assert (await _doc(fa, inv, "void")).status_code == 409
    await _credit(fa, inv, "2000")
    short = (await fa.ok(await _credit_note(fa, inv, "7000", on="2026-06-20", returned=True)))["id"]
    refused = await _doc(fa, short, "finalize")
    assert refused.status_code == 409 and "8,000" in refused.json()["detail"]
    made = await fa.operate(f"/assets/{car}/sale/credit", {"kind": "returned", "date": "2026-06-20"}, key="ret-1")
    note = await fa.state(made["credit_note_id"])
    assert note["line_items"][0]["asset_returned"] is True and D(str(note["total"])) == D("8000")
    await _finalize(fa, made["credit_note_id"])
    got = await fa.asset(car)
    assert got["status"] == "active" and got["sale"]["state"] == "returned"
    assert D(got["nbv"]) == D("11200")
    await _assert_clearing(fa, car, "0")
    await _run(fa, "2026-06")
    assert D((await fa.asset(car))["accumulated_depreciation"]) == D("1200")
    await fa.fold_matches(car)


async def test_s4_reissued_in_later_period_recomputes(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-02")
    inv = await _sell(fa, car, on="2026-03-10")
    await _finalize(fa, inv)
    assert D((await fa.asset(car))["disposal"]["gain_or_loss"]) == D("-1600")
    await fa.ok(await _doc(fa, inv, "void"))
    await _run(fa, "2026-05")
    assert D((await fa.asset(car))["accumulated_depreciation"]) == D("1000")
    again = await fa.operate(f"/assets/{car}/sale/reissue", {"date": "2026-04-15"}, key="again-1")
    assert again["invoice_id"] != inv and (await fa.asset(car))["sale"]["invoice_id"] == again["invoice_id"]
    await _finalize(fa, again["invoice_id"])
    je = (await _sale_journals(fa, car))[-1]
    assert je["ts"][:10] == "2026-04-15"
    assert _lines(je) == {"1220": (D("1000"), D(0)), "6400": (D(0), D("400")), CLEARING: (D("10000"), D(0)),
                          "1210": (D(0), D("12000")), "6980": (D("1400"), D(0))}
    assert (await _movements(fa, car))[-1]["periods"] == {"2026-04": "-200.00", "2026-05": "-200.00"}
    assert D((await fa.asset(car))["disposal"]["gain_or_loss"]) == D("-1400")
    await _assert_clearing(fa, car, "10000")
    await fa.fold_matches(car)


@pytest.mark.parametrize("caller", ["route", "finalize_document", "import", "legacy", "repair"])
async def test_s4_every_caller_passes_hook(fa, caller):
    from celerp.services import auto_je
    from celerp_docs.routes import finalize_document

    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-03")
    number = (await fa.asset(car))["number"]
    cid, uid = fa.auth["company_id"], await _owner_id(fa)
    if caller == "import":
        doc = {"doc_type": "invoice", "status": "final", "currency": "USD", "issue_date": "2026-05-20",
               "total": 10000.0, "tax": 0.0, "line_items": [
                   {"line_id": "imp-1", "name": "Car", "quantity": 1, "unit_price": 10000.0, "asset_id": car}]}
        with pytest.raises(HTTPException) as refused:
            await auto_je.create_for_doc_finalized(fa.session, company_id=cid, user_id=uid, doc_id="doc:IMP-1",
                                                   doc=doc)
        assert number in refused.value.detail
        await fa.session.rollback()
        return
    inv = await _sell(fa, car)
    if caller == "finalize_document":
        await finalize_document(inv, cid, SimpleNamespace(id=uid), fa.session)
    else:
        await _finalize(fa, inv)
    assert (await fa.asset(car))["status"] == "sold"
    if caller == "legacy":
        source = await _asset_line(fa, inv)
        doc = {"doc_type": "credit_note", "status": "final", "currency": "USD", "issue_date": "2026-06-10",
               "original_doc_id": inv, "total": 1000.0, "tax": 0.0, "line_items": [
                   {"line_id": "cn-1", "name": "Car", "quantity": 1, "unit_price": 1000.0, "asset_id": car,
                    "source_line_id": source["line_id"]}]}
        with pytest.raises(HTTPException) as refused:
            await auto_je.create_for_credit_note_finalized(fa.session, company_id=cid, user_id=None,
                                                           doc_id="doc:CN-LEGACY", doc=doc)
        assert number in refused.value.detail
        await fa.session.rollback()
    if caller == "repair":
        before = len(await fa.module_journals())
        await auto_je.create_for_doc_finalized(fa.session, company_id=cid, user_id=uid, doc_id=inv,
                                               doc=await fa.state(inv))
        await fa.session.commit()
        assert len(await fa.module_journals()) == before
    assert (await fa.asset(car))["status"] == "sold"
    await _assert_clearing(fa, car, "10000")


async def test_s4_proceeds_net_of_tax(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-03")
    inv = await _sell(fa, car, price="9000")
    await _patch_lines(fa, inv, [{**await _asset_line(fa, inv), "tax_rate": 7}])
    assert D(str((await fa.state(inv))["total"])) == D("9630")
    assert D((await _sale(fa, car))["proceeds"]) == D("9000")
    await _finalize(fa, inv)
    assert _lines(await _doc_entry(fa, inv)) == {"1120": (D("9630"), D(0)), CLEARING: (D(0), D("9000")),
                                                 "2120": (D(0), D("630"))}
    assert D((await fa.asset(car))["disposal"]["gain_or_loss"]) == D("-2200")
    await _assert_clearing(fa, car, "9000")
    await _assert_totals(fa, inv)


async def _fx_mixed_sale(fa: FA) -> tuple[str, str]:
    """A EUR invoice in the USD books: the car and a delivery line, a discount, shipping and 7% tax."""
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-03")
    inv = await _sell(fa, car, price="8000")
    line = {**await _asset_line(fa, inv), "tax_rate": 7}
    await _patch_lines(fa, inv, [line, {"name": "Delivery", "quantity": 1, "unit_price": 2000.0, "tax_rate": 7}],
                       currency="EUR", conversion_rate=1.0837, discount=100, shipping=50)
    await _finalize(fa, inv)
    return car, inv


async def _allocations(fa: FA, doc_id: str) -> dict:
    from celerp.models.ledger import LedgerEntry

    je = await _doc_entry(fa, doc_id)
    created = (await fa.session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == fa.auth["company_id"], LedgerEntry.entity_id == je["entity_id"],
        LedgerEntry.event_type == "acc.journal_entry.created"))).scalars().first()
    return (created.metadata_ or {})["line_allocations"]


async def test_s4_mixed_invoice_discount_tax_fx_allocation(fa):
    car, inv = await _fx_mixed_sale(fa)
    await _assert_totals(fa, inv)
    line = await _asset_line(fa, inv)
    mine = next(a for a in (await _allocations(fa, inv)).values() if a["line_id"] == line["line_id"])
    entry = _lines(await _doc_entry(fa, inv))
    assert entry[CLEARING] == (D(0), D(str(mine["base"])))
    assert D((await fa.asset(car))["sale"]["proceeds"]) == D(str(mine["base"]))
    await _assert_clearing(fa, car, str(mine["base"]))
    half_doc = (D(str(mine["doc"])) / 2).quantize(D("0.01"))
    cn = await _credit(fa, inv, str(half_doc))
    debit = _lines(await _doc_entry(fa, cn))[CLEARING][0]
    assert abs(debit - D(str(mine["base"])) / 2) <= D("0.01"), (debit, mine)
    await _assert_clearing(fa, car, str(D(str(mine["base"])) - debit))


async def test_s4_unvoid_follows_restored_gl(fa):
    from celerp.models.projections import Projection

    car, inv = await _fx_mixed_sale(fa)
    proceeds = _lines(await _doc_entry(fa, inv))[CLEARING][1]
    await fa.ok(await _doc(fa, inv, "void"))
    # The rate and the price on the document change while it is void; the books do not.
    row = await fa.session.get(Projection, {"company_id": fa.auth["company_id"], "entity_id": inv})
    lines = [dict(li, unit_price=li["unit_price"] * 2) for li in row.state["line_items"]]
    row.state = {**row.state, "conversion_rate": 2.5, "line_items": lines}
    await fa.session.commit()
    await fa.ok(await _doc(fa, inv, "unvoid"))
    assert _lines((await _sale_journals(fa, car))[-1])[CLEARING] == (proceeds, D(0))
    assert D((await fa.asset(car))["sale"]["proceeds"]) == proceeds
    await _assert_clearing(fa, car, str(proceeds))


async def test_s4_document_write_validation(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    van = await _contributed(fa, name="Van")
    car_number = (await fa.asset(car))["number"]

    async def create(doc_type: str, lines: list[dict], **body):
        return await _discard_refused(
            fa, await fa.client.post("/docs", headers=fa.h, json={"doc_type": doc_type, "line_items": lines, **body}))

    def line(name: str, asset_id: str | None = None, **kw) -> dict:
        return {"name": name, "quantity": 1, "unit_price": 100.0, **({"asset_id": asset_id} if asset_id else {}), **kw}

    on_bill = await create("bill", [line("Bill line", car)])
    assert on_bill.status_code == 422 and "Bill line" in on_bill.json()["detail"]
    not_pending = await create("invoice", [line("Unsold car", car)])
    assert not_pending.status_code in (409, 422) and car_number in not_pending.json()["detail"]
    inv = await _sell(fa, car, on="2026-01-20")
    twice = await create("invoice", [line("Car again", car), line("Car twice", car)])
    assert twice.status_code == 422 and "Car twice" in twice.json()["detail"]
    other = await create("invoice", [line("Car elsewhere", car)])
    assert other.status_code == 409 and f"{car_number} is on draft invoice" in other.json()["detail"]
    asset_line = await _asset_line(fa, inv)
    two = await _patch(fa, inv, [{**asset_line, "quantity": 2}])
    assert two.status_code == 422 and asset_line["name"] in two.json()["detail"]
    swapped = await _patch(fa, inv, [{**asset_line, "asset_id": van}])
    assert swapped.status_code in (409, 422)
    await _patch_lines(fa, inv, [asset_line, {"name": "Delivery", "quantity": 1, "unit_price": 50.0}])
    await _finalize(fa, inv)
    goods = next(li for li in (await fa.state(inv))["line_items"] if not li.get("asset_id"))
    credit = await create("credit_note", [line("Wrong credit", car, source_line_id=goods["line_id"], unit_price=50.0)],
                          original_doc_id=inv, total=50.0)
    assert credit.status_code == 422 and "Wrong credit" in credit.json()["detail"]
    final = await _patch(fa, inv, [{**asset_line, "asset_id": van}])
    assert final.status_code == 409
    assert (await _asset_line(fa, inv))["asset_id"] == car


async def test_s4_finalize_races_correct_dispose_and_disable(committed_engine):
    from celerp.models.projections import Projection
    from celerp.modules.registry import get_enabled, set_enabled
    from celerp.services.company_lock import locked_company
    from celerp_docs.routes import finalize_document
    from celerp_fixed_assets import corrections, disposal, sales
    from test_runs import _race_company

    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    cid, uid = await _race_company(factory)
    async with factory() as s:
        car, state = (await s.execute(select(Projection.entity_id, Projection.state).where(
            Projection.company_id == cid, Projection.entity_type == "fa_asset"))).one()
        company = await locked_company(s, cid)
        company.settings = set_enabled(dict(company.settings or {}),
                                       get_enabled(company.settings) | {"celerp-fixed-assets", "celerp-docs",
                                                                        "celerp-accounting"})
        await s.commit()
    number = state["number"]
    body = {"date": "2026-05-20", "price": "10000", "contact_name": "Buyer Co", "op_key": "sell"}
    async with factory() as s:
        preview = await sales.sell(s, cid, uid, car, {**body, "preview": True})
        inv = (await sales.sell(s, cid, uid, car, {**body, "fingerprint": preview["fingerprint"]}))["invoice_id"]
        await s.commit()

    async def attempt(call):
        async with factory() as s:
            try:
                result = await call(s)
                await s.commit()
                return result
            except HTTPException as exc:
                await s.rollback()
                return exc

    async def operate(s, fn, *args, body):
        preview = await fn(s, cid, uid, *args, {**body, "preview": True})
        return await fn(s, cid, uid, *args, {**body, "fingerprint": preview["fingerprint"]})

    async def disable(s):
        company = await locked_company(s, cid)
        company.settings = set_enabled(dict(company.settings or {}),
                                       get_enabled(company.settings) - {"celerp-fixed-assets"})

    outcomes = await asyncio.wait_for(asyncio.gather(
        attempt(lambda s: finalize_document(inv, cid, SimpleNamespace(id=uid), s, commit=False)),
        attempt(lambda s: operate(s, corrections.correct, car, body={"changes": {"cost": "13000"},
                                                                     "posting_date": "2026-05-01", "op_key": "c"})),
        attempt(lambda s: operate(s, disposal.dispose, body={"date": "2026-05-25", "reason": "scrapped",
                                                             "asset_ids": [car], "op_key": "d"})),
        attempt(disable),
    ), timeout=25)
    for outcome in outcomes:
        if isinstance(outcome, HTTPException):
            assert outcome.status_code in (403, 409, 422) and number in str(outcome.detail), outcome.detail
    async with factory() as s:
        asset = (await s.get(Projection, {"company_id": cid, "entity_id": car})).state
        invoice = (await s.get(Projection, {"company_id": cid, "entity_id": inv})).state
    assert (invoice["status"] == "draft") == (asset["status"] != "sold")
    assert asset["status"] in ("active", "sold")


async def test_s4_deleted_draft_and_double_sale_refused(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    number = (await fa.asset(car))["number"]
    inv = await _sell(fa, car, on="2026-02-10")
    draft_number = await _number(fa, inv)
    twice = await fa.post(f"/assets/{car}/sell", {"date": "2026-02-11", "price": "1", "contact_name": "X",
                                                  "preview": True, "op_key": "twice"})
    assert twice.status_code == 409 and f"{number} is on draft invoice {draft_number}" in twice.json()["detail"]
    scrap = await fa.post(f"/assets/{car}/dispose", {"date": "2026-02-11", "reason": "scrapped", "preview": True,
                                                     "op_key": "scrap"})
    assert scrap.status_code == 409 and f"{number} is on draft invoice {draft_number}" in scrap.json()["detail"]
    assert (await fa.client.delete(f"/docs/{inv}", headers=fa.h)).status_code == 200
    sale = await _sale(fa, car)
    assert sale["draft_deleted"] is True and set(sale["actions"]) >= {"reissue", "cancel"}
    again = await fa.operate(f"/assets/{car}/sale/reissue", {}, key="again")
    assert (await fa.state(again["invoice_id"]))["status"] == "draft"
    assert (await fa.asset(car))["sale"]["invoice_id"] == again["invoice_id"]
    await fa.operate(f"/assets/{car}/sale/cancel", {}, key="cancel")
    got = await fa.asset(car)
    assert got["status"] == "active" and not got.get("sale")
    assert (await fa.state(again["invoice_id"])) is None or (await fa.state(again["invoice_id"]))["status"] == "void"
    await fa.ok(await fa.post(f"/assets/{car}/dispose", {"date": "2026-02-11", "reason": "scrapped",
                                                         "preview": True, "op_key": "scrap2"}))


async def test_s4_module_off_void_and_credit_correct_disposal(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    van = await _contributed(fa, name="Van")
    truck = await _contributed(fa, name="Truck")
    await _run(fa, "2026-03")
    car_inv, van_inv = await _sell(fa, car), await _sell(fa, van)
    await _finalize(fa, car_inv)
    await _finalize(fa, van_inv)
    truck_inv = await _sell(fa, truck)
    truck_number = (await fa.asset(truck))["number"]
    await _set_module(fa, False)
    await _credit(fa, car_inv, "2000")
    await _assert_clearing(fa, car, "8000")
    await fa.ok(await _doc(fa, van_inv, "void"))
    assert (await fa.asset(van))["sale"]["state"] == "pending"
    await _assert_clearing(fa, van, "0")
    assert (await fa.asset(car))["status"] == "sold"
    refused = await _doc(fa, truck_inv, "finalize")
    assert refused.status_code == 409 and truck_number in refused.json()["detail"]
    assert (await fa.state(truck_inv))["status"] == "draft"


async def test_s4_module_off_unvoid_completes_the_sale(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-03")
    inv = await _sell(fa, car)
    await _finalize(fa, inv)
    await fa.ok(await _doc(fa, inv, "void"))
    await _set_module(fa, False)
    await fa.ok(await _doc(fa, inv, "unvoid"))
    assert (await fa.asset(car))["status"] == "sold"
    await _assert_clearing(fa, car, "10000")


# ── 3.1 invariants ───────────────────────────────────────────────────────────


async def test_invariant_clearing_nets_zero_per_sale(fa):
    car, inv = await _sold_car(fa)
    await _assert_clearing(fa, car, "10000")
    adjust = await _credit(fa, inv, "2000")
    await _assert_clearing(fa, car, "8000")
    await fa.ok(await _doc(fa, adjust, "void"))
    await _assert_clearing(fa, car, "10000")
    await fa.ok(await _doc(fa, adjust, "unvoid"))
    await _assert_clearing(fa, car, "8000")
    ret = await _credit(fa, inv, "8000", on="2026-07-01", returned=True)
    await _assert_clearing(fa, car, "0")
    await fa.ok(await _doc(fa, ret, "void"))
    await _assert_clearing(fa, car, "8000")
    await fa.ok(await _doc(fa, adjust, "void"))
    await _assert_clearing(fa, car, "10000")
    await fa.ok(await _doc(fa, inv, "void"))
    await _assert_clearing(fa, car, "0")
    await fa.ok(await _doc(fa, inv, "unvoid"))
    await _assert_clearing(fa, car, "10000")
    await fa.ok(await _doc(fa, inv, "revert-to-draft"))
    await _assert_clearing(fa, car, "0")
    await _finalize(fa, inv)
    await _assert_clearing(fa, car, "10000")
    await fa.fold_matches(car)


async def test_invariant_document_totals_unchanged_by_asset_lines(fa):
    car, inv = await _fx_mixed_sale(fa)
    await _assert_totals(fa, inv)
    line = await _asset_line(fa, inv)
    cn = await _credit(fa, inv, "100", tax_rate=7, line=line)
    from celerp.services.auto_je import _sale_totals

    total, tax, _ = _sale_totals(await fa.state(cn), "USD")
    lines = _lines(await _doc_entry(fa, cn))
    assert sum(lines["1120"], D(0)) == D(str(total)) and sum(lines.get("2120", (D(0), D(0))), D(0)) == D(str(tax))
    van = await _contributed(fa, name="Van")
    plain = await _sell(fa, van, price="5000")
    await _finalize(fa, plain)
    await _assert_totals(fa, plain)


# ── The disposal result follows the net proceeds ─────────────────────────────
#
# Gain and loss on separate accounts (the seeded chart shares one), so each account must
# hold the true figure, not just their net. The car carries 11,200 on 20 May.

GAIN, LOSS = "4980", "6980"


async def _split_result_accounts(fa: FA) -> None:
    r = await fa.client.post("/accounting/accounts", headers=fa.h, json={
        "code": GAIN, "name": "Gain on disposal of assets", "account_type": "revenue"})
    assert r.status_code in (200, 201), r.text
    r = await fa.client.put("/accounting/posting-accounts/disposal_gain", headers=fa.h, json={"code": GAIN})
    assert r.status_code == 200, r.text


async def _sold_split(fa: FA, price: str) -> tuple[str, str]:
    await _split_result_accounts(fa)
    return await _sold_car(fa, price=price)


async def _fixed(fa: FA) -> tuple[D, D]:
    """Cost and accumulated depreciation across the books (the demo company holds other assets)."""
    return await fa.balance("1210"), await fa.balance("1220")


async def _assert_result(fa: FA, asset_id: str, gain: str, loss: str, remaining: str, fixed=None) -> tuple[D, D]:
    """The gain account holds ``gain`` (credit), the loss account ``loss`` (debit), the asset
    records their difference, clearing nets to zero, and cost and depreciation stand at
    ``fixed`` (as at the sale). Returns cost and depreciation."""
    assert (-await fa.balance(GAIN), await fa.balance(LOSS)) == (D(gain), D(loss))
    assert D((await fa.asset(asset_id))["disposal"]["gain_or_loss"]) == D(gain) - D(loss)
    assert D((await _sale(fa, asset_id))["remaining"]) == D(remaining)
    await _assert_clearing(fa, asset_id, remaining)
    now = await _fixed(fa)
    assert fixed is None or now == fixed, (now, fixed)
    return now


async def test_d1_credit_shrinks_a_gain(fa):
    car, inv = await _sold_split(fa, "11300")
    fixed = await _assert_result(fa, car, "100", "0", "11300")
    await _credit(fa, inv, "30")
    assert _lines((await _sale_journals(fa, car))[-1]) == {GAIN: (D("30"), D(0)), CLEARING: (D(0), D("30"))}
    await _assert_result(fa, car, "70", "0", "11270", fixed)


async def test_d1_credit_turns_a_gain_into_a_loss_and_back(fa):
    car, inv = await _sold_split(fa, "11250")
    fixed = await _assert_result(fa, car, "50", "0", "11250")
    cn = await _credit(fa, inv, "100")
    assert _lines((await _sale_journals(fa, car))[-1]) == {
        GAIN: (D("50"), D(0)), LOSS: (D("50"), D(0)), CLEARING: (D(0), D("100"))}
    await _assert_result(fa, car, "0", "50", "11150", fixed)
    await fa.ok(await _doc(fa, cn, "void"))
    await _assert_result(fa, car, "50", "0", "11250", fixed)
    await fa.ok(await _doc(fa, cn, "unvoid"))
    await _assert_result(fa, car, "0", "50", "11150", fixed)
    await fa.fold_matches(car)


async def test_d1_credit_grows_a_loss(fa):
    car, inv = await _sold_split(fa, "10000")
    fixed = await _assert_result(fa, car, "0", "1200", "10000")
    await _credit(fa, inv, "2000")
    await _assert_result(fa, car, "0", "3200", "8000", fixed)


async def test_d1_credits_voided_out_of_order(fa):
    car, inv = await _sold_split(fa, "11300")
    first = await _credit(fa, inv, "60")
    fixed = await _assert_result(fa, car, "40", "0", "11240")
    second = await _credit(fa, inv, "90", on="2026-06-20")
    await _assert_result(fa, car, "0", "50", "11150", fixed)
    await fa.ok(await _doc(fa, first, "void"))
    await _assert_result(fa, car, "10", "0", "11210", fixed)
    await fa.ok(await _doc(fa, second, "void"))
    await _assert_result(fa, car, "100", "0", "11300", fixed)
    await fa.fold_matches(car)


async def test_d1_return_then_reversals_leave_both_accounts_empty(fa):
    car, inv = await _sold_split(fa, "11250")
    adjust = await _credit(fa, inv, "100")
    ret = await _credit(fa, inv, "11150", on="2026-07-01", returned=True)
    assert (-await fa.balance(GAIN), await fa.balance(LOSS)) == (D(0), D(0))
    await fa.ok(await _doc(fa, ret, "void"))
    fixed = await _assert_result(fa, car, "0", "50", "11150")
    await fa.ok(await _doc(fa, adjust, "void"))
    await _assert_result(fa, car, "50", "0", "11250", fixed)
    await fa.ok(await _doc(fa, inv, "void"))
    assert (await fa.balance(GAIN), await fa.balance(LOSS)) == (D(0), D(0))
    await _assert_clearing(fa, car, "0")
    await fa.ok(await _doc(fa, inv, "unvoid"))
    await _assert_result(fa, car, "50", "0", "11250", fixed)
    await fa.fold_matches(car)


async def test_d1_foreign_currency_credit_splits_in_the_books_currency(fa):
    await _split_result_accounts(fa)
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-03")
    inv = await _sell(fa, car, price="9000")
    await _patch_lines(fa, inv, [await _asset_line(fa, inv)], currency="EUR", conversion_rate=1.25)
    await _finalize(fa, inv)
    fixed = await _assert_result(fa, car, "50", "0", "11250")
    made = await fa.operate(f"/assets/{car}/sale/credit", {"kind": "adjust", "amount": "80", "date": "2026-06-10"},
                            key="fx-adj")
    await _finalize(fa, made["credit_note_id"])
    await _assert_result(fa, car, "0", "50", "11150", fixed)
