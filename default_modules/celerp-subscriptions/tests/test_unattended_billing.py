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
    from celerp.modules.registry import disable_for_company, enable_for_company
    from celerp.services.company_lock import locked_company

    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await _auto(client, hd, start="2026-07-01", catchup=1)
    kept = ("generation_mode", "consents", "periods", "catchup_periods")
    before = await template(client, hd, eid)
    company = await locked_company(session, uuid.UUID(cid))
    company.settings = disable_for_company(company.settings, "celerp-subscriptions")
    await session.commit()
    await tick(session, cid, "2026-07-01")
    assert await billed_docs(session, eid) == []
    company = await locked_company(session, uuid.UUID(cid))
    company.settings, _ = enable_for_company(company.settings, "celerp-subscriptions")
    await session.commit()
    after = await template(client, hd, eid)
    assert {k: after.get(k) for k in kept} == {k: before.get(k) for k in kept}
    await tick(session, cid, "2026-08-01")  # back on: resumes within the catch-up window
    assert [d["period_index"] for d in await billed_docs(session, eid)] == [0, 1]


async def test_unattended_rechecks_the_mode_under_the_lock(client, session, monkeypatch):
    """A pass that read the agreement before the operator set billing back to manual bills
    nothing: what it may bill is decided again from the agreement as locked."""
    import copy
    import uuid
    from types import SimpleNamespace
    from celerp_subscriptions import billing

    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await _auto(client, hd, start="2026-07-01")
    stale = [SimpleNamespace(entity_id=r.entity_id, state=copy.deepcopy(r.state))
             for r in await billing.agreements(session, uuid.UUID(cid))]
    assert (await schedule(client, hd, eid, generation_mode="manual_due")).status_code == 200

    async def snapshot(_session, _company_id):
        return stale

    monkeypatch.setattr(billing, "agreements", snapshot)
    await tick(session, cid, "2026-07-01")
    assert await billed_docs(session, eid) == []


async def test_a_document_voided_while_it_is_emailed_stays_void(client, session, monkeypatch, mail):
    """The sent mark goes through the document's own send transition, which refuses a void
    document: an email that crossed a void never turns the document back into a sent one."""
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await _auto(client, hd, start="2026-07-01", send=True,
                      contact_id=await contact(client, hd, "member@example.test"))
    real = mail.__call__

    async def void_then_send(to, subject, body_html, body_text="", **kw):
        [doc] = await billed_docs(session, eid)
        r = await client.post(f"/docs/{doc['entity_id']}/void", headers=hd, json={"reason": "Wrong customer"})
        assert r.status_code == 200, r.text
        return await real(to, subject, body_html, body_text, **kw)

    from celerp.services import email
    monkeypatch.setattr(email, "send_email", void_then_send)
    await tick(session, cid, "2026-07-01")
    [doc] = await billed_docs(session, eid)
    assert doc["status"] == "void"
    assert (await summary(client, hd))["counts"]["sent"] == 1  # the email did go


async def test_an_interrupted_send_is_shown_with_a_retry(client, session, monkeypatch, mail):
    """A send cut off before its outcome was recorded (the process stopped mid-send) is not
    left silent: the summary names it and offers the retry; a transport that raises records
    a failed send."""
    import asyncio

    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    stopped = await _auto(client, hd, start="2026-07-01", send=True,
                          contact_id=await contact(client, hd, "member@example.test"))

    async def cut_off(*_, **__):
        raise asyncio.CancelledError

    from celerp.services import email
    monkeypatch.setattr(email, "send_email", cut_off)
    with pytest.raises(asyncio.CancelledError):
        await tick(session, cid, "2026-07-01")
    [row] = [r for r in (await summary(client, hd))["rows"] if r["kind"] == "send_unconfirmed"]
    assert row["entity_id"] == stopped and row["retry_url"]
    monkeypatch.setattr(email, "send_email", mail)
    assert (await client.post(row["retry_url"], headers=hd)).status_code == 200
    assert mail.sent == ["member@example.test"]

    async def broken(*_, **__):
        raise RuntimeError("relay unreachable")

    monkeypatch.setattr(email, "send_email", broken)
    raising = await _auto(client, hd, start="2026-07-01", send=True,
                          contact_id=await contact(client, hd, "other@example.test"))
    await tick(session, cid, "2026-07-01")
    failed = [r for r in (await summary(client, hd))["rows"] if r["kind"] == "send_failed"]
    assert [r["entity_id"] for r in failed] == [raising] and "relay unreachable" in failed[0]["message"]


async def test_a_retried_send_is_recorded_as_the_person_who_retried(client, session, monkeypatch, mail):
    from sqlalchemy import select
    from celerp.models.ledger import LedgerEntry

    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    manager, manager_id = await member(client, session, hd, "manager")
    eid = await _auto(client, hd, start="2026-07-01", send=True,
                      contact_id=await contact(client, hd, "member@example.test"))
    mail.result = (False, "mailbox unavailable")
    await tick(session, cid, "2026-07-01")
    row = next(r for r in (await summary(client, hd))["rows"] if r["kind"] == "send_failed")
    mail.result = (True, "accepted")
    assert (await client.post(row["retry_url"], headers=manager)).status_code == 200
    actors = (await session.execute(select(LedgerEntry.actor_id).where(
        LedgerEntry.entity_id == row["doc_id"], LedgerEntry.event_type == "doc.sent"))).scalars().all()
    assert [str(a) for a in actors] == [manager_id]


async def test_purchase_agreements_are_not_emailed_unattended(client, session, monkeypatch):
    """Purchase orders are never emailed from the app, so a purchase agreement cannot be set
    to send itself."""
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-07-01", doc_type="subscription_po")
    r = await schedule(client, hd, eid, auto_send=True)
    assert r.status_code == 422, r.text
    assert not (await template(client, hd, eid)).get("auto_send")
