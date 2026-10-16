# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Fix posting account: a class account archived in the chart holds its assets out of
the run until the class is given a new account.

A profit and loss account (depreciation expense) can change for future postings only.
A balance sheet account (cost, accumulated depreciation) moves the class's balances to
the new account in one journal, crediting the archived account, which only has to exist.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from fa_fixtures import FA

pytestmark = pytest.mark.asyncio

D = Decimal


async def _account(fa: FA, code: str, name: str, account_type: str, parent: str) -> None:
    r = await fa.client.post("/accounting/accounts", headers=fa.h, json={
        "code": code, "name": name, "account_type": account_type, "parent_code": parent})
    assert r.status_code == 200, r.text


async def _archive(fa: FA, code: str) -> None:
    r = await fa.client.patch(f"/accounting/accounts/{code}", headers=fa.h, json={"is_active": False})
    assert r.status_code == 200, r.text


async def _contributed(fa: FA, cost: str = "12000", on: str = "2026-01-15", cls: str = "Vehicles") -> str:
    class_id = (await fa.classes())[cls]["id"]
    done = await fa.operate("/assets", {"kind": "contributed", "class_id": class_id, "name": "Owner's car",
                                        "available_date": on, "cost": cost, "credit_account": "3100"},
                            key=f"c-{uuid.uuid4().hex[:8]}")
    return done["assets"][0]["id"]


async def _run(fa: FA, month: str) -> dict:
    return await fa.operate("/runs", {"month": month}, key=f"run-{uuid.uuid4().hex[:8]}")


async def _vehicles(fa: FA) -> dict:
    return (await fa.classes())["Vehicles"]


async def _managed(fa: FA) -> None:
    await fa.ok(await fa.patch("/settings", {"first_managed_period": "2026-01-01"}))


async def test_s2_future_postings_only_refused_for_balance_sheet_accounts(fa):
    await _managed(fa)
    await _account(fa, "6405", "Vehicle depreciation", "expense", "6000")
    cls = await _vehicles(fa)
    await fa.ok(await fa.patch(f"/classes/{cls['id']}", {"depreciation_account": "6405"}))
    car = await _contributed(fa)
    await _run(fa, "2026-01")
    assert await fa.balance("6405") == D("200")
    await _archive(fa, "6405")

    # The run holds the car, naming Fix posting account.
    preview = await fa.ok(await fa.post("/runs", {"month": "2026-02", "preview": True, "op_key": "p"}))
    held = preview["months"][0]["exceptions"]
    assert [h["action"] for h in held] == ["fix_account"]

    path = f"/classes/{cls['id']}/fix-account"
    for field in ("asset_account", "accumulated_depreciation_account"):
        refused = await fa.post(path, {"field": field, "account": "1210", "mode": "future", "preview": True,
                                       "op_key": f"f-{field}"})
        assert refused.status_code == 422, refused.text
        assert "Move existing balances" in refused.json()["detail"]

    moved = await fa.post(path, {"field": "depreciation_account", "account": "6400", "mode": "move",
                                 "preview": True, "op_key": "m"})
    assert moved.status_code == 422 and "Future postings only" in moved.json()["detail"]

    shown = await fa.ok(await fa.post(path, {"field": "depreciation_account", "account": "6400", "mode": "future",
                                             "preview": True, "op_key": "f"}))
    assert shown["journals"] == [] and shown["old_account"] == "6405" and shown["new_account"] == "6400"
    await fa.operate(path, {"field": "depreciation_account", "account": "6400", "mode": "future"}, key="f")
    assert (await _vehicles(fa))["depreciation_account"] == "6400"

    await _run(fa, "2026-02")
    assert await fa.balance("6400") == D("200") and await fa.balance("6405") == D("200")
    assert (await fa.asset(car)).get("exception") is None


async def test_s2_inactive_historical_account_reclass_source(fa):
    await _managed(fa)
    await _account(fa, "1215", "Vehicles at cost", "asset", "1200")
    cls = await _vehicles(fa)
    await fa.ok(await fa.patch(f"/classes/{cls['id']}", {"asset_account": "1215"}))
    # An opening van brought in at the cutover, its cost already on 1215 in the ledger.
    await fa.journal("2025-12-31", [{"account": "1215", "debit": 5000}, {"account": "3100", "credit": 5000}])
    await fa.operate("/assets", {"kind": "opening", "class_id": cls["id"], "name": "Van",
                                 "available_date": "2025-06-01", "cost": "5000", "accumulated_depreciation": "0"},
                     key="o-van")
    car = await _contributed(fa)
    await _run(fa, "2026-01")
    assert await fa.balance("1215") == D("17000")

    # While its assets hold a balance there, the class account changes only by moving it.
    refused = await fa.patch(f"/classes/{cls['id']}", {"asset_account": "1210"})
    assert refused.status_code == 409 and "Fix posting account" in refused.json()["detail"]

    await _archive(fa, "1215")
    # A new line to the archived account is refused by the chart.
    class_id = cls["id"]
    new_line = await fa.post("/assets", {"kind": "contributed", "class_id": class_id, "name": "Trailer",
                                         "available_date": "2026-02-01", "cost": "100", "credit_account": "3100",
                                         "preview": True, "op_key": "trailer"})
    assert new_line.status_code == 422 and "1215" in new_line.text

    path = f"/classes/{class_id}/fix-account"
    body = {"field": "asset_account", "account": "1210", "mode": "move"}
    before = await fa.balance("1210")
    shown = await fa.ok(await fa.post(path, {**body, "preview": True, "op_key": "mv"}))
    lines = shown["journals"][0]["lines"]
    assert {(ln["account"], ln["debit"], ln["credit"]) for ln in lines} >= {("1210", "17000.00", "0.00")}
    assert sum(D(ln["credit"]) for ln in lines if ln["account"] == "1215") == D("17000")
    await fa.operate(path, body, key="mv")
    assert await fa.balance("1215") == D("0") and await fa.balance("1210") == before + D("17000")
    assert (await _vehicles(fa))["asset_account"] == "1210"

    # Undo puts the balances back while nothing has happened since; doing it again moves them once more.
    await fa.operate(f"/classes/{class_id}/fix-account/undo", {}, key="undo-mv")
    assert await fa.balance("1215") == D("17000") and (await _vehicles(fa))["asset_account"] == "1215"
    await fa.operate(path, body, key="mv-2")
    assert await fa.balance("1215") == D("0")

    await _run(fa, "2026-02")
    assert (await fa.asset(car)).get("exception") is None
    # Once a run has posted on the class, the move is no longer undone.
    late = await fa.post(f"/classes/{class_id}/fix-account/undo", {"preview": True, "op_key": "undo-late"})
    assert late.status_code == 409 and "Correct" not in late.json()["detail"]

    recon = await fa.ok(await fa.get("/reconciliation", at="2026-02-28"))
    assert recon["errors"] == []
    by_account = {a["account"]: a for a in recon["accounts"]}
    assert by_account["1215"]["ledger"] == "0.00" and by_account["1215"]["status"] == "Reconciled"
    january = await fa.ok(await fa.get("/reconciliation", at="2026-01-31"))
    assert january["errors"] == []
    assert {a["account"]: a for a in january["accounts"]}["1215"]["status"] == "Reconciled"
