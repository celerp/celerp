# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Taxes that change what a document charges: prices that already include the tax, tax on the
shipping charge, reverse charge (the buyer books the tax both ways and pays the supplier only
the price) and perception (an extra charge collected for the tax authority)."""
from __future__ import annotations

import pytest

from tax_money_support import (_account_net, _moved, code, codes, company, doc, finalized, get, line, patch_doc, role_funds, roles)

pytestmark = pytest.mark.asyncio


async def test_inclusive_back_calculation_rounding(client, session):
    auth = await company(session)
    vat = (await codes(client, auth, [code("VAT", 7)]))["VAT"]["id"]
    pvat = (await codes(client, auth, [code("Input VAT", 7)], "purchase"))["Input VAT"]["id"]

    before = await roles(session, auth)
    one = await doc(client, auth, "invoice", [line(107.0, vat)], prices_include_tax=True)
    issued = await finalized(client, auth, one)
    assert (issued["subtotal"], issued["tax"], issued["total"]) == (100.0, 7.0, 107.0)
    [snap] = issued["line_items"][0]["taxes"]
    assert (snap["inclusive"], snap["base"], snap["amount"]) == (True, 100.0, 7.0)
    after = await roles(session, auth)
    assert _moved(before, after) == {"receivable": 107.0, "sales_revenue": -100.0, "tax_output": -7.0}

    # Each line is back-calculated and rounded on its own: 10.00 holds 0.65 of tax, three times.
    three = await get(client, auth, await doc(client, auth, "invoice", [line(10.0, vat)] * 3,
                                              prices_include_tax=True))
    assert (three["tax"], three["total"]) == (1.95, 30.0)

    bill = await doc(client, auth, "bill", [line(107.0, pvat)], prices_include_tax=True)
    await finalized(client, auth, bill)
    moved = _moved(after, await roles(session, auth))
    assert (moved["payable"], moved["tax_input"]) == (-107.0, 7.0)

    # A price list can hold the flag; a document on it starts with it.
    h = auth["headers"]
    lists = (await client.get("/companies/me/price-lists", headers=h)).json()
    gross = {"name": "Shop", "prices_include_tax": True}
    r = await client.patch("/companies/me/price-lists", headers=h, json={"price_lists": [*lists, gross]})
    assert r.status_code == 200, r.text
    on_list = await get(client, auth, await doc(client, auth, "invoice", [line(50.0, vat)], price_list="Shop"))
    assert on_list["prices_include_tax"] is True

    r = await client.patch("/companies/me/price-lists", headers=h,
                           json={"price_lists": [*lists, {**gross, "prices_include_tax": "yes"}]})
    assert r.status_code == 422, r.text
    r = await patch_doc(client, auth, on_list["id"], "prices_include_tax", "yes")
    assert r.status_code == 422, r.text
    assert (await get(client, auth, on_list["id"]))["prices_include_tax"] is True


async def test_inclusive_toggle_then_line_save(client, session):
    """Turning a draft's Prices include tax off and saving a line with the taxes the reloaded
    page holds is accepted, and the stored taxes follow the new flag."""
    auth = await company(session)
    vat = (await codes(client, auth, [code("VAT", 7)]))["VAT"]["id"]
    doc_id = await doc(client, auth, "invoice", [line(107.0, vat)], prices_include_tax=True)
    assert (await patch_doc(client, auth, doc_id, "prices_include_tax", False)).status_code == 200

    reloaded = await get(client, auth, doc_id)
    [snap] = reloaded["line_items"][0]["taxes"]
    assert snap["inclusive"] is False
    lines = [{**reloaded["line_items"][0], "description": "Consulting"}]
    r = await patch_doc(client, auth, doc_id, "line_items", lines)
    assert r.status_code == 200, r.text

    saved = await get(client, auth, doc_id)
    [snap] = saved["line_items"][0]["taxes"]
    assert (snap["inclusive"], snap["base"], snap["amount"]) == (False, 107.0, 7.49)
    assert (saved["subtotal"], saved["tax"], saved["total"]) == (107.0, 7.49, 114.49)


async def test_shipping_taxed_main_supply_code(client, session):
    auth = await company(session)
    saved = await codes(client, auth, [code("VAT", 7), code("Zero", 0, category="zero")])
    vat, zero = saved["VAT"]["id"], saved["Zero"]["id"]
    inv = await get(client, auth, await doc(client, auth, "invoice", [line(100.0, vat)], shipping=10.0))
    [ship] = inv["shipping_taxes"]
    assert (ship["code_id"], ship["rate"], ship["amount"]) == (vat, 7.0, 0.7)
    assert (inv["tax"], inv["total"]) == (7.7, 117.7)

    r = await patch_doc(client, auth, inv["id"], "shipping_taxes", [{"code_id": zero}])
    assert r.status_code == 200, r.text
    changed = await get(client, auth, inv["id"])
    assert (changed["tax"], changed["total"]) == (7.0, 117.0)

    chosen = await get(client, auth, await doc(client, auth, "invoice", [line(107.0, vat)], shipping=10.0,
                                               prices_include_tax=True, shipping_taxes=[{"code_id": zero}]))
    assert [(t["code_id"], t["amount"]) for t in chosen["shipping_taxes"]] == [(zero, 0.0)]
    assert (chosen["tax"], chosen["total"]) == (7.0, 117.0)

    r = await patch_doc(client, auth, inv["id"], "shipping_taxes", [{"code_id": vat}])
    assert r.status_code == 200, r.text
    before = await roles(session, auth)
    await finalized(client, auth, inv["id"])
    assert _moved(before, await roles(session, auth))["tax_output"] == -7.7


async def test_reverse_charge_posts_output_and_input_balanced(client, session):
    auth = await company(session)
    rc = (await codes(client, auth, [code("Reverse charge", 7, "purchase", "reverse_charge")],
                      "purchase"))["Reverse charge"]["id"]
    before = await roles(session, auth)
    bill = await finalized(client, auth, await doc(client, auth, "bill", [line(100.0, rc)]))
    assert (bill["tax"], bill["total"]) == (0.0, 100.0)
    [snap] = bill["line_items"][0]["taxes"]
    assert (snap["category"], snap["amount"]) == ("reverse_charge", 7.0)
    moved = _moved(before, await roles(session, auth))
    assert (moved["payable"], moved["tax_input"], moved["tax_output"]) == (-100.0, 7.0, -7.0)

    sales_rc = (await codes(client, auth, [code("EU service", 0, "sales", "reverse_charge")]))["EU service"]["id"]
    before = await roles(session, auth)
    sale = await finalized(client, auth, await doc(client, auth, "invoice", [line(100.0, sales_rc)]))
    assert sale["total"] == 100.0
    moved = _moved(before, await roles(session, auth))
    assert moved["receivable"] == 100.0 and "tax_output" not in moved


async def test_reverse_charge_keeps_each_line_fund(client, session):
    """With funds on, a reverse charge bill books the tax both ways in the fund of the line it
    was charged on, and each fund's payable is that fund's lines alone."""
    from test_fund_movements import _setup

    auth = await company(session)
    f = await _setup(client, auth)
    rc = (await codes(client, auth, [code("Reverse charge", 7, "purchase", "reverse_charge")],
                      "purchase"))["Reverse charge"]["id"]
    await finalized(client, auth, await doc(client, auth, "bill", [
        {**line(1000.0, rc), "fund_id": f["R"]}, {**line(500.0, rc), "fund_id": f["U1"]}]))
    assert await role_funds(session, auth, "payable", "tax_input", "tax_output") == {
        ("payable", f["R"]): -1000.0, ("payable", f["U1"]): -500.0,
        ("tax_input", f["R"]): 70.0, ("tax_input", f["U1"]): 35.0,
        ("tax_output", f["R"]): -70.0, ("tax_output", f["U1"]): -35.0}


async def test_perception_added_charge(client, session):
    auth = await company(session)
    cid = auth["company_id"]
    sales = await codes(client, auth, [code("VAT", 7, "sales"),
                                       code("Perception", 3, "sales", "perception", output_account="2140")])
    inv = await finalized(client, auth, await doc(client, auth, "invoice", [
        line(100.0, sales["VAT"]["id"], sales["Perception"]["id"])]))
    assert (inv["tax"], inv["total"]) == (10.0, 110.0)
    assert await _account_net(session, cid, "2140") == -3.0
    moved = _moved({}, await roles(session, auth))
    assert (moved["receivable"], moved["tax_output"]) == (110.0, -7.0)

    purchase = await codes(client, auth, [code("Input VAT", 7, "purchase"),
                                          code("Perception paid", 3, "purchase", "perception", input_account="1140")],
                           "purchase")
    before = await roles(session, auth)
    await finalized(client, auth, await doc(client, auth, "bill", [
        line(100.0, purchase["Input VAT"]["id"], purchase["Perception paid"]["id"])]))
    assert await _account_net(session, cid, "1140") == 3.0
    assert _moved(before, await roles(session, auth))["payable"] == -110.0
