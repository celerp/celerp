# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Credit notes an earlier release left in an edge state, settled at start: one whose
invoice went back to draft is settled when the invoice is issued again, one refunded at
its own rate is brought to its invoice's rate refund and all, and one whose invoice is
void keeps only what it already paid out.

Oracle at every step: 1120 equals open invoices less open credit note balances; a full
reversal brings every leg back to 0; running the settlement again changes nothing."""
from __future__ import annotations

import uuid

import pytest

from celerp.events.engine import emit_event
from test_cost_restatement import _state
from test_credit_note_settlement_owned import _ar, _backfill, _cn, _legacy_cn, _pay, _post, _svc_invoice
from test_set_aside_older_paths import _net

pytestmark = pytest.mark.asyncio


async def _books(session, auth) -> dict:
    session.expire_all()
    gl, sub = await _ar(session, auth)
    assert gl == sub, f"1120 {gl} != open invoices less open credit note balances {sub}"
    return {"1120": gl, **{a: await _net(session, auth, a, prefix="je:") for a in ("4100", "1111", "6960")}}


async def _doc(session, auth, doc) -> tuple:
    session.expire_all()
    s = await _state(session, auth, doc)
    return s.get("status"), float(s.get("amount_outstanding") or 0), float(s.get("credited") or 0)


async def _raw(session, auth, doc, event_type, data=None, **meta):
    """What an earlier release wrote, written the way it wrote it."""
    await emit_event(session, company_id=auth["company_id"], entity_id=doc, entity_type="doc",
                     event_type=event_type, data=data or {}, actor_id=auth["user_id"], location_id=None,
                     source="api", idempotency_key=f"main-{uuid.uuid4().hex}", metadata_=meta or None)
    await session.commit()


async def _refund(client, auth, cn, amount):
    return await _post(client, auth, f"/docs/{cn}/cn-refund",
                       {"amount": amount, "date": "2026-10-09", "bank_account": "1111"})


async def _marker(session):
    from celerp.migrations._data_reconcile import get_meta
    from celerp_docs.legacy_credit_notes import MARKER_KEY

    conn = await session.connection()
    return await conn.run_sync(lambda c: get_meta(c, MARKER_KEY))


async def _hook(session):
    from celerp.migrations._data_reconcile import set_meta
    from celerp_docs.legacy_credit_notes import MARKER_KEY, legacy_credit_notes_hook

    conn = await session.connection()
    await conn.run_sync(lambda c: set_meta(c, MARKER_KEY, ""))
    await legacy_credit_notes_hook(session=session)
    await session.commit()
    return await _marker(session)


# N6: a credit note whose invoice went back to draft


async def _back_to_draft(session, auth, inv):
    from celerp.services import auto_je

    await auto_je.void_for_doc_finalized(session, company_id=auth["company_id"], user_id=auth["user_id"],
                                         doc_id=inv, revert_count=0)
    await _raw(session, auth, inv, "doc.reverted_to_draft", {"reverted_by": str(auth["user_id"]),
                                                             "previous_status": "final"})


async def test_credit_note_on_a_draft_invoice_is_settled_when_the_invoice_is_issued(client, session, auth):
    inv = await _svc_invoice(client, auth, 80.0)
    cn = await _legacy_cn(client, session, auth, inv, 40.0)
    await _back_to_draft(session, auth, inv)
    assert await _hook(session) != "done"
    assert await _books(session, auth) == {"1120": -40.0, "4100": 40.0, "1111": 0.0, "6960": 0.0}
    assert await _doc(session, auth, cn) == ("final", 40.0, 0.0)
    held = await _books(session, auth)
    await _backfill(session, auth)
    assert await _books(session, auth) == held
    f = await _post(client, auth, f"/docs/{inv}/finalize")
    assert f.status_code == 200, f.text
    assert await _books(session, auth) == {"1120": 40.0, "4100": -40.0, "1111": 0.0, "6960": 0.0}
    assert await _doc(session, auth, inv) == ("partial", 40.0, 40.0)
    assert await _doc(session, auth, cn) == ("paid", 0.0, 40.0)
    a = await _post(client, auth, f"/docs/{cn}/apply-to-invoice", {"target_doc_id": inv, "amount": 40.0})
    assert a.status_code != 200, "one credit note spent twice: " + a.text
    assert await _hook(session) == "done"
    assert await _books(session, auth) == {"1120": 40.0, "4100": -40.0, "1111": 0.0, "6960": 0.0}
    assert (await _pay(client, auth, inv, 40.0)).status_code == 200
    assert await _books(session, auth) == {"1120": 0.0, "4100": -40.0, "1111": 40.0, "6960": 0.0}


async def test_settlement_marks_itself_done_when_nothing_is_pending(client, session, auth):
    inv = await _svc_invoice(client, auth, 80.0)
    cn = await _legacy_cn(client, session, auth, inv, 40.0)
    assert await _hook(session) == "done"
    assert await _doc(session, auth, cn) == ("paid", 0.0, 40.0)
    assert await _books(session, auth) == {"1120": 40.0, "4100": -40.0, "1111": 0.0, "6960": 0.0}


# N7: a credit note refunded or applied at its own rate, brought to its invoice's


async def _other_rate_legacy_cn(session, auth, client, inv, total):
    cn = await _cn(client, auth, inv, total, finalize=False)
    await _raw(session, auth, cn, "doc.updated", {"fields_changed": {"conversion_rate": {"old": 1.1, "new": 1.3}}})
    st = await _state(session, auth, inv)
    await _raw(session, auth, inv, "doc.updated",
               {"fields_changed": {"amount_outstanding": {"old": st["amount_outstanding"],
                                                          "new": st["amount_outstanding"] - total}}},
               source_credit_note=cn)
    await _raw(session, auth, cn, "doc.finalized")
    return cn


async def test_refund_at_the_old_rate_is_restated_at_the_invoice_rate(client, session, auth):
    from celerp_docs import legacy_credit_notes as L

    inv = await _svc_invoice(client, auth, 100.0, currency="EUR", conversion_rate=1.1)
    cn = await _other_rate_legacy_cn(session, auth, client, inv, 40.0)
    assert (await _refund(client, auth, cn, 10.0)).status_code == 200
    await _backfill(session, auth)
    settled = {"1120": 77.0, "4100": -66.0, "1111": -13.0, "6960": 2.0}
    assert await _books(session, auth) == settled
    await L._post(session, auth["company_id"], cn, await _state(session, auth, cn), await _state(session, auth, inv), "rerun")
    await session.commit()
    await _backfill(session, auth)
    assert await _books(session, auth) == settled
    assert (await _pay(client, auth, inv, 70.0, conversion_rate=1.1)).status_code == 200
    assert await _books(session, auth) == {"1120": 0.0, "4100": -66.0, "1111": 64.0, "6960": 2.0}
    pay = (await _state(session, auth, inv))["payments"][-1]["index"]
    for doc, index in ((inv, pay), (cn, 0)):
        v = await _post(client, auth, f"/docs/{doc}/void-payment", {"payment_index": index})
        assert v.status_code == 200, v.text
        await _books(session, auth)
    assert (await _post(client, auth, f"/docs/{cn}/void")).status_code == 200
    await _books(session, auth)
    assert (await _post(client, auth, f"/docs/{inv}/void")).status_code == 200
    assert await _books(session, auth) == {"1120": 0.0, "4100": 0.0, "1111": 0.0, "6960": 0.0}


async def test_application_at_the_old_rate_is_restated_and_undone_with_it(client, session, auth):
    inv = await _svc_invoice(client, auth, 100.0, currency="EUR", conversion_rate=1.1)
    other = await _svc_invoice(client, auth, 50.0, currency="EUR", conversion_rate=1.3)
    cn = await _other_rate_legacy_cn(session, auth, client, inv, 40.0)
    a = await _post(client, auth, f"/docs/{cn}/apply-to-invoice", {"target_doc_id": other, "amount": 10.0})
    assert a.status_code == 200, a.text
    await _backfill(session, auth)
    settled = await _books(session, auth)
    assert settled["6960"] == 2.0, settled
    await _backfill(session, auth)
    assert await _books(session, auth) == settled
    index = next(p["index"] for p in (await _state(session, auth, cn))["payments"] if p.get("method") == "applied")
    v = await _post(client, auth, f"/docs/{cn}/void-payment", {"payment_index": index})
    assert v.status_code == 200, v.text
    assert (await _books(session, auth))["6960"] == 0.0


async def test_credit_note_at_its_invoice_rate_is_not_restated(client, session, auth):
    inv = await _svc_invoice(client, auth, 100.0, currency="EUR", conversion_rate=1.1)
    cn = await _legacy_cn(client, session, auth, inv, 40.0, currency="EUR", conversion_rate=1.1)
    assert (await _refund(client, auth, cn, 10.0)).status_code == 200
    await _backfill(session, auth)
    assert (await _books(session, auth))["6960"] == 0.0


# N8: a credit note whose invoice is void


async def _void_on_main(session, auth, inv):
    from celerp.services import auto_je

    await auto_je.void_for_doc_voided(session, company_id=auth["company_id"], user_id=auth["user_id"], doc_id=inv)
    await _raw(session, auth, inv, "doc.voided", {"pre_void_status": "final"})


async def test_credit_note_on_a_void_invoice_keeps_only_what_it_paid_out(client, session, auth):
    inv = await _svc_invoice(client, auth, 80.0)
    other = await _svc_invoice(client, auth, 50.0)
    cn = await _legacy_cn(client, session, auth, inv, 40.0)
    assert (await _refund(client, auth, cn, 10.0)).status_code == 200
    await _void_on_main(session, auth, inv)
    await _backfill(session, auth)
    settled = {"1120": 50.0, "4100": -40.0, "1111": -10.0, "6960": 0.0}
    assert await _books(session, auth) == settled
    assert (await _doc(session, auth, cn))[:2] == ("closed", 0.0)
    for path, body in ((f"/docs/{cn}/cn-refund", {"amount": 30.0, "date": "2026-10-09", "bank_account": "1111"}),
                       (f"/docs/{cn}/apply-to-invoice", {"target_doc_id": other, "amount": 30.0}),
                       (f"/docs/{cn}/void-payment", {"payment_index": 0}),
                       (f"/docs/{cn}/void", None)):
        r = await _post(client, auth, path, body)
        assert r.status_code in (409, 422), f"{path} {r.status_code} {r.text}"
    assert await _books(session, auth) == settled
    await _backfill(session, auth)
    assert await _books(session, auth) == settled
    d = await client.request("DELETE", f"/docs/{cn}/payments/0", headers=auth["headers"], json={})
    assert d.status_code in (409, 422), d.text
    assert await _books(session, auth) == settled


async def test_unused_credit_note_on_a_void_invoice_is_voided_with_it(client, session, auth):
    inv = await _svc_invoice(client, auth, 80.0)
    cn = await _legacy_cn(client, session, auth, inv, 40.0)
    await _void_on_main(session, auth, inv)
    await _backfill(session, auth)
    assert (await _doc(session, auth, cn))[0] == "void"
    assert await _books(session, auth) == {"1120": 0.0, "4100": 0.0, "1111": 0.0, "6960": 0.0}


async def test_a_refund_on_a_credit_note_of_a_void_invoice_is_refused_through_every_refund_route(client, session, auth):
    """The one refund implementation refuses a credit note whose invoice is void, the same
    refusal cn-refund, apply, void-payment and void give: nothing is written."""
    inv = await _svc_invoice(client, auth, 80.0)
    cn = await _legacy_cn(client, session, auth, inv, 40.0)
    assert (await _refund(client, auth, cn, 10.0)).status_code == 200
    await _void_on_main(session, auth, inv)
    await _backfill(session, auth)
    held = await _books(session, auth)
    payment = next(p for p in (await _state(session, auth, cn)).get("payments") or [] if p.get("status") == "active")
    r = await _post(client, auth, f"/docs/{cn}/refund",
                    {"payment_index": payment["index"], "amount": 10.0, "payment_date": "2026-10-09"})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "credit_note.invoice_void"
    assert await _books(session, auth) == held


async def test_a_refund_of_an_invoice_payment_still_gives_the_money_back(client, session, auth):
    """Neighbour: the void-invoice refusal is for credit notes only; an invoice's own
    payment refunds as before."""
    inv = await _svc_invoice(client, auth, 80.0)
    assert (await _pay(client, auth, inv, 80.0)).status_code == 200
    payment = next(p for p in (await _state(session, auth, inv)).get("payments") or [] if p.get("status") == "active")
    r = await _post(client, auth, f"/docs/{inv}/refund",
                    {"payment_index": payment["index"], "amount": 30.0, "payment_date": "2026-10-09"})
    assert r.status_code == 200, r.text
    assert (await _doc(session, auth, inv))[:2] == ("partial", 30.0)
