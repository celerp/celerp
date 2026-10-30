# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Depreciation runs: one journal per month, exceptions isolated per asset, exactly once.

Every scenario starts from a started demo company managed from 1 January 2026. A
contributed car (12,000 over 60 months, straight line, full month) charges 200 a month
from January, so the expected figures can be read off the calendar.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fa_fixtures import FA

pytestmark = pytest.mark.asyncio

D = Decimal


async def _managed_from(fa: FA, first: str = "2026-01-01") -> None:
    await fa.ok(await fa.patch("/settings", {"first_managed_period": first}))


async def _contributed(fa: FA, name: str = "Owner's car", cls: str = "Vehicles", cost: str = "12000",
                       on: str = "2026-01-15") -> str:
    class_id = (await fa.classes())[cls]["id"]
    done = await fa.operate("/assets", {"kind": "contributed", "class_id": class_id, "name": name,
                                        "available_date": on, "cost": cost, "credit_account": "3100"},
                            key=f"c-{uuid.uuid4().hex[:8]}")
    return done["assets"][0]["id"]


async def _run(fa: FA, month: str) -> dict:
    return await fa.operate("/runs", {"month": month}, key=f"run-{uuid.uuid4().hex[:8]}")


async def _reverse(fa: FA, month: str) -> dict:
    return await fa.operate(f"/runs/{month}/reverse", {}, key=f"rev-{uuid.uuid4().hex[:8]}")


async def _runs(fa: FA) -> list[dict]:
    return (await fa.ok(await fa.get("/runs")))["items"]


async def _run_journals(fa: FA) -> dict[str, dict]:
    """Run journals by key (``run:<yyyy-mm>:<cycle>``)."""
    prefix = "je:mod:celerp-fixed-assets:"
    return {je["entity_id"].removeprefix(prefix): je for je in await fa.module_journals()
            if je["entity_id"].startswith(f"{prefix}run:")}


def _lines(je: dict) -> dict[str, tuple[D, D]]:
    out: dict[str, tuple[D, D]] = {}
    for e in je["entries"]:
        dr, cr = out.get(e["account"], (D(0), D(0)))
        out[e["account"]] = (dr + D(str(e.get("debit") or 0)), cr + D(str(e.get("credit") or 0)))
    return out


async def _movements(fa: FA, asset_id: str) -> list[dict]:
    found = [m for m in await fa.entities("fa_movement") if m["asset_id"] == asset_id]
    return sorted(found, key=lambda m: int(m["entity_id"].rsplit(":", 1)[1]))


async def _lock(fa: FA, through: str | None) -> None:
    r = await fa.client.post("/accounting/period-lock", headers=fa.h, json={"lock_date": through})
    assert r.status_code == 200, r.text


async def _set_module(fa: FA, on: bool) -> None:
    from celerp.modules.registry import get_enabled, set_enabled
    from celerp.services.company_lock import locked_company

    company = await locked_company(fa.session, fa.auth["company_id"])
    enabled = get_enabled(company.settings) | {"celerp-fixed-assets", "celerp-accounting", "celerp-docs"}
    if not on:
        enabled.discard("celerp-fixed-assets")
    company.settings = set_enabled(dict(company.settings or {}), enabled)
    await fa.session.commit()


async def _tick(fa: FA, today: date):
    from celerp_fixed_assets import runs

    outcome = await runs.tick(fa.session, fa.auth["company_id"], today)
    await fa.session.commit()
    return outcome


async def _notices(fa: FA) -> list:
    from celerp.models.notification import Notification

    fa.session.expire_all()
    return list((await fa.session.execute(select(Notification).where(
        Notification.company_id == fa.auth["company_id"], Notification.category == "fixed_assets"))).scalars())


async def _owner_id(fa: FA) -> str:
    from celerp.models.accounting import UserCompany

    return str((await fa.session.execute(select(UserCompany.user_id).where(
        UserCompany.company_id == fa.auth["company_id"], UserCompany.role == "owner"))).scalar_one())


# ── Posting a run ────────────────────────────────────────────────────────────


