# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Money on invoices drawn from a memo: each drawn line's amount comes from the memo line,
the last units take the remainder, and the memo's shipping, fixed discount and tax amount
without a rate are taken once, by one invoice, and given back when that invoice lets go."""
from __future__ import annotations

import uuid

import pytest

from celerp_docs.doc_money import document_money
from memo_draw_support import (
    auth, back_to_memo, draft, draw, drawn, drawn_lines, key_of, memo_out, ok, service_line, state,
)


async def invoice_all(client, h, memo: str, key: str | None = None) -> str:
    return (await ok(await client.post(f"/docs/{memo}/convert", headers=h,
                                       json={"idempotency_key": key or uuid.uuid4().hex})))["target_doc_id"]


async def set_available(client, h, doc_id: str, quantities: dict[str, float]):
    return await ok(await client.post(f"/docs/{doc_id}/set-available", headers=h,
                                      json={"line_ids": list(quantities), "quantities": quantities}))


def fixed(memo_doc: dict) -> dict:
    """The memo's ``memo_fixed_amounts``: what is left on it and each unreleased take."""
    return memo_doc["memo_fixed_amounts"]


def stored_money_matches(doc: dict) -> None:
    """Invariant (b): an invoice's totals are document_money over its own stored lines."""
    money = document_money(doc, doc["line_items"], doc.get("currency") or "USD", keep_unrated_tax=True)
    for k in ("subtotal", "discount_amount", "tax", "total"):
        assert doc[k] == pytest.approx(money[k]), k


def back_ids(doc: dict, memo: str) -> list[str]:
    return [li["line_id"] for li in drawn_lines(doc, memo)]


# ── Line money ────────────────────────────────────────────────────────────────

_TERMS = {
    "plain": ({}, {}),
    "line_discount": ({"discount_pct": 10}, {}),
    "header_pct": ({}, {"discount": 5, "discount_type": "percentage"}),
    "line_tax": ({"tax_rate": 7}, {}),
    "doc_tax": ({}, {"tax_rate": 7}),
}


@pytest.mark.parametrize("currency", ["JPY", "USD", "KWD"])
@pytest.mark.parametrize("lots_per_line", [1, 3])
@pytest.mark.parametrize("terms", list(_TERMS))
async def test_drawn_money_matches_memo_money(client, session, currency, lots_per_line, terms):
    h = await auth(session)
    line_extra, header = _TERMS[terms]
    if currency != "USD":
        header = {**header, "conversion_rate": 0.01 if currency == "JPY" else 3.25}
    prices = [10.0, 3.33, 7.77, 1.01, 12.5, 0.99, 4.44]
    memo, lids, _ = await memo_out(client, h, [(3, p) for p in prices], lots_per_line=lots_per_line,
                                   line_extra=line_extra, currency=currency, **header)
    m = await state(client, h, memo)

    first = await drawn(client, h, memo, {lid: 1 for lid in lids})
    second = await drawn(client, h, memo, {lid: 2 for lid in lids})

    a, b = await state(client, h, first), await state(client, h, second)
    for doc in (a, b):
        stored_money_matches(doc)
    by_line = {}
    for li in drawn_lines(a, memo) + drawn_lines(b, memo):
        by_line[li["drawn_from"]["memo_line_id"]] = by_line.get(li["drawn_from"]["memo_line_id"], 0) + li["line_total"]
    for li in m["line_items"]:
        assert by_line[li["line_id"]] == pytest.approx(li["line_total"]), li["line_id"]

    whole, wlids, _ = await memo_out(client, h, [(3, p) for p in prices], lots_per_line=lots_per_line,
                                     line_extra=line_extra, currency=currency, **header)
    w = await state(client, h, whole)
    inv = await state(client, h, await invoice_all(client, h, whole))
    lines = drawn_lines(inv, whole)
    assert [li["drawn_from"]["memo_line_id"] for li in lines] == wlids
    assert [li["line_total"] for li in lines] == [li["line_total"] for li in w["line_items"]]
    for k in ("subtotal", "discount_amount", "tax", "total"):
        assert inv[k] == pytest.approx(w[k]), k


