# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A void invoice owes nothing and cannot be paid online.

Voiding clears the balance; unvoiding works it out again from the total and what was
paid. The share view drops its Pay bar and the payment link refuses for any document
that is not awaiting payment, so an invoice voided before its balance was cleared
cannot take a payment either.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection
from celerp.output.doc_print import render_doc_print_html
from celerp_docs.doc_projections import apply_documents_event
from test_payments_stripe import _h, _payable_invoice, _register, payments_on  # noqa: F401

pytestmark = pytest.mark.usefixtures("docs_running")

PAY_BAR = 'class="dp-paybar"'


def test_void_clears_the_balance_and_unvoid_works_it_out_again():
    state = {"doc_type": "invoice", "status": "partial", "currency": "USD", "total": 1070.0,
             "amount_paid": 300.0, "amount_outstanding": 770.0}
    voided = apply_documents_event(state, "doc.voided", {"pre_void_status": "partial"})
    assert voided["amount_outstanding"] == 0
    assert voided["amount_paid"] == 300.0
    restored = apply_documents_event(voided, "doc.unvoided", {"restored_status": "partial"})
    assert restored["status"] == "partial"
    assert restored["amount_outstanding"] == 770.0


@pytest.mark.asyncio
async def test_a_voided_invoice_owes_nothing_until_unvoided(client):
    tok = await _register(client)
    eid, _ = await _payable_invoice(client, tok)
    assert (await client.post(f"/docs/{eid}/void", json={}, headers=_h(tok))).status_code == 200
    doc = (await client.get(f"/docs/{eid}", headers=_h(tok))).json()
    assert (doc["status"], doc["amount_outstanding"]) == ("void", 0)
    assert (await client.post(f"/docs/{eid}/unvoid", json={}, headers=_h(tok))).status_code == 200
    doc = (await client.get(f"/docs/{eid}", headers=_h(tok))).json()
    assert (doc["status"], doc["amount_outstanding"]) == ("final", 1070.0)


@pytest.mark.parametrize("status", ["void", "draft"])
def test_the_pay_bar_shows_only_while_awaiting_payment(status):
    """A balance left on the document, as an older void kept it, does not bring the bar back."""
    doc = {"doc_type": "invoice", "status": status, "currency": "USD", "total": 1070.0,
           "amount_outstanding": 1070.0, "line_items": []}
    assert PAY_BAR not in render_doc_print_html(doc, pay_url="/pay/t")
    assert PAY_BAR in render_doc_print_html({**doc, "status": "final"}, pay_url="/pay/t")


async def _void_with_old_balance(client, session, tok, eid) -> None:
    """Void the invoice, then put back the balance a void used to keep."""
    assert (await client.post(f"/docs/{eid}/void", json={}, headers=_h(tok))).status_code == 200
    row = (await session.execute(select(Projection).where(Projection.entity_id == eid))).scalars().one()
    row.state = {**row.state, "amount_outstanding": 1070.0}
    await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("old_balance", [False, True])
async def test_a_voided_invoice_cannot_be_paid_online(client, session, payments_on, monkeypatch,  # noqa: F811
                                                      old_balance):
    opened = []

    async def _mk(**kw):
        opened.append(kw)
        return {"url": "https://stripe.test/cs_1"}
    monkeypatch.setattr("celerp.services.payments.create_checkout", _mk)

    tok = await _register(client)
    eid, token = await _payable_invoice(client, tok)
    if old_balance:
        await _void_with_old_balance(client, session, tok, eid)
    else:
        assert (await client.post(f"/docs/{eid}/void", json={}, headers=_h(tok))).status_code == 200

    assert f"/pay/{token}" not in (await client.get(f"/share/{token}")).text
    r = await client.get(f"/pay/{token}", follow_redirects=False)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "pay.not_awaiting_payment"
    assert opened == []