async def test_run_posts_one_journal_per_month_at_month_end(fa):
    await _managed_from(fa)
    # Review refuses a run with nothing in it, as Confirm would.
    refused = await fa.post("/runs", {"month": "2026-03", "preview": True, "op_key": "e"})
    assert refused.status_code == 422 and "Nothing to depreciate yet" in refused.json()["detail"]

    car = await _contributed(fa)
    preview = await fa.ok(await fa.post("/runs", {"month": "2026-03", "preview": True, "op_key": "p"}))
    assert [m["month"] for m in preview["months"]] == ["2026-01", "2026-02", "2026-03"]
    assert D(preview["total"]) == D("600") and preview["ready"] == 1 and preview["attention"] == 0
    done = await _run(fa, "2026-03")
    assert [r["month"] for r in done["runs"]] == ["2026-01", "2026-02", "2026-03"]
    journals = await _run_journals(fa)
    assert sorted(journals) == ["run:2026-01:1", "run:2026-02:1", "run:2026-03:1"]
    for key, day in (("run:2026-01:1", "2026-01-31"), ("run:2026-02:1", "2026-02-28"), ("run:2026-03:1", "2026-03-31")):
        assert journals[key]["ts"][:10] == day
        assert _lines(journals[key]) == {"6400": (D("200"), D(0)), "1220": (D(0), D("200"))}
    assert D((await fa.asset(car))["accumulated_depreciation"]) == D("600")
    runs = await _runs(fa)
    assert [r["month"] for r in runs] == ["2026-03", "2026-02", "2026-01"]
    assert {r["status"] for r in runs} == {"posted"}
    run_moves = [m for m in await _movements(fa, car) if m["operation"] == "run"]
    assert [m["effective_date"] for m in run_moves] == ["2026-01-31", "2026-02-28", "2026-03-31"]
    await fa.fold_matches(car)
    again = await fa.post("/runs", {"month": "2026-03", "preview": True, "op_key": "again"})
    assert again.status_code == 409 and "already posted" in again.json()["detail"]["message"]
    await _run(fa, "2026-04")
    assert sorted(await _run_journals(fa))[-1] == "run:2026-04:1"
    future = await fa.post("/runs", {"month": "2099-01", "preview": True, "op_key": "f"})
    assert future.status_code == 422


async def test_u13_a_run_lists_each_asset_and_the_months_it_catches_up(fa):
    await _managed_from(fa, "2026-03-01")
    car = await _contributed(fa, on="2026-01-15")
    van = await _contributed(fa, name="Van", cost="6000", on="2026-04-02")
    preview = await fa.ok(await fa.post("/runs", {"month": "2026-04", "preview": True, "op_key": "p"}))
    march, april = preview["months"]
    # January and February come before the first managed period, so they are caught up in the last month run.
    assert [(a["asset_id"], a["name"], a["months"], D(a["amount"])) for a in march["assets"]] == [
        (car, "Owner's car", ["2026-03"], D("200"))]
    assert sorted((a["name"], a["months"], D(a["amount"])) for a in april["assets"]) == [
        ("Owner's car", ["2026-01", "2026-02", "2026-04"], D("600")), ("Van", ["2026-04"], D("100"))]
    await _run(fa, "2026-04")
    posted = {r["month"]: r for r in await _runs(fa)}
    assert {a["asset_id"]: a["months"] for a in posted["2026-04"]["assets"]} == {
        car: ["2026-01", "2026-02", "2026-04"], van: ["2026-04"]}


