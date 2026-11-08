# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""One document per period under real concurrency: two requests, two dispatchers, a
crash between the claim and its commit. Runs on real PostgreSQL across connections,
with the shared race helpers in race_support."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agreement_kit import agreement, freeze_day, generate_due, mark_read, schedule
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from race_support import _Held, _app_client, _race, _seed, _until_waiting_or_done

pytestmark = [pytest.mark.process, pytest.mark.asyncio]


async def _company(factory) -> dict:
    """A seeded company with its books, so finalizing a billed document posts."""
    from test_helpers import provision_company_books

    seeded = await _seed(factory)
    async with factory() as s:
        await provision_company_books(s, seeded["company_id"])
        await s.commit()
    return seeded


def _factory(engine):
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


async def _billed(factory, company_id) -> list[tuple[str, int]]:
    """(agreement, period) of every billed document, one entry per document."""
    async with factory() as s:
        rows = (await s.execute(select(Projection.state).where(
            Projection.company_id == company_id, Projection.entity_type == "doc"))).scalars().all()
    return sorted((st["source_agreement"], st["period_index"]) for st in rows if st.get("source_agreement"))


async def _claims(factory, company_id) -> list[str]:
    async with factory() as s:
        return sorted((await s.execute(select(LedgerEntry.idempotency_key).where(
            LedgerEntry.company_id == company_id, LedgerEntry.idempotency_key.like("sub:%")))).scalars().all())


async def _auto(client, owner, *, start, catchup) -> str:
    eid = await agreement(client, owner, start=start)
    r = await schedule(client, owner, eid, generation_mode="auto_invoice", catchup_periods=catchup)
    assert r.status_code == 200, r.text
    return eid


def _holding_first(factory, held: _Held):
    """A session factory whose first session holds its commit until released."""
    opened = []

    @asynccontextmanager
    async def _open():
        async with factory() as s:
            if not opened:
                held.hold(s)
            opened.append(s)
            yield s

    return _open


async def test_same_period_two_workers_one_invoice(committed_engine, monkeypatch):
    freeze_day(monkeypatch, "2026-03-15")
    factory = _factory(committed_engine)
    seeded = await _company(factory)
    owner = seeded["h"]["owner"]
    async with _app_client(factory) as (client, pending):
        eid = await agreement(client, owner, start="2026-03-01")

        def _generate(key):
            async def _go(held):
                if held is not None:
                    pending.append(held)
                return await generate_due(client, owner, eid, key=key)
            return _go

        first, second = await _race(committed_engine, _generate("worker-a"), _generate("worker-b"))
    assert first.status_code == second.status_code == 200, (first.text, second.text)
    assert first.json()["doc_id"] == second.json()["doc_id"]
    assert await _billed(factory, seeded["company_id"]) == [(eid, 0)]
    assert await _claims(factory, seeded["company_id"]) == [f"sub:{eid}:p0:invoice:r0"]


class _Crash:
    """The request's commit never happens: the process dies first."""

    def hold(self, session) -> None:
        async def _die() -> None:
            raise ConnectionError("process stopped before commit")

        session.commit = _die


async def test_restart_between_claim_and_commit_one_invoice(committed_engine, monkeypatch):
    freeze_day(monkeypatch, "2026-03-15")
    factory = _factory(committed_engine)
    seeded = await _company(factory)
    owner = seeded["h"]["owner"]
    async with _app_client(factory) as (client, pending):
        eid = await agreement(client, owner, start="2026-03-01")
        pending.append(_Crash())
        with pytest.raises(ConnectionError):
            await generate_due(client, owner, eid, key="before-crash")
        assert await _billed(factory, seeded["company_id"]) == []
        retried = await generate_due(client, owner, eid, key="after-restart")
        assert retried.status_code == 200 and retried.json()["period_index"] == 0
        again = await generate_due(client, owner, eid, key="after-restart-2")
        assert again.json()["doc_id"] == retried.json()["doc_id"]
    assert await _billed(factory, seeded["company_id"]) == [(eid, 0)]
    assert await _claims(factory, seeded["company_id"]) == [f"sub:{eid}:p0:invoice:r0"]


