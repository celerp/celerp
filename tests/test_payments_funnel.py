# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Online-payments discovery funnel: the Payments settings tab, the send-modal
hint, the unpaid-invoice hint, the cheap flag endpoint, and outstanding-balance
amounts on the Pay button (never the face total)."""
from __future__ import annotations

import pytest


# ── compose_doc_email: Pay button charges/labels the remaining balance ───────

def test_email_pay_button_uses_amount_due_not_total():
    from celerp_docs.doc_email import compose_doc_email
    html, text = compose_doc_email(
        doc_type_label="Invoice", doc_number="INV-1", sender_name="Acme",
        contact_name="Buyer", total=1070.0, currency="USD",
        message=None, view_url="https://x/share/t", pay_url="https://x/pay/t",
        amount_due=570.0,
    )
    assert ">Pay USD 570.00<" in html
    assert "Amount due: <strong>USD 570.00</strong>" in html
    assert "Amount: <strong>USD 1,070.00</strong>" in html
    assert "Amount due: USD 570.00" in text


def test_email_no_due_line_when_nothing_paid_yet():
    from celerp_docs.doc_email import compose_doc_email
    html, text = compose_doc_email(
        doc_type_label="Invoice", doc_number="INV-1", sender_name="Acme",
        contact_name="Buyer", total=1070.0, currency="USD",
        message=None, view_url="https://x/share/t", pay_url="https://x/pay/t",
        amount_due=1070.0,
    )
    assert ">Pay USD 1,070.00<" in html
    assert "Amount due:" not in html and "Amount due:" not in text


# ── /payments/enabled: the cheap per-render flag ──────────────────────────────

@pytest.mark.asyncio
async def test_payments_enabled_flag_endpoint(client, monkeypatch):
    r = await client.post("/auth/register", json={
        "company_name": "FunnelCo", "email": "funnel@test.com", "name": "A", "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.get("/payments/enabled", headers=h)
    assert r.status_code == 200 and r.json() == {"enabled": False}
    monkeypatch.setattr("celerp.services.payments.payments_enabled", lambda: True)
    r = await client.get("/payments/enabled", headers=h)
    assert r.json() == {"enabled": True}


# ── monthly reminder: 30-day re-arm per company ───────────────────────────────

@pytest.mark.asyncio
async def test_payments_reminder_respects_30_day_window(client, session):
    from sqlalchemy import select
    from celerp.models.company import Company
    from celerp.models.notification import Notification
    from celerp.services.payments_reminder import _remind_company

    r = await client.post("/auth/register", json={
        "company_name": "RemindCo", "email": "remind@test.com", "name": "A", "password": "password123"})
    assert r.status_code == 200
    company_id = (await session.execute(select(Company.id).where(Company.name == "RemindCo"))).scalar_one()

    assert await _remind_company(session, company_id) is True     # first nudge fires
    assert await _remind_company(session, company_id) is False    # re-armed only after 30 days

    notifs = (await session.execute(
        select(Notification).where(Notification.company_id == company_id,
                                   Notification.category == "payments")
    )).scalars().all()
    assert len(notifs) == 1
    assert notifs[0].action_url == "/settings/payments"