async def test_s2_depreciation_destination_per_class_and_asset(fa):
    await _managed_from(fa)
    site = await fa.ok(await fa.post("/classes", {"name": "Site equipment", "kind": "tangible",
                                                  "method": "straight_line", "life_months": 60,
                                                  "depreciation_account": "1240"}))
    header = await fa.patch(f"/classes/{site['id']}", {"depreciation_account": "1200"})
    assert header.status_code == 422 and "header" in str(header.json()["detail"]).lower()
    plant = await _contributed(fa, "Generator", cls="Site equipment")
    hoist = await _contributed(fa, "Hoist", cls="Site equipment")
    bad = await fa.patch(f"/assets/{hoist}", {"changes": {"depreciation_account": "1200"},
                                               "previous": {"depreciation_account": None}})
    assert bad.status_code == 422
    operator = await fa.role_headers("operator")
    blocked = await fa.patch(f"/assets/{hoist}", {"changes": {"depreciation_account": "6400"},
                                                   "previous": {"depreciation_account": None}}, headers=operator)
    assert blocked.status_code == 403
    await fa.ok(await fa.patch(f"/assets/{hoist}", {"changes": {"depreciation_account": "6400"},
                                                     "previous": {"depreciation_account": None}}))
    await _run(fa, "2026-01")
    je = (await _run_journals(fa))["run:2026-01:1"]
    assert _lines(je) == {"1240": (D("200"), D(0)), "6400": (D("200"), D(0)), "1220": (D(0), D("400"))}
    for asset_id in (plant, hoist):
        assert D((await fa.asset(asset_id))["accumulated_depreciation"]) == D("200")


# ── Reversing a run ──────────────────────────────────────────────────────────