async def test_drawn_line_tax_recomputed_per_invoice(client, session):
    h = await auth(session)
    memo, lids, _ = await memo_out(client, h, [(3, 3.3333333333)], lots_per_line=3, line_extra={"tax_rate": 7})
    m = await state(client, h, memo)
    assert m["line_items"][0]["line_total"] == pytest.approx(10.0)

    invs = [await state(client, h, await drawn(client, h, memo, {lids[0]: 1})) for _ in range(3)]

    amounts = [drawn_lines(d, memo)[0]["line_total"] for d in invs]
    assert sorted(amounts) == pytest.approx([3.33, 3.33, 3.34])
    assert sum(amounts) == pytest.approx(10.0)
    for d in invs:
        assert d["tax"] == pytest.approx(0.23)
        stored_money_matches(d)


async def test_remainder_after_set_available_is_proportional(client, session):
    h = await auth(session)
    memo, lids, _ = await memo_out(client, h, [(10, 10.0)], lots_per_line=10)

    one = await state(client, h, await drawn(client, h, memo, {lids[0]: 4}))
    await set_available(client, h, memo, {lids[0]: 3})
    two = await state(client, h, await drawn(client, h, memo, {lids[0]: 3}))

    assert drawn_lines(one, memo)[0]["line_total"] == pytest.approx(40.0)
    assert drawn_lines(two, memo)[0]["line_total"] == pytest.approx(30.0)


async def test_remainder_ignores_edited_drawn_price(client, session):
    h = await auth(session)
    memo, lids, _ = await memo_out(client, h, [(3, 3.3333333333)], lots_per_line=3)
    first = await drawn(client, h, memo, {lids[0]: 1})
    doc = await state(client, h, first)
    lines = [{**li, "unit_price": 9.0} if li.get("drawn_from") else li for li in doc["line_items"]]
    await ok(await client.patch(f"/docs/{first}", headers=h,
                                json={"line_items": lines, "expected_version": doc["version"]}))

    second = await state(client, h, await drawn(client, h, memo, {lids[0]: 1}))
    last = await state(client, h, await drawn(client, h, memo, {lids[0]: 1}))

    recorded = [t["lines"][0]["memo_amount"] for t in (await state(client, h, memo))["invoiced_on"]]
    assert sum(recorded) == pytest.approx(10.0)
    assert drawn_lines(last, memo)[0]["line_total"] == pytest.approx(10.0 - recorded[0] - recorded[1])
    assert drawn_lines(second, memo)[0]["line_total"] == pytest.approx(3.33)


# ── Fixed amounts: shipping, fixed discount, tax amount without a rate ───────

async def test_shipping_goes_whole_to_the_first_draw(client, session):
    h = await auth(session)
    memo, lids, _ = await memo_out(client, h, [(4, 10.0)], lots_per_line=4, shipping=50.0)

    first = await state(client, h, await drawn(client, h, memo, {lids[0]: 1}))
    second = await state(client, h, await drawn(client, h, memo, {lids[0]: 1}))
    assert (first["shipping"], second.get("shipping") or 0) == (50.0, 0)

    other, olids, _ = await memo_out(client, h, [(2, 10.0)], lots_per_line=2, shipping=50.0)
    pf = await draft(client, h, lines=[service_line()], shipping=20.0)
    doc = await state(client, h, await drawn(client, h, other, {olids[0]: 1}, target=pf))
    assert doc["shipping"] == pytest.approx(70.0)
    taken = doc["memo_fixed_taken"]
    assert [(t["memo_id"], t["shipping"]) for t in taken] == [(other, 50.0)]


