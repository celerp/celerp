# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Year end: what stands between a tax year and its close, each item noted or
accepted with its reason and effect, and the close that waits for them."""
from __future__ import annotations

from decimal import Decimal

import pytest

from fa_fixtures import FA
from test_runs import _contributed, _managed_from, _run

pytestmark = pytest.mark.asyncio

D = Decimal


async def _year_end(fa: FA, year: int, headers=None) -> dict:
    return await fa.ok(await fa.client.get("/fixed-assets/api/year-end", headers=headers or fa.h,
                                           params={"year": year}))


async def _mark(fa: FA, year: int, key: str, state: str, headers=None, **body):
    return await fa.post(f"/year-end/{year}/marks", {"key": key, "state": state, **body}, headers)


async def _accept_all(fa: FA, year: int) -> None:
    """Accept every open year-end item, as an accountant signing the year off would."""
    for item in (await _year_end(fa, year))["items"]:
        if item["state"] != "accepted":
            await fa.ok(await _mark(fa, year, item["key"], "accepted", reason="Reviewed at year end",
                                    effect="None on the figures"))


async def _setup(fa: FA) -> str:
    """Tax years end 30 June; the car's runs stop at March, so April to June wait."""
    await fa.ok(await fa.patch("/settings", {"tax_regimes": ["zz_asset"], "tax_year_end": "06-30"}))
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-03")
    return car


async def test_year_end_items_listed(fa, zz):
    car = await _setup(fa)
    operator = await fa.role_headers("operator")
    request = await fa.ok(await fa.post(f"/assets/{car}/corrections", {"changes": {"cost": "13000"}, "op_key": "ask"},
                                        headers=operator))
    ye = await _year_end(fa, 2026)
    assert (ye["start"], ye["end"], ye["ready"]) == ("2025-07-01", "2026-06-30", False)
    items = {i["key"]: i for i in ye["items"]}
    assert {"run:2026-04", "run:2026-05", "run:2026-06"} <= set(items)
    assert D(items["run:2026-04"]["amount"]) == D("200")
    assert f"correction:{request['id']}" in items
    # Vehicles has no zz_asset category, so the tax book cannot be worked out for the car.
    assert f"tax:zz_asset:{car}" in items
    assert all(i["state"] == "open" for i in items.values())
    assert all(i["label"] and i["href"].startswith("/fixed-assets") for i in items.values())


async def test_year_end_noted_keeps_close_blocked(fa, zz):
    await _setup(fa)
    await fa.ok(await _mark(fa, 2026, "run:2026-04", "noted", reason="Run next week"))
    ye = await _year_end(fa, 2026)
    (item,) = [i for i in ye["items"] if i["key"] == "run:2026-04"]
    assert item["state"] == "noted" and item["reason"] == "Run next week" and ye["ready"] is False
    refused = await fa.post("/tax-years/zz_asset/2026/close", {})
    assert refused.status_code == 409 and "run:2026-04" in refused.json()["detail"]


async def test_year_end_accepted_lets_close_proceed(fa, zz):
    car = await _setup(fa)
    await fa.ok(await fa.patch(f"/tax-books/{car}/zz_asset", {"category": "zz_plant"}))
    await _accept_all(fa, 2026)
    ye = await _year_end(fa, 2026)
    assert ye["ready"] is True and {i["state"] for i in ye["items"]} == {"accepted"}
    await fa.ok(await fa.post("/tax-years/zz_asset/2026/close", {}))
    # The printed year end lists what was accepted, with its reason and effect.
    page = await fa.page("/fixed-assets/year-end/print", year=2026)
    assert page.status_code == 200 and "Reviewed at year end" in page.text and "None on the figures" in page.text
    # Amounts sit right-aligned under their header on paper as on screen.
    assert 'class="cell--money"' not in page.text and 'td class="cell--number"' in page.text
    # Back to open, the item waits again.
    await fa.ok(await _mark(fa, 2026, "run:2026-04", "open"))
    assert (await _year_end(fa, 2026))["ready"] is False