async def test_s3_reverse_run_latest_open_only(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-03")
    older = await fa.post("/runs/2026-02/reverse", {"preview": True, "op_key": "r0"})
    assert older.status_code == 409 and "Correct" in older.json()["detail"]
    await _reverse(fa, "2026-03")
    journals = await _run_journals(fa)
    assert journals["run:2026-03:1"]["status"] == "void"
    assert D((await fa.asset(car))["accumulated_depreciation"]) == D("400")
    assert {r["month"]: r["status"] for r in await _runs(fa)}["2026-03"] == "reversed"
    await fa.fold_matches(car)

    await _lock(fa, "2026-02-28")
    await _run(fa, "2026-03")
    journals = await _run_journals(fa)
    assert journals["run:2026-03:2"]["status"] == "posted"
    assert D((await fa.asset(car))["accumulated_depreciation"]) == D("600")
    await _lock(fa, "2026-03-31")
    locked = await fa.post("/runs/2026-03/reverse", {"preview": True, "op_key": "r1"})
    assert locked.status_code == 409 and "Correct" in locked.json()["detail"]


async def test_s3_reverse_run_refused_after_newer_movement(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    van = await _contributed(fa, "Second car")
    await _run(fa, "2026-03")
    await fa.journal("2026-03-31", [{"account": "6400", "debit": 50}, {"account": "1220", "credit": 50}])
    line = await fa.line("Manual")
    await fa.operate("/link-depreciation", {"source": line["source"], "allocations": [
        {"asset_id": car, "amount": "50"}]}, key="ld")
    refused = await fa.post("/runs/2026-03/reverse", {"preview": True, "op_key": "r"})
    number = (await fa.asset(car))["number"]
    assert refused.status_code == 409
    said = refused.json()["detail"]["message"]
    assert number in said and "Correct" in said
    assert (await fa.asset(van))["number"] not in said
    await _run(fa, "2026-04")
    april = (await _run_journals(fa))["run:2026-04:1"]
    assert _lines(april)["1220"] == (D(0), D("350"))  # the car owes 150 after the linked 50
    await _reverse(fa, "2026-04")
    assert (await _run_journals(fa))["run:2026-04:1"]["status"] == "void"


# ── Exceptions ───────────────────────────────────────────────────────────────


async def test_missing_reading_isolated_then_caught_up(fa):
    await _managed_from(fa)
    machines = await fa.ok(await fa.post("/classes", {"name": "Metered machines", "kind": "tangible",
                                                      "method": "units_of_use", "params": {"total_units": "10000"}}))
    assert machines["params"] == {"total_units": "10000"}
    no_units = await fa.post("/classes", {"name": "Bad", "kind": "tangible", "method": "units_of_use"})
    assert no_units.status_code == 422
    car = await _contributed(fa)
    press = await _contributed(fa, "Press", cls="Metered machines", cost="10000")
    for period, units in (("2026-01", "100"), ("2026-02", "120")):
        await fa.ok(await fa.post(f"/assets/{press}/readings", {"period": period, "units": units}))
    bad = await fa.post(f"/assets/{press}/readings", {"period": "2026-03", "units": "-1"})
    assert bad.status_code == 422
    preview = await fa.ok(await fa.post("/runs", {"month": "2026-03", "preview": True, "op_key": "p"}))
    march = preview["months"][-1]
    (exc,) = march["exceptions"]
    assert exc["asset_id"] == press and exc["amount"] is None and exc["action"] == "enter_reading"
    await _run(fa, "2026-03")
    journals = await _run_journals(fa)
    assert _lines(journals["run:2026-01:1"])["1220"] == (D(0), D("300"))
    assert _lines(journals["run:2026-03:1"])["1220"] == (D(0), D("200"))
    statuses = {r["month"]: r["status"] for r in await _runs(fa)}
    assert statuses["2026-03"] == "posted_with_exceptions"
    exception = (await fa.asset(press))["exception"]
    assert exception["first_month"] == "2026-03" and exception["amount"] is None
    assert [a["asset_id"] for a in (await fa.ok(await fa.get("/attention")))["items"]] == [press]

    await fa.ok(await fa.post(f"/assets/{press}/readings", {"period": "2026-03", "units": "90"}))
    await fa.ok(await fa.post(f"/assets/{press}/readings", {"period": "2026-04", "units": "50"}))
    await _run(fa, "2026-04")
    april = (await _run_journals(fa))["run:2026-04:1"]
    assert _lines(april)["1220"] == (D(0), D("340"))
    catch_up = [m for m in await _movements(fa, press) if m.get("catch_up")]
    assert [(m["effective_date"], D(m["amounts"]["accumulated_depreciation"])) for m in catch_up] == [
        ("2026-03-31", D("90"))]
    assert (await fa.asset(press))["exception"] is None
    assert (await fa.ok(await fa.get("/attention")))["items"] == []
    for asset_id in (car, press):
        await fa.fold_matches(asset_id)


async def test_s8_exception_persists_across_year_end(fa):
    await _managed_from(fa, "2025-11-01")
    car = await _contributed(fa, on="2025-11-10")
    for month in ("2025-11", "2025-12", "2026-01"):
        await fa.ok(await fa.post(f"/assets/{car}/skip", {"month": month, "reason": "Vehicle off the road"}))
    no_reason = await fa.post(f"/assets/{car}/skip", {"month": "2026-02", "reason": " "})
    assert no_reason.status_code == 422
    await _run(fa, "2026-01")
    assert await _run_journals(fa) == {}
    exception = (await fa.asset(car))["exception"]
    assert exception["first_month"] == "2025-11"
    assert exception["months"] == ["2025-11", "2025-12", "2026-01"]
    assert D(exception["amount"]) == D("600")
    assert exception["responsible_user_id"] == await _owner_id(fa)
    assert exception["action"] == "skip"
    assert {r["status"] for r in await _runs(fa)} == {"posted_with_exceptions"}
    assert [a["asset_id"] for a in (await fa.ok(await fa.get("/attention")))["items"]] == [car]

    await _run(fa, "2026-02")
    feb = (await _run_journals(fa))["run:2026-02:1"]
    assert _lines(feb)["1220"] == (D(0), D("800"))
    catch_up = sorted(m["effective_date"] for m in await _movements(fa, car) if m.get("catch_up"))
    assert catch_up == ["2025-11-30", "2025-12-31", "2026-01-31"]
    assert (await fa.asset(car))["exception"] is None
    await fa.fold_matches(car)


# ── Exactly once ─────────────────────────────────────────────────────────────


async def _race_company(factory):
    from celerp_fixed_assets import operations, setup
    from test_helpers import company_auth

    cid, uid = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        await company_auth(s, cid, uid)
        await setup.start(s, cid, uid)
        await setup.update_settings(s, cid, uid, {"first_managed_period": "2026-01-01", "run_mode": "post"})
        await s.commit()
    async with factory() as s:
        cls = next(c for c in await setup.classes(s, cid) if c["name"] == "Vehicles")
        body = {"kind": "contributed", "class_id": cls["id"], "name": "Car", "available_date": "2026-01-15",
                "cost": "12000", "credit_account": "3100", "op_key": "car"}
        preview = await operations.create_asset(s, cid, uid, {**body, "preview": True})
        await operations.create_asset(s, cid, uid, {**body, "fingerprint": preview["fingerprint"]})
        await s.commit()
    return cid, uid


async def _committed_journals(factory, cid) -> list[dict]:
    from celerp.models.projections import Projection

    async with factory() as s:
        rows = (await s.execute(select(Projection).where(
            Projection.company_id == cid, Projection.entity_type == "journal_entry",
            Projection.entity_id.startswith("je:mod:celerp-fixed-assets:run:")))).scalars().all()
        return [{"id": r.entity_id, **r.state} for r in rows]


async def test_s8_two_processes_one_run_journal(committed_engine):
    from celerp_fixed_assets import runs

    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    cid, uid = await _race_company(factory)

    async def tick():
        async with factory() as s:
            outcome = await runs.tick(s, cid, date(2026, 2, 2))
            await s.commit()
            return outcome

    async def manual():
        # Either step may come after a tick has posted the month, and is then refused.
        body = {"month": "2026-01", "op_key": "manual"}
        async with factory() as s:
            try:
                preview = await runs.run(s, cid, uid, {**body, "preview": True})
                await s.rollback()
                result = await runs.run(s, cid, uid, {**body, "fingerprint": preview["fingerprint"]})
                await s.commit()
                return result
            except HTTPException as exc:
                await s.rollback()
                return exc

    await asyncio.gather(tick(), tick(), manual())
    journals = await _committed_journals(factory, cid)
    assert [j["id"].rsplit(":", 2)[-2:] for j in journals] == [["2026-01", "1"]]
    assert journals[0]["status"] == "posted"


async def test_s8_ambiguous_commit_resolved_by_status(committed_engine):
    """A run whose journal committed but whose run record the process never saw: the next
    tick reads the journal's status and completes the record instead of posting again."""
    from celerp.models.projections import Projection
    from celerp_fixed_assets import runs

    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    cid, _ = await _race_company(factory)
    async with factory() as s:
        await runs.tick(s, cid, date(2026, 2, 2))
        await s.rollback()  # the commit never happened
    assert await _committed_journals(factory, cid) == []
    async with factory() as s:
        await runs.tick(s, cid, date(2026, 2, 2))
        await s.commit()
    async with factory() as s:  # the run record is lost; the journal stands
        await s.execute(delete(Projection).where(Projection.company_id == cid, Projection.entity_id == "fa:run:2026-01"))
        await s.commit()
    async with factory() as s:
        await runs.tick(s, cid, date(2026, 2, 3))
        await s.commit()
    journals = await _committed_journals(factory, cid)
    assert len(journals) == 1
    async with factory() as s:
        run = await s.get(Projection, {"company_id": cid, "entity_id": "fa:run:2026-01"})
        assert run.state["status"] == "posted" and run.state["journal_ids"] == [journals[0]["id"]]
        moves = (await s.execute(select(Projection.state).where(
            Projection.company_id == cid, Projection.entity_type == "fa_movement"))).scalars().all()
    assert len([m for m in moves if m.get("operation") == "run"]) == 1


async def test_s8_module_off_mid_run_and_restart(fa):
    await _managed_from(fa)
    await fa.ok(await fa.patch("/settings", {"run_mode": "post"}))
    await _contributed(fa)
    await _set_module(fa, False)
    await _tick(fa, date(2026, 2, 2))
    assert await _run_journals(fa) == {}
    assert (await fa.ok(await fa.get("/setup")))["settings"]["auto_post_paused"] is True
    await _set_module(fa, True)
    await _tick(fa, date(2026, 2, 3))
    assert await _run_journals(fa) == {}
    assert {r["month"]: r["status"] for r in await _runs(fa)} == {"2026-01": "prepared"}
    assert any("paused" in n.body for n in await _notices(fa))
    await _run(fa, "2026-01")
    assert (await fa.ok(await fa.get("/setup")))["settings"]["auto_post_paused"] is False
    await _tick(fa, date(2026, 3, 2))
    assert sorted(await _run_journals(fa)) == ["run:2026-01:1", "run:2026-02:1"]


async def test_s8_fingerprint_detects_changes(fa):
    await _managed_from(fa)
    await _contributed(fa)

    async def stale_after(change) -> None:
        preview = await fa.ok(await fa.post("/runs", {"month": "2026-03", "preview": True, "op_key": "fp"}))
        await change()
        r = await fa.post("/runs", {"month": "2026-03", "preview": False, "op_key": "fp",
                                    "fingerprint": preview["fingerprint"]})
        detail = r.json()["detail"]
        message = detail["message"] if isinstance(detail, dict) else detail
        assert r.status_code == 409 and "changed since you reviewed it" in message, r.text

    async def new_asset():
        await _contributed(fa, "Trailer", cost="6000", on="2026-02-01")

    async def registration():
        van = await fa.line("Delivery van")
        await fa.register(van["source"], key="van", mode="separate", available_date="2026-02-01")

    async def account_mapping():
        cls = (await fa.classes())["Vehicles"]
        await fa.ok(await fa.patch(f"/classes/{cls['id']}", {"depreciation_account": "6950"}))

    async def concurrent_posting():
        await fa.journal("2026-02-10", [{"account": "6400", "debit": 10}, {"account": "1220", "credit": 10}])

    async def lock_change():
        await _lock(fa, "2026-01-31")

    for change in (new_asset, registration, account_mapping, concurrent_posting, lock_change):
        await stale_after(change)
    await _run(fa, "2026-03")


async def test_s8_auto_post_user_revoked_prepares(fa):
    await _managed_from(fa)
    await _contributed(fa)
    manager_id, manager = await fa.member("manager")
    await fa.ok(await fa.patch("/settings", {"run_mode": "post"}, headers=manager))
    assert (await fa.ok(await fa.get("/setup")))["settings"]["auto_post_user"] == str(manager_id)
    await _tick(fa, date(2026, 2, 2))
    assert sorted(await _run_journals(fa)) == ["run:2026-01:1"]
    (posted,) = [n for n in await _notices(fa) if n.title == "Depreciation posted automatically"]
    assert posted.priority == "high" and posted.action_url == "/fixed-assets/runs/2026-01"
    # Told to the whole company, so it names the month and no amount.
    assert posted.body == "Depreciation for January 2026 was posted automatically.", posted.body
    demoted = await fa.client.patch(f"/companies/me/users/{manager_id}", headers=fa.h, json={"role": "viewer"})
    assert demoted.status_code == 200, demoted.text
    await _tick(fa, date(2026, 4, 2))
    assert sorted(await _run_journals(fa)) == ["run:2026-01:1"]
    assert {r["month"]: r["status"] for r in await _runs(fa)}["2026-03"] == "prepared"
    notices = await _notices(fa)
    assert any("Choose another user" in n.body for n in notices), [n.body for n in notices]


async def test_loop_prepares_and_notifies(fa):
    await _managed_from(fa)
    await _contributed(fa)
    await _tick(fa, date(2026, 2, 1))
    assert await _run_journals(fa) == {}
    (run,) = await _runs(fa)
    assert run["month"] == "2026-01" and run["status"] == "prepared" and D(run["total"]) == D("200")
    (notice,) = await _notices(fa)
    assert notice.body == "Depreciation for January 2026 is ready to post.", notice.body
    await _tick(fa, date(2026, 2, 1))
    assert len(await _notices(fa)) == 1
    await fa.ok(await fa.patch("/settings", {"run_day": 5}))
    await _tick(fa, date(2026, 3, 3))
    assert [r["month"] for r in await _runs(fa)] == ["2026-01"]
    await _tick(fa, date(2026, 3, 5))
    assert [r["month"] for r in await _runs(fa)] == ["2026-02", "2026-01"]
    await _run(fa, "2026-02")
    assert await _notices(fa) == []
    await fa.ok(await fa.patch("/settings", {"run_mode": "manual"}))
    await _tick(fa, date(2026, 4, 5))
    assert [r["month"] for r in await _runs(fa)] == ["2026-02", "2026-01"]


# ── Movements and the projection ─────────────────────────────────────────────


async def test_s8_run_movements_rebuild_projection(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    trailer = await _contributed(fa, "Trailer", cost="6000")
    await _run(fa, "2026-01")
    await fa.ok(await fa.post(f"/assets/{trailer}/skip", {"month": "2026-02", "reason": "Under repair"}))
    await _run(fa, "2026-02")
    await _run(fa, "2026-03")
    await _reverse(fa, "2026-03")
    await _run(fa, "2026-03")
    movements = await fa.entities("fa_movement")
    for key, je in (await _run_journals(fa)).items():
        if je["status"] != "posted":
            continue
        moved = sum((D(c["amount"]) for m in movements for c in m.get("contributions") or []
                     if c["journal_id"] == je["entity_id"]), D(0))
        assert moved == _lines(je)["1220"][1], key
    for asset_id in (car, trailer):
        await fa.fold_matches(asset_id)
    assert D((await fa.asset(trailer))["accumulated_depreciation"]) == D("300")


async def test_invariant_projection_rebuilds_from_movements(fa):
    from celerp.events.engine import ProjectionEngine
    from celerp.services.company_lock import lock_company

    await _managed_from(fa)
    car = await _contributed(fa)
    cls = (await fa.classes())["Buildings"]
    shed = (await fa.operate("/assets", {"kind": "opening", "class_id": cls["id"], "name": "Shed",
                                         "available_date": "2020-01-01", "cost": "48000",
                                         "accumulated_depreciation": "6000"}, key="op"))["assets"][0]["id"]
    await _run(fa, "2026-02")
    await _reverse(fa, "2026-02")
    await _run(fa, "2026-03")
    before = {a: await fa.state(a) for a in (car, shed)}
    assert D(before[shed]["accumulated_depreciation"]) > D("6000")
    await lock_company(fa.session, fa.auth["company_id"])
    await ProjectionEngine.rebuild(fa.session, company_id=fa.auth["company_id"])
    await fa.session.commit()
    for asset_id, state in before.items():
        assert await fa.state(asset_id) == state
        await fa.fold_matches(asset_id)


async def test_asset_history_query_uses_index(fa):
    from sqlalchemy import text

    from celerp_fixed_assets.operations import movements_query

    car = await _contributed(fa)
    cid = fa.auth["company_id"]
    await fa.session.execute(text(
        "INSERT INTO projections (company_id, entity_id, entity_type, state, version, updated_at) "
        "SELECT :c, 'fa:mv:bulk:' || n, 'fa_movement', "
        "json_build_object('asset_id', 'fa:asset:bulk-' || (n % 5000)), 1, now() "
        "FROM generate_series(1, 50000) AS n"), {"c": cid})
    await fa.session.execute(text("ANALYZE projections"))
    query = movements_query(cid, car)
    compiled = query.compile(dialect=fa.session.bind.dialect, compile_kwargs={"literal_binds": True})
    plan = "\n".join(r[0] for r in (await fa.session.execute(text(f"EXPLAIN {compiled}"))).all())
    assert "ix_projections_asset" in plan, plan


async def test_r2_4_the_next_charge_is_the_assets_own_first_month_and_names_what_it_catches_up(fa):
    """An asset available after the next run month is next charged in its own first month, a
    full month's charge, not the company's next run at nil; one registered into months already
    run is charged by the next run with those months caught up, and its page names them."""
    await _managed_from(fa)
    later = await _contributed(fa, name="Delivery van", on="2026-08-05")
    assert (await fa.asset(later))["next_charge"] == {"month": "2026-08", "held": False, "amount": "200.00",
                                                      "catches_up": []}
    await _run(fa, "2026-03")
    back = await _contributed(fa, name="Forklift", on="2026-02-10")
    assert (await fa.asset(back))["next_charge"] == {"month": "2026-04", "held": False, "amount": "600.00",
                                                     "catches_up": ["2026-02", "2026-03"]}
    page = (await fa.page(f"/fixed-assets/assets/{back}")).text
    assert "(includes 2026-02, 2026-03)" in page
