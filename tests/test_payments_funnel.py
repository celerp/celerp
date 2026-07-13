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


# ── reminder backoff: 4/6/9/14-week gaps, then silence for good ───────────────

@pytest.mark.asyncio
async def test_payments_reminder_backoff_series_then_stops(client, session):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select
    from celerp.models.company import Company
    from celerp.models.notification import Notification
    from celerp.services.payments_reminder import BACKOFF_WEEKS, _STATE_KEY, _remind_company

    r = await client.post("/auth/register", json={
        "company_name": "RemindCo", "email": "remind@test.com", "name": "A", "password": "password123"})
    assert r.status_code == 200
    company = (await session.execute(select(Company).where(Company.name == "RemindCo"))).scalar_one()

    def _age_last(weeks: int):
        """Pretend the last nudge happened `weeks` ago."""
        s = dict(company.settings)
        st = dict(s[_STATE_KEY])
        st["last_at"] = (datetime.now(timezone.utc) - timedelta(weeks=weeks)).isoformat()
        s[_STATE_KEY] = st
        company.settings = s

    assert await _remind_company(session, company) is True      # nudge 1: immediate
    assert await _remind_company(session, company) is False     # gap not served

    for i, gap in enumerate(BACKOFF_WEEKS):
        _age_last(gap - 1)
        assert await _remind_company(session, company) is False, f"fired early before gap {gap}w"
        _age_last(gap)
        assert await _remind_company(session, company) is True, f"nudge {i + 2} after {gap}w"

    # Series exhausted: silent forever, no matter how much time passes.
    _age_last(52)
    assert await _remind_company(session, company) is False

    notifs = (await session.execute(
        select(Notification).where(Notification.company_id == company.id,
                                   Notification.category == "payments")
    )).scalars().all()
    assert len(notifs) == len(BACKOFF_WEEKS) + 1
    assert all(n.action_url == "/settings/payments" for n in notifs)
