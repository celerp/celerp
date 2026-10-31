# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Setting a tax rate on an issued memo whose tax was entered as an amount: the preview shows
the new tax before anything is stored, the write stores exactly what the preview showed, and
the memo can then be invoiced in parts. Refused once the amount is rated, while an invoice holds
it, on a closed memo, and while the memo holds a payment."""
from __future__ import annotations

import uuid

import pytest

from celerp.models.ledger import LedgerEntry
from memo_draw_support import auth, auth_full, draft, draw, drawn, key_of, memo_out, ok, pay, rate, state
from sqlalchemy import func, select

pytestmark = pytest.mark.asyncio


async def memo_m(client, h, **header):
    return await memo_out(client, h, [(1, 100.0), (1, 250.0)], tax=35.0, **header)


async def events(session, doc_id: str) -> int:
    return await session.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.entity_id == doc_id))


async def test_memo_tax_rate_correction(client, session):
    a = await auth_full(session)
    h = a["headers"]
    memo, lids, _ = await memo_m(client, h)

    # (1) a partial draw names the fix
    r = await draw(client, h, memo, line_ids=[lids[0]])
    assert key_of(r) == "docs.draw_unrated_tax_partial"
    assert "35.00" in r.json()["detail"]["message"]

    # (2) the correction at 8%
    before = await state(client, h, memo)
    company_events = select(func.count()).select_from(LedgerEntry).where(LedgerEntry.company_id == a["company_id"])
    written = await session.scalar(company_events)
    await ok(await rate(client, h, memo, 8))
    after = await state(client, h, memo)
    assert (after["tax"], after["tax_rate"]) == (28.0, 8) and after["version"] > before["version"]
    assert after["line_items"] == before["line_items"]
    # one memo event and nothing else: no line, no journal entry
    assert await session.scalar(company_events) == written + 1

    # (3) the same partial draw now works onto a new invoice and a draft with rate 8, not a draft with none
    inv = await drawn(client, h, memo, line_ids=[lids[0]])
    assert (await state(client, h, inv))["tax_rate"] == 8
    memo2, lids2, _ = await memo_m(client, h)
    await ok(await rate(client, h, memo2, 8))
    assert (await draw(client, h, memo2, line_ids=[lids2[0]], target=await draft(client, h, tax_rate=8))).status_code == 200
    r = await draw(client, h, memo2, line_ids=[lids2[1]], target=await draft(client, h))
    assert key_of(r) == "docs.draw_terms_differ"

    # (4) refused: a second correction, a memo issued with a rate, an unreleased take, a closed memo
    assert key_of(await rate(client, h, memo, 9)) == "docs.memo_rate_no_unrated_tax"
    rated, _, _ = await memo_out(client, h, [(1, 100.0)], tax_rate=7)
    assert key_of(await rate(client, h, rated, 9)) == "docs.memo_rate_no_unrated_tax"
    held, held_lids, _ = await memo_m(client, h)
    inv_all = (await ok(await client.post(f"/docs/{held}/convert", headers=h,
                                          json={"idempotency_key": uuid.uuid4().hex})))["target_doc_id"]
    assert key_of(await rate(client, h, held, 8)) == "docs.memo_rate_no_unrated_tax"
    closed, closed_lids, _ = await memo_m(client, h)
    await ok(await client.post(f"/docs/{closed}/set-available", headers=h, json={"line_ids": closed_lids}))
    await ok(await client.post(f"/docs/{closed}/close", headers=h, json={}))
    assert key_of(await rate(client, h, closed, 8)) == "docs.memo_rate_no_unrated_tax"

    # (5) once Invoice all's invoice is voided the tax is left again and the correction is allowed
    await ok(await client.post(f"/docs/{inv_all}/finalize", headers=h))
    await ok(await client.post(f"/docs/{inv_all}/void", headers=h, json={"reason": "wrong customer"}))
    figures = {k: (await state(client, h, inv_all))[k] for k in ("tax", "total")}
    await ok(await rate(client, h, held, 8))
    assert {k: (await state(client, h, inv_all))[k] for k in ("tax", "total")} == figures

    # (6) the memo changed after the preview: its save is refused, a new preview's is not
    stale, _, _ = await memo_m(client, h)
    shown = await ok(await rate(client, h, stale, 8, dry_run=True))
    v = (await state(client, h, stale))["version"]
    await ok(await client.patch(f"/docs/{stale}", headers=h, json={"fields_changed": {"internal_note": {"new": "Back Friday"}}, "expected_version": v}))
    r = await rate(client, h, stale, 8, binding=shown["binding"])
    assert r.status_code == 409 and key_of(r) == "docs.memo_rate_preview_differs", r.text
    assert not (await state(client, h, stale)).get("tax_rate")
    await ok(await rate(client, h, stale, 8))


async def test_memo_tax_rate_save_is_the_rate_previewed(client, session):
    """The save stores the rate whose preview is on screen, or nothing: another rate sent with
    that preview's binding is refused, and a save with no binding never reaches the memo."""
    h = await auth(session)
    memo, _, _ = await memo_m(client, h)
    before, count = await state(client, h, memo), await events(session, memo)
    shown = await ok(await rate(client, h, memo, 10, dry_run=True))

    r = await rate(client, h, memo, 20, binding=shown["binding"])
    assert r.status_code == 409 and key_of(r) == "docs.memo_rate_preview_differs", r.text
    assert "save again" in r.json()["detail"]["message"]
    no_binding = await client.post(f"/docs/{memo}/tax-rate", headers=h, json={"tax_rate": 10})
    assert no_binding.status_code == 422, no_binding.text
    assert await state(client, h, memo) == before and await events(session, memo) == count

    stored = await ok(await rate(client, h, memo, 10, binding=shown["binding"]))
    assert (stored["tax_rate"], stored["tax"], stored["total"]) == (10, shown["tax"], shown["total"])