async def test_fixed_discount_used_in_draw_order_never_below_zero(client, session):
    h = await auth(session)
    memo, lids, _ = await memo_out(client, h, [(6, 10.0), (3, 10.0), (5, 10.0)], lots_per_line=1,
                                   discount=100.0, discount_type="flat")

    invs = [await state(client, h, await drawn(client, h, memo, {lid: q}))
            for lid, q in zip(lids, (6, 3, 5))]

    assert [d["discount"] for d in invs] == [60.0, 30.0, 10.0]
    assert all(d["total"] >= 0 for d in invs)
    assert fixed(await state(client, h, memo))["discount"] == 0


async def test_unrated_tax_partial_draw_refused_names_the_fix(client, session):
    h = await auth(session)
    memo, lids, _ = await memo_out(client, h, [(2, 100.0), (1, 50.0)], lots_per_line=1, tax=35.0)
    pf = await draft(client, h, lines=[service_line()])

    for r in (await draw(client, h, memo, {lids[0]: 1}), await draw(client, h, memo, line_ids=lids, target=pf)):
        assert r.status_code == 409, r.text
        assert key_of(r) in ("docs.draw_unrated_tax_partial", "docs.draw_unrated_tax_existing")
        text = r.json()["detail"]["message"]
        assert "set a tax rate" in text
    r = await draw(client, h, memo, {lids[0]: 1})
    assert "Invoice all" in r.json()["detail"]["message"]

    inv = await invoice_all(client, h, memo)
    doc = await state(client, h, inv)
    assert doc["tax"] == pytest.approx(35.0)

    await ok(await back_to_memo(client, h, inv, back_ids(doc, memo)[:1]))
    assert (await state(client, h, inv))["tax"] == pytest.approx(35.0)
    assert (await draw(client, h, memo, {lids[0]: 1})).status_code == 200

    rest = await state(client, h, inv)
    await ok(await back_to_memo(client, h, inv, back_ids(rest, memo)))
    r = await draw(client, h, memo, {lids[1]: 1})
    assert key_of(r) == "docs.draw_unrated_tax_partial", r.text
    assert (await state(client, h, await invoice_all(client, h, memo)))["tax"] == pytest.approx(35.0)


async def test_fixed_amounts_replay_moves_once(client, session):
    h = await auth(session)
    memo, lids, _ = await memo_out(client, h, [(4, 10.0)], lots_per_line=4, shipping=50.0,
                                   discount=10.0, discount_type="flat")
    key = uuid.uuid4().hex

    a = await drawn(client, h, memo, {lids[0]: 2}, key=key)
    b = await drawn(client, h, memo, {lids[0]: 2}, key=key)

    assert a == b
    doc = await state(client, h, a)
    assert (doc["shipping"], doc["discount"]) == (50.0, 10.0)
    assert len((await state(client, h, memo))["invoiced_on"]) == 1


@pytest.mark.parametrize("release", ["delete", "void", "back_all", "back_some", "finalized"])
async def test_fixed_amounts_return_on_delete_and_full_return_not_partial(client, session, release):
    h = await auth(session)
    memo, lids, _ = await memo_out(client, h, [(2, 50.0), (2, 50.0)], lots_per_line=2, shipping=50.0,
                                   discount=30.0, discount_type="flat")
    pf = await draft(client, h, lines=[service_line(100.0)], shipping=10.0, discount=5.0, discount_type="flat")
    await drawn(client, h, memo, {lids[0]: 1, lids[1]: 1}, target=pf)
    doc = await state(client, h, pf)
    assert (doc["shipping"], doc["discount"]) == (60.0, 35.0)

    if release == "delete":
        await ok(await client.delete(f"/docs/{pf}", headers=h))
    elif release == "void":
        await ok(await client.post(f"/docs/{pf}/finalize", headers=h))
        await ok(await client.post(f"/docs/{pf}/void", headers=h, json={}))
    elif release == "back_all":
        await ok(await back_to_memo(client, h, pf, back_ids(doc, memo)))
        after = await state(client, h, pf)
        assert (after["shipping"], after["discount"]) == (10.0, 5.0)
    elif release == "back_some":
        await ok(await back_to_memo(client, h, pf, back_ids(doc, memo)[:1]))
        after = await state(client, h, pf)
        assert (after["shipping"], after["discount"]) == (60.0, 35.0)
    else:
        await ok(await client.post(f"/docs/{pf}/finalize", headers=h))

    left = fixed(await state(client, h, memo))
    nxt = await state(client, h, await drawn(client, h, memo, {lids[0]: 1}))
    if release in ("back_some", "finalized"):
        assert (left["shipping"], left["discount"]) == (0, 0)
        assert (nxt.get("shipping") or 0, nxt.get("discount") or 0) == (0, 0)
    else:
        assert (left["shipping"], left["discount"]) == (50.0, 30.0)
        assert (nxt["shipping"], nxt["discount"]) == (50.0, 30.0)