async def test_year_end_marks_need_manage_accounting(fa, zz):
    await _setup(fa)
    operator = await fa.role_headers("operator")
    refused = await _mark(fa, 2026, "run:2026-04", "noted", headers=operator, reason="x")
    assert refused.status_code == 403
    # Reading the year end needs only the fixed asset view.
    assert (await fa.client.get("/fixed-assets/api/year-end", headers=operator, params={"year": 2026})).status_code == 200


async def test_year_end_accept_needs_reason_and_effect(fa, zz):
    await _setup(fa)
    for body in ({}, {"reason": "Reviewed"}, {"effect": "None"}, {"reason": " ", "effect": "None"}):
        r = await _mark(fa, 2026, "run:2026-04", "accepted", **body)
        assert r.status_code == 422, body
    unknown = await _mark(fa, 2026, "run:1999-01", "accepted", reason="x", effect="y")
    assert unknown.status_code == 404
    bad_state = await _mark(fa, 2026, "run:2026-04", "ignored", reason="x", effect="y")
    assert bad_state.status_code == 422


async def test_year_end_mark_lapses_when_amount_changes(fa, zz):
    await _setup(fa)
    await fa.ok(await _mark(fa, 2026, "run:2026-06", "accepted", reason="Runs after the close",
                            effect="200 of depreciation in June"))
    (item,) = [i for i in (await _year_end(fa, 2026))["items"] if i["key"] == "run:2026-06"]
    assert item["state"] == "accepted" and D(item["accepted_amount"]) == D("200")
    await _contributed(fa, "Second car", on="2026-04-01")
    (item,) = [i for i in (await _year_end(fa, 2026))["items"] if i["key"] == "run:2026-06"]
    assert item["state"] == "open" and item["lapsed"] is True and D(item["amount"]) == D("400")



async def test_u14_a_register_with_no_cutover_is_never_ready_to_close(client, session):
    from ui.i18n import t
    from fa_fixtures import demo_owner

    fa = FA(client, session, await demo_owner(client))
    await fa.ok(await client.post("/companies/me/demo/reseed", headers=fa.h))
    await fa.ok(await fa.post("/start"))
    unconfirmed = await _year_end(fa, 2025)
    assert unconfirmed["ready"] is False, "a register with no confirmed cutover is never ready to close"
    assert unconfirmed.get("register") == "unconfirmed"
    page = await fa.page("/fixed-assets/year-end", year=2025)
    assert t("fixed_assets.ye_unconfirmed") in page.text and t("fixed_assets.ye_ready") not in page.text
    assert 'href="/fixed-assets"' in page.text, "the statement links to where the register is set up"


async def test_u14_a_year_holds_only_its_own_items_and_none_before_the_cutover(fa, zz):
    from ui.i18n import t
    from test_sales import _sell

    await fa.ok(await fa.patch("/settings", {"tax_year_end": "06-30"}))
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-03")
    await _sell(fa, car, on="2026-07-20")
    before = await _year_end(fa, 2025)
    assert (before["ready"], before["items"]) == (False, []), "a year ending before the cutover has nothing to close"
    assert before.get("register") == "before"
    page = await fa.page("/fixed-assets/year-end", year=2025)
    assert t("fixed_assets.ye_before", cutover="2025-12-31") in page.text
    assert t("fixed_assets.ye_nothing") not in page.text and t("fixed_assets.ye_ready") not in page.text

    this_year = {i["key"] for i in (await _year_end(fa, 2026))["items"]}
    assert f"sale:{car}" not in this_year, "a sale dated after the year end waits on its own year"
    assert {"run:2026-04", "run:2026-05", "run:2026-06"} <= this_year, "the year's own months still block it"
    next_year = await _year_end(fa, 2027)
    assert next_year["register"] == "kept" and f"sale:{car}" in {i["key"] for i in next_year["items"]}
    page = await fa.page("/fixed-assets/year-end", year=2027)
    assert t("fixed_assets.ye_period_lock") in page.text and "form-input--narrow" in page.text
