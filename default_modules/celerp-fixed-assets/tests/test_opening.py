# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Opening balances and the reconciliation of the register with the ledger.

Every scenario starts from a demo company. Its first managed period is confirmed from the
suggestion, so the cutover falls before the demo's first journal and the ledger holds
nothing on the class accounts at the cutover.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from fa_fixtures import FA, demo_owner

pytestmark = pytest.mark.asyncio

D = Decimal


def _month_end(day: date) -> date:
    following = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return following - timedelta(days=1)


async def _settings(fa: FA) -> dict:
    return (await fa.ok(await fa.get("/setup")))["settings"]


async def _recon(fa: FA, at: str) -> dict:
    return await fa.ok(await fa.get("/reconciliation", at=at))


def _account(recon: dict, code: str) -> dict:
    (found,) = [a for a in recon["accounts"] if a["account"] == code]
    return found


async def _opening(fa: FA, name: str = "Warehouse", cost: str = "250000", depreciation: str = "40000",
                   cls: str = "Buildings", available: str = "2020-01-01") -> str:
    class_id = (await fa.classes())[cls]["id"]
    done = await fa.operate("/assets", {"kind": "opening", "class_id": class_id, "name": name,
                                        "available_date": available, "cost": cost,
                                        "accumulated_depreciation": depreciation}, key=f"o-{uuid.uuid4().hex[:8]}")
    return done["assets"][0]["id"]


async def _acknowledge(fa: FA, difference: dict, headers=None, **body):
    return await fa.post("/reconciliation/acknowledge", {
        "key": difference["key"], "amount": difference["amount"], **body}, headers)


async def _run(fa: FA, month: str, **body) -> dict:
    return await fa.operate("/runs", {"month": month, **body}, key=f"run-{uuid.uuid4().hex[:8]}")


async def _run_preview(fa: FA, month: str) -> dict:
    return await fa.ok(await fa.post("/runs", {"month": month, "preview": True, "op_key": uuid.uuid4().hex[:8]}))


def _held(preview: dict) -> dict[str, str]:
    return {e["number"]: e["action"] for m in preview["months"] for e in m["exceptions"]}


# ── The first managed period ─────────────────────────────────────────────────


async def test_s7_first_period_explicit(client, session):
    fa = FA(client, session, await demo_owner(client))
    await fa.ok(await client.post("/companies/me/demo/reseed", headers=fa.h))
    await fa.ok(await fa.post("/start"))
    got = await fa.ok(await fa.get("/setup"))
    assert got["settings"]["first_managed_period"] is None and got["settings"]["cutover"] is None
    suggested = date.fromisoformat(got["first_period"]["suggested"])
    assert suggested.day == 1
    van = await fa.line("Delivery van")
    vehicles = (await fa.classes())["Vehicles"]["id"]
    refused = await fa.post("/register", {"source": van["source"], "class_id": vehicles, "mode": "separate",
                                          "preview": True, "op_key": "early"})
    assert refused.status_code == 409 and "first managed period" in refused.json()["detail"]

    # Depreciation already booked by hand moves the suggestion to the month after it.
    booked = _month_end(suggested + timedelta(days=40))
    je = await fa.journal(booked.isoformat(), [{"account": "6400", "debit": 500}, {"account": "1220", "credit": 500}])
    got = await fa.ok(await fa.get("/setup"))
    after = (booked + timedelta(days=1)).isoformat()
    assert got["first_period"]["suggested"] == after
    assert {"account": "1220", "date": booked.isoformat(), "journal_id": je} in got["first_period"]["evidence"][
        "depreciation"]
    assert got["settings"]["first_managed_period"] is None, "a suggestion is never applied without confirm"

    confirmed = await fa.ok(await fa.patch("/settings", {"first_managed_period": after}))
    assert confirmed["first_managed_period"] == after and confirmed["cutover"] == booked.isoformat()
    assert (await fa.ok(await fa.get("/setup")))["first_period"] is None

    # The cutover cannot move past a registered line, nor once a run has posted.
    await fa.register(van["source"], mode="separate")
    past_van = (date.fromisoformat(van["date"]).replace(day=1) + timedelta(days=32)).replace(day=1).isoformat()
    moved = await fa.patch("/settings", {"first_managed_period": past_van})
    assert moved.status_code == 409 and "Delivery van" in moved.json()["detail"]
    month = date.fromisoformat(van["date"]).strftime("%Y-%m")
    await _run(fa, month)
    moved = await fa.patch("/settings", {"first_managed_period": suggested.isoformat()})
    assert moved.status_code == 409 and "run" in moved.json()["detail"].lower()


