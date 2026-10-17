# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Asset money at the books' currency precision.

Every amount the register stores is rounded once, with the canonical money rules, to the
precision of the currency the books are kept in: two places for USD, none for JPY, three
for KWD and four for CLF. What an asset records equals what its journal posts, and what
the API returns is the stored amount at that precision, never a two-place rendering of it.
Each company here is a demo company whose books are set to the currency before its demo
records are imported.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from fa_fixtures import FA, start_demo
from test_runs import _contributed, _managed_from, _movements, _run
from test_sales import _assert_clearing, _credit, _doc, _finalize, _sale, _sell

D = Decimal
MODULE_JE = "je:mod:celerp-fixed-assets:"


def _places(text: str) -> int:
    return len(text.split(".", 1)[1]) if "." in text else 0


async def _demo(client, session, currency: str) -> FA:
    return await start_demo(client, session, currency)


async def _acquisition(fa: FA, asset_id: str) -> dict:
    short = asset_id.removeprefix("fa:asset:")
    (je,) = [je for je in await fa.module_journals() if je["entity_id"].startswith(f"{MODULE_JE}asset:{short}:acq:")]
    return je


def test_money_text_keeps_currency_precision():
    from celerp_fixed_assets.projections import money

    assert money(D("100.125"), "KWD") == "100.125"
    assert money("1000003", "JPY") == "1000003"
    assert money(D("100.12345"), "CLF") == "100.1235"
    assert money(D("100.005"), "USD") == "100.01"
    assert money(None, "USD") == "0.00"
    # What is stored reads back as the same amount.
    for value, currency in (("100.125", "KWD"), ("1000003", "JPY"), ("100.1235", "CLF"), ("0.10", "USD")):
        assert D(json.loads(json.dumps({"v": money(value, currency)}))["v"]) == D(value)


@pytest.mark.parametrize("currency", ["KWD", "JPY"])
async def test_f01_sample_bills_follow_the_books_currency(client, session, currency):
    """Setting the books' currency before the company keeps books of its own brings the
    sample asset bills along, so their lines can be registered at their sample figures."""
    fa = await _demo(client, session, currency)
    bills = [d for d in await fa.entities("doc") if d.get("doc_type") == "bill"]
    assert len(bills) == 4 and {b["currency"] for b in bills} == {currency}
    van = await fa.line("Delivery van")
    assert D(van["amount"]) == D("32000")
    # Changing it again before any books of its own follows again; the bills stay four.
    await fa.ok(await fa.client.patch("/companies/me/books", headers=fa.h, json={"currency": "USD"}))
    bills = [d for d in await fa.entities("doc") if d.get("doc_type") == "bill"]
    assert len(bills) == 4 and {b["currency"] for b in bills} == {"USD"}


@pytest.mark.parametrize("currency,cost,stored", [
    ("USD", "100.005", "100.01"),
    ("JPY", "1000003.4", "1000003"),
    ("KWD", "100.1255", "100.126"),
])
async def test_f01_contributed_cost_equals_journal(client, session, currency, cost, stored):
    """A cost entered with more places than the currency has is rounded once, and the
    asset, its movement and its journal all hold that one amount."""
    fa = await _demo(client, session, currency)
    await _managed_from(fa)
    class_id = (await fa.classes())["Vehicles"]["id"]
    body = {"kind": "contributed", "class_id": class_id, "name": "Car", "available_date": "2026-01-15",
            "cost": cost, "credit_account": "3100"}
    preview = await fa.ok(await fa.post("/assets", {**body, "preview": True, "op_key": "c-1"}))
    assert preview["assets"][0]["cost"] == stored
    assert preview["journals"][0]["lines"][0]["debit"] == stored
    done = await fa.operate("/assets", body, key="c-2")
    car = done["assets"][0]["id"]
    (mv,) = await _movements(fa, car)
    assert mv["amounts"]["cost"] == stored
    je = await _acquisition(fa, car)
    assert D(str(je["entries"][0]["debit"])) == D(stored)
    asset = await fa.asset(car)
    assert D(asset["cost"]) == D(stored) and D(asset["nbv"]) == D(stored)
    await fa.fold_matches(car)