async def test_two_processes_dispatch_same_company_one_invoice(committed_engine, monkeypatch):
    from celerp_docs import periodic

    freeze_day(monkeypatch, "2026-03-15")
    factory = _factory(committed_engine)
    seeded = await _company(factory)
    owner, cid = seeded["h"]["owner"], seeded["company_id"]
    today = date(2026, 3, 15)
    async with _app_client(factory) as (client, _pending):
        eid = await _auto(client, owner, start="2026-01-01", catchup=2)  # periods 0, 1, 2 due
        await asyncio.gather(
            periodic.run_company(factory, cid, today=today),
            periodic.run_company(factory, cid, today=today),
            client.post("/subscriptions/generate-due", headers=owner, json={"items": [{"entity_id": eid, "period_index": 0, "request_key": "concurrent-batch"}]}),
        )
        await periodic.run_company(factory, cid, today=today)
    assert await _billed(factory, cid) == [(eid, 0), (eid, 1), (eid, 2)]
    assert await _claims(factory, cid) == [f"sub:{eid}:p{k}:invoice:r0" for k in range(3)]


async def test_two_dispatchers_respect_budget_and_claims(committed_engine, monkeypatch):
    from celerp.modules import slots
    from celerp_docs import periodic

    # Billing alone, so the held commit is the pass that holds the company's billing budget.
    monkeypatch.setitem(slots._slots, "periodic_operation", [
        c for c in slots.get("periodic_operation") if c["handler"].startswith("celerp_subscriptions.")])
    freeze_day(monkeypatch, "2026-07-15")
    factory = _factory(committed_engine)
    seeded = await _company(factory)
    owner, cid = seeded["h"]["owner"], seeded["company_id"]
    today = date(2026, 7, 15)
    async with _app_client(factory) as (client, _pending):
        eids = [await _auto(client, owner, start="2026-04-01", catchup=3) for _ in range(3)]  # 12 due
    held = _Held()
    first = asyncio.create_task(periodic.run_company(_holding_first(factory, held), cid, today=today))
    await asyncio.wait_for(held.reached.wait(), 30)
    second = asyncio.create_task(periodic.run_company(factory, cid, today=today))
    await _until_waiting_or_done(committed_engine, second)
    held.release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 30)
    billed = await _billed(factory, cid)
    assert len(billed) == 10 and len(set(billed)) == 10  # one budget per pass, one document per period
    assert {e for e, _ in billed} <= set(eids)


async def test_concurrent_email_delivery_only_claims_one_send(committed_engine, monkeypatch):
    """Two workers recovering the same pending email cannot both call the transport."""
    from agreement_kit import contact
    from celerp.services import email
    from celerp_subscriptions import billing

    freeze_day(monkeypatch, "2026-07-01")
    factory = _factory(committed_engine)
    seeded = await _company(factory)
    owner, cid = seeded["h"]["owner"], seeded["company_id"]
    async with _app_client(factory) as (client, _pending):
        recipient = await contact(client, owner, "concurrency@example.test")
        eid = await agreement(client, owner, start="2026-07-01", contact_id=recipient)
        changed = await schedule(client, owner, eid, generation_mode="auto_invoice", auto_send=True)
        assert changed.status_code == 200, changed.text

    async with factory() as session:
        report = await billing.run_due(session=session, company_id=cid, today=date(2026, 7, 1), budget=1)
        assert report.done == 1
        await session.commit()

    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def send_once(*, operation_id=None, **kwargs):
        calls.append((operation_id, kwargs["to"]))
        started.set()
        await release.wait()
        return email.EmailResult(True, "accepted")

    monkeypatch.setattr(email, "send_email", send_once)
    first = asyncio.create_task(billing.deliver(factory, cid, eid, 0))
    try:
        await asyncio.wait_for(started.wait(), 30)
        second = await asyncio.wait_for(billing.deliver(factory, cid, eid, 0), 30)
        assert second["state"] == "sending"
    finally:
        release.set()
    first_result = await asyncio.wait_for(first, 30)
    assert first_result["state"] == "sent"
    assert len(calls) == 1 and calls[0][1] == "concurrency@example.test"
    async with factory() as session:
        state = (await session.get(Projection, {"company_id": cid, "entity_id": eid})).state
    assert state["periods"]["0"]["send"]["state"] == "sent"