async def test_s7_cutover_moves_with_opening_assets_while_no_run(fa):
    cutover = (await _settings(fa))["cutover"]
    warehouse = await _opening(fa)
    earlier = (date.fromisoformat(cutover).replace(day=1) - timedelta(days=1)).replace(day=1)
    moved = await fa.ok(await fa.patch("/settings", {"first_managed_period": earlier.isoformat()}))
    new_cut = moved["cutover"]
    recorded = [m for m in await fa.entities("fa_movement") if m["asset_id"] == warehouse]
    reversed_ids = {m.get("reverses") for m in recorded}
    (opening,) = [m for m in recorded if m["operation"] == "opening" and m["entity_id"] not in reversed_ids]
    assert opening["posting_date"] == new_cut
    await fa.fold_matches(warehouse)
    assert D((await fa.asset(warehouse))["cost"]) == D("250000")
    recon = await _recon(fa, new_cut)
    assert D(_account(recon, "1210")["registered"]) == D("250000")


# ── Opening assets ───────────────────────────────────────────────────────────


async def test_s7_opening_initializes_without_journal(fa):
    cutover = (await _settings(fa))["cutover"]
    before = await fa.balance("1210")
    warehouse = await _opening(fa)
    (mv,) = [m for m in await fa.entities("fa_movement") if m["asset_id"] == warehouse]
    assert mv["kind"] == "opening" and mv["posting_date"] == cutover and mv["provenance"] == "form"
    assert mv["contributions"] == [] and await fa.module_journals() == []
    assert await fa.balance("1210") == before
    recon = await _recon(fa, cutover)
    assets, depreciation = _account(recon, "1210"), _account(recon, "1220")
    assert D(assets["ledger"]) == 0 and D(assets["registered"]) == D("250000")
    assert D(depreciation["registered"]) == D("-40000")
    (diff,) = [d for d in assets["differences"] if d["rule"] == "opening"]
    assert D(diff["amount"]) == D("-250000") and diff["state"] == "open"
    assert assets["status"] == "Differences open"
    # Later dates carry the opening register too.
    today = await _recon(fa, date.today().isoformat())
    assert D(_account(today, "1210")["registered"]) >= D("250000")


async def test_s7_opening_posts_nothing_finish_needs_zero(fa):
    cutover = (await _settings(fa))["cutover"]
    await _opening(fa)
    refused = await fa.post("/opening/finish", {})
    assert refused.status_code == 409 and "1210" in refused.json()["detail"]
    await fa.journal(cutover, [{"account": "1210", "debit": 250000}, {"account": "1220", "credit": 40000},
                               {"account": "3100", "credit": 210000}])
    opening = await fa.ok(await fa.get("/opening"))
    assert all(D(a["difference"]) == 0 for a in opening["accounts"])
    done = await fa.ok(await fa.post("/opening/finish", {}))
    assert done["state"] == "finished"
    recon = await _recon(fa, cutover)
    assert _account(recon, "1210")["status"] == "Reconciled"
    assert _account(recon, "1220")["status"] == "Reconciled"
    assert await fa.module_journals() == []


async def test_s7_opening_asset_after_cutover_refused(fa):
    cutover = (await _settings(fa))["cutover"]
    cls = (await fa.classes())["Buildings"]
    body = {"kind": "opening", "class_id": cls["id"], "name": "Warehouse", "cost": "1000"}
    late = (date.fromisoformat(cutover) + timedelta(days=1)).isoformat()
    refused = await fa.post("/assets", {**body, "available_date": late, "preview": True, "op_key": "late"})
    assert refused.status_code == 422 and f"Dated after the cutover {cutover}" in refused.json()["detail"]
    done = await fa.operate("/assets", {**body, "available_date": cutover}, key="on")
    assert done["assets"][0]["number"].startswith("FA-")


# ── Differences ──────────────────────────────────────────────────────────────