async def test_memo_shows_where_fixed_amounts_went(client, session):
    h = await auth(session)
    memo, lids, _ = await memo_out(client, h, [(4, 25.0)], lots_per_line=4, shipping=50.0,
                                   discount=60.0, discount_type="flat")

    inv = await drawn(client, h, memo, {lids[0]: 2})

    m = await state(client, h, memo)
    f = fixed(m)
    assert (f["shipping"], f["discount"], f["tax"]) == (0, 10.0, 0)
    assert [(t["invoice_id"], t["shipping"], t["discount"]) for t in f["takes"]] == [(inv, 50.0, 50.0)]
    assert f["takes"][0]["doc_number"] == (await state(client, h, inv))["ref_id"]
    # "Still on memo": the two units still out at 25.00 plus the fixed discount left.
    assert f["still_on_memo"] == pytest.approx(50.0 - 10.0)


# ── Tax amount without a rate drawn whole ─────────────────────────────────────

async def test_full_draw_onto_populated_draft_unrated_tax_refused(client, session):
    h = await auth(session)
    memo, lids, _ = await memo_out(client, h, [(2, 100.0)], lots_per_line=2, tax=35.0)
    pf = await draft(client, h, lines=[service_line()])
    v = (await state(client, h, pf))["version"]

    r = await draw(client, h, memo, line_ids=lids, target=pf)

    assert r.status_code == 409, r.text
    assert key_of(r) == "docs.draw_unrated_tax_existing"
    text = r.json()["detail"]["message"]
    assert "new invoice" in text and "tax rate" in text
    assert (await state(client, h, pf))["version"] == v
    empty = await draft(client, h)
    assert (await state(client, h, await drawn(client, h, memo, line_ids=lids, target=empty)))["tax"] == 35.0


async def test_full_draw_onto_new_invoice_keeps_unrated_tax(client, session):
    h = await auth(session)
    memo, lids, _ = await memo_out(client, h, [(2, 100.0), (1, 40.0)], lots_per_line=1, tax=35.0)
    m = await state(client, h, memo)

    doc = await state(client, h, await drawn(client, h, memo, line_ids=lids))

    assert doc["tax"] == pytest.approx(35.0)
    assert doc["total"] == pytest.approx(m["total"])


# ── Takes released and taken again ────────────────────────────────────────────

async def test_redraw_onto_same_draft_equals_one_draw(client, session):
    h = await auth(session)
    memo, lids, _ = await memo_out(client, h, [(2, 100.0)], lots_per_line=2, shipping=50.0,
                                   discount=100.0, discount_type="flat")
    pf = await draft(client, h)

    await drawn(client, h, memo, line_ids=lids, target=pf)
    await ok(await back_to_memo(client, h, pf, back_ids(await state(client, h, pf), memo)))
    once = (fixed(await state(client, h, memo)), (await state(client, h, pf))["shipping"],
            (await state(client, h, pf))["discount"])
    await drawn(client, h, memo, line_ids=lids, target=pf)
    taken = (await state(client, h, pf))["memo_fixed_taken"]
    assert [t["shipping"] for t in taken] == [50.0]
    await ok(await back_to_memo(client, h, pf, back_ids(await state(client, h, pf), memo)))
    twice = (fixed(await state(client, h, memo)), (await state(client, h, pf))["shipping"],
             (await state(client, h, pf))["discount"])
    assert twice == once