async def test_one_confirmed_send_again_sends_one_message_however_often_it_arrives(committed_engine, monkeypatch):
    """A period's email was cut off. The user confirms Send again and the request arrives twice
    (a double click, a second tab, a colleague): each names the attempt it saw, so the second,
    arriving while the first is in flight, is answered with the send as stored (P2e-4)."""
    from agreement_kit import contact
    from celerp.services import email
    from celerp_subscriptions import billing

    freeze_day(monkeypatch, "2026-07-01")
    factory = _factory(committed_engine)
    seeded = await _company(factory)
    owner, cid = seeded["h"]["owner"], seeded["company_id"]
    async with _app_client(factory) as (client, _pending):
        recipient = await contact(client, owner, "again@example.test")
        eid = await agreement(client, owner, start="2026-07-01", contact_id=recipient)
        assert (await schedule(client, owner, eid, generation_mode="auto_invoice", auto_send=True)).status_code == 200
        async with factory() as s:
            assert (await billing.run_due(session=s, company_id=cid, today=date(2026, 7, 1), budget=1)).done == 1
            await s.commit()

        async def cut_off(**_kwargs):
            return email.EmailResult(False, "delivery outcome unknown", unconfirmed=True)
        monkeypatch.setattr(email, "send_email", cut_off)
        assert (await billing.deliver(factory, cid, eid, 0))["state"] == "unconfirmed"

        started, release, calls = asyncio.Event(), asyncio.Event(), []

        async def slow_ok(*, operation_id=None, **_kwargs):
            calls.append(operation_id)
            started.set()
            await release.wait()
            return email.EmailResult(True, "accepted")
        monkeypatch.setattr(email, "send_email", slow_ok)
        url, body = f"/subscriptions/{eid}/periods/0/send", {"again": True, "attempt": 1}
        first = asyncio.create_task(client.post(url, headers=owner, json=body))
        try:
            await asyncio.wait_for(started.wait(), 30)
            second = asyncio.create_task(client.post(url, headers=owner, json=body))
            for _ in range(200):  # the second answers, or reaches the mail server too
                if second.done() or len(calls) > 1:
                    break
                await asyncio.sleep(0.05)
        finally:
            release.set()
        first, second = await asyncio.wait_for(first, 30), await asyncio.wait_for(second, 30)
    assert first.status_code == second.status_code == 200, (first.text, second.text)
    assert len(calls) == 1
    assert first.json()["send"] == "sent"


async def test_a_send_in_flight_reads_sending_and_is_not_sent_again(committed_engine, monkeypatch):
    """The unattended pass is emailing a period and the mail server is slow to answer. The
    owner opens the agreement meanwhile: the period reads as sending, with no Send again, and a
    send pressed on it is refused while the first is with the mail server, so the customer
    gets one email (P2f-2). A send record left in flight longer than a send can take (the
    process stopped mid-send) reads as cut off and can be sent again."""
    from agreement_kit import contact
    from celerp.services import email
    from celerp_subscriptions import billing

    freeze_day(monkeypatch, "2026-07-01")
    factory = _factory(committed_engine)
    seeded = await _company(factory)
    owner, cid = seeded["h"]["owner"], seeded["company_id"]
    async with _app_client(factory) as (client, _pending):
        recipient = await contact(client, owner, "inflight@example.test")
        eid = await agreement(client, owner, start="2026-07-01", contact_id=recipient)
        assert (await schedule(client, owner, eid, generation_mode="auto_invoice", auto_send=True)).status_code == 200
        async with factory() as s:
            assert (await billing.run_due(session=s, company_id=cid, today=date(2026, 7, 1), budget=1)).done == 1
            await s.commit()

        started, release, calls = asyncio.Event(), asyncio.Event(), []

        async def slow_ok(*, operation_id=None, **_kwargs):
            calls.append(operation_id)
            started.set()
            await release.wait()
            return email.EmailResult(True, "accepted")
        monkeypatch.setattr(email, "send_email", slow_ok)
        unattended = asyncio.create_task(billing.deliver(factory, cid, eid, 0))
        try:
            await asyncio.wait_for(started.wait(), 30)
            listed = (await client.get(f"/subscriptions/{eid}/periods", headers=owner)).json()
            row = next(r for r in listed["rows"] if r["period_index"] == 0)
            assert (row["send"], row["again"]) == ("sending", False)
            shown = next(r for r in (await client.get("/subscriptions/summary", headers=owner)).json()["rows"]
                         if r["kind"].startswith("send"))
            assert (shown["kind"], shown["again"]) == ("send_sending", False)
            pressed = await client.post(f"/subscriptions/{eid}/periods/0/send", headers=owner,
                                        json={"attempt": row["attempt"], "again": True})
            assert pressed.status_code == 409, pressed.text
            assert pressed.json()["detail"]["message_key"] == "subscriptions.err_send_in_progress"
            # The same record, once older than a send can take, reads as cut off.
            monkeypatch.setattr(billing, "SEND_TIMEOUT", billing.SEND_TIMEOUT * 0)
            stale = next(r for r in (await client.get(f"/subscriptions/{eid}/periods", headers=owner)).json()["rows"]
                         if r["period_index"] == 0)
            assert (stale["send"], stale["again"]) == ("unconfirmed", True)
        finally:
            release.set()
        assert (await asyncio.wait_for(unattended, 30))["state"] == "sent"
    assert len(calls) == 1


