# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The fixed assets pages, driven the way a user drives them, against the real API."""
from __future__ import annotations

import re
import uuid

import pytest

from ui.i18n import t

pytestmark = pytest.mark.asyncio

API = "/fixed-assets/api"


def _fingerprint(html: str) -> str:
    found = re.search(r'name="fingerprint" value="([^"]+)"', html)
    assert found, html
    return found.group(1)


async def _review_and_confirm(ui, action: str, form: dict) -> str:
    """Review shows the journal lines; Confirm does what was reviewed. Returns where it leads."""
    form = {**form, "idempotency_key": str(uuid.uuid4())}
    review = await ui.post(f"/fixed-assets/op/{action}/review", data=form)
    assert review.status_code == 200 and t("fixed_assets.confirm") in review.text, review.text
    done = await ui.post(f"/fixed-assets/op/{action}/confirm", data={**form, "fingerprint": _fingerprint(review.text)})
    assert done.status_code == 200 and "HX-Redirect" in done.headers, done.text
    return done.headers["HX-Redirect"]


async def _demo(ui) -> None:
    r = await ui.api.post("/companies/me/demo/reseed")
    assert r.status_code == 200, r.text


async def test_start_panel_then_register_page(owner_ui):
    await _demo(owner_ui)
    page = await owner_ui.get("/fixed-assets")
    assert page.status_code == 200 and t("fixed_assets.start_title") in page.text
    assert t("fixed_assets.starter.vehicles") in page.text
    assert "fixed_assets." not in page.text, "every label on the page reads as words"

    started = await owner_ui.post("/fixed-assets/start")
    assert started.headers.get("HX-Redirect") == "/fixed-assets"

    page = await owner_ui.get("/fixed-assets")
    assert page.status_code == 200 and t("fixed_assets.start_title") not in page.text
    assert t("fixed_assets.register_from_bills") in page.text and 'href="/fixed-assets/intake"' in page.text


async def test_register_a_bill_line_from_the_intake_page(owner_ui):
    await _demo(owner_ui)
    await owner_ui.post("/fixed-assets/start")
    lines = (await owner_ui.api.get(f"{API}/intake")).json()["items"]
    line = next(ln for ln in lines if ln["kind"] == "acquisition" and str(ln["quantity"]) in ("1", "1.0"))
    vehicles = next(c for c in (await owner_ui.api.get(f"{API}/classes")).json()["items"] if c["name"] == "Vehicles")

    intake = await owner_ui.get("/fixed-assets/intake")
    assert intake.status_code == 200 and t("fixed_assets.deal_with_line") in intake.text
    line_page = await owner_ui.get("/fixed-assets/intake/line", params={"source": line["source"]})
    assert line_page.status_code == 200 and t("fixed_assets.review") in line_page.text

    href = await _review_and_confirm(owner_ui, "register", {
        "source": line["source"], "mode": "separate", "class_id": vehicles["id"], "name": "Delivery van",
        "available_date": line["date"]})
    assert href.startswith("/fixed-assets/assets/fa:asset:")

    asset = await owner_ui.get(href)
    assert asset.status_code == 200 and "Delivery van" in asset.text
    assert t("fixed_assets.movement.registration") in asset.text
    assert f">{line['document_ref']}</a>" in asset.text, "the source reads as the bill's reference"

    asset_id = href.rsplit("/", 1)[1]
    cell = await owner_ui.patch(f"/fixed-assets/assets/{asset_id}/field/serial", data={"value": "VAN-001"})
    assert cell.status_code == 200 and "VAN-001" in cell.text
    assert (await owner_ui.api.get(f"{API}/assets/{asset_id}")).json()["serial"] == "VAN-001"

    register = await owner_ui.get("/fixed-assets")
    assert "Delivery van" in register.text

    gone = await _review_and_confirm(owner_ui, "undo-asset", {"asset_id": asset_id})
    assert gone == "/fixed-assets"
    assert any(ln["source"] == line["source"] for ln in (await owner_ui.api.get(f"{API}/intake")).json()["items"])


async def test_add_an_opening_asset_by_hand(owner_ui):
    await _demo(owner_ui)
    await owner_ui.post("/fixed-assets/start")
    classes = (await owner_ui.api.get(f"{API}/classes")).json()["items"]
    buildings = next(c for c in classes if c["name"] == "Buildings")
    cutover = (await owner_ui.api.get(f"{API}/setup")).json()["settings"]["cutover"]

    for kind in ("opening", "contributed", "physical"):
        form = await owner_ui.get("/fixed-assets/assets/new", params={"kind": kind})
        assert form.status_code == 200 and t(f"fixed_assets.kind_hint.{kind}") in form.text

    href = await _review_and_confirm(owner_ui, "add-asset", {
        "kind": "opening", "name": "Warehouse", "class_id": buildings["id"],
        "available_date": "2020-01-01" if not cutover or cutover > "2020-01-01" else cutover,
        "cost": "120000", "accumulated_depreciation": "15000"})
    page = await owner_ui.get(href)
    assert page.status_code == 200 and "Warehouse" in page.text
    assert t("fixed_assets.movement.opening") in page.text and t("fixed_assets.no_sources") in page.text


async def test_a_review_whose_figures_changed_is_refused_on_confirm(owner_ui):
    await _demo(owner_ui)
    await owner_ui.post("/fixed-assets/start")
    vehicles = next(c for c in (await owner_ui.api.get(f"{API}/classes")).json()["items"] if c["name"] == "Vehicles")
    form = {"kind": "opening", "name": "Forklift", "class_id": vehicles["id"], "available_date": "2020-01-01",
            "cost": "9000", "idempotency_key": str(uuid.uuid4())}
    review = await owner_ui.post("/fixed-assets/op/add-asset/review", data=form)
    stale = await owner_ui.post("/fixed-assets/op/add-asset/confirm",
                                data={**form, "cost": "9500", "fingerprint": _fingerprint(review.text)})
    assert "HX-Redirect" not in stale.headers
    assert "flash--error" in stale.text and t("fixed_assets.review_again") in stale.text


async def test_settings_page_saves_and_edits_a_class(owner_ui):
    await _demo(owner_ui)
    not_started = await owner_ui.get("/settings/fixed-assets")
    assert not_started.status_code == 302 and not_started.headers["location"] == "/fixed-assets"

    await owner_ui.post("/fixed-assets/start")
    page = await owner_ui.get("/settings/fixed-assets")
    assert page.status_code == 200 and t("fixed_assets.classes_title") in page.text

    saved = await owner_ui.post("/settings/fixed-assets", data={"run_mode": "manual"})
    assert saved.status_code == 200 and "error" not in saved.headers.get("HX-Trigger", "")
    assert (await owner_ui.api.get(f"{API}/setup")).json()["settings"]["run_mode"] == "manual"

    vehicles = next(c for c in (await owner_ui.api.get(f"{API}/classes")).json()["items"] if c["name"] == "Vehicles")
    url = f"/settings/fixed-assets/classes/{vehicles['id']}/life_months"
    editor = await owner_ui.get(f"{url}/edit")
    assert editor.status_code == 200 and 'name="value"' in editor.text
    cell = await owner_ui.patch(url, data={"value": "72"})
    assert cell.status_code == 200 and "72" in cell.text
    classes = (await owner_ui.api.get(f"{API}/classes")).json()["items"]
    assert next(c for c in classes if c["id"] == vehicles["id"])["life_months"] == 72