async def test_s7_difference_open_until_acknowledged(fa):
    cutover = (await _settings(fa))["cutover"]
    await _opening(fa)
    operator = await fa.role_headers("operator")
    assert (await fa.post("/opening/finish", {"keep_open": True}, headers=operator)).status_code == 403
    kept = await fa.ok(await fa.post("/opening/finish", {"keep_open": True}))
    assert kept["state"] == "kept_open"
    (diff,) = [d for d in _account(await _recon(fa, cutover), "1210")["differences"] if d["rule"] == "opening"]
    assert diff["state"] == "open"
    today = await _recon(fa, date.today().isoformat())
    assert _account(today, "1210")["status"] == "Differences open" and today["status"] != "Reconciled"

    assert (await _acknowledge(fa, diff, state="noted")).status_code == 422
    assert (await _acknowledge(fa, diff, headers=operator, state="noted", reason="Valuer's report due")).status_code \
        == 403
    stale = await fa.post("/reconciliation/acknowledge", {"key": diff["key"], "amount": "1.00", "state": "noted",
                                                         "reason": "Valuer's report due"})
    assert stale.status_code == 409
    await fa.ok(await _acknowledge(fa, diff, state="noted", reason="Valuer's report due"))
    account = _account(await _recon(fa, cutover), "1210")
    assert account["status"] == "Differences acknowledged"
    (diff,) = [d for d in account["differences"] if d["rule"] == "opening"]
    assert diff["state"] == "noted" and diff["reason"] == "Valuer's report due"
    # Back to open is always possible.
    await fa.ok(await _acknowledge(fa, diff, state="open"))
    assert _account(await _recon(fa, cutover), "1210")["status"] == "Differences open"


async def test_s7_acknowledged_difference_reopens_on_change(fa):
    cutover = (await _settings(fa))["cutover"]
    warehouse = await _opening(fa)
    await fa.ok(await fa.post("/opening/finish", {"keep_open": True}))
    (diff,) = [d for d in _account(await _recon(fa, cutover), "1210")["differences"] if d["rule"] == "opening"]
    await fa.ok(await _acknowledge(fa, diff, state="noted", reason="Valuer's report due"))
    await fa.operate(f"/assets/{warehouse}/correct", {"changes": {"opening_values": {"cost": "260000"}}}, key="ov")
    account = _account(await _recon(fa, cutover), "1210")
    (diff,) = [d for d in account["differences"] if d["rule"] == "opening"]
    assert D(diff["amount"]) == D("-260000") and diff["state"] == "open"
    assert account["status"] == "Differences open"


# ── Manual depreciation after the cutover ────────────────────────────────────


async def _separate_class(fa: FA) -> str:
    """A class with an accumulated amortisation account of its own."""
    r = await fa.client.post("/accounting/accounts", headers=fa.h, json={
        "code": "1225", "name": "Accumulated amortisation", "account_type": "asset", "parent_code": "1200"})
    assert r.status_code == 200, r.text
    created = await fa.ok(await fa.post("/classes", {
        "name": "Licences", "kind": "intangible", "asset_account": "1230",
        "accumulated_depreciation_account": "1225", "method": "straight_line", "life_months": 36}))
    return created["id"]


async def test_s7_allocate_historical_depreciation(fa):
    laptops = await fa.line("Laptop")
    done = await fa.register(laptops["source"], class_name="Computer equipment", mode="separate", key="lap")
    ids = [a["id"] for a in done["assets"]]
    numbers = [a["number"] for a in done["assets"]]
    booked = _month_end(date.fromisoformat(laptops["date"]))
    je = await fa.journal(booked.isoformat(), [{"account": "6400", "debit": 300}, {"account": "1220", "credit": 300}])
    month = booked.strftime("%Y-%m")
    held = _held(await _run_preview(fa, month))
    assert all(held.get(n) == "disposition_journal" for n in numbers)
    journals = len(await fa.module_journals())
    await fa.operate("/link-depreciation", {"source": f"{je}:1", "allocations": [
        {"asset_id": a, "amount": "100"} for a in ids]}, key="alloc")
    assert len(await fa.module_journals()) == journals
    assert [D((await fa.asset(a))["accumulated_depreciation"]) for a in ids] == [D("100")] * len(ids)
    assert not set(_held(await _run_preview(fa, month))) & set(numbers)
    account = _account(await _recon(fa, booked.isoformat()), "1220")
    assert not [d for d in account["differences"] if any(ln["journal_id"] == je for ln in d["lines"])]


