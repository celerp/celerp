# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""An agreement's stored status decides whether a period can bill, by one allow-list rule
(billing.billable: active, on a schedule) that the preview and every writer read.

The doors are read from the code (eligibility_census): every caller of the functions that bill
a period, and every status a status writer can store. Each status is driven through each door
here, and the door must bill exactly when the preview lists the period, never answering 500. A
new caller or a new status fails until it is driven below."""
from __future__ import annotations

import uuid

import pytest

from agreement_kit import (
    agreement, bill_early, billed_docs, company_id, freeze_day, generate_due, register, schedule, template, tick,
)
from eligibility_census import BILLERS, billing_doors, guarded, status_values

TODAY = "2026-03-01"
# A stored value no writer names today: the rule must refuse it as well.
UNNAMED = "unnamed_status"


async def _actor(client, hd) -> uuid.UUID:
    users = (await client.get("/companies/me/users", headers=hd)).json()["items"]
    return uuid.UUID(str(users[0].get("id") or users[0].get("user_id")))


async def _store(client, session, hd, eid: str, status: str) -> None:
    """Store ``status`` on the agreement through the module's own writer, the way any earlier
    writer or release may have left it."""
    from celerp_subscriptions import billing

    if status == (await template(client, hd, eid)).get("status"):
        return
    cid = uuid.UUID(await company_id(client, hd))
    await billing.locked_company(session, cid)
    await billing.write_agreement(session, cid, eid, {"status": status}, await _actor(client, hd))
    await session.commit()


async def _due_listed(client, hd, eid) -> bool:
    r = await client.get("/subscriptions/due", headers=hd)
    assert r.status_code == 200, r.text
    return eid in {i["entity_id"] for i in r.json()["items"]}


async def _catch_up_listed(client, hd, eid) -> bool:
    r = await client.get("/subscriptions/catch-up", headers=hd)
    assert r.status_code == 200, r.text
    return eid in {i["entity_id"] for i in r.json()["items"]}


async def _early_listed(client, hd, eid) -> bool:
    # The agreement page offers Bill early on an active agreement.
    return (await template(client, hd, eid)).get("status") == "active"


async def _manual(client, monkeypatch, start: str):
    freeze_day(monkeypatch, TODAY)
    hd = await register(client)
    return hd, await agreement(client, hd, start=start)


async def _automatic(client, monkeypatch, start: str, today: str, window: int):
    freeze_day(monkeypatch, today)
    hd = await register(client)
    eid = await agreement(client, hd, start=start)
    assert (await schedule(client, hd, eid, generation_mode="auto_invoice", catchup_periods=window)).status_code == 200
    return hd, eid


async def _call(make):
    try:
        return await make()
    except Exception as exc:  # the test client re-raises an unhandled server error (a 500 in production)
        return type("R", (), {"status_code": 500, "text": f"unhandled {type(exc).__name__}: {exc}", "json": dict})()


async def _single_due(client, session, monkeypatch, status):
    hd, eid = await _manual(client, monkeypatch, TODAY)
    await _store(client, session, hd, eid, status)
    listed = await _due_listed(client, hd, eid)
    r = await _call(lambda: generate_due(client, hd, eid))
    return hd, eid, listed, r, r.status_code == 200


async def _bulk_due(client, session, monkeypatch, status):
    hd, eid = await _manual(client, monkeypatch, TODAY)
    await _store(client, session, hd, eid, status)
    listed = await _due_listed(client, hd, eid)
    r = await _call(lambda: client.post("/subscriptions/generate-due", headers=hd, json={"items": [
        {"entity_id": eid, "period_index": 0, "request_key": str(uuid.uuid4())}]}))
    return hd, eid, listed, r, r.status_code == 200 and r.json()["results"][0]["ok"]


async def _early(client, session, monkeypatch, status):
    hd, eid = await _manual(client, monkeypatch, "2026-04-01")
    await _store(client, session, hd, eid, status)
    listed = await _early_listed(client, hd, eid)
    r = await _call(lambda: bill_early(client, hd, eid, str(uuid.uuid4()), period_index=0))
    return hd, eid, listed, r, r.status_code == 200


async def _catch_up(client, session, monkeypatch, status):
    hd, eid = await _automatic(client, monkeypatch, "2026-01-01", "2026-06-15", 1)
    await _store(client, session, hd, eid, status)
    listed = await _catch_up_listed(client, hd, eid)
    r = await _call(lambda: client.post("/subscriptions/catch-up/run", headers=hd, json={"items": [
        {"entity_id": eid, "period_index": 0, "request_key": str(uuid.uuid4())}]}))
    return hd, eid, listed, r, r.status_code == 200 and r.json()["results"][0]["ok"]


async def _dispatcher(client, session, monkeypatch, status):
    hd, eid = await _automatic(client, monkeypatch, TODAY, TODAY, 6)
    await _store(client, session, hd, eid, status)
    listed = await _due_listed(client, hd, eid)
    cid = await company_id(client, hd)

    class Ran:
        status_code, text = 200, ""

    async def run():
        await tick(session, cid, TODAY)
        return Ran()

    r = await _call(run)
    return hd, eid, listed, r, bool(await billed_docs(session, eid))


# Each door the census finds, by its route (the dispatcher's pass by its function).
DRIVERS = {
    "POST /{entity_id}/generate-due": _single_due,
    "POST /generate-due": _bulk_due,
    "POST /{entity_id}/bill-early": _early,
    "POST /catch-up/run": _catch_up,
    "run_due": _dispatcher,
}


def _door_names() -> set[str]:
    """Every caller of a biller, by its route; a biller itself is driven through its callers."""
    return {route or fn for fn, route in ((n.split("::")[1], r) for n, (_, r) in billing_doors().items())
            if fn not in BILLERS}


def test_every_billing_door_is_driven():
    assert _door_names() == set(DRIVERS)


def test_every_billing_door_reads_the_one_rule_before_it_bills():
    unguarded = [name for name, (biller, _) in billing_doors().items() if not guarded(name.split("::")[1], biller)]
    assert unguarded == []


@pytest.mark.parametrize("status", [*status_values(), UNNAMED])
@pytest.mark.parametrize("door", sorted(DRIVERS))
async def test_a_door_bills_exactly_when_the_preview_lists_the_period(client, session, monkeypatch, door, status):
    hd, eid, listed, r, ok = await DRIVERS[door](client, session, monkeypatch, status)
    assert r.status_code != 500, r.text
    billed = [d for d in await billed_docs(session, eid) if d.get("status") not in ("draft", "void")]
    assert listed == (status == "active")
    assert bool(billed) == listed == ok, (door, status, listed, r.text)


async def test_documents_finalize_of_a_draft_agreement_is_refused_naming_activate(client, session, monkeypatch):
    freeze_day(monkeypatch, TODAY)
    hd = await register(client)
    eid = await agreement(client, hd, start=TODAY, activate=False)
    r = await _call(lambda: client.post(f"/docs/{eid}/finalize", headers=hd))
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "subscriptions.err_agreement_activate", r.text
    assert (await template(client, hd, eid)).get("status") == "draft"
    assert (await client.post(f"/subscriptions/{eid}/activate", headers=hd)).status_code == 200
    assert (await generate_due(client, hd, eid)).status_code == 200


async def test_a_voided_agreement_bills_nothing_until_it_is_unvoided(client, session, monkeypatch):
    hd, eid = await _manual(client, monkeypatch, TODAY)
    assert (await client.post(f"/docs/{eid}/void", headers=hd, json={"reason": "entered twice"})).status_code == 200
    assert not await _due_listed(client, hd, eid)
    r = await _call(lambda: generate_due(client, hd, eid))
    assert r.status_code == 409 and r.json()["detail"]["message_key"] == "subscriptions.err_bill_void", r.text
    assert await billed_docs(session, eid) == []
    assert (await client.post(f"/docs/{eid}/unvoid", headers=hd, json={})).status_code == 200
    assert await _due_listed(client, hd, eid)
    assert (await generate_due(client, hd, eid)).status_code == 200
    assert [d["period_index"] for d in await billed_docs(session, eid)] == [0]
