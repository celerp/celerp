# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Unattended billing: what the dispatcher bills, sends and reports on its own.

An agreement bills itself only in the mode its authorizer chose, only while that
person still holds the permission, at most 10 documents per company per pass, and
never a backlog older than its catch-up window. Sending is a separate choice, and a
failed send keeps the invoice and offers a retry that sends once."""
from __future__ import annotations

import pytest

from celerp.services.email import EmailResult
from celerp_subscriptions.summary import KINDS as SUMMARY_KINDS, _key as summary_key
from ui.i18n import t

from agreement_kit import (
    agreement, billed_docs, company_id, contact, freeze_day, mark_read, member, register, revoke,
    schedule, summary, template, tick,
)

pytestmark = pytest.mark.asyncio


_REFUSED = EmailResult(False, "550 mailbox unavailable", reason="email.err_smtp_refused")


class _Mail:
    """Stands in for the email transport: records each send and answers ``result``."""

    def __init__(self, result=EmailResult(True, "accepted")):
        self.result, self.sent, self.operation_ids = result, [], []

    async def __call__(self, to, subject, body_html, body_text="", **kwargs):
        self.sent.append(to)
        self.operation_ids.append(kwargs.get("operation_id"))
        return self.result


@pytest.fixture
def mail(monkeypatch):
    from celerp.services import email

    m = _Mail()
    monkeypatch.setattr(email, "send_email", m)
    return m


def _stopped_long_ago(monkeypatch) -> None:
    """The process stopped mid-send longer ago than a send can take, so the send it left
    recorded as sending is no longer with the mail server: it reads as cut off."""
    from celerp_subscriptions import billing
    monkeypatch.setattr(billing, "SEND_TIMEOUT", billing.SEND_TIMEOUT * 0)


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
    lapsed = {"billing_lapsed", "sending_lapsed"}
    page = await summary(client, hd)
    text = str(page)
    assert "Automatic billing stopped: authorization lapsed" in text
    assert "Automatic sending stopped: authorization lapsed" in text
    # Shown while the authorization stays lapsed: no key, so Mark as read leaves both listed.
    assert [r["key"] for r in page["rows"] if r["kind"] in lapsed] == [None, None]
    assert (await mark_read(client, hd, page)).status_code == 200
    assert sorted(r["kind"] for r in (await summary(client, hd))["rows"] if r["kind"] in lapsed) == sorted(lapsed)


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
    mail.result = _REFUSED
    await tick(session, cid, "2026-07-01")
    [doc] = await billed_docs(session, eid)
    assert doc["status"] == "final"
    report = await summary(client, hd)
    assert report["counts"]["generated"] == 1 and report["counts"]["send_failed"] == 1
    row = next(r for r in report["rows"] if r["kind"] == "send_failed")
    assert row["doc_id"] == doc["entity_id"] and t("email.err_smtp_refused") in row["message"]
    await tick(session, cid, "2026-07-02")
    assert len(mail.sent) == 1  # a failed send waits for the operator's retry
    mail.result = EmailResult(True, "accepted")
    retried = await client.post(row["retry_url"], headers=hd, json={"attempt": row["attempt"]})
    assert retried.status_code == 200, retried.text
    again = await client.post(row["retry_url"], headers=hd, json={"attempt": row["attempt"]})
    assert again.status_code == 200
    assert len(mail.sent) == 2  # the retry sent once
    assert len(set(mail.operation_ids)) == 2  # a confirmed failure permits a new send identity
    assert [d["status"] for d in await billed_docs(session, eid)] == ["sent"]
    counts = (await summary(client, hd))["counts"]
    assert counts["sent"] == 0 and counts["send_failed"] == 0  # the operator sent it by hand


async def test_operator_summary_since_ack(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    manager, _ = await member(client, session, hd, "manager")
    eid = await _auto(client, hd, start="2026-07-01")
    assert (await summary(client, hd))["counts"]["generated"] == 0
    await tick(session, cid, "2026-07-01")
    assert (await summary(client, hd))["counts"]["generated"] == 1
    # Mark as read covers the rows the page showed: the August bill below still lists.
    ack = await mark_read(client, hd, await summary(client, hd))
    assert ack.status_code == 200 and ack.json()["read"] == 1, ack.text
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


@pytest.mark.parametrize("body", [None, {}, {"keys": None}, {"keys": "x"}, {"keys": {}}, {"keys": True},
                                  {"keys": [1]}, {"keys": [None]}, {"keys": [[]]}, {"read_at": "2026-07-01T00:00:00Z"}])
async def test_mark_as_read_without_readable_row_keys_is_refused_and_acknowledges_nothing(
        client, session, monkeypatch, body):
    """Mark as read names the rows the page showed by their keys: a missing or unreadable list is
    refused (422) and the summary still lists the row."""
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    await _auto(client, hd, start="2026-07-01")
    await tick(session, await company_id(client, hd), "2026-07-01")
    ack = await client.post("/subscriptions/summary/ack", headers=hd, **({} if body is None else {"json": body}))
    assert ack.status_code == 422, ack.text
    assert (await summary(client, hd))["counts"]["generated"] == 1


@pytest.mark.parametrize("keys", [[], ["generated|doc:x|0|2026-07-01T00:00:00+00:00"], ["|||"], ["x" * 5000],
                                  ["generated"] * 1000])
async def test_keys_of_no_row_shown_acknowledge_nothing(client, session, monkeypatch, keys):
    """A key that names no row the summary holds (made up, stale, empty, huge, repeated) marks
    nothing read and is not kept."""
    import uuid

    from celerp.models.company import Company
    from celerp_subscriptions.summary import ACK_SETTING

    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    await _auto(client, hd, start="2026-07-01")
    await tick(session, cid, "2026-07-01")
    ack = await client.post("/subscriptions/summary/ack", headers=hd, json={"keys": keys})
    assert ack.status_code == 200 and ack.json()["read"] == 0, ack.text
    assert (await summary(client, hd))["counts"]["generated"] == 1
    session.expire_all()
    stored = ((await session.get(Company, uuid.UUID(cid))).settings or {}).get(ACK_SETTING) or {}
    assert all(v == [] for v in stored.values()), stored


async def test_an_older_page_marked_read_keeps_what_a_newer_one_marked(client, session, monkeypatch):
    """An older page marked read after a newer one (a second tab) keeps the rows the newer one
    marked read, and adds only the rows it showed itself."""
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    await _auto(client, hd, start="2026-07-01")
    await tick(session, cid, "2026-07-01")
    older = await summary(client, hd)
    freeze_day(monkeypatch, "2026-08-01")
    await tick(session, cid, "2026-08-01")
    newer = await summary(client, hd)
    assert newer["counts"]["generated"] == 2
    assert (await mark_read(client, hd, newer)).json()["read"] == 2
    assert (await mark_read(client, hd, older)).json()["read"] == 2
    assert (await summary(client, hd))["counts"]["generated"] == 0


_READ = "2026-07-01T09:00:00+00:00"


def _summary_row(kind: str, at: str, eid: str = "doc:agreement", lasts: bool = False) -> dict:
    """A summary row as summary._rows gives it: keyed by what it is, or shown while it lasts."""
    row = {"entity_id": eid, "doc_id": None, "period_index": 0, "kind": kind, "at": None if lasts else at}
    return {**row, "key": None if lasts else summary_key(row)}


# Each change to the row between the page being read and Mark as read pressed on it: whether the
# page showed the row, the row's time after (None: gone), and whether it is listed after.
_CHANGES = {
    "unchanged": (True, _READ, False),
    "committed_after_read": (False, "2026-07-01T09:00:05+00:00", True),
    "stamped_before_read_committed_after": (False, "2026-07-01T08:59:59+00:00", True),
    "reading_changed": (True, "2026-07-01T09:05:00+00:00", True),
    "gone": (True, None, False),
}


@pytest.mark.parametrize("change", sorted(_CHANGES))
@pytest.mark.parametrize("kind,lasts", [*((k, False) for k in SUMMARY_KINDS),
                                        *((k, True) for k in ("send_pending", "billing_lapsed", "sending_lapsed"))])
async def test_mark_as_read_marks_exactly_the_rows_the_page_showed(client, session, monkeypatch, kind, lasts, change):
    """Generated over every summary kind and every change to a row between the page being read
    and Mark as read pressed on it: only a row the page showed, as it showed it, is marked read;
    one committed after the read (whatever its time), or whose reading changed, stays listed; a
    row shown while it lasts (an email waiting to go out on its own, a lapsed authorization) is
    never hidden; and a key whose row is gone is not kept."""
    import uuid

    from celerp.models.company import Company
    from celerp_subscriptions import summary as summary_mod

    hd = await register(client)
    cid, user = uuid.UUID(await company_id(client, hd)), uuid.uuid4()
    shown, at_after, listed_after = _CHANGES[change]
    other = _summary_row("generated", _READ, "doc:other")  # a row the page shows beside it
    held = [other, *([_summary_row(kind, _READ, lasts=lasts)] if shown else [])]

    async def rows(*_args):
        return [dict(r) for r in held]
    monkeypatch.setattr(summary_mod, "_rows", rows)
    page = await summary_mod.report(session, cid, user)
    held[:] = [other, *([_summary_row(kind, at_after, lasts=lasts)] if at_after else [])]
    await summary_mod.acknowledge(session, cid, user, [r["key"] for r in page["rows"] if r["key"]])
    after = (await summary_mod.report(session, cid, user))["rows"]
    assert [r["kind"] for r in after if r["entity_id"] == "doc:agreement"] == (
        [kind] if at_after and (listed_after or lasts) else []), (kind, change, after)
    assert other["key"] not in [r["key"] for r in after]
    stored = ((await session.get(Company, cid)).settings or {})[summary_mod.ACK_SETTING][str(user)]
    assert set(stored) <= {r["key"] for r in held if r["key"]}, stored  # nothing of a row gone is kept
    assert len((await summary_mod.report(session, cid, user, everything=True))["rows"]) == len(held)


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
    _stopped_long_ago(monkeypatch)
    [row] = [r for r in (await summary(client, hd))["rows"] if r["kind"] == "send_unconfirmed"]
    # A self-hosted transport cannot replay an outcome it did not confirm: the row offers
    # Send again, a new message behind a confirm, instead of a plain retry.
    assert row["entity_id"] == stopped and row["again"] is True
    assert row["retry_url"] == f"/subscriptions/{stopped}/periods/0/send"
    monkeypatch.setattr(email, "send_email", mail)
    assert mail.sent == []

    async def broken(*_, **__):
        raise RuntimeError("relay unreachable")

    monkeypatch.setattr(email, "send_email", broken)
    raising = await _auto(client, hd, start="2026-07-01", send=True,
                          contact_id=await contact(client, hd, "other@example.test"))
    await tick(session, cid, "2026-07-01")
    unconfirmed = [r for r in (await summary(client, hd))["rows"] if r["kind"] == "send_unconfirmed"]
    assert raising in [r["entity_id"] for r in unconfirmed]
    send = (await template(client, hd, raising))["periods"]["0"]["send"]
    assert send["state"] == "unconfirmed" and "error_key" not in send


async def test_a_retried_send_is_recorded_as_the_person_who_retried(client, session, monkeypatch, mail):
    from sqlalchemy import select
    from celerp.models.ledger import LedgerEntry

    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    manager, manager_id = await member(client, session, hd, "manager")
    eid = await _auto(client, hd, start="2026-07-01", send=True,
                      contact_id=await contact(client, hd, "member@example.test"))
    mail.result = _REFUSED
    await tick(session, cid, "2026-07-01")
    row = next(r for r in (await summary(client, hd))["rows"] if r["kind"] == "send_failed")
    mail.result = EmailResult(True, "accepted")
    assert (await client.post(row["retry_url"], headers=manager, json={"attempt": row["attempt"]})).status_code == 200
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
    assert r.json()["detail"]["message_key"] == "subscriptions.err_send_purchase"
    assert not (await template(client, hd, eid)).get("auto_send")

async def test_delayed_mail_completion_cannot_change_reopened_period(client, session, monkeypatch, mail):
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await _auto(client, hd, start="2026-07-01", send=True,
                      contact_id=await contact(client, hd, "member@example.test"))
    original = mail.__call__

    async def superseded(to, subject, body_html, body_text="", **kw):
        [first] = await billed_docs(session, eid)
        assert (await client.post(f"/docs/{first['entity_id']}/void", headers=hd,
                                  json={"reason": "Replace"})).status_code == 200
        assert (await client.post(f"/subscriptions/{eid}/periods/0/reopen", headers=hd)).status_code == 200
        return await original(to, subject, body_html, body_text, **kw)

    from celerp.services import email
    monkeypatch.setattr(email, "send_email", superseded)
    await tick(session, cid, "2026-07-01")
    state = await template(client, hd, eid)
    entry = state["periods"]["0"]
    assert entry["state"] == "released" and entry["release_count"] == 1
    assert not entry.get("send")
    docs = await billed_docs(session, eid)
    assert len(docs) == 1 and docs[0]["status"] == "void"

async def test_relay_retry_keeps_original_transport_identity(client, session, monkeypatch, mail):
    import asyncio
    from celerp.services import email

    monkeypatch.setattr(email, "relay_send_is_idempotent", lambda: True)
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await _auto(client, hd, start="2026-07-01", send=True,
                      contact_id=await contact(client, hd, "member@example.test"))
    keys = []

    async def interrupted(*args, **kw):
        keys.append(kw["operation_id"])
        raise asyncio.CancelledError

    monkeypatch.setattr(email, "send_email", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await tick(session, cid, "2026-07-01")
    _stopped_long_ago(monkeypatch)
    row = next(r for r in (await summary(client, hd))["rows"] if r["kind"] == "send_unconfirmed")
    assert row["retry_url"]

    async def rejected(*args, **kw):
        keys.append(kw["operation_id"])
        return email.EmailResult(False, "This operation id belongs to a different email",
                                 reason="email.err_relay_refused")

    monkeypatch.setattr(email, "send_email", rejected)
    assert (await client.post(row["retry_url"], headers=hd, json={"attempt": row["attempt"]})).status_code == 200
    assert (await template(client, hd, eid))["periods"]["0"]["send"]["state"] == "unconfirmed"
    assert (await template(client, hd, eid))["periods"]["0"]["send"]["n"] == 1

    async def verified(*args, **kw):
        keys.append(kw["operation_id"])
        return await mail(*args, **kw)

    monkeypatch.setattr(email, "send_email", verified)
    assert (await client.post(row["retry_url"], headers=hd, json={"attempt": row["attempt"]})).status_code == 200
    assert len(keys) == 3 and len(set(keys)) == 1


async def test_inactive_company_is_not_billed(client, session, monkeypatch):
    from datetime import date
    import uuid
    from celerp.models.company import Company
    from celerp_subscriptions import billing

    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await _auto(client, hd, start="2026-07-01")
    company = await session.get(Company, uuid.UUID(cid))
    company.is_active = False
    await session.commit()

    await tick(session, cid, "2026-07-01")
    result = await billing.run_due(session=session, company_id=uuid.UUID(cid), today=date(2026, 7, 1), budget=10)
    await session.commit()
    assert result.done == 0
    assert await billed_docs(session, eid) == []


async def test_uncertain_email_reuses_original_payload_without_recomposition(client, session, monkeypatch, mail):
    import asyncio
    from celerp.services import email
    from celerp_docs import routes as docs_routes

    monkeypatch.setattr(email, "relay_send_is_idempotent", lambda: True)
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await _auto(client, hd, start="2026-07-01", send=True,
                      contact_id=await contact(client, hd, "member@example.test"))

    async def uncertain(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(email, "send_email", uncertain)
    with pytest.raises(asyncio.CancelledError):
        await tick(session, cid, "2026-07-01")
    _stopped_long_ago(monkeypatch)
    row = next(r for r in (await summary(client, hd))["rows"] if r["kind"] == "send_unconfirmed")
    assert row["retry_url"]

    async def should_not_recompose(*args, **kwargs):
        raise AssertionError("Uncertain retries must reuse the original email")

    monkeypatch.setattr(docs_routes, "doc_email", should_not_recompose)
    monkeypatch.setattr(email, "send_email", mail)
    retried = await client.post(row["retry_url"], headers=hd, json={"attempt": row["attempt"]})
    assert retried.status_code == 200, retried.text
    assert mail.sent == ["member@example.test"]
    assert (await template(client, hd, eid))["periods"]["0"]["send"]["n"] == 1


async def test_email_preparation_failure_is_visible_and_retryable(client, session, monkeypatch, mail):
    from celerp_docs import routes as docs_routes

    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await _auto(client, hd, start="2026-07-01", send=True,
                      contact_id=await contact(client, hd, "member@example.test"))
    original = docs_routes.doc_email

    async def bad(*args, **kwargs):
        raise RuntimeError("synthetic preparation failure")

    monkeypatch.setattr(docs_routes, "doc_email", bad)
    await tick(session, cid, "2026-07-01")
    [doc] = await billed_docs(session, eid)
    assert doc["status"] == "final"
    report = await summary(client, hd)
    assert report["counts"]["send_failed"] == 1
    row = next(r for r in report["rows"] if r["kind"] == "send_failed")
    assert row["retry_url"] and t("subscriptions.err_send_preparation") in row["message"]
    assert mail.sent == []

    monkeypatch.setattr(docs_routes, "doc_email", original)
    retry = await client.post(row["retry_url"], headers=hd, json={"attempt": row["attempt"]})
    assert retry.status_code == 200, retry.text
    assert mail.sent == ["member@example.test"]
    assert [d["status"] for d in await billed_docs(session, eid)] == ["sent"]


async def test_missing_private_payload_refuses_uncertain_retry(client, session, monkeypatch):
    import asyncio
    import uuid
    from sqlalchemy import delete
    from celerp.models.subscription_email import SubscriptionEmailAttempt
    from celerp.services import email

    monkeypatch.setattr(email, "relay_send_is_idempotent", lambda: True)
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await _auto(client, hd, start="2026-07-01", send=True,
                      contact_id=await contact(client, hd, "member@example.test"))

    async def interrupted(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(email, "send_email", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await tick(session, cid, "2026-07-01")
    _stopped_long_ago(monkeypatch)
    state = await template(client, hd, eid)
    attempt_id = uuid.UUID(state["periods"]["0"]["send"]["operation_id"])
    await session.execute(delete(SubscriptionEmailAttempt).where(
        SubscriptionEmailAttempt.company_id == uuid.UUID(cid),
        SubscriptionEmailAttempt.operation_id == attempt_id))
    await session.commit()
    row = next(r for r in (await summary(client, hd))["rows"] if r["kind"] == "send_unconfirmed")
    assert row["again"] is True
    assert (await client.post(row["retry_url"], headers=hd, json={"attempt": row["attempt"]})).status_code == 409


async def test_uncertain_send_replay_window_enforced_at_api(client, session, monkeypatch, mail):
    import asyncio
    import uuid
    from datetime import datetime, timedelta, timezone
    from celerp.services import email
    from celerp.models.subscription_email import SubscriptionEmailAttempt
    from celerp_subscriptions import billing

    monkeypatch.setattr(email, "relay_send_is_idempotent", lambda: True)
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await _auto(client, hd, start="2026-07-01", send=True,
                      contact_id=await contact(client, hd, "member@example.test"))

    async def cut_off(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(email, "send_email", cut_off)
    with pytest.raises(asyncio.CancelledError):
        await tick(session, cid, "2026-07-01")
    _stopped_long_ago(monkeypatch)
    state = await template(client, hd, eid)
    operation_id = uuid.UUID(state["periods"]["0"]["send"]["operation_id"])
    attempt = await session.get(
        SubscriptionEmailAttempt, {"company_id": uuid.UUID(cid), "operation_id": operation_id})
    attempt.created_at = datetime.now(timezone.utc) - billing.RECOVERY_RETRY_AGE - timedelta(seconds=1)
    await session.commit()

    row = next(r for r in (await summary(client, hd))["rows"] if r["kind"] == "send_unconfirmed")
    assert row["again"] is True
    monkeypatch.setattr(email, "send_email", mail)
    refused = await client.post(row["retry_url"], headers=hd, json={"attempt": row["attempt"]})
    assert refused.status_code == 409
    assert mail.sent == []


async def test_auto_arrears_bills_on_first_bill_date_with_zero_catchup(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-01-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await _auto(client, hd, start="2026-01-01", billing_timing="arrears", catchup=0)
    freeze_day(monkeypatch, "2026-01-31")
    await tick(session, cid, "2026-01-31")
    assert await billed_docs(session, eid) == []
    freeze_day(monkeypatch, "2026-02-01")
    await tick(session, cid, "2026-02-01")
    assert [d["period_index"] for d in await billed_docs(session, eid)] == [0]

async def test_failed_generation_is_visible_once_and_clears_after_recovery(client, session, monkeypatch):
    from celerp_docs import routes as docs_routes

    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await _auto(client, hd, start="2026-07-01")
    original = docs_routes.finalize_document

    async def broken(*args, **kwargs):
        raise ValueError("synthetic finalize failure")

    monkeypatch.setattr(docs_routes, "finalize_document", broken)
    await tick(session, cid, "2026-07-01")
    first = (await template(client, hd, eid))["billing_failure"]
    assert first["period_index"] == 0 and first["at"]
    assert (await summary(client, hd))["counts"]["generation_failed"] == 1
    assert await billed_docs(session, eid) == []
    await tick(session, cid, "2026-07-01")
    assert (await template(client, hd, eid))["billing_failure"] == first
    page = await summary(client, hd)
    assert page["counts"]["generation_failed"] == 1  # once, not once per pass
    assert (await mark_read(client, hd, page)).status_code == 200
    assert (await summary(client, hd))["counts"]["generation_failed"] == 0

    monkeypatch.setattr(docs_routes, "finalize_document", original)
    await tick(session, cid, "2026-07-01")
    assert [d["period_index"] for d in await billed_docs(session, eid)] == [0]
    assert "billing_failure" not in await template(client, hd, eid)


async def test_revoked_document_permission_stops_automated_generation(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-07-01")
    owner = await register(client)
    cid = await company_id(client, owner)
    manager, _ = await member(client, session, owner, "manager")
    eid = await agreement(client, owner, start="2026-07-01")
    assert (await schedule(client, manager, eid, generation_mode="auto_invoice")).status_code == 200
    await revoke(client, owner, "finalize_documents", "manager")
    await tick(session, cid, "2026-07-01")
    assert await billed_docs(session, eid) == []
    assert "authorization lapsed" in str(await summary(client, owner))
    denied = await agreement(client, owner, start="2026-07-01")
    assert (await schedule(client, manager, denied, generation_mode="auto_invoice")).status_code == 403


async def test_a_period_not_billed_has_no_email_to_send(client, session, monkeypatch, mail):
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    eid = await _auto(client, hd, start="2026-08-01", send=True,
                      contact_id=await contact(client, hd, "member@example.test"))
    r = await client.post(f"/subscriptions/{eid}/periods/0/send", headers=hd, json={"attempt": 0})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "subscriptions.err_send_not_billed"
    assert mail.sent == []


async def _cut_off_send(client, session, monkeypatch) -> tuple[dict, str, str]:
    """An auto-sent invoice whose send the relay took and was cut off before its answer,
    long enough ago that it reads as cut off: (headers, company id, agreement id)."""
    import asyncio
    from celerp.services import email

    monkeypatch.setattr(email, "relay_send_is_idempotent", lambda: True)
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await _auto(client, hd, start="2026-07-01", send=True,
                      contact_id=await contact(client, hd, "member@example.test"))

    async def cut_off(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(email, "send_email", cut_off)
    with pytest.raises(asyncio.CancelledError):
        await tick(session, cid, "2026-07-01")
    _stopped_long_ago(monkeypatch)
    return hd, cid, eid


@pytest.mark.parametrize("change", ["void", "no-operation", "unreadable-operation", "other-payload"])
async def test_a_cut_off_send_is_retried_only_as_the_same_message_to_a_live_document(
        client, session, monkeypatch, mail, change):
    """Retrying a cut-off send repeats the stored message under its stored operation. When the
    document was voided since, or the stored operation or message no longer match the send,
    a plain retry is refused by key and nothing is sent."""
    import uuid
    from celerp.services import email
    from celerp_subscriptions import billing

    hd, cid, eid = await _cut_off_send(client, session, monkeypatch)
    [doc] = await billed_docs(session, eid)
    if change == "void":
        r = await client.post(f"/docs/{doc['entity_id']}/void", headers=hd, json={"reason": "Wrong customer"})
        assert r.status_code == 200, r.text
    else:
        periods = (await template(client, hd, eid))["periods"]
        periods["0"]["send"] |= {"no-operation": {"operation_id": ""},
                                 "unreadable-operation": {"operation_id": "not-an-operation"},
                                 "other-payload": {"payload_digest": "0" * 64}}[change]
        await billing.write_agreement(session, uuid.UUID(cid), eid, {"periods": periods}, None)
        await session.commit()
    monkeypatch.setattr(email, "send_email", mail)
    n = (await template(client, hd, eid))["periods"]["0"]["send"]["n"]
    r = await client.post(f"/subscriptions/{eid}/periods/0/send", headers=hd, json={"attempt": n})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "subscriptions.err_send_unverified"
    assert mail.sent == []


async def test_an_email_too_large_to_send_is_a_failed_send(client, session, monkeypatch, mail):
    from celerp_subscriptions import billing

    monkeypatch.setattr(billing, "MAX_EMAIL_PAYLOAD_BYTES", 10)
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await _auto(client, hd, start="2026-07-01", send=True,
                      contact_id=await contact(client, hd, "member@example.test"))
    await tick(session, cid, "2026-07-01")
    send = (await template(client, hd, eid))["periods"]["0"]["send"]
    assert send["state"] == "failed" and send["error_key"] == "subscriptions.err_send_preparation"
    assert mail.sent == []


async def test_an_emailed_document_that_cannot_be_marked_sent_still_reads_as_emailed(
        client, session, monkeypatch, mail):
    """The email went; the document refused its sent mark. The period says the email was
    sent (it was), and the document keeps the status it had."""
    from fastapi import HTTPException
    from celerp_docs import routes as docs_routes

    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await _auto(client, hd, start="2026-07-01", send=True,
                      contact_id=await contact(client, hd, "member@example.test"))
    mail.result = _REFUSED
    await tick(session, cid, "2026-07-01")
    send = (await template(client, hd, eid))["periods"]["0"]["send"]
    assert send["state"] == "failed"

    async def refuses(*args, **kwargs):
        raise HTTPException(status_code=409, detail="refused")

    monkeypatch.setattr(docs_routes, "mark_doc_sent", refuses)
    mail.result = EmailResult(True, "accepted")
    r = await client.post(f"/subscriptions/{eid}/periods/0/send", headers=hd, json={"attempt": send["n"]})
    assert r.status_code == 200, r.text
    assert r.json()["send"] == "sent" and mail.sent == ["member@example.test"] * 2
    assert [d["status"] for d in await billed_docs(session, eid)] == ["final"]


async def test_a_failure_recording_a_failed_run_does_not_stop_the_pass(client, session, monkeypatch):
    """Billing one agreement failed and so did recording that failure: the pass still ends
    and bills the company's other agreements."""
    import uuid
    from datetime import date
    from celerp_subscriptions import billing

    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = uuid.UUID(await company_id(client, hd))
    broken = await _auto(client, hd, start="2026-07-01")
    healthy = await _auto(client, hd, start="2026-07-01")
    real_bill, real_write = billing._bill, billing.write_agreement

    async def bill(session, company, company_id, entity_id, *args, **kwargs):
        if entity_id == broken:
            raise RuntimeError("synthetic billing failure")
        return await real_bill(session, company, company_id, entity_id, *args, **kwargs)

    async def write(session, company_id, entity_id, changes, *args, **kwargs):
        if "billing_failure" in changes:
            raise RuntimeError("synthetic failure recording it")
        return await real_write(session, company_id, entity_id, changes, *args, **kwargs)

    monkeypatch.setattr(billing, "_bill", bill)
    monkeypatch.setattr(billing, "write_agreement", write)
    report = await billing.run_due(session=session, company_id=cid, today=date(2026, 7, 1), budget=10)
    await session.commit()
    assert report.done == 1
    assert await billed_docs(session, broken) == [] and len(await billed_docs(session, healthy)) == 1