async def test_s7_manual_journal_dispositioned_once_never_assigned(fa):
    licences = await _separate_class(fa)
    van = await fa.line("Delivery van")
    (car,) = (await fa.register(van["source"], mode="separate", key="van"))["assets"]
    first = date.fromisoformat(van["date"])
    software = await fa.operate("/assets", {"kind": "contributed", "class_id": licences, "name": "Design suite",
                                            "available_date": first.isoformat(), "cost": "3600",
                                            "credit_account": "3100"}, key="lic")
    (suite,) = software["assets"]
    booked = _month_end(first)
    je = await fa.journal(booked.isoformat(), [{"account": "6400", "debit": 250}, {"account": "1220", "credit": 250}])
    month = booked.strftime("%Y-%m")
    held = _held(await _run_preview(fa, month))
    assert held.get(car["number"]) == "disposition_journal" and suite["number"] not in held
    assert f"{je}:1" in {i["source"] for i in await fa.intake()}

    no_reason = await fa.post("/keep-line", {"source": f"{je}:1", "preview": True, "op_key": "k0"})
    assert no_reason.status_code == 422
    await fa.operate("/keep-line", {"source": f"{je}:1", "reason": "Depreciation of an asset sold last year"},
                     key="keep")
    again = await fa.post("/keep-line", {"source": f"{je}:1", "reason": "Again", "preview": True, "op_key": "k2"})
    assert again.status_code in (409, 422)
    assert f"{je}:1" not in {i["source"] for i in await fa.intake()}
    assert car["number"] not in _held(await _run_preview(fa, month))
    assert D((await fa.asset(car["id"]))["accumulated_depreciation"]) == 0, "never assigned to an asset"
    account = _account(await _recon(fa, booked.isoformat()), "1220")
    (kept,) = [d for d in account["differences"] if any(ln["journal_id"] == je for ln in d["lines"])]
    assert kept["rule"] == 4 and kept["state"] == "noted" and D(kept["amount"]) == D("-250")


# ── The identity at every month end ──────────────────────────────────────────


async def _check_identity(fa: FA, at: str) -> dict:
    from celerp_fixed_assets import recon

    report = await _recon(fa, at)
    assert report["errors"] == [], (at, report["errors"])
    for account in report["accounts"]:
        ledger, registered = D(account["ledger"]), D(account["registered"])
        differences = sum((D(d["amount"]) for d in account["differences"]), D(0))
        assert ledger - registered == differences, (at, account)
        if account["status"] == "Reconciled":
            assert ledger == registered and not account["differences"], (at, account)
        else:
            assert account["differences"], (at, account)
    fa.session.expire_all()
    classified = await recon.classify(fa.session, fa.auth["company_id"], at)
    keys = [c["key"] for c in classified]
    assert len(keys) == len(set(keys)), "every line is classified once"
    for c in classified:
        assert c["rule"] in (1, 2, 3, 4, 5, 6), c
    return report


async def test_invariant_reconciliation_identity(fa):
    settings = await _settings(fa)
    cutover = date.fromisoformat(settings["cutover"])
    await _check_identity(fa, cutover.isoformat())
    van, forklift = await fa.line("Delivery van"), await fa.line("Forklift")
    laptops, fitout = await fa.line("Laptop"), await fa.line("Office fit-out")
    await _check_identity(fa, date.today().isoformat())  # unregistered lines are differences

    (truck,) = (await fa.register(forklift["source"], mode="separate", key="fork"))["assets"]
    (car,) = (await fa.register(van["source"], mode="separate", key="van"))["assets"]
    done = await fa.register(laptops["source"], class_name="Computer equipment", mode="separate", key="lap")
    await fa.register(fitout["source"], class_name="Assets under construction", mode="separate", key="fit")
    last = (date.today().replace(day=1) - timedelta(days=1))
    await _run(fa, last.strftime("%Y-%m"))
    # A class change moves balances between accounts; an impairment adds its own bucket.
    software = (await fa.classes())["Software"]["id"]
    await fa.operate(f"/assets/{done['assets'][0]['id']}/correct", {"changes": {"class_id": software}}, key="cls")
    await fa.operate(f"/assets/{truck['id']}/impair", {"date": last.isoformat(), "recoverable_amount": "5000",
                                                     "basis": "Market quote"},
                     key="imp")

    month = cutover + timedelta(days=1)
    while month <= date.today():
        report = await _check_identity(fa, _month_end(month).isoformat())
        month = _month_end(month) + timedelta(days=1)
    assert _account(report, "1210")["status"] == "Reconciled", report

    # A voided source is a difference until it is replaced; the register still holds the van.
    r = await fa.client.post(f"/docs/{van['document']}/void", headers=fa.h, json={"reason": "Wrong supplier"})
    assert r.status_code == 200, r.text
    report = await _check_identity(fa, date.today().isoformat())
    voided = [d for d in _account(report, "1210")["differences"] if d["rule"] == 3]
    assert voided and D(car and voided[0]["amount"]) == D("-32000")
    assert voided[0]["action"] == "replace_source" and voided[0]["asset_id"] == car["id"]