@pytest.mark.parametrize(("header", "tax"), [({}, 28.0), ({"discount": 50.0, "discount_type": "flat"}, 24.0),
                                             ({"discount": 10, "discount_type": "percentage"}, 25.2)])
async def test_memo_tax_rate_correction_preview_matches_stored(client, session, header, tax):
    h = await auth(session)
    memo, _, _ = await memo_m(client, h, **header)
    before, count = await state(client, h, memo), await events(session, memo)

    preview = await ok(await rate(client, h, memo, 8, dry_run=True))

    assert preview["tax"] == pytest.approx(tax)
    assert await state(client, h, memo) == before and await events(session, memo) == count
    await ok(await rate(client, h, memo, 8))
    stored = await state(client, h, memo)
    assert (stored["tax"], stored["total"]) == (preview["tax"], preview["total"])
    from celerp_docs.doc_money import memo_tax_rate_money

    money = memo_tax_rate_money(before, 8)
    assert (money["tax"], money["total"]) == (stored["tax"], stored["total"])
    assert key_of(await rate(client, h, memo, 9, dry_run=True)) == "docs.memo_rate_no_unrated_tax"


@pytest.mark.parametrize("paid", ["part", "full"])
async def test_memo_tax_rate_correction_refused_while_paid(client, session, paid):
    h = await auth(session)
    memo, _, _ = await memo_m(client, h)
    total = (await state(client, h, memo))["total"]
    await ok(await pay(client, h, memo, 50.0 if paid == "part" else total))
    before = await state(client, h, memo)
    if paid == "full":
        assert before["status"] == "paid"

    for dry_run in (True, False):
        r = await rate(client, h, memo, 8, dry_run=dry_run)
        assert r.status_code == 409 and key_of(r) == "docs.memo_rate_paid", r.text
        if paid == "part":
            assert "50.00 on 2026-03-02" in r.json()["detail"]["message"]
    assert await state(client, h, memo) == before

    await ok(await client.post(f"/docs/{memo}/void-payment", headers=h, json={"payment_index": 0}))
    assert (await state(client, h, memo))["status"] == "final"
    await ok(await rate(client, h, memo, 8))
