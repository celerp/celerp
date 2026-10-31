# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Unattended billing: what the dispatcher bills, sends and reports on its own.

An agreement bills itself only in the mode its authorizer chose, only while that
person still holds the permission, at most 10 documents per company per pass, and
never a backlog older than its catch-up window. Sending is a separate choice, and a
failed send keeps the invoice and offers a retry that sends once."""
from __future__ import annotations

import pytest

from agreement_kit import (
    agreement, billed_docs, company_id, contact, freeze_day, member, register, revoke, schedule,
    summary, template, tick,
)

pytestmark = pytest.mark.asyncio


class _Mail:
    """Stands in for the email transport: records each send and answers ``result``."""

    def __init__(self, result=(True, "accepted")):
        self.result, self.sent = result, []

    async def __call__(self, to, subject, body_html, body_text="", **_):
        self.sent.append(to)
        return self.result


@pytest.fixture
def mail(monkeypatch):
    from celerp.services import email

    m = _Mail()
    monkeypatch.setattr(email, "send_email", m)
    return m


async def _auto(client, hd, *, start, catchup=0, send=False, **fields) -> str:
    eid = await agreement(client, hd, start=start, **fields)
    change = {"generation_mode": "auto_invoice", "catchup_periods": catchup}
    if send:
        change["auto_send"] = True
    r = await schedule(client, hd, eid, **change)
    assert r.status_code == 200, r.text
    return eid


async def test_unattended_offline_six_months_preview_only(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-01-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    stale = await _auto(client, hd, start="2026-01-01", catchup=1)
    freeze_day(monkeypatch, "2026-07-15")
    fresh = await _auto(client, hd, start="2026-07-01", catchup=1)
    await tick(session, cid, "2026-07-15")
    assert await billed_docs(session, stale) == []  # seven periods behind, the window is one
    assert [d["period_index"] for d in await billed_docs(session, fresh)] == [0]
    preview = (await client.get("/subscriptions/catch-up", headers=hd)).json()
    row = next(i for i in preview["items"] if i["entity_id"] == stale)
    assert [p["period_index"] for p in row["periods"]] == list(range(7))
    await tick(session, cid, "2026-07-16")
    assert await billed_docs(session, stale) == []  # a later pass never skips ahead either


async def test_unattended_batch_size_bounded(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-07-15")
    hd = await register(client)
    cid = await company_id(client, hd)
    eids = [await _auto(client, hd, start="2026-04-01", catchup=3) for _ in range(3)]  # 4 due each

    async def periods():
        return {eid: [d["period_index"] for d in await billed_docs(session, eid)] for eid in eids}

    await tick(session, cid, "2026-07-15")
    first = await periods()
    assert sum(len(p) for p in first.values()) == 10
    assert all(p == list(range(len(p))) for p in first.values())  # oldest first, never a gap
    await tick(session, cid, "2026-07-15")
    assert await periods() == {eid: [0, 1, 2, 3] for eid in eids}
    await tick(session, cid, "2026-07-15")
    assert sum(len(p) for p in (await periods()).values()) == 12


async def test_unattended_stops_when_authorizer_loses_permission(client, session, monkeypatch, mail):
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    manager, manager_id = await member(client, session, hd, "manager")
    to = "member@example.test"
    billing_by_manager = await agreement(client, hd, start="2026-07-01", contact_id=await contact(client, hd, to))
    assert (await schedule(client, manager, billing_by_manager, generation_mode="auto_invoice")).status_code == 200
    sending_by_manager = await agreement(client, hd, start="2026-07-01", contact_id=await contact(client, hd, to))
    assert (await schedule(client, hd, sending_by_manager, generation_mode="auto_invoice")).status_code == 200
    assert (await schedule(client, manager, sending_by_manager, auto_send=True)).status_code == 200
    assert (await template(client, hd, sending_by_manager))["consents"]["sending"]["by"] == manager_id
    await revoke(client, hd, "automate_billing", "manager")
    await revoke(client, hd, "automate_sending", "manager")
    await tick(session, cid, "2026-07-01")
    assert await billed_docs(session, billing_by_manager) == []
    # Billing was authorized by someone who still may; only the sending stops.
    docs = await billed_docs(session, sending_by_manager)
    assert [(d["period_index"], d["status"]) for d in docs] == [(0, "final")]
    assert mail.sent == []
    text = str(await summary(client, hd))
    assert "Automatic billing stopped: authorization lapsed" in text
    assert "Automatic sending stopped: authorization lapsed" in text


async def test_auto_send_separate_from_auto_invoice(client, session, monkeypatch, mail):
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    quiet = await _auto(client, hd, start="2026-07-01", contact_id=await contact(client, hd, "quiet@example.test"))
    loud = await _auto(client, hd, start="2026-07-01", send=True,
                       contact_id=await contact(client, hd, "loud@example.test"))
    await tick(session, cid, "2026-07-01")
    await tick(session, cid, "2026-07-02")
    assert [d["status"] for d in await billed_docs(session, quiet)] == ["final"]
    assert [d["status"] for d in await billed_docs(session, loud)] == ["sent"]
    assert mail.sent == ["loud@example.test"]  # once, across two passes


async def test_email_failure_keeps_invoice_and_shows_retry(client, session, monkeypatch, mail):
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await _auto(client, hd, start="2026-07-01", send=True,
                      contact_id=await contact(client, hd, "member@example.test"))
    mail.result = (False, "mailbox unavailable")
    await tick(session, cid, "2026-07-01")
    [doc] = await billed_docs(session, eid)
    assert doc["status"] == "final"
    report = await summary(client, hd)
    assert report["counts"]["generated"] == 1 and report["counts"]["send_failed"] == 1
    row = next(r for r in report["rows"] if r["kind"] == "send_failed")
    assert row["doc_id"] == doc["entity_id"] and "mailbox unavailable" in row["message"]
    await tick(session, cid, "2026-07-02")
    assert len(mail.sent) == 1  # a failed send waits for the operator's retry
    mail.result = (True, "accepted")
    retried = await client.post(row["retry_url"], headers=hd)
    assert retried.status_code == 200, retried.text
    again = await client.post(row["retry_url"], headers=hd)
    assert again.status_code == 200
    assert len(mail.sent) == 2  # the retry sent once
    assert [d["status"] for d in await billed_docs(session, eid)] == ["sent"]
    assert (await summary(client, hd))["counts"]["sent"] == 1


async def test_operator_summary_since_ack(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    manager, _ = await member(client, session, hd, "manager")
    eid = await _auto(client, hd, start="2026-07-01")
    assert (await summary(client, hd))["counts"]["generated"] == 0
    await tick(session, cid, "2026-07-01")
    assert (await summary(client, hd))["counts"]["generated"] == 1
    # A client-supplied time is ignored: the server stamps the acknowledgment.
    ack = await client.post("/subscriptions/summary/ack", headers=hd, json={"at": "2100-01-01T00:00:00Z"})
    assert ack.status_code == 200, ack.text
    assert (await summary(client, hd))["counts"]["generated"] == 0
    assert (await summary(client, manager))["counts"]["generated"] == 1  # per user
    freeze_day(monkeypatch, "2026-08-01")
    await tick(session, cid, "2026-08-01")
    mine = await summary(client, hd)
    assert mine["counts"]["generated"] == 1
    assert [r["period_index"] for r in mine["rows"] if r["kind"] == "generated"] == [1]
    assert (await summary(client, manager))["counts"]["generated"] == 2
    shown = await summary(client, hd, all=1)
    assert [r["period_index"] for r in shown["rows"] if r["entity_id"] == eid] == [1, 0]  # newest first


async def test_module_disabled_skips_dispatch_keeps_records(client, session, monkeypatch):
    import uuid
    from celerp.models.company import Company
    from celerp.modules.registry import disable_for_company, enable_for_company

    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await _auto(client, hd, start="2026-07-01", catchup=1)
    kept = ("generation_mode", "consents", "periods", "catchup_periods")
    before = await template(client, hd, eid)
    company = await session.get(Company, uuid.UUID(cid))
    company.settings = disable_for_company(company.settings, "celerp-subscriptions")
    await session.flush()
    await tick(session, cid, "2026-07-01")
    assert await billed_docs(session, eid) == []
    company.settings, _ = enable_for_company(company.settings, "celerp-subscriptions")
    await session.flush()
    after = await template(client, hd, eid)
    assert {k: after.get(k) for k in kept} == {k: before.get(k) for k in kept}
    await tick(session, cid, "2026-08-01")  # back on: resumes within the catch-up window
    assert [d["period_index"] for d in await billed_docs(session, eid)] == [0, 1]
