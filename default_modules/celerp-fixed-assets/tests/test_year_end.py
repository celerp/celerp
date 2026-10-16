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
    request = await fa.ok(await fa.post(f"/assets/{car}/corrections", {"changes": {"cost": "13000"}},
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
    page = await fa.client.get("/fixed-assets/year-end/print", headers=fa.h, params={"year": 2026})
    assert page.status_code == 200 and "Reviewed at year end" in page.text and "None on the figures" in page.text
    # Back to open, the item waits again.
    await fa.ok(await _mark(fa, 2026, "run:2026-04", "open"))
    assert (await _year_end(fa, 2026))["ready"] is False


async def test_year_end_marks_need_manage_accounting(fa, zz):
    await _setup(fa)
    operator = await fa.role_headers("operator")
    refused = await _mark(fa, 2026, "run:2026-04", "noted", headers=operator, reason="x")
    assert refused.status_code == 403
    viewer = await fa.role_headers("viewer")
    assert (await fa.client.get("/fixed-assets/api/year-end", headers=viewer, params={"year": 2026})).status_code == 200


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
