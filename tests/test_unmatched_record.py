# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Recording an unmatched online payment on an invoice the person chooses, from
Settings > Payments, and putting it back by deleting it from the invoice."""

from __future__ import annotations

import datetime

import pytest

from test_payments_stripe import BOOKS, PAID, _company_id, _doc_state, _h, _payable_invoice, _register
from test_payments_stripe import payments_on  # noqa: F401  (fixture)

REFUSED = {
    "currency": "This invoice is in another currency than the payment.",
    "too_small": "This invoice owes less than the payment.",
    "paid": "This invoice is already paid.",
}


async def _park(cid: str, reference: str = "pi_lost", amount_minor: int = 107000, currency: str = "usd",
                *, refund: bool = False) -> None:
    """An online payment for an invoice that no longer exists, kept among the
    unmatched payments, with a refund of 100.00 kept with it when *refund*."""
    from celerp.services.payments import receive_payment, receive_refund
    assert await receive_payment({"company_id": cid, "entity_id": "doc:deleted", "reference": reference,
                                  "amount_minor": amount_minor, "currency": currency,
                                  "paid_at": PAID.isoformat(), "context": BOOKS, "managed": True}) is True
    if refund:
        assert await receive_refund({
            "company_id": cid, "entity_id": "doc:deleted", "reference": reference, "refund_id": "re_lost",
            "transition": "applied", "cycle": 1, "amount_minor": 10000, "currency": currency,
            "occurred_at": (PAID + datetime.timedelta(days=1)).isoformat(), "context": BOOKS}) is True


async def _listed(client, tok) -> list[str]:
    return [p["reference"] for p in (await client.get("/payments/unmatched", headers=_h(tok))).json()["items"]]


def _record(client, tok, eid, reference="pi_lost"):
    return client.post("/payments/unmatched/record", headers=_h(tok),
                       json={"reference": reference, "entity_id": eid})


@pytest.mark.asyncio
async def test_an_unmatched_payment_recorded_on_an_invoice_leaves_the_list_and_pays_it(client, session, payments_on):
    tok = await _register(client)
    cid = _company_id(tok)
    eid, _ = await _payable_invoice(client, tok)
    await _park(cid, refund=True)
    assert await _listed(client, tok) == ["pi_lost"]

    r = await _record(client, tok, eid)

    assert r.status_code == 200, r.text
    assert await _listed(client, tok) == []
    doc = await _doc_state(client, tok, eid)
    [payment] = doc["payments"]
    assert (payment["reference"], payment["method"], payment["amount"]) == ("pi_lost", "stripe", 1070.0)
    assert payment["payment_date"] == PAID.date().isoformat()
    # The refund kept with it is applied in the same transaction.
    assert payment.get("refunded")
    assert (await client.get("/payments/unmatched", headers=_h(tok))).json()["refunds"] == []
    assert doc["amount_outstanding"] == 100.0


@pytest.mark.asyncio
async def test_recording_it_twice_changes_nothing(client, session, payments_on):
    tok = await _register(client)
    eid, _ = await _payable_invoice(client, tok)
    await _park(_company_id(tok))
    assert (await _record(client, tok, eid)).status_code == 200
    before = await _doc_state(client, tok, eid)

    r = await _record(client, tok, eid)

    assert r.status_code == 200, r.text
    after = await _doc_state(client, tok, eid)
    assert after["payments"] == before["payments"] and after["version"] == before["version"]


@pytest.mark.asyncio
async def test_a_later_delivery_of_it_and_of_its_refund_go_to_the_chosen_invoice(client, session, payments_on):
    """Cloud delivers again after a System Recovery, and Stripe reports refunds: they
    go to the invoice a person chose, never back on the list or onto a second invoice."""
    from celerp.services.payments import receive_payment, receive_refund
    tok = await _register(client)
    cid = _company_id(tok)
    eid, _ = await _payable_invoice(client, tok)
    other, _ = await _payable_invoice(client, tok)
    await _park(cid)
    assert (await _record(client, tok, eid)).status_code == 200

    assert await receive_payment({"company_id": cid, "entity_id": other, "reference": "pi_lost",
                                  "amount_minor": 107000, "currency": "usd", "paid_at": PAID.isoformat(),
                                  "context": BOOKS, "managed": True}) is True
    assert await receive_refund({
        "company_id": cid, "entity_id": "doc:deleted", "reference": "pi_lost", "refund_id": "re_later",
        "transition": "applied", "cycle": 1, "amount_minor": 5000, "currency": "usd",
        "occurred_at": (PAID + datetime.timedelta(days=2)).isoformat(), "context": BOOKS}) is True

    assert not (await _doc_state(client, tok, other)).get("payments")
    assert (await _doc_state(client, tok, eid))["amount_outstanding"] == 50.0
    body = (await client.get("/payments/unmatched", headers=_h(tok))).json()
    assert body == {"items": [], "refunds": []}


@pytest.mark.parametrize("reason", ["currency", "too_small", "paid"])
@pytest.mark.asyncio
async def test_an_invoice_that_cannot_take_it_is_refused_with_the_reason_and_the_row_stays(
        client, session, payments_on, reason):
    tok = await _register(client)
    eid, _ = await _payable_invoice(client, tok)
    if reason == "paid":
        assert (await client.post(f"/docs/{eid}/payment", headers=_h(tok), json={
            "amount": 1070.0, "payment_date": "2026-07-13", "bank_account": "1110"})).status_code == 200
    await _park(_company_id(tok), amount_minor=200000 if reason == "too_small" else 107000,
                currency="eur" if reason == "currency" else "usd")
    before = await _doc_state(client, tok, eid)

    r = await _record(client, tok, eid)

    assert r.status_code == 409
    assert r.json()["detail"] == {"message": REFUSED[reason], "reason": reason}
    assert await _listed(client, tok) == ["pi_lost"]
    assert (await _doc_state(client, tok, eid)).get("payments") == before.get("payments")


@pytest.mark.asyncio
async def test_a_role_without_record_payments_is_refused(client, session, payments_on):
    from sqlalchemy import update
    from celerp.models.accounting import UserCompany
    tok = await _register(client)
    eid, _ = await _payable_invoice(client, tok)
    await _park(_company_id(tok))
    await session.execute(update(UserCompany).values(role="viewer"))
    await session.commit()

    r = await _record(client, tok, eid)
    listed = await client.get(f"/payments/unmatched/invoices?reference=pi_lost", headers=_h(tok))

    assert r.status_code == 403 and r.json()["detail"] == "Requires the record_payments permission"
    assert listed.status_code == 403
    assert await _listed(client, tok) == ["pi_lost"]
    assert not (await _doc_state(client, tok, eid)).get("payments")


@pytest.mark.asyncio
async def test_the_dropdown_offers_only_open_invoices_that_take_the_whole_payment(client, session, payments_on):
    tok = await _register(client)
    fits, _ = await _payable_invoice(client, tok)
    paid, _ = await _payable_invoice(client, tok)
    assert (await client.post(f"/docs/{paid}/payment", headers=_h(tok), json={
        "amount": 1070.0, "payment_date": "2026-07-13", "bank_account": "1110"})).status_code == 200
    small = (await client.post("/docs", headers=_h(tok), json={
        "doc_type": "invoice", "contact_name": "Buyer", "currency": "USD", "total": 500.0, "subtotal": 500.0,
        "line_items": [{"description": "Widget", "quantity": 1, "unit_price": 500.0}]})).json()["id"]
    assert (await client.post(f"/docs/{small}/finalize", headers=_h(tok))).status_code == 200
    draft = (await client.post("/docs", headers=_h(tok), json={
        "doc_type": "invoice", "contact_name": "Buyer", "currency": "USD", "total": 5000.0, "subtotal": 5000.0,
        "line_items": [{"description": "Widget", "quantity": 1, "unit_price": 5000.0}]})).json()["id"]
    await _park(_company_id(tok))

    r = await client.get("/payments/unmatched/invoices?reference=pi_lost", headers=_h(tok))

    assert r.status_code == 200, r.text
    assert [i["id"] for i in r.json()["items"]] == [fits]
    assert draft and small and paid


@pytest.mark.asyncio
async def test_deleting_it_from_the_invoice_puts_it_back_on_the_list(client, session, payments_on):
    tok = await _register(client)
    eid, _ = await _payable_invoice(client, tok)
    await _park(_company_id(tok))
    assert (await _record(client, tok, eid)).status_code == 200
    payment = (await _doc_state(client, tok, eid))["payments"][0]
    assert payment.get("unmatched") is True

    r = await client.delete(f"/docs/{eid}/payments/{payment['index']}", headers=_h(tok))

    assert r.status_code == 200, r.text
    assert await _listed(client, tok) == ["pi_lost"]
    doc = await _doc_state(client, tok, eid)
    assert doc["amount_outstanding"] == 1070.0
    # Back on the list, it can be recorded again.
    assert (await _record(client, tok, eid)).status_code == 200
    assert await _listed(client, tok) == []


@pytest.mark.asyncio
async def test_a_payment_stripe_delivered_on_its_own_invoice_still_cannot_be_deleted(client, session, payments_on):
    from celerp.services.payments import receive_payment
    tok = await _register(client)
    eid, _ = await _payable_invoice(client, tok)
    assert await receive_payment({"company_id": _company_id(tok), "entity_id": eid, "reference": "pi_card",
                                  "amount_minor": 107000, "currency": "usd", "paid_at": PAID.isoformat(),
                                  "context": BOOKS, "managed": True}) is True
    assert "unmatched" not in (await _doc_state(client, tok, eid))["payments"][0]

    r = await client.delete(f"/docs/{eid}/payments/0", headers=_h(tok))

    assert r.status_code == 422


def test_the_refusal_is_shown_in_the_users_language():
    from ui.api_client import APIError
    from ui.i18n import set_lang
    from ui.routes.settings_payments import _refusal
    error = APIError(409, REFUSED["too_small"], {"message": REFUSED["too_small"], "reason": "too_small"})
    set_lang("de")
    try:
        assert _refusal(error) == "Auf dieser Rechnung ist weniger offen als die Zahlung."
    finally:
        set_lang("en")
    assert _refusal(error) == REFUSED["too_small"]



def test_the_list_hint_says_how_to_record_one_here():
    from fasthtml.common import to_xml
    from ui.i18n import set_lang
    from ui.routes.settings_payments import _unmatched_payments
    row = [{"reference": "pi_1", "received_at": "2026-10-01T00:00:00", "amount": 10, "currency": "USD"}]
    set_lang("de")
    try:
        assert "doppelklicken Sie auf die Zelle Dokument" in to_xml(_unmatched_payments(row))
    finally:
        set_lang("en")
    assert "double-click its Document cell" in to_xml(_unmatched_payments(row))

# ── Settings > Payments: the invoice cell ────────────────────────────────────

INVOICES = {"items": [{"id": "doc:inv-1", "ref": "INV-0001", "contact_name": "Buyer",
                       "outstanding": 1070.0, "currency": "USD"}]}


@pytest.fixture
async def ui_client():
    from httpx import ASGITransport, AsyncClient
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                           follow_redirects=False) as c:
        yield c


def _ui_cookies() -> dict:
    from test_helpers import make_test_token
    return {"celerp_token": make_test_token(role="owner")}


@pytest.mark.asyncio
async def test_the_invoice_cell_opens_the_searchable_list_of_invoices(ui_client):
    from unittest.mock import AsyncMock, patch
    with patch("ui.api_client.get_unmatched_invoices", new=AsyncMock(return_value=INVOICES)):
        r = await ui_client.get("/settings/payments/unmatched/pi_lost/invoice/edit", cookies=_ui_cookies())
    assert r.status_code == 200
    assert "combobox" in r.text and "INV-0001 - Buyer" in r.text and 'value="doc:inv-1"' in r.text
    assert 'hx-patch="/settings/payments/unmatched/pi_lost/invoice"' in r.text


@pytest.mark.asyncio
async def test_choosing_an_invoice_records_it_and_removes_the_row(ui_client):
    from unittest.mock import AsyncMock, patch
    record = AsyncMock(return_value={"ok": True})
    with patch("ui.api_client.record_unmatched_payment", new=record):
        r = await ui_client.patch("/settings/payments/unmatched/pi_lost/invoice", cookies=_ui_cookies(),
                                  data={"value": "doc:inv-1"})
    record.assert_awaited_once()
    assert record.await_args.args[1:] == ("pi_lost", "doc:inv-1")
    assert r.headers.get("HX-Retarget") == "closest tr" and r.headers.get("HX-Reswap") == "delete"


@pytest.mark.asyncio
async def test_a_refusal_keeps_the_list_open_with_the_reason_in_the_users_language(ui_client):
    from unittest.mock import AsyncMock, patch
    from ui.api_client import APIError
    error = APIError(409, REFUSED["paid"], {"message": REFUSED["paid"], "reason": "paid"})
    with patch("ui.api_client.record_unmatched_payment", new=AsyncMock(side_effect=error)), \
         patch("ui.api_client.get_unmatched_invoices", new=AsyncMock(return_value=INVOICES)):
        r = await ui_client.patch("/settings/payments/unmatched/pi_lost/invoice",
                                  cookies={**_ui_cookies(), "celerp_lang": "de"}, data={"value": "doc:inv-1"})
    assert "HX-Reswap" not in r.headers
    assert "Diese Rechnung ist bereits bezahlt." in r.text and "cell-error" in r.text
    assert "combobox" in r.text
