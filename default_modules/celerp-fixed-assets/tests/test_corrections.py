# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Correct: one action and one preview for a change to an asset after runs have posted.

A change in estimate (life, residual, method, convention) runs forward from an effective
date and posts nothing now. An error correction (cost, dates, class, opening values,
sources, maintenance that was capitalised) recomputes from the original date and posts
the difference on an open day: the current financial year to profit or loss, earlier
years to opening retained earnings, listed in the prior-period adjustments report.

Figures follow the runs tests: a contributed car of 12,000 over 60 months, straight
line, full month, charges 200 a month.
"""
from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fa_fixtures import FA
from test_runs import _contributed, _lines, _lock, _managed_from, _movements, _notices, _run, _run_journals

pytestmark = pytest.mark.asyncio

D = Decimal
PREFIX = "je:mod:celerp-fixed-assets:"


async def _correct(fa: FA, asset_id: str, changes: dict, headers=None, **body) -> dict:
    return await fa.operate(f"/assets/{asset_id}/correct", {"changes": changes, **body},
                            key=f"corr-{uuid.uuid4().hex[:8]}", headers=headers)


async def _correction_journals(fa: FA, asset_id: str) -> list[dict]:
    key = f"{PREFIX}asset:{asset_id.removeprefix('fa:asset:')}:corr:"
    return sorted((je for je in await fa.module_journals() if je["entity_id"].startswith(key)),
                  key=lambda je: je["entity_id"])


def _sum_lines(journals: list[dict]) -> dict[str, tuple[D, D]]:
    out: dict[str, tuple[D, D]] = {}
    for je in journals:
        for account, (dr, cr) in _lines(je).items():
            a, b = out.get(account, (D(0), D(0)))
            out[account] = (a + dr, b + cr)
    return out


def _snapshot(journals: dict[str, dict]) -> dict[str, tuple]:
    return {k: (je["status"], je.get("ts"), str(_lines(je))) for k, je in journals.items()}


async def _bill(fa: FA, name: str, price: str, issue_date: str) -> str:
    r = await fa.client.post("/docs", headers=fa.h, json={
        "doc_type": "bill", "contact_name": "City Motors", "issue_date": issue_date, "total": float(price),
        "line_items": [{"name": name, "quantity": 1, "unit_price": float(price), "receive_as": "asset"}]})
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    r = await fa.client.post(f"/docs/{doc_id}/finalize", headers=fa.h)
    assert r.status_code == 200, r.text
    return doc_id


async def _void(fa: FA, doc_id: str) -> None:
    r = await fa.client.post(f"/docs/{doc_id}/void", headers=fa.h, json={"reason": "Wrong price"})
    assert r.status_code == 200, r.text


# ── Change in estimate ───────────────────────────────────────────────────────


async def test_s3_life_change_estimate_prospective(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-06")
    before = _snapshot(await _run_journals(fa))
    done = await _correct(fa, car, {"life_months": 48}, effective_date="2026-07-01")
    assert done["correction"]["classification"] == "estimate" and done["journals"] == []
    assert _snapshot(await _run_journals(fa)) == before
    assert (await fa.asset(car))["policy"]["life_months"] == 48
    await _run(fa, "2026-07")
    july = (await _run_journals(fa))["run:2026-07:1"]
    assert _lines(july)["1220"] == (D(0), D("257.14"))  # 10,800 left over the 42 months remaining
    # An estimate cannot reach back into a month already depreciated.
    back = await fa.post(f"/assets/{car}/correct", {"changes": {"life_months": 36}, "effective_date": "2026-05-01",
                                                    "preview": True, "op_key": "back"})
    assert back.status_code == 422 and "2026-08" in back.json()["detail"]


async def test_s3_class_edit_leaves_assets_until_applied(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    cls = (await fa.classes())["Vehicles"]
    await fa.ok(await fa.patch(f"/classes/{cls['id']}", {"life_months": 48}))
    assert (await fa.asset(car))["policy"]["life_months"] == 60
    await _run(fa, "2026-01")
    assert _lines((await _run_journals(fa))["run:2026-01:1"])["1220"] == (D(0), D("200"))
    preview = await fa.ok(await fa.post(f"/classes/{cls['id']}/apply-to-assets",
                                        {"effective_date": "2026-02-01", "preview": True, "op_key": "ap"}))
    assert [a["id"] for a in preview["assets"]] == [car]
    await fa.operate(f"/classes/{cls['id']}/apply-to-assets", {"effective_date": "2026-02-01"}, key="ap")
    assert (await fa.asset(car))["policy"]["life_months"] == 48
    await _run(fa, "2026-02")
    assert _lines((await _run_journals(fa))["run:2026-02:1"])["1220"] == (D(0), D("251.06"))  # 11,800 / 47


# ── Error correction ─────────────────────────────────────────────────────────


async def test_s3_backdated_error_economic_vs_posting_dates(fa):
    await _managed_from(fa, "2025-01-01")
    car = await _contributed(fa, cost="12000", on="2025-06-15")
    await _run(fa, "2025-12")
    await _run(fa, "2026-03")
    await _lock(fa, "2026-01-31")
    before = _snapshot(await _run_journals(fa))

    preview = await fa.ok(await fa.post(f"/assets/{car}/correct", {
        "changes": {"cost": "15000"}, "posting_date": "2026-04-20", "preview": True, "op_key": "c1"}))
    assert preview["classification"] == "error"
    assert {y["year"]: (D(y["reported"]), D(y["restated"])) for y in preview["prior_years"]} == {
        2025: (D("1400"), D("1750"))}
    done = await _correct(fa, car, {"cost": "15000"}, posting_date="2026-04-20")
    journals = await _correction_journals(fa, car)
    assert {je["ts"][:10] for je in journals} == {"2026-04-20"}
    assert _sum_lines(journals) == {"1210": (D("3000"), D(0)), "3100": (D(0), D("3000")),
                                    "6400": (D("150"), D(0)), "3200": (D("350"), D(0)),
                                    "1220": (D(0), D("500"))}
    prior = [je for je in journals if "3200" in _lines(je)]
    assert len(prior) == 1 and prior[0]["source"]["tags"] == {"prior_period_adjustment": [2025]}
    assert _snapshot(await _run_journals(fa)) == before
    corrective = [m for m in await _movements(fa, car) if m["kind"] == "corrective"]
    assert {m["effective_date"] for m in corrective if "cost" in m["amounts"]} == {"2025-06-15"}
    assert all(m["posting_date"] == "2026-04-20" and m["corrects"] == done["correction"]["id"] for m in corrective)
    asset = await fa.asset(car)
    assert D(asset["cost"]) == D("15000") and D(asset["accumulated_depreciation"]) == D("2500")
    await fa.fold_matches(car)
    await _run(fa, "2026-04")
    assert _lines((await _run_journals(fa))["run:2026-04:1"])["1220"] == (D(0), D("250"))

    report = await fa.ok(await fa.get("/reports/prior-period-adjustments", year="2025"))
    (row,) = report["items"]
    assert row["years"] == [2025] and row["asset_id"] == car
    assert {y["year"]: (D(y["reported"]), D(y["restated"])) for y in report["years"]} == {
        2025: (D("1400"), D("1750"))}
    assert report["immaterial"] == []
    assert (await fa.ok(await fa.get("/reports/prior-period-adjustments", year="2024")))["items"] == []

    # The immaterial option: the prior-year part to current profit or loss, with its reason.
    await _lock(fa, None)
    van =await _contributed(fa, "Second car", cost="12000", on="2025-06-15")
    plain = await fa.post(f"/assets/{van}/correct", {"changes": {"cost": "15000"}, "posting_date": "2026-04-20",
                                                     "immaterial_reason": "", "prior_years": "profit_and_loss",
                                                     "preview": True, "op_key": "c2"})
    assert plain.status_code == 422 and "reason" in plain.json()["detail"].lower()
    await _correct(fa, van, {"cost": "15000"}, posting_date="2026-04-20", prior_years="profit_and_loss",
                   immaterial_reason="Below the materiality threshold")
    lines = _sum_lines(await _correction_journals(fa, van))
    assert "3200" not in lines and lines["6400"][0] > 0
    report = await fa.ok(await fa.get("/reports/prior-period-adjustments", year="2025"))
    assert [i["reason"] for i in report["immaterial"]] == ["Below the materiality threshold"]


async def test_s3_classification_default_and_override_authority(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    fields = {f["field"]: f for f in (await fa.ok(await fa.get(f"/assets/{car}/correct-fields")))["fields"]}
    assert fields["life_months"]["classification"] == "estimate"
    assert fields["cost"]["classification"] == "error"
    await _run(fa, "2026-03")

    operator = await fa.role_headers("operator")
    refused = await fa.post(f"/assets/{car}/corrections", {
        "changes": {"life_months": 48}, "classification": "error", "override_reason": "Life was keyed wrong",
        "op_key": "ask"}, headers=operator)
    assert refused.status_code == 403 and "Manage accounting" in refused.json()["detail"]
    blank = await fa.post(f"/assets/{car}/correct", {"changes": {"life_months": 48}, "classification": "error",
                                                     "preview": True, "op_key": "o"})
    assert blank.status_code == 422 and "reason" in blank.json()["detail"].lower()
    done = await _correct(fa, car, {"life_months": 48}, classification="error",
                          override_reason="Life was keyed wrong")
    stored = await fa.state(done["correction"]["id"])
    assert stored["classification"] == "error" and stored["default_classification"] == "estimate"
    assert stored["override_reason"] == "Life was keyed wrong"
    # As an error the new life runs from the start: three months at 250 against 200 posted.
    assert _sum_lines(await _correction_journals(fa, car)) == {"6400": (D("150"), D(0)), "1220": (D(0), D("150"))}
    # An error field cannot be turned into an estimate.
    other = await _contributed(fa, "Second car")
    nope = await fa.post(f"/assets/{other}/correct", {"changes": {"cost": "13000"}, "classification": "estimate",
                                                      "override_reason": "x", "preview": True, "op_key": "n"})
    assert nope.status_code == 422


async def test_s3_one_action_multiple_coordinated_entries(fa, monkeypatch):
    from celerp_fixed_assets import corrections

    await _managed_from(fa, "2025-01-01")
    car = await _contributed(fa, cost="12000", on="2025-06-15")
    await _run(fa, "2025-12")
    await _run(fa, "2026-03")
    body = {"changes": {"maintenance": {"expense_account": "6950"}}, "posting_date": "2026-04-20"}
    preview = await fa.ok(await fa.post(f"/assets/{car}/correct", {**body, "preview": True, "op_key": "m"}))
    assert len(preview["journals"]) == 3

    real, calls = corrections._post, []

    async def failing(*args, **kwargs):
        calls.append(kwargs.get("key"))
        if len(calls) == 3:
            raise HTTPException(status_code=409, detail="The ledger refused the entry.")
        return await real(*args, **kwargs)

    monkeypatch.setattr(corrections, "_post", failing)
    failed = await fa.post(f"/assets/{car}/correct", {**body, "preview": False, "op_key": "m",
                                                      "fingerprint": preview["fingerprint"]})
    assert failed.status_code == 409 and len(calls) == 3
    assert await _correction_journals(fa, car) == []
    asset = await fa.asset(car)
    assert asset["status"] == "active" and D(asset["cost"]) == D("12000")
    assert not [m for m in await _movements(fa, car) if m["kind"] == "corrective"]

    monkeypatch.setattr(corrections, "_post", real)
    await fa.ok(await fa.post(f"/assets/{car}/correct", {**body, "preview": False, "op_key": "m2",
                                                         "fingerprint": preview["fingerprint"]}))
    journals = await _correction_journals(fa, car)
    assert len(journals) == 3
    assert _sum_lines(journals) == {"3200": (D("12000"), D("1400")), "1210": (D(0), D("12000")),
                                    "1220": (D("2000"), D(0)), "6400": (D(0), D("600"))}
    asset = await fa.asset(car)
    assert asset["status"] == "expensed" and D(asset["nbv"]) == D(0)
    await fa.fold_matches(car)


async def test_correct_available_date_and_class(fa):
    await _managed_from(fa)
    car = await _contributed(fa, on="2026-03-15")
    printer = await _contributed(fa, "Plotter", on="2026-01-15")
    await _run(fa, "2026-04")  # the car March and April: 400; the plotter January to April: 800
    await _correct(fa, car, {"available_date": "2026-01-15"}, posting_date="2026-05-10")
    assert (await fa.asset(car))["available_date"] == "2026-01-15"
    assert _sum_lines(await _correction_journals(fa, car)) == {"6400": (D("400"), D(0)), "1220": (D(0), D("400"))}
    assert {m["effective_date"] for m in await _movements(fa, car) if m["kind"] == "corrective"
            and "cost" in m["amounts"]} == {"2026-03-15", "2026-01-15"}
    await fa.fold_matches(car)

    target = (await fa.classes())["Computer equipment"]
    await _correct(fa, printer, {"class_id": target["id"]}, posting_date="2026-05-10")
    moved = await fa.asset(printer)
    assert moved["class_id"] == target["id"] and moved["policy"]["life_months"] == 36
    # Same accounts, so only the depreciation difference posts: four months at 12,000 / 36.
    assert _sum_lines(await _correction_journals(fa, printer)) == {
        "6400": (D("533.33"), D(0)), "1220": (D(0), D("533.33"))}
    building = (await fa.classes())["Assets under construction"]
    earlier = {je["entity_id"] for je in await _correction_journals(fa, car)}
    await _correct(fa, car, {"class_id": building["id"]}, posting_date="2026-05-10")
    lines = _sum_lines([je for je in await _correction_journals(fa, car) if je["entity_id"] not in earlier])
    assert lines["1240"] == (D("12000"), D(0)) and lines["1210"] == (D(0), D("12000"))
    # Construction does not depreciate: what was charged comes back.
    assert lines["1220"] == (D("800"), D(0)) and lines["6400"] == (D(0), D("800"))
    await fa.fold_matches(car)


async def test_correct_opening_values_posts_no_journal(fa):
    await _managed_from(fa)
    cls = (await fa.classes())["Vehicles"]
    done = await fa.operate("/assets", {"kind": "opening", "class_id": cls["id"], "name": "Old truck",
                                        "available_date": "2023-01-15", "cost": "30000",
                                        "accumulated_depreciation": "18000"}, key="op")
    truck = done["assets"][0]["id"]
    cost = await fa.post(f"/assets/{truck}/correct", {"changes": {"cost": "31000"}, "preview": True, "op_key": "x"})
    assert cost.status_code == 422 and "opening values" in cost.json()["detail"].lower()
    await _correct(fa, truck, {"opening_values": {"cost": "30000", "accumulated_depreciation": "17500"}})
    assert await _correction_journals(fa, truck) == []
    assert D((await fa.asset(truck))["accumulated_depreciation"]) == D("17500")
    await fa.fold_matches(truck)


async def test_correction_undo_while_latest_and_open(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-03")
    done = await _correct(fa, car, {"cost": "15000"}, posting_date="2026-04-20")
    correction_id = done["correction"]["id"]
    await fa.operate(f"/corrections/{correction_id}/undo", {}, key="cu")
    assert all(je["status"] == "void" for je in await _correction_journals(fa, car))
    asset = await fa.asset(car)
    assert D(asset["cost"]) == D("12000") and D(asset["accumulated_depreciation"]) == D("600")
    assert (await fa.state(correction_id))["state"] == "undone"
    await fa.fold_matches(car)


# ── Sourced cost ─────────────────────────────────────────────────────────────


async def test_s1_replace_source_preview_with_later_movements(fa):
    await _managed_from(fa, "2026-01-01")
    doc = await _bill(fa, "Pickup truck", "12000", "2026-01-10")
    line = await fa.line("Pickup truck")
    asset = (await fa.register(line["source"], key="reg"))["assets"][0]["id"]
    await _run(fa, "2026-02")
    await _run(fa, "2026-03")
    await _void(fa, doc)
    await _bill(fa, "Pickup truck", "12100", "2026-01-10")
    new = await fa.line("Pickup truck")
    body = {"changes": {"replace_source": {"old": line["source"], "new": new["source"]}},
            "posting_date": "2026-04-20"}
    preview = await fa.ok(await fa.post(f"/assets/{asset}/correct", {**body, "preview": True, "op_key": "rs"}))
    kept = {(m["operation"], m["effective_date"], m["posting_date"]) for m in preview["kept"]}
    assert ("run", "2026-02-28", "2026-02-28") in kept and ("run", "2026-03-31", "2026-03-31") in kept
    corrected = {(m["effective_date"], m["posting_date"], D(m["cost"])) for m in preview["corrected"]}
    assert corrected == {("2026-01-10", "2026-04-20", D("-12000")), ("2026-01-10", "2026-04-20", D("12100"))}
    await fa.ok(await fa.post(f"/assets/{asset}/correct", {**body, "preview": False, "op_key": "rs",
                                                           "fingerprint": preview["fingerprint"]}))
    got = await fa.asset(asset)
    assert D(got["cost"]) == D("12100") and [s["source"] for s in got["sources"]] == [new["source"]]
    # The cost moved through the bills; the correction posts only the depreciation difference.
    assert _sum_lines(await _correction_journals(fa, asset)) == {"6400": (D("5"), D(0)), "1220": (D(0), D("5"))}
    released = await fa.state(f"fa:src:{line['source']}")
    assert released["status"] == "released"
    await fa.fold_matches(asset)


async def test_s1_voided_source_dated_difference_then_replaced(fa):
    await _managed_from(fa, "2026-01-01")
    doc = await _bill(fa, "Forklift", "6000", "2026-01-10")
    line = await fa.line("Forklift")
    asset = (await fa.register(line["source"], key="reg"))["assets"][0]["id"]
    await _run(fa, "2026-03")
    await _void(fa, doc)
    held = await fa.ok(await fa.post("/runs", {"month": "2026-04", "preview": True, "op_key": "h"}))
    assert any(e["action"] == "review_source" for m in held["months"] for e in m["exceptions"])
    await _bill(fa, "Forklift", "6100", "2026-01-10")
    new = await fa.line("Forklift")
    await _correct(fa, asset, {"replace_source": {"old": line["source"], "new": new["source"]}},
                   posting_date="2026-04-20")
    assert D((await fa.asset(asset))["cost"]) == D("6100")
    assert _sum_lines(await _correction_journals(fa, asset)) == {"6400": (D("5"), D(0)), "1220": (D(0), D("5"))}
    clear = await fa.ok(await fa.post("/runs", {"month": "2026-04", "preview": True, "op_key": "h2"}))
    assert not any(e["action"] == "review_source" for m in clear["months"] for e in m["exceptions"])


async def test_link_and_release_additional_source(fa):
    await _managed_from(fa)
    van = await fa.line("Delivery van")
    asset = (await fa.register(van["source"], key="reg"))["assets"][0]["id"]
    je = await fa.journal("2026-09-20", [{"account": "1210", "debit": 800}, {"account": "1111", "credit": 800}])
    extra = f"{je}:0"
    fields = await fa.ok(await fa.get(f"/assets/{asset}/correct-fields"))
    assert extra in [s["source"] for s in fields["sources"]]
    await _correct(fa, asset, {"link_source": {"source": extra}})
    assert D((await fa.asset(asset))["cost"]) == D("32800")
    assert (await fa.state(f"fa:src:{extra}"))["status"] == "claimed"
    credit = await fa.journal("2026-09-21", [{"account": "1111", "debit": 300}, {"account": "1210", "credit": 300}])
    await _correct(fa, asset, {"link_source": {"source": f"{credit}:1"}})
    assert D((await fa.asset(asset))["cost"]) == D("32500")
    await _correct(fa, asset, {"release_source": {"source": extra}})
    assert D((await fa.asset(asset))["cost"]) == D("31700")
    assert (await fa.state(f"fa:src:{extra}"))["status"] == "released"
    assert extra in [i["source"] for i in await fa.intake()]
    last = await fa.post(f"/assets/{asset}/correct", {"changes": {"release_source": {"source": van["source"]}},
                                                      "preview": True, "op_key": "rl"})
    assert last.status_code == 422 and "Undo" in last.json()["detail"]
    await fa.fold_matches(asset)


# ── Request and apply ────────────────────────────────────────────────────────


async def test_s9_request_without_posting_apply_needs_it(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-03")
    operator = await fa.role_headers("operator")
    direct = await fa.post(f"/assets/{car}/correct", {"changes": {"cost": "13000"}, "preview": True, "op_key": "d"},
                           headers=operator)
    assert direct.status_code == 403
    request = await fa.ok(await fa.post(f"/assets/{car}/corrections", {
        "changes": {"cost": "13000"}, "reason": "The invoice was 13,000", "op_key": "ask"}, headers=operator))
    assert request["state"] == "requested" and request["need"] == "Needs Manage accounting"
    assert await _correction_journals(fa, car) == []
    assert D((await fa.asset(car))["cost"]) == D("12000")
    (notice,) = [n for n in await _notices(fa) if "/fixed-assets/corrections" in (n.action_url or "")]
    assert "Manage accounting" in notice.body
    applied = await fa.post(f"/corrections/{request['id']}/apply", {"preview": True, "op_key": "a"},
                            headers=operator)
    assert applied.status_code == 403
    listed = (await fa.ok(await fa.get("/corrections")))["items"]
    assert [c["id"] for c in listed] == [request["id"]]
    await fa.operate(f"/corrections/{request['id']}/apply", {"posting_date": "2026-04-20"}, key="a")
    assert (await fa.state(request["id"]))["state"] == "applied"
    lines = _sum_lines(await _correction_journals(fa, car))
    assert lines["1210"] == (D("1000"), D(0)) and lines["3100"] == (D(0), D("1000"))
    assert not [n for n in await _notices(fa) if "/fixed-assets/corrections" in (n.action_url or "")]


async def test_request_withdrawn_by_requester_only(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    operator = await fa.role_headers("operator")
    other = await fa.role_headers("operator")
    request = await fa.ok(await fa.post(f"/assets/{car}/corrections", {"changes": {"cost": "13000"},
                                                                     "op_key": "ask"}, headers=operator))
    refused = await fa.post(f"/corrections/{request['id']}/withdraw", {}, headers=other)
    assert refused.status_code == 403
    await fa.ok(await fa.post(f"/corrections/{request['id']}/withdraw", {}, headers=operator))
    assert (await fa.state(request["id"]))["state"] == "withdrawn"
    again = await fa.post(f"/corrections/{request['id']}/apply", {"preview": True, "op_key": "w"})
    assert again.status_code == 409


async def _requests(fa: FA) -> list[dict]:
    return await fa.entities("fa_correction")


async def _ask(fa: FA, asset_id: str, headers, key: str = "ask-1", cost: str = "13000"):
    return await fa.post(f"/assets/{asset_id}/corrections", {"changes": {"cost": cost}, "reason": "The invoice",
                                                              "op_key": key}, headers=headers)


async def test_f07_request_retried_under_its_key_is_the_same_request(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    operator_id, operator = await fa.member("operator")
    first = await fa.ok(await _ask(fa, car, operator))
    # A lost response sent again is the same request: one correction, one waiting notice.
    again = await fa.ok(await _ask(fa, car, operator))
    assert again["id"] == first["id"] and again["state"] == "requested"
    assert len(await _requests(fa)) == 1
    (notice,) = [n for n in await _notices(fa) if "/fixed-assets/corrections" in (n.action_url or "")]
    assert notice.body.startswith("1 ")
    # The same key with other details is refused, not a second request.
    changed = await _ask(fa, car, operator, cost="14000")
    assert changed.status_code == 409 and "different details" in changed.json()["detail"]
    # Without a key from its form there is nothing to tell a retry from a new request.
    keyless = await fa.post(f"/assets/{car}/corrections", {"changes": {"cost": "13000"}}, headers=operator)
    assert keyless.status_code == 422
    # A new key is a new request.
    other = await fa.ok(await _ask(fa, car, operator, key="ask-2"))
    assert other["id"] != first["id"] and len(await _requests(fa)) == 2
    await fa.ok(await fa.post(f"/corrections/{other['id']}/withdraw", {}, headers=operator))

    # Applied, a retry returns the applied request and asks nobody for anything.
    await fa.operate(f"/corrections/{first['id']}/apply", {"posting_date": "2026-04-20"}, key="apply")
    after = await fa.ok(await _ask(fa, car, operator))
    assert after["id"] == first["id"] and after["state"] == "applied"
    assert len(await _requests(fa)) == 2
    assert not [n for n in await _notices(fa) if "/fixed-assets/corrections" in (n.action_url or "")]
    assert len(await _correction_journals(fa, car)) == 1

    # Withdrawn, a retry stays withdrawn.
    withdrawn = await fa.ok(await _ask(fa, car, operator, key="ask-2"))
    assert withdrawn["id"] == other["id"] and withdrawn["state"] == "withdrawn"

    # A requester who lost the role cannot retry it either.
    r = await fa.client.patch(f"/companies/me/users/{operator_id}", headers=fa.h, json={"role": "viewer"})
    assert r.status_code == 200, r.text
    assert (await _ask(fa, car, operator)).status_code == 401
    # Even a token still claiming the old role is refused where the request is saved.
    from test_helpers import make_authed_token
    stale = await make_authed_token(fa.session, str(operator_id), str(fa.auth["company_id"]), "operator")
    assert (await _ask(fa, car, {"Authorization": f"Bearer {stale}"})).status_code == 403
    assert len(await _requests(fa)) == 2


async def test_f07_same_key_sent_at_once_makes_one_request(committed_engine):
    from celerp.models.projections import Projection
    from celerp_fixed_assets import corrections
    from test_runs import _race_company
    from test_transfer import _assets

    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    cid, uid = await _race_company(factory)
    async with factory() as s:
        (car,) = [a["id"] for a in await _assets(s, cid)]
    body = {"changes": {"cost": "13000"}, "reason": "The invoice", "op_key": "ask-once"}

    async def attempt():
        async with factory() as s:
            try:
                done = await corrections.request(s, cid, uid, car, dict(body))
                await s.commit()
                return done
            except HTTPException as e:
                await s.rollback()
                return e

    outcomes = await asyncio.gather(attempt(), attempt(), attempt())
    assert all(isinstance(o, dict) for o in outcomes), outcomes
    assert len({o["id"] for o in outcomes}) == 1
    async with factory() as s:
        rows = (await s.execute(select(Projection).where(
            Projection.company_id == cid, Projection.entity_type == "fa_correction"))).scalars().all()
        assert len(rows) == 1


async def test_s9_accounting_fields_need_manage_accounting(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    cls = (await fa.classes())["Vehicles"]
    operator = await fa.role_headers("operator")
    for changes in ({"cost": "13000"}, {"residual_percent": 10}, {"method": "sum_of_years_digits"},
                    {"class_id": cls["id"]}, {"opening_values": {"cost": "1"}}):
        r = await fa.post(f"/assets/{car}/correct", {"changes": changes, "preview": True, "op_key": "f"},
                          headers=operator)
        assert r.status_code == 403, (changes, r.text)
    mapping = await fa.patch(f"/assets/{car}", {"changes": {"depreciation_account": "6400"},
                                                "previous": {"depreciation_account": None}}, headers=operator)
    assert mapping.status_code == 403
    apply_all = await fa.post(f"/classes/{cls['id']}/apply-to-assets", {"preview": True, "op_key": "x"},
                              headers=operator)
    assert apply_all.status_code == 403
