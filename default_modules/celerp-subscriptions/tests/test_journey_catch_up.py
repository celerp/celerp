# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Catching up after the app was off for six months, through the API the pages use.

Initial conditions: a fresh company whose admin and a manager may both automate billing. Two
monthly agreements from 1 January bill automatically with a catch-up window of one period;
the clock then moves to 15 July, so each is seven periods behind. Two more agreements start on
1 July: one authorized by the admin, one by the manager, whose permission is then removed.
One dispatcher pass runs per step, as the app's background loop does.
"""
from __future__ import annotations

import uuid

import pytest

from agreement_kit import (
    agreement, billed_docs, company_id, freeze_day, member, register, revoke, schedule, summary, tick,
)

pytestmark = pytest.mark.asyncio


async def _auto(client, headers, owner, *, start) -> str:
    eid = await agreement(client, owner, start=start)
    r = await schedule(client, headers, eid, generation_mode="auto_invoice", catchup_periods=1)
    assert r.status_code == 200, r.text
    return eid


async def test_e2e3_catch_up_after_six_months(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-01-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    manager, _ = await member(client, session, hd, "manager")
    first, second = [await _auto(client, hd, hd, start="2026-01-01") for _ in range(2)]

    freeze_day(monkeypatch, "2026-07-15")
    current = await _auto(client, hd, hd, start="2026-07-01")
    managed = await _auto(client, manager, hd, start="2026-07-01")
    await revoke(client, hd, "automate_billing", "manager")
    await tick(session, cid, "2026-07-15")
    assert await billed_docs(session, first) == await billed_docs(session, second) == []  # outside the window
    assert [d["period_index"] for d in await billed_docs(session, current)] == [0]
    assert await billed_docs(session, managed) == []  # its authorizer no longer may

    preview = {i["entity_id"]: [p["period_index"] for p in i["periods"]]
               for i in (await client.get("/subscriptions/catch-up", headers=hd)).json()["items"]}
    assert preview == {first: list(range(7)), second: list(range(7))}

    r = await client.post(f"/subscriptions/{first}/periods/0/skip", headers=hd, json={"reason": "Office closed"})
    assert r.status_code == 200, r.text
    preview = {i["entity_id"]: [p["period_index"] for p in i["periods"]]
               for i in (await client.get("/subscriptions/catch-up", headers=hd)).json()["items"]}
    assert preview[first] == list(range(1, 7))
    items = [{"entity_id": eid, "period_index": k, "request_key": str(uuid.uuid4())}
             for eid, ks in ((first, range(1, 7)), (second, range(7))) for k in ks]
    results = (await client.post("/subscriptions/catch-up/run", headers=hd, json={"items": items})).json()["results"]
    assert all(row["ok"] for row in results), results
    assert [d["period_index"] for d in await billed_docs(session, first)] == list(range(1, 7))
    assert [d["period_index"] for d in await billed_docs(session, second)] == list(range(7))
    periods = (await client.get(f"/subscriptions/{first}/periods", headers=hd)).json()
    [skipped] = [p for p in periods["rows"] if p["period_index"] == 0]
    assert skipped["status"] == "skipped" and skipped["reason"] == "Office closed", skipped
    assert (await client.get("/subscriptions/catch-up", headers=hd)).json()["items"] == []

    rows = (await summary(client, hd))["rows"]
    assert [(r["entity_id"], r["period_index"]) for r in rows if r["kind"] == "generated"] == [(current, 0)]
    assert [r["entity_id"] for r in rows if r["kind"] == "billing_lapsed"] == [managed]

    await tick(session, cid, "2026-07-16")
    assert await billed_docs(session, managed) == []
