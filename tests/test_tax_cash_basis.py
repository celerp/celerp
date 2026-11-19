# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Cash-basis tax: on a code marked cash basis the tax is due when the money moves, so it waits
on a pending account at issue and moves to the tax account in proportion to each payment, credit
note, refund or advance; voiding a payment moves it back. At the cut-off the country sets, the
tax still pending on old documents is listed and moved only once the user confirms."""
from __future__ import annotations

import uuid

import pytest

from tax_money_support import code, codes, company, doc, finalized, get, line, ok, pay, post, roles

pytestmark = pytest.mark.asyncio


async def _cash_company(client, session, side: str = "sales") -> tuple[dict, str]:
    from test_l10n_country_update import _apply_country
    auth = await company(session)
    await _apply_country(client, auth, "zzES")
    path = "/companies/me/taxes" if side == "sales" else "/companies/me/purchasing-taxes"
    listed = ok(await client.get(path, headers=auth["headers"]))
    saved = await codes(client, auth, [*listed, code("IVA caja", 21, side, cash_basis=True)], side)
    return auth, saved["IVA caja"]["id"]


async def test_cash_basis_pending_moves_with_allocations(client, session):
    """After every allocation the pending output tax is the tax share of what is still
    outstanding (21/121 of it), and pending plus recognized output tax is the tax of the
    invoices less the tax of the credit note. An advance is a payment dated before its
    invoice. The last payments clear the pending tax to zero. A bill does the same with
    input tax."""
    auth, caja = await _cash_company(client, session)
    open_docs: list[str] = []

    async def check(issued_tax: float):
        books = await roles(session, auth)
        owed = [(await get(client, auth, d))["amount_outstanding"] for d in open_docs]
        assert books.get("tax_output_pending", 0) == pytest.approx(-sum(o * 21 / 121 for o in owed), abs=0.02)
        assert books.get("tax_output_pending", 0) + books.get("tax_output", 0) == pytest.approx(-issued_tax, abs=0.01)

    inv = await finalized(client, auth, await doc(client, auth, "invoice", [line(1000.0, caja)]))
    open_docs.append(inv["id"])
    assert inv["total"] == 1210.0
    await check(210.0)
    ok(await pay(client, auth, inv["id"], 605.0))
    await check(210.0)
    note = await finalized(client, auth, await doc(client, auth, "credit_note", [line(100.0, caja)],
                                                   original_doc_id=inv["id"]))
    ok(await post(client, auth, f"/docs/{note['id']}/apply-to-invoice", {
        "target_doc_id": inv["id"], "amount": 121.0, "idempotency_key": uuid.uuid4().hex}))
    await check(189.0)
    ok(await post(client, auth, f"/docs/{inv['id']}/refund", {
        "payment_index": 0, "amount": 60.5, "payment_date": "2026-10-25", "idempotency_key": uuid.uuid4().hex}))
    await check(189.0)
    ok(await pay(client, auth, inv["id"], 100.0))
    index = len((await get(client, auth, inv["id"]))["payments"]) - 1
    ok(await post(client, auth, f"/docs/{inv['id']}/void-payment", {"payment_index": index}))
    await check(189.0)

    advance = await finalized(client, auth, await doc(client, auth, "invoice", [line(1000.0, caja)]))
    open_docs.append(advance["id"])
    ok(await pay(client, auth, advance["id"], 242.0, date="2026-09-01"))
    await check(399.0)
    for d in open_docs:
        ok(await pay(client, auth, d, (await get(client, auth, d))["amount_outstanding"]))
    await check(399.0)
    assert (await roles(session, auth)).get("tax_output_pending", 0) == 0

    buyer, input_caja = await _cash_company(client, session, "purchase")
    bill = await finalized(client, buyer, await doc(client, buyer, "bill", [line(1000.0, input_caja)]))
    assert (await roles(session, buyer)).get("tax_input_pending") == 210.0
    ok(await pay(client, buyer, bill["id"], 1210.0))
    books = await roles(session, buyer)
    assert (books.get("tax_input_pending", 0), books.get("tax_input")) == (0, 210.0)


async def _preview(client, auth) -> dict:
    return ok(await post(client, auth, "/settings/taxes/cash-basis/preview"))


async def _confirm(client, auth, binding: str):
    return await post(client, auth, "/settings/taxes/cash-basis/confirm",
                      {"binding": binding, "idempotency_key": uuid.uuid4().hex})


async def test_cash_basis_cutoff_catchup_confirmed(client, session):
    """The catch-up lists the tax still pending on documents whose cut-off has passed and moves
    nothing until confirmed. A preview made stale by a later payment is refused, as is a
    cut-off inside a locked period; the confirmed move leaves documents whose cut-off is still ahead alone. With nothing past its cut-off the preview
    says so and there is nothing to confirm."""
    auth, caja = await _cash_company(client, session)
    old = await finalized(client, auth, await doc(client, auth, "invoice", [line(1000.0, caja)],
                                                  issue_date="2024-03-01"))
    ok(await pay(client, auth, old["id"], 605.0, date="2024-06-01"))
    recent = await finalized(client, auth, await doc(client, auth, "invoice", [line(1000.0, caja)]))
    before = await roles(session, auth)

    preview = await _preview(client, auth)
    assert [(d["doc_id"], d["cutoff"], d["pending"]) for d in preview["documents"]] == \
        [(old["id"], "2025-12-31", 105.0)]
    assert await roles(session, auth) == before

    ok(await pay(client, auth, old["id"], 121.0, date="2025-06-01"))
    assert (await _confirm(client, auth, preview["fingerprint"])).status_code == 409
    ok(await post(client, auth, "/accounting/period-lock", {"lock_date": "2025-12-31"}))
    r = await _confirm(client, auth, (await _preview(client, auth))["fingerprint"])
    assert r.status_code == 422, r.text
    ok(await post(client, auth, "/accounting/period-lock", {"lock_date": None}))
    assert (await roles(session, auth)).get("tax_output_pending") == pytest.approx(-294.0, abs=0.01)
    ok(await _confirm(client, auth, (await _preview(client, auth))["fingerprint"]))
    assert (await roles(session, auth)).get("tax_output_pending") == pytest.approx(-210.0, abs=0.01)
    assert (await get(client, auth, recent["id"]))["amount_outstanding"] == 1210.0

    fresh, caja2 = await _cash_company(client, session)
    await finalized(client, fresh, await doc(client, fresh, "invoice", [line(1000.0, caja2)]))
    empty = await _preview(client, fresh)
    assert empty["documents"] == [] and "No pending cash-basis tax at" in str(empty)
    r = await _confirm(client, fresh, empty["fingerprint"])
    assert r.status_code == 422, r.text