async def test_the_summary_covers_its_window_so_mark_as_read_stores_only_rows_in_it(client, session, monkeypatch):
    """One agreement billed unattended each month and the summary marked read each month: the
    summary (all=1 too) holds only rows of the last SUMMARY_WINDOW, and the stored keys are exactly
    the rows in it, so a row past the window leaves the page and its key leaves storage."""
    import uuid
    from datetime import datetime, timezone

    from celerp.models.company import Company
    from celerp_subscriptions import billing
    from celerp_subscriptions.summary import ACK_SETTING, SUMMARY_WINDOW

    def on(day):
        instant = datetime.fromisoformat(day).replace(hour=12, tzinfo=timezone.utc)
        freeze_day(monkeypatch, day)
        monkeypatch.setattr(billing, "now", lambda: instant)
        return instant

    on("2026-01-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    await _auto(client, hd, start="2026-01-01")
    for month in range(1, 13):
        instant = on(f"2026-{month:02d}-01")
        await tick(session, cid, f"2026-{month:02d}-01")
        assert (await mark_read(client, hd, await summary(client, hd))).status_code == 200
        everything = (await summary(client, hd, all=1))["rows"]
        assert everything and all(datetime.fromisoformat(r["at"]) >= instant - SUMMARY_WINDOW for r in everything)
        session.expire_all()
        stored = ((await session.get(Company, uuid.UUID(cid))).settings or {})[ACK_SETTING]
        assert [sorted(v) for v in stored.values()] == [sorted(r["key"] for r in everything)], month
    # Nothing billed for longer than the window: the last bill's row leaves the page and, on the
    # next Mark as read, its key leaves storage.
    on("2027-04-01")
    assert (await summary(client, hd, all=1))["rows"] == []
    assert (await mark_read(client, hd, await summary(client, hd))).status_code == 200
    session.expire_all()
    assert ((await session.get(Company, uuid.UUID(cid))).settings or {})[ACK_SETTING] == {
        k: [] for k in stored}


async def test_a_failed_bill_keeps_its_reason_is_counted_to_check_and_offers_generate_due(client, session, monkeypatch):
    """An agreement billing on its own with no line items fails its bill: the summary row says
    why (the refusal Generate due would give), the summary line and the list banner count it to
    check in the warning style, and the row offers Generate due, the door that fixes it."""
    import uuid

    from bs4 import BeautifulSoup

    from celerp_subscriptions.summary import INFORMATIONAL, NEEDS_YOU, KINDS
    from test_helpers import ui_signed_in

    freeze_day(monkeypatch, "2026-07-01")
    r = await client.post("/auth/register", json={
        "company_name": "Failed bill Co", "email": f"failed-bill-{uuid.uuid4().hex[:8]}@test.test", "name": "Owner", "password": "validpass1"})
    token = r.json()["access_token"]
    hd = {"Authorization": f"Bearer {token}"}
    cid = await company_id(client, hd)
    eid = await _auto(client, hd, start="2026-07-01", line_items=[])
    await tick(session, cid, "2026-07-01")
    refused = (await client.post(f"/subscriptions/{eid}/generate-due", headers=hd,
                                 json={"request_key": "why"})).json()["detail"]
    page = await summary(client, hd)
    ref = (await template(client, hd, eid))["ref_id"]
    assert [(r["kind"], r["message"]) for r in page["rows"]] == [
        ("generation_failed", t("subscriptions.summary_generation_failed_because", ref=ref,
                                error=refused if isinstance(refused, str) else refused["message"]))]
    assert set(KINDS) == set(NEEDS_YOU) | set(INFORMATIONAL) and not set(NEEDS_YOU) & set(INFORMATIONAL)
    async with ui_signed_in(token) as ui:
        listing = BeautifulSoup((await ui.get("/subscriptions")).text, "html.parser")
        shown = BeautifulSoup((await ui.get("/subscriptions/summary")).text, "html.parser")
    counted = t("subscriptions.summary_counts", generated=0, sent=0, failed=1)
    banner = next(d for d in listing.select(".flash") if "/subscriptions/summary" in str(d))
    assert "flash--warning" in banner["class"] and t("subscriptions.summary_banner", generated=0, sent=0, failed=1) in banner.text
    assert counted in shown.text
    assert shown.find("button", attrs={"hx-post": f"/subscriptions/{eid}/generate-due"}) is not None


async def test_a_failed_bill_leaves_the_next_agreement_in_the_same_pass_billed(client, session, monkeypatch):
    """A bill that fails takes its document back with it, and the next agreement in the same pass
    bills under the reference it frees: the failed document is not left behind to block it."""
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    failing = await _auto(client, hd, start="2026-07-01", line_items=[])
    billing = await _auto(client, hd, start="2026-07-01")
    await tick(session, cid, "2026-07-01")
    assert await billed_docs(session, failing) == []
    assert [d["period_index"] for d in await billed_docs(session, billing)] == [0]


async def test_every_savepoint_the_module_opens_holds_its_rows():
    """A savepoint whose failure is caught holds the rows written inside it until it ends
    (billing.savepoint); the module opens no other kind, so none leaves a row behind its rollback."""
    from status_census import savepoints

    opened = savepoints()
    assert [s for s in opened if s[2] == "begin_nested"] == [("billing.py", "savepoint", "begin_nested")]
    assert {(f, fn) for f, fn, how in opened if how == "savepoint"} >= {
        ("billing.py", "run_due"), ("routes.py", "catch_up_run")}