async def test_s7_each_difference_names_its_fix_and_when(fa):
    first = date.fromisoformat((await fa.line("Delivery van"))["date"])
    booked = _month_end(first)
    je = await fa.journal(booked.isoformat(), [{"account": "1210", "debit": 900}, {"account": "3100", "credit": 900}])

    def manual(report: dict) -> dict:
        (found,) = [d for d in _account(report, "1210")["differences"] if d["lines"][0]["journal_id"] == je]
        return found

    found = manual(await _recon(fa, date.today().isoformat()))
    assert (found["rule"], found["action"], found["permission"]) == (5, "intake", "manage_accounting")
    assert found["fix_from"] is None and found["asset_id"] is None
    r = await fa.client.post("/accounting/period-lock", headers=fa.h, json={"lock_date": booked.isoformat()})
    assert r.status_code == 200, r.text
    locked = manual(await _recon(fa, date.today().isoformat()))
    assert locked["fix_from"] == (booked + timedelta(days=1)).isoformat()


async def test_u2_asset_added_before_the_cutover_is_in_the_opening_register(fa):
    """A van contributed before the cutover is part of the register at the cutover, so its
    later disposal leaves the register and the ledger together: no opening difference for
    it, the registered balance never below zero, and the register's own balances agree."""
    from test_runs import _contributed

    cutover = date.fromisoformat((await _settings(fa))["cutover"])
    added = (cutover - timedelta(days=80)).isoformat()
    at_cut = await _check_identity(fa, cutover.isoformat())
    van = await _contributed(fa, name="Van", cost="32000", on=added)
    opened = await _check_identity(fa, cutover.isoformat())
    assert D(_account(opened, "1210")["registered"]) - D(_account(at_cut, "1210")["registered"]) == D("32000")
    assert [d["amount"] for d in _account(opened, "1210")["differences"]] == \
        [d["amount"] for d in _account(at_cut, "1210")["differences"]]
    today = date.today().isoformat()
    await fa.operate(f"/assets/{van}/dispose", {"date": today, "reason": "scrapped"}, key="u2-dispose")
    after = await _check_identity(fa, today)
    assert D(_account(after, "1210")["registered"]) >= 0
    assert D(_account(after, "1210")["registered"]) == D(_account(at_cut, "1210")["registered"])
    def opening_differences(report):
        return [d["amount"] for d in _account(report, "1210")["differences"] if d["rule"] == "opening"]

    assert opening_differences(after) == opening_differences(at_cut)


async def test_l4_depreciation_journalled_before_the_cutover_with_no_asset_moves_the_opening_difference(fa):
    """A manual depreciation journal on 1220 before the cutover, for no asset in the
    register, moves the 1220 opening difference by its amount and nothing else: 1210 is
    untouched, nothing is invented in the register and the identity holds."""
    cutover = date.fromisoformat((await _settings(fa))["cutover"])
    before = await _check_identity(fa, cutover.isoformat())
    await fa.journal((cutover - timedelta(days=10)).isoformat(),
                     [{"account": "6400", "debit": 533.34}, {"account": "1220", "credit": 533.34}])
    after = await _check_identity(fa, cutover.isoformat())
    assert _account(after, "1210") == _account(before, "1210")
    assert D(_account(after, "1220")["ledger"]) - D(_account(before, "1220")["ledger"]) == D("-533.34")
    assert D(_account(after, "1220")["registered"]) == D(_account(before, "1220")["registered"])

    def opening(report):
        return sum((D(d["amount"]) for d in _account(report, "1220")["differences"] if d["rule"] == "opening"), D(0))

    assert opening(after) - opening(before) == D("-533.34")
    assert [d for d in _account(after, "1220")["differences"] if d["rule"] != "opening"] == \
        [d for d in _account(before, "1220")["differences"] if d["rule"] != "opening"]


async def test_u3_a_register_that_does_not_add_up_names_both_figures_as_the_table_does(fa, monkeypatch):
    """When the assets' own balances and the Register column disagree, the alert names the
    account by code and name and gives both amounts in the money format, each called what
    the table calls it."""
    from celerp_fixed_assets import recon

    real = recon._register

    async def off_by(*args, **kwargs):
        opening, standing = await real(*args, **kwargs)
        return opening, {**standing, "1210": standing.get("1210", D(0)) + D("1234.5")}

    monkeypatch.setattr(recon, "_register", off_by)
    today = date.today().isoformat()
    report = await _recon(fa, today)
    registered = _account(report, "1210")["registered"]
    assert report["errors"] == [{"account": "1210", "name": _account(report, "1210")["name"],
                                 "held": str(D(registered) + D("1234.50")), "registered": registered}]
    page = (await fa.page("/fixed-assets/reconciliation", at=today)).text
    box = page[page.index('id="fa-recon-errors"'):]
    box = box[:box.index("</div>")]
    assert f"1210 {_account(report, '1210')['name']}" in box and "1,234.50" in box, box
    assert "1234.50" not in box and 'class="error-list"' in box, box