async def test_delete_renumber_redraw_keeps_takes_released(client, session):
    h = await auth(session)
    memo, lids, _ = await memo_out(client, h, [(4, 100.0)], lots_per_line=4, shipping=50.0)
    first = await drawn(client, h, memo, {lids[0]: 1})
    number = (await state(client, h, first))["ref_id"]
    nxt = int("".join(c for c in number if c.isdigit())[-4:] or 1)
    await ok(await client.delete(f"/docs/{first}", headers=h))
    await ok(await client.patch("/docs/sequences/proforma", headers=h, json={"next": nxt}))

    again = await drawn(client, h, memo, {lids[0]: 1})

    assert again == first
    m = await state(client, h, memo)
    assert len(fixed(m)["takes"]) == 1
    await ok(await back_to_memo(client, h, again, back_ids(await state(client, h, again), memo)))
    assert fixed(await state(client, h, memo))["shipping"] == 50.0
    assert (await state(client, h, again)).get("shipping", 0) == 0


async def test_release_after_discount_type_changed_keeps_percentage(client, session):
    h = await auth(session)
    memo, lids, _ = await memo_out(client, h, [(2, 30.0), (2, 50.0)], lots_per_line=1, discount=100.0,
                                   discount_type="flat")
    inv = await drawn(client, h, memo, {lids[0]: 2})
    doc = await state(client, h, inv)
    assert doc["discount"] == 60.0
    await ok(await client.patch(f"/docs/{inv}", headers=h, json={
        "discount": 10, "discount_type": "percentage", "expected_version": doc["version"]}))

    await ok(await back_to_memo(client, h, inv, back_ids(doc, memo)))

    after = await state(client, h, inv)
    assert (after["discount"], after["discount_type"]) == (10, "percentage")
    assert fixed(await state(client, h, memo))["discount"] == 100.0


async def test_stale_page_save_after_draw_refused(client, session):
    h = await auth(session)
    memo, lids, _ = await memo_out(client, h, [(2, 10.0)], lots_per_line=2, shipping=50.0)
    pf = await draft(client, h, lines=[service_line()])
    page = await state(client, h, pf)

    await drawn(client, h, memo, {lids[0]: 1}, target=pf)

    r = await client.patch(f"/docs/{pf}", headers=h, json={
        "line_items": page["line_items"], "discount": 0, "discount_type": "flat",
        "expected_version": page["version"]})
    assert r.status_code == 409, r.text
    doc = await state(client, h, pf)
    assert doc["shipping"] == 50.0 and len(drawn_lines(doc, memo)) == 1
    assert len(fixed(await state(client, h, memo))["takes"]) == 1


# ── Every way back after a return (J4 table) ──────────────────────────────────

async def _redraw(client, h, memo, lids, nxt, x, full):
    """The next draw after the return: onto a new invoice, onto X, or onto X switched to 10%."""
    if nxt == "x_pct":
        doc = await state(client, h, x)
        await ok(await client.patch(f"/docs/{x}", headers=h, json={
            "discount": 10, "discount_type": "percentage", "expected_version": doc["version"]}))
    target = None if nxt == "new" else x
    if full:
        return await draw(client, h, memo, line_ids=lids, target=target)
    return await draw(client, h, memo, {lids[0]: 1}, target=target)


