# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Card collection under real concurrency on PostgreSQL: a charge's payment arriving while an
amendment credits its invoice, two amendments of one agreement at once, and Cloud's answer to a
cancel crossing its delivery of the charge's success. Whichever commits first, what the payer is
charged in all is what the agreement owes (the 30.00 / 16.45 / 27.42 example: 40.97, never
57.42), and a known outcome is never lost."""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agreement_kit import agreement, company_id, freeze_day, register, schedule, template
from celerp.models.projections import Projection
from race_support import _Held, _app_client, _race
from test_collection import _Cloud

pytestmark = [pytest.mark.process, pytest.mark.asyncio]

PLAN = {"description": "Plan", "quantity": 1, "unit_price": 30.0, "line_total": 30.0}


class _Sessions:
    """The app's own delivery sessions on the race database; ``hold_next`` holds the commit of
    the next one opened."""

    def __init__(self, factory) -> None:
        self.factory, self.held = factory, None

    def __call__(self) -> AsyncSession:
        s = self.factory()
        if self.held is not None:
            self.held, held = None, self.held
            held.hold(s)
        return s


@asynccontextmanager
async def _collecting(committed_engine, monkeypatch, answer: str = "submitted"):
    """July's 30.00 invoice billed on an agreement that collects by card and its charge asked
    for, Cloud answering ``answer``: (client, headers, company id, agreement id, July's id,
    factory, sessions, cloud)."""
    from celerp.gateway import state
    from celerp.services import collection
    from test_collection import _bind, _payer

    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    sessions = _Sessions(factory)
    monkeypatch.setattr(state, "_feature_flags",
                        {**state.get_feature_flags(), "payments_enabled": True, "collection_protocol": 1})
    monkeypatch.setattr("celerp.services.payments._own_session", sessions)
    cloud = _Cloud()
    cloud.answers["attempt"] = lambda p: (200, {"state": answer, "attempt_number": p["attempt_number"]})
    monkeypatch.setattr(collection, "_ask", cloud)
    freeze_day(monkeypatch, "2026-07-01")
    async with _app_client(factory) as (client, pending):
        hd = await register(client)
        cid, payer = await company_id(client, hd), await _payer(client, hd)
        eid = await agreement(client, hd, start="2026-07-01", contact_id=payer, line_items=[dict(PLAN)])
        assert await _bind(cid, payer)
        assert (await schedule(client, hd, eid, generation_mode="auto_invoice_collect")).status_code == 200
        await _pass(factory, cid, "2026-07-01")
        [july] = await _docs(factory, eid)
        assert (july["total"], july["collection"]["state"]) == (30.0, answer)
        yield client, pending, hd, cid, eid, july["entity_id"], factory, sessions, cloud


async def _pass(factory, cid: str, day: str) -> None:
    """One dispatcher pass for the company on ``day``, each step committing on its own."""
    from celerp.events.engine import unattended
    from celerp_docs import periodic
    await unattended(periodic.run_company(factory, uuid.UUID(cid), today=date.fromisoformat(day)))


async def _docs(factory, eid: str) -> list[dict]:
    async with factory() as s:
        rows = (await s.execute(select(Projection).where(
            Projection.entity_type == "doc", Projection.state["source_agreement"].as_string() == eid))).scalars().all()
    return sorted((dict(r.state, entity_id=r.entity_id) for r in rows), key=lambda d: d.get("ref_id") or "")


def _amend_body(line: str, quantity: float, key: str) -> dict:
    return {"effective": "2026-07-15", "quantities": {line: quantity}, "request_key": key}


def _charged(cloud, doc_id: str) -> list[int]:
    return [p["amount_minor"] for p in cloud.sent("attempt") if p["entity_id"] == doc_id]


@pytest.mark.parametrize("first", ["payment", "amendment"])
async def test_a_charge_paid_while_an_amendment_credits_its_invoice_is_never_charged_twice(
        committed_engine, monkeypatch, first):
    """July's 30.00 succeeds at Cloud while the amendment's 16.45 credit is issued against it.
    Payment first: July is paid, the credit goes to the amendment's invoice, which is charged
    10.97. Amendment first: the credit settles part of July, so the 30.00 no longer fits it and
    is kept among the unmatched payments, and the amendment's 27.42 waits on July. Either way
    no more than 40.97 is asked for in all."""
    from celerp.models.payment_closure import UnmatchedPayment
    from celerp.services import payments
    async with _collecting(committed_engine, monkeypatch) as (client, pending, hd, cid, eid, july, factory,
                                                                sessions, cloud):
        [sent] = cloud.sent("attempt")
        cloud.answers["cancel"] = (409, {"detail": "attempt_submitted"})  # the charge went through
        line = (await template(client, hd, eid))["line_items"][0]["line_id"]
        freeze_day(monkeypatch, "2026-07-15")

        async def _paid(held):
            sessions.held = held
            return await payments.receive_payment({
                "company_id": cid, "entity_id": july, "reference": "pi_race", "amount_minor": 3000,
                "currency": "USD", "paid_at": "2026-07-15T11:00:00+00:00", "managed": True,
                "context": sent["context"]})

        async def _amended(held):
            if held is not None:
                pending.append(held)
            return await client.post(f"/subscriptions/{eid}/amend", headers=hd,
                                     json=_amend_body(line, 1.6667, uuid.uuid4().hex))

        doors = {"payment": _paid, "amendment": _amended}
        one, two = await _race(committed_engine, doors[first], doors[{"payment": "amendment",
                                                                       "amendment": "payment"}[first]])
        paid, amended = (one, two) if first == "payment" else (two, one)
        assert paid is True and amended.status_code == 200, amended.text
        new = amended.json()["invoice_id"]
        await _pass(factory, cid, "2026-07-16")
        await _pass(factory, cid, "2026-07-17")
        docs = {d["entity_id"]: d for d in await _docs(factory, eid)}
        async with factory() as s:
            parked = (await s.execute(select(UnmatchedPayment.reference))).scalars().all()
    if first == "payment":
        assert parked == [] and _charged(cloud, new) == [1097]
        assert docs[july]["amount_outstanding"] == 0 and docs[new]["amount_outstanding"] == 10.97
    else:
        assert parked == ["pi_race"] and _charged(cloud, new) == []
        assert docs[new]["collection"]["held_by"] == docs[july]["ref_id"]
    assert sum(_charged(cloud, july)) + sum(_charged(cloud, new)) <= 4097


@pytest.mark.parametrize("same_key", [False, True])
async def test_two_amendments_at_once_settle_one_after_the_other(committed_engine, monkeypatch, same_key):
    """Two amendments of one agreement at once take the agreement one after the other: with two
    request keys both stand, the second crediting the first's invoice; the same request key with
    another quantity is refused (409) and the first stands alone."""
    async with _collecting(committed_engine, monkeypatch) as (client, pending, hd, cid, eid, july, factory,
                                                                sessions, cloud):
        line = (await template(client, hd, eid))["line_items"][0]["line_id"]
        freeze_day(monkeypatch, "2026-07-15")
        keys = [uuid.uuid4().hex] * 2 if same_key else [uuid.uuid4().hex, uuid.uuid4().hex]

        def _amended(quantity, key):
            async def _go(held):
                if held is not None:
                    pending.append(held)
                return await client.post(f"/subscriptions/{eid}/amend", headers=hd,
                                         json=_amend_body(line, quantity, key))
            return _go

        one, two = await _race(committed_engine, _amended(2, keys[0]), _amended(3, keys[1]))
        assert one.status_code == 200, one.text
        quantities = [a["line_items"][0]["quantity"] for a in (await template(client, hd, eid))["amendments"]]
        if same_key:
            assert two.status_code == 409, two.text
            assert quantities == [2]
        else:
            assert two.status_code == 200, two.text
            assert quantities == [2, 3]  # one entry per amendment: the later one replaced the first's terms
            docs = {d["entity_id"]: d for d in await _docs(factory, eid)}
            note = docs[two.json()["credit_note_id"]]
            assert note["original_doc_id"] == one.json()["invoice_id"]


@pytest.mark.parametrize("first", ["cancel_answer", "success"])
async def test_a_cancel_answer_crossing_the_charge_success_keeps_the_success(committed_engine, monkeypatch, first):
    """The amendment's credit queued a cancel of July's 30.00; Cloud's 'cancelled' answer and its
    delivery of the charge's success reach the app at once. Whichever is recorded first, the
    success stands, July is not charged again for what it owes and the amendment's invoice waits."""
    from celerp_subscriptions import collect
    from test_collection import _attempt_outcome
    async with _collecting(committed_engine, monkeypatch) as (client, pending, hd, cid, eid, july, factory,
                                                                sessions, cloud):
        line = (await template(client, hd, eid))["line_items"][0]["line_id"]
        freeze_day(monkeypatch, "2026-07-15")
        cloud.answers["cancel"] = None  # the cancel leaves; Cloud's answer is lost
        r = await client.post(f"/subscriptions/{eid}/amend", headers=hd,
                              json=_amend_body(line, 1.6667, uuid.uuid4().hex))
        assert r.status_code == 200, r.text
        new = r.json()["invoice_id"]
        assert {d["entity_id"]: d for d in await _docs(factory, eid)}[july]["collection"]["cancel"] == "queued"
        cloud.answers["cancel"] = (200, {"state": "canceled"})

        async def _answered(held):
            opened = []

            @asynccontextmanager
            async def _open():
                async with factory() as s:
                    opened.append(s)
                    if held is not None and len(opened) == 2:  # the session recording Cloud's answer
                        held.hold(s)
                    yield s
            await collect.advance(_open, uuid.UUID(cid), july)
            return True

        async def _succeeded(held):
            sessions.held = held
            return await _attempt_outcome(cid, july, 1, "succeeded")

        doors = {"cancel_answer": _answered, "success": _succeeded}
        one, two = await _race(committed_engine, doors[first], doors[{"cancel_answer": "success",
                                                                       "success": "cancel_answer"}[first]])
        assert one is True and two is True
        await _pass(factory, cid, "2026-07-16")
        await _pass(factory, cid, "2026-07-17")
        docs = {d["entity_id"]: d for d in await _docs(factory, eid)}
    assert docs[july]["collection"]["state"] == "succeeded"
    assert _charged(cloud, july) == [3000] and _charged(cloud, new) == []
    assert docs[new]["collection"]["held_by"] == docs[july]["ref_id"]