async def test_a_send_cut_off_after_the_summary_was_read_is_listed_again(committed_engine, monkeypatch):
    """The app stops while the unattended pass is emailing a period. The owner reads the summary
    meanwhile ("being emailed right now, check again in a few minutes") and marks it read. Once
    a send can no longer be with the mail server the period reads as cut off, and the summary
    lists it again: its reading changed after the owner read it (P2g-1)."""
    from datetime import datetime as real_datetime

    from agreement_kit import contact
    from celerp.services import email
    from celerp_subscriptions import billing

    freeze_day(monkeypatch, "2026-07-01")
    factory = _factory(committed_engine)
    seeded = await _company(factory)
    owner, cid = seeded["h"]["owner"], seeded["company_id"]
    async with _app_client(factory) as (client, _pending):
        recipient = await contact(client, owner, "cutoff@example.test")
        eid = await agreement(client, owner, start="2026-07-01", contact_id=recipient)
        assert (await schedule(client, owner, eid, generation_mode="auto_invoice", auto_send=True)).status_code == 200
    async with factory() as s:
        assert (await billing.run_due(session=s, company_id=cid, today=date(2026, 7, 1), budget=1)).done == 1
        await s.commit()

    started = asyncio.Event()

    async def hangs(**_kwargs):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(email, "send_email", hangs)
    # The pass hands the email over, then the process stops before the answer comes back.
    task = asyncio.create_task(billing.deliver(factory, cid, eid, 0))
    await asyncio.wait_for(started.wait(), 30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with _app_client(factory) as (client, _pending):
        fresh = (await client.get("/subscriptions/summary", headers=owner)).json()
        assert [r["kind"] for r in fresh["rows"] if r["kind"].startswith("send")] == ["send_sending"]
        assert (await mark_read(client, owner, fresh)).status_code == 200

        class Later(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return real_datetime.now(tz) + billing.SEND_TIMEOUT + timedelta(minutes=1)
        monkeypatch.setattr(billing, "datetime", Later)
        row = next(r for r in (await client.get(f"/subscriptions/{eid}/periods", headers=owner)).json()["rows"]
                   if r["period_index"] == 0)
        assert (row["send"], row["again"]) == ("unconfirmed", True)
        later = (await client.get("/subscriptions/summary", headers=owner)).json()
        assert [r["kind"] for r in later["rows"] if r["kind"].startswith("send")] == ["send_unconfirmed"]
        assert later["counts"]["send_unconfirmed"] == 1
        # Read again, it is covered until its reading changes once more.
        assert (await mark_read(client, owner, later)).status_code == 200
        assert (await client.get("/subscriptions/summary", headers=owner)).json()["rows"] == []


async def _auto_send_billed(committed_engine, monkeypatch, address: str):
    """A company whose agreement bills itself and emails each bill; its first period billed."""
    from agreement_kit import contact
    from celerp_subscriptions import billing

    freeze_day(monkeypatch, "2026-07-01")
    factory = _factory(committed_engine)
    seeded = await _company(factory)
    owner, cid = seeded["h"]["owner"], seeded["company_id"]
    async with _app_client(factory) as (client, _pending):
        eid = await agreement(client, owner, start="2026-07-01", contact_id=await contact(client, owner, address))
        assert (await schedule(client, owner, eid, generation_mode="auto_invoice", auto_send=True)).status_code == 200
    async with factory() as s:
        assert (await billing.run_due(session=s, company_id=cid, today=date(2026, 7, 1), budget=1)).done == 1
        await s.commit()
    return factory, owner, cid, eid


def _clock(monkeypatch, by: timedelta) -> None:
    """Move the module's wall clock (billing.datetime) ``by`` ahead of the real one."""
    from datetime import datetime as real_datetime

    from celerp_subscriptions import billing

    class Later(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) + by
    monkeypatch.setattr(billing, "datetime", Later)


def _send_kinds(body: dict) -> list[str]:
    return [r["kind"] for r in body["rows"] if r["kind"].startswith("send")]


async def test_mark_as_read_after_waiting_leaves_a_send_cut_off_since_the_page_was_read(committed_engine, monkeypatch):
    """The page says a bill is being emailed and to check again in a few minutes. The owner waits
    that long and presses Mark as read on the page as loaded: the send, cut off by age since the
    page was read, is still listed (the acknowledgment covers what the page showed, P2h-1)."""
    from celerp.services import email
    from celerp_subscriptions import billing

    factory, owner, cid, eid = await _auto_send_billed(committed_engine, monkeypatch, "waited@example.test")
    started = asyncio.Event()

    async def hangs(**_kwargs):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(email, "send_email", hangs)
    task = asyncio.create_task(billing.deliver(factory, cid, eid, 0))
    await asyncio.wait_for(started.wait(), 30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with _app_client(factory) as (client, _pending):
        shown = (await client.get("/subscriptions/summary", headers=owner)).json()
        assert _send_kinds(shown) == ["send_sending"]
        _clock(monkeypatch, billing.SEND_TIMEOUT + timedelta(minutes=1))
        ack = await mark_read(client, owner, shown)
        assert ack.status_code == 200, ack.text
        assert _send_kinds((await client.get("/subscriptions/summary", headers=owner)).json()) == ["send_unconfirmed"]


async def test_mark_as_read_leaves_a_failure_that_arrived_after_the_page_was_read(committed_engine, monkeypatch):
    """The owner loads the summary; the unattended pass then fails to email a bill; Mark as read
    on the page as loaded leaves that failure listed, it was never shown (P2h-1)."""
    from celerp.services import email
    from celerp_subscriptions import billing

    factory, owner, cid, eid = await _auto_send_billed(committed_engine, monkeypatch, "refused@example.test")
    async with _app_client(factory) as (client, _pending):
        first = (await client.get("/subscriptions/summary", headers=owner)).json()
        assert "generated" in [r["kind"] for r in first["rows"]]
        assert (await mark_read(client, owner, first)).status_code == 200
        _clock(monkeypatch, timedelta(seconds=5))
        shown = (await client.get("/subscriptions/summary", headers=owner)).json()
        assert "generated" not in [r["kind"] for r in shown["rows"]]  # the acknowledgment is kept
        assert "send_failed" not in _send_kinds(shown)

        async def refused(**_kwargs):
            return email.EmailResult(False, "connection refused", reason="email.err_smtp_unreachable")
        monkeypatch.setattr(email, "send_email", refused)
        _clock(monkeypatch, timedelta(seconds=10))
        await billing.deliver(factory, cid, eid, 0)
        _clock(monkeypatch, timedelta(seconds=20))
        ack = await mark_read(client, owner, shown)
        assert ack.status_code == 200, ack.text
        assert "send_failed" in _send_kinds((await client.get("/subscriptions/summary", headers=owner)).json())


async def test_mark_as_read_leaves_a_row_stamped_before_the_page_and_committed_after_it(
        committed_engine, monkeypatch):
    """The unattended pass stamps a bill when it makes it and commits once at the end of the pass
    (a billing failure has no key: it is shown while it lasts, whatever is marked read). The
    owner opens the summary while the pass still runs, so the page cannot show the row, though
    the row's time is earlier than the page's. The pass commits and the owner
    presses Mark as read on the page as loaded: the row was never shown, so it stays listed."""
    from agreement_kit import contact
    from celerp_subscriptions import billing

    freeze_day(monkeypatch, "2026-07-01")
    factory = _factory(committed_engine)
    seeded = await _company(factory)
    owner, cid = seeded["h"]["owner"], seeded["company_id"]
    async with _app_client(factory) as (client, _pending):
        eid = await agreement(client, owner, start="2026-07-01",
                              contact_id=await contact(client, owner, "midpass@example.test"))
        assert (await schedule(client, owner, eid, generation_mode="auto_invoice",
                               catchup_periods=0)).status_code == 200
        async with factory() as pass_session:
            await billing.run_due(session=pass_session, company_id=cid, today=date(2026, 7, 1), budget=1)
            shown = (await asyncio.wait_for(client.get("/subscriptions/summary", headers=owner), 30)).json()
            await pass_session.commit()
        assert "generated" not in [r["kind"] for r in shown["rows"]]
        everything = (await client.get("/subscriptions/summary", headers=owner, params={"all": "true"})).json()
        made = next(r for r in everything["rows"] if r["kind"] == "generated")
        assert (await mark_read(client, owner, shown)).status_code == 200
        after = (await client.get("/subscriptions/summary", headers=owner)).json()
        assert [r["kind"] for r in after["rows"] if r["entity_id"] == eid] == ["generated"], (made, after)
        # Marked read from a page that shows it, it goes.
        assert (await mark_read(client, owner, after)).status_code == 200
        assert (await client.get("/subscriptions/summary", headers=owner)).json()["rows"] == []