@pytest.mark.parametrize("nxt", ["new", "x", "x_pct"])
@pytest.mark.parametrize("returned", ["partial", "full"])
async def test_way_back_after_return_fixed_discount(client, session, nxt, returned):
    h = await auth(session)
    memo, lids, _ = await memo_out(client, h, [(1, 100.0), (1, 100.0)], discount=300.0, discount_type="flat")
    x = await drawn(client, h, memo, line_ids=lids)
    assert (await state(client, h, x))["discount"] == 200.0
    ids = back_ids(await state(client, h, x), memo)
    await ok(await back_to_memo(client, h, x, ids[:1] if returned == "partial" else ids))

    back = lids[:1] if returned == "partial" else lids
    r = await _redraw(client, h, memo, back, nxt, x, full=True)

    if nxt == "x_pct":
        assert key_of(r) == "docs.draw_terms_differ", r.text
        return
    assert r.status_code == 200, r.text
    if nxt == "new":
        took = (await state(client, h, r.json()["target_doc_id"]))["discount"]
        assert took == (100.0 if returned == "partial" else 200.0)


@pytest.mark.parametrize("nxt", ["new", "x", "x_pct"])
@pytest.mark.parametrize("returned", ["partial", "full"])
async def test_way_back_after_return_unrated_tax(client, session, nxt, returned):
    h = await auth(session)
    memo, lids, _ = await memo_out(client, h, [(1, 100.0), (1, 100.0)], tax=35.0)
    x = await drawn(client, h, memo, line_ids=lids)
    ids = back_ids(await state(client, h, x), memo)
    await ok(await back_to_memo(client, h, x, ids[:1] if returned == "partial" else ids))

    back = lids[:1] if returned == "partial" else lids
    r = await _redraw(client, h, memo, back, nxt, x, full=True)

    if nxt == "x_pct":
        assert key_of(r) == "docs.draw_terms_differ", r.text
        return
    assert r.status_code == 200, r.text
    target = await state(client, h, r.json()["target_doc_id"])
    if returned == "partial":
        assert (target.get("tax") or 0) == (35.0 if nxt == "x" else 0)
    else:
        assert target["tax"] == 35.0
    if returned == "full" and nxt == "new":
        assert len([t for t in fixed(await state(client, h, memo))["takes"] if t["tax"]]) == 1


async def test_way_back_after_full_return_partial_redraw_refused(client, session):
    h = await auth(session)
    memo, lids, _ = await memo_out(client, h, [(1, 100.0), (1, 100.0)], tax=35.0)
    x = await drawn(client, h, memo, line_ids=lids)
    await ok(await back_to_memo(client, h, x, back_ids(await state(client, h, x), memo)))

    r = await draw(client, h, memo, {lids[0]: 1})

    assert key_of(r) == "docs.draw_unrated_tax_partial", r.text


@pytest.mark.parametrize("rate", [None, "same", "other"])
async def test_way_back_after_return_draft_with_other_lines(client, session, rate):
    h = await auth(session)
    memo, lids, _ = await memo_out(client, h, [(1, 100.0), (1, 100.0)], tax=35.0)
    x = await draft(client, h, lines=[service_line()])
    empty = await draft(client, h)
    await drawn(client, h, memo, line_ids=lids, target=empty)
    await ok(await back_to_memo(client, h, empty, back_ids(await state(client, h, empty), memo)))
    if rate:
        doc = await state(client, h, x)
        await ok(await client.patch(f"/docs/{x}", headers=h, json={
            "tax_rate": 7, "expected_version": doc["version"]}))
        m = await state(client, h, memo)
        await ok(await client.post(f"/docs/{memo}/tax-rate", headers=h, json={
            "tax_rate": 7 if rate == "same" else 10, "expected_version": m["version"]}))

    full = await draw(client, h, memo, line_ids=lids, target=x)
    part = await draw(client, h, memo, {lids[0]: 1}, target=x)

    if rate is None:
        assert key_of(full) == "docs.draw_unrated_tax_existing", full.text
        assert key_of(part) == "docs.draw_unrated_tax_partial", part.text
    elif rate == "same":
        assert full.status_code == 200, full.text
        assert (await state(client, h, x))["tax_rate"] == 7
    else:
        assert key_of(full) == "docs.draw_terms_differ", full.text
        assert key_of(part) == "docs.draw_terms_differ", part.text