async def test_f01_jpy_run_and_sale_have_no_fraction(client, session):
    fa = await _demo(client, session, "JPY")
    await _managed_from(fa)
    car = await _contributed(fa, cost="1000003")
    preview = await fa.ok(await fa.post("/runs", {"month": "2026-03", "preview": True, "op_key": "r-p"}))
    totals = [m["total"] for m in preview["months"]]
    assert totals and all(_places(t) == 0 for t in totals), totals
    assert _places(preview["total"]) == 0
    await _run(fa, "2026-03")
    sell = await fa.ok(await fa.post(f"/assets/{car}/sell", {"date": "2026-05-20", "price": "500000",
                                                            "contact_id": await fa.buyer(), "preview": True,
                                                            "op_key": "s-p"}))
    assert sell["price"] == "500000" and _places(sell["carrying_amount"]) == 0
    inv = await _sell(fa, car, price="500000")
    await _finalize(fa, inv)
    sale = await _sale(fa, car)
    assert sale["proceeds"] == "500000" and sale["remaining"] == "500000"
    await _assert_clearing(fa, car, "500000")
    await fa.fold_matches(car)


async def test_f01_kwd_sale_credit_return_and_refinalize_keep_three_places(client, session):
    """A KWD sale of 100.125 keeps its third place through the sale, a partial credit, the
    return of what is left, and the sale restored when the return is voided."""
    fa = await _demo(client, session, "KWD")
    await _managed_from(fa)
    car = await _contributed(fa)
    preview = await fa.ok(await fa.post("/runs", {"month": "2026-03", "preview": True, "op_key": "r-p"}))
    assert [m["total"] for m in preview["months"]] == ["200.000", "200.000", "200.000"]
    assert preview["total"] == "600.000"
    await _run(fa, "2026-03")
    inv = await _sell(fa, car, price="100.125")
    await _finalize(fa, inv)
    sale = await _sale(fa, car)
    assert (sale["proceeds"], sale["credited"], sale["remaining"]) == ("100.125", "0.000", "100.125")
    assert (await fa.state(car))["sale"]["proceeds"] == "100.125"
    assert all(_places(ln[k]) == 3 for ln in sale["journal"]["lines"] for k in ("debit", "credit"))
    await _assert_clearing(fa, car, "100.125")

    cn = await _credit(fa, inv, "0.125")
    sale = await _sale(fa, car)
    assert sale["credit_notes"][0]["amount"] == "0.125"
    assert (sale["credited"], sale["remaining"]) == ("0.125", "100.000")
    await _assert_clearing(fa, car, "100.000")

    back = await fa.ok(await fa.post(f"/assets/{car}/sale/credit", {
        "kind": "returned", "date": "2026-07-05", "preview": True, "op_key": "ret-p"}))
    assert back["amount"] == "100.000" and back["remaining"] == "100.000"
    ret = await fa.operate(f"/assets/{car}/sale/credit", {"kind": "returned", "date": "2026-07-05"}, key="ret")
    await _finalize(fa, ret["credit_note_id"])
    await _assert_clearing(fa, car, "0")
    got = await fa.asset(car)
    assert got["status"] == "active" and got["sale"]["credits"][ret["credit_note_id"]]["amount"] == "100.000"
    await fa.fold_matches(car)

    await fa.ok(await _doc(fa, ret["credit_note_id"], "void"))
    sale = await _sale(fa, car)
    assert sale["state"] == "sold" and sale["proceeds"] == "100.125" and sale["remaining"] == "100.000"
    await _assert_clearing(fa, car, "100.000")
    await fa.ok(await _doc(fa, cn, "void"))
    await _assert_clearing(fa, car, "100.125")
    await fa.fold_matches(car)
    assert D(await fa.balance("1160")) == 0
