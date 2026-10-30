# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Turning fixed assets off: the preflight, and the records left readable while off."""
from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

import pytest

from fa_fixtures import FA
from test_runs import _contributed, _managed_from, _run, _set_module, _tick
from test_sales import _sell

pytestmark = pytest.mark.asyncio

MODULES = "/companies/me/modules"
PKG = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _running(monkeypatch):
    """The module as a running installation holds it: listed from its module folder, loaded
    as Celerp's own, and every /fixed-assets path recorded as its own, so the module guards
    apply to them. The record is a path pattern rather than an app's route objects, so it
    holds whichever app instance serves the request, and it holds only this module."""
    from starlette.routing import Route
    from celerp.modules import loader

    monkeypatch.setenv("MODULE_DIR", str(PKG.parent))
    manifest = {**loader.read_manifest(PKG), "first_party": True}
    paths = Route("/fixed-assets{rest:path}", endpoint=lambda request: None,
                  methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
    monkeypatch.setattr(loader, "_module_routes", {manifest["name"]: [paths]})
    monkeypatch.setattr(loader, "_loaded", [*loader._loaded, manifest])


async def _started(fa: FA) -> None:
    """The company has chosen its modules, fixed assets among them, and manages from January."""
    await _set_module(fa, True)
    await _managed_from(fa)


async def _turn_off(fa: FA):
    return await fa.client.post(f"{MODULES}/celerp-fixed-assets/disable", headers=fa.h)


async def _listed(fa: FA) -> dict:
    found = (await fa.client.get(MODULES, headers=fa.h)).json()
    return next(m for m in found if m["name"] == "celerp-fixed-assets")["turn_off"]


async def test_s11_turn_off_refused_naming_a_pending_sale(fa):
    await _started(fa)
    car = await _contributed(fa)
    van = await _contributed(fa, name="Van")
    await _run(fa, "2026-03")
    await _sell(fa, car)
    number = (await fa.asset(car))["number"]
    operator = await fa.role_headers("operator")
    await fa.ok(await fa.post(f"/assets/{van}/corrections", {"changes": {"cost": "13000"}, "op_key": "ask"},
                                headers=operator))
    van_number = (await fa.asset(van))["number"]

    refused = await _turn_off(fa)
    assert refused.status_code == 409 and number in refused.json()["detail"], refused.text
    listed = await _listed(fa)
    assert [i["href"] for i in listed["blocking"]] == [f"/fixed-assets/assets/{car}"]
    assert any(van_number in i["label"] for i in listed["listed"])

    await fa.operate(f"/assets/{car}/sale/cancel", {}, key=f"c-{uuid.uuid4().hex[:8]}")
    done = await _turn_off(fa)
    assert done.status_code == 200, done.text
    assert "celerp-fixed-assets" not in done.json()["enabled_modules"]


async def test_s11_prepared_run_listed_and_kept(fa):
    await _started(fa)
    await _contributed(fa)
    await _tick(fa, date(2026, 2, 2))
    listed = await _listed(fa)
    assert listed["blocking"] == []
    runs_listed = [i["label"] for i in listed["listed"] if i["href"] == "/fixed-assets/runs"]
    assert len(runs_listed) == 1 and "2026" in runs_listed[0], listed
    await fa.ok(await _turn_off(fa))
    runs = await fa.get("/runs")
    assert runs.status_code == 200 and runs.json()["items"][0]["status"] == "prepared", runs.text


async def test_s11_records_read_only_while_off(fa):
    await _started(fa)
    car = await _contributed(fa)
    await fa.ok(await _turn_off(fa))

    shown = await fa.get(f"/assets/{car}")
    assert shown.status_code == 200 and shown.json()["name"] == "Owner's car", shown.text
    refused = await fa.patch(f"/assets/{car}", {"name": "Renamed"})
    assert refused.status_code == 403 and refused.json()["detail"]["message_key"] == "modules.off_for_company", refused.text

    for path in (f"/fixed-assets/assets/{car}", "/fixed-assets"):
        page = await fa.page(path)
        assert page.status_code == 200, page.text
        assert 'id="fa-read-only"' in page.text and 'class="records-read-only"' in page.text
    # An action page's form is hidden, so its body says so and offers the way on in its place.
    from bs4 import BeautifulSoup
    from ui.i18n import t
    soup = BeautifulSoup((await fa.page(f"/fixed-assets/assets/{car}/dispose")).text, "html.parser")
    told = soup.find(string=t("fixed_assets.turn_on_to_act")).find_parent("form")
    assert told["hx-post"] == "/modules/celerp-fixed-assets/enable" and not told.find_parent(class_="records-read-only")
    assert not soup.select("#fa-read-only button")
    # A record page keeps its records to read, so the way on stays in the notice at its top,
    # even when its cards carry forms of their own.
    for path in ("/fixed-assets", f"/fixed-assets/assets/{car}"):
        record = BeautifulSoup((await fa.page(path)).text, "html.parser")
        assert record.select("#fa-read-only button") and t("fixed_assets.turn_on_to_act") not in record.text, path
    posted = await fa.submit(f"/fixed-assets/assets/{car}/readings", {"month": "2026-01", "reading": "10"})
    assert 'id="fa-read-only"' not in posted.text
    assert (await fa.asset(car))["name"] == "Owner's car"
