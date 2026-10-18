# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The fixed assets pages, driven the way a user drives them, against the real API."""
from __future__ import annotations

import html
import json
import re
import uuid
from decimal import Decimal
from urllib.parse import quote

import pytest
from bs4 import BeautifulSoup

from ui.i18n import doc_type_label, t

pytestmark = pytest.mark.asyncio

API = "/fixed-assets/api"


def _fingerprint(html: str) -> str:
    found = re.search(r'name="fingerprint" value="([^"]+)"', html)
    assert found, html
    return found.group(1)


async def _confirmed(ui, action: str, form: dict):
    """Review shows the journal lines; Confirm does what was reviewed. Returns Confirm's response."""
    form = {**form, "idempotency_key": str(uuid.uuid4())}
    review = await ui.post(f"/fixed-assets/op/{action}/review", data=form)
    assert review.status_code == 200 and t("fixed_assets.confirm") in review.text, review.text
    done = await ui.post(f"/fixed-assets/op/{action}/confirm", data={**form, "fingerprint": _fingerprint(review.text)})
    assert done.status_code == 200 and "HX-Redirect" in done.headers, done.text
    return done


async def _review_and_confirm(ui, action: str, form: dict) -> str:
    """Where a confirmed operation leads."""
    return (await _confirmed(ui, action, form)).headers["HX-Redirect"]


def _toast(response) -> str:
    return json.loads(response.headers["HX-Trigger"])["celerpToast"]["message"]


async def _demo(ui) -> None:
    r = await ui.api.post("/companies/me/demo/reseed")
    assert r.status_code == 200, r.text


async def _buyer(ui) -> str:
    """A demo contact to sell an asset to."""
    return (await ui.api.get("/crm/contacts", params={"limit": 1})).json()["items"][0]["id"]


async def _start(ui) -> None:
    """Start fixed assets and confirm the first managed period it suggests."""
    await ui.post("/fixed-assets/start")
    suggested = (await ui.api.get(f"{API}/setup")).json()["first_period"]["suggested"]
    r = await ui.api.patch(f"{API}/settings", json={"first_managed_period": suggested})
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
    await _start(owner_ui)
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


async def test_u32_a_registering_review_shows_the_class_date_and_how_it_depreciates(owner_ui):
    await _demo(owner_ui)
    await _start(owner_ui)
    line = next(ln for ln in (await owner_ui.api.get(f"{API}/intake")).json()["items"]
                if ln["kind"] == "acquisition" and str(ln["quantity"]) in ("1", "1.0"))
    vehicles = await _class(owner_ui, "Vehicles")
    reviews = [
        (line["date"], await owner_ui.post("/fixed-assets/op/register/review", data={
            "source": line["source"], "mode": "separate", "class_id": vehicles["id"], "name": "Delivery van",
            "available_date": line["date"], "idempotency_key": str(uuid.uuid4())})),
        ("2020-01-01", await owner_ui.post("/fixed-assets/op/add-asset/review", data={
            "kind": "opening", "name": "Forklift", "class_id": vehicles["id"], "available_date": "2020-01-01",
            "cost": "9000", "idempotency_key": str(uuid.uuid4())})),
    ]
    for day, review in reviews:
        text = re.sub(r"<[^>]+>", " ", review.text)
        for label, value in ((t("fixed_assets.class"), "Vehicles"), (t("fixed_assets.available_date"), day),
                             (t("fixed_assets.method"), t(f"fixed_assets.method.{vehicles['method']}")),
                             (t("fixed_assets.life_months"), str(vehicles["life_months"]))):
            assert re.search(re.escape(label) + r"\s+" + re.escape(value), text), (label, value)
        assert "fixed_assets." not in review.text


async def test_add_an_opening_asset_by_hand(owner_ui):
    await _demo(owner_ui)
    await _start(owner_ui)
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
    assert t("fixed_assets.movement.opening") in page.text and t("fixed_assets.origin.opening") in page.text


async def test_a_review_whose_figures_changed_is_refused_on_confirm(owner_ui):
    await _demo(owner_ui)
    await _start(owner_ui)
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


async def test_import_page_asks_for_the_start_and_first_period_and_returns_to_the_upload(owner_ui):
    await _demo(owner_ui)
    page = await owner_ui.get("/fixed-assets/import")
    assert page.status_code == 200 and t("fixed_assets.start_title") in page.text
    assert 'id="csv_file"' not in page.text
    await owner_ui.post("/fixed-assets/start")
    page = await owner_ui.get("/fixed-assets/import")
    assert t("fixed_assets.first_period_title") in page.text and 'id="csv_file"' not in page.text
    assert 'name="back" value="/fixed-assets/import"' in page.text
    suggested = (await owner_ui.api.get(f"{API}/setup")).json()["first_period"]["suggested"]
    elsewhere = await owner_ui.post("/fixed-assets/first-period",
                                    data={"first_managed_period": suggested, "back": "https://example.com/"})
    assert elsewhere.headers.get("HX-Redirect") == "/fixed-assets", "only a fixed assets page is returned to"
    confirmed = await owner_ui.post("/fixed-assets/first-period",
                                    data={"first_managed_period": suggested, "back": "/fixed-assets/import"})
    assert confirmed.headers.get("HX-Redirect") == "/fixed-assets/import", confirmed.headers
    page = await owner_ui.get("/fixed-assets/import")
    assert 'id="csv_file"' in page.text and t("fixed_assets.first_period_title") not in page.text


async def test_confirm_the_first_period_then_reconcile_and_finish_opening(owner_ui):
    await _demo(owner_ui)
    await owner_ui.post("/fixed-assets/start")
    page = await owner_ui.get("/fixed-assets")
    assert page.status_code == 200 and t("fixed_assets.first_period_title") in page.text
    suggested = (await owner_ui.api.get(f"{API}/setup")).json()["first_period"]["suggested"]
    confirmed = await owner_ui.post("/fixed-assets/first-period", data={"first_managed_period": suggested})
    assert confirmed.headers.get("HX-Redirect") == "/fixed-assets", confirmed.headers
    assert (await owner_ui.api.get(f"{API}/setup")).json()["settings"]["first_managed_period"] == suggested

    # The demo's bills are not registered yet, so the register says so and links the reconciliation.
    page = await owner_ui.get("/fixed-assets")
    assert t("fixed_assets.first_period_title") not in page.text
    assert t("fixed_assets.recon_banner") in page.text and 'href="/fixed-assets/reconciliation"' in page.text
    recon = await owner_ui.get("/fixed-assets/reconciliation")
    assert recon.status_code == 200 and t("fixed_assets.recon_rule.5") in recon.text
    assert "fixed_assets." not in recon.text, "every label on the page reads as words"
    report = (await owner_ui.api.get(f"{API}/reconciliation")).json()
    diff = next(d for a in report["accounts"] for d in a["differences"] if d["rule"] == 5)
    noted = await owner_ui.post("/fixed-assets/reconciliation/acknowledge", data={
        "key": diff["key"], "amount": diff["amount"], "state": "noted", "reason": "Registered next week"})
    assert noted.headers.get("HX-Redirect") == "/fixed-assets/reconciliation", noted.headers
    report = (await owner_ui.api.get(f"{API}/reconciliation")).json()
    assert next(d for a in report["accounts"] for d in a["differences"] if d["key"] == diff["key"])["state"] == "noted"

    opening = await owner_ui.get("/fixed-assets/opening")
    assert opening.status_code == 200 and t("fixed_assets.opening_finish") in opening.text
    assert "fixed_assets." not in opening.text
    kept = await owner_ui.post("/fixed-assets/opening/finish", data={"keep_open": "1"})
    assert kept.headers.get("HX-Redirect") == "/fixed-assets/opening", kept.headers
    assert t("fixed_assets.opening_state.kept_open") in (await owner_ui.get("/fixed-assets/opening")).text
    reopened = await owner_ui.post("/fixed-assets/opening/finish", data={"reopen": "1"})
    assert reopened.headers.get("HX-Redirect") == "/fixed-assets/opening"
    assert t("fixed_assets.opening_finish") in (await owner_ui.get("/fixed-assets/opening")).text


async def _managed_car(ui, on: str = "2026-08-10") -> str:
    """A started demo company managed from August 2026, with one contributed car."""
    await _demo(ui)
    await ui.post("/fixed-assets/start")
    r = await ui.api.patch(f"{API}/settings", json={"first_managed_period": "2026-08-01"})
    assert r.status_code == 200, r.text
    vehicles = next(c for c in (await ui.api.get(f"{API}/classes")).json()["items"] if c["name"] == "Vehicles")
    href = await _review_and_confirm(ui, "add-asset", {
        "kind": "contributed", "name": "Delivery car", "class_id": vehicles["id"], "available_date": on,
        "cost": "12000", "credit_account": "3100"})
    return href.rsplit("/", 1)[1]


async def test_u34_a_contributed_asset_says_how_it_came_in_and_links_its_journal(owner_ui):
    car = await _managed_car(owner_ui)
    page = (await owner_ui.get(f"/fixed-assets/assets/{car}")).text
    (movement,) = [m for m in (await owner_ui.api.get(f"{API}/assets/{car}/movements")).json()["items"]
                   if m["operation"] == "contributed"]
    journal = movement["contributions"][0]["journal_id"]
    card = page.split(t("fixed_assets.sources"), 1)[1].split("</div>", 1)[0]
    assert t("fixed_assets.origin.contributed") in card and t("fixed_assets.no_sources") not in card
    assert f'href="/accounting?q={quote(journal, safe="")}"' in card


async def test_u34_the_origin_comes_from_the_first_movement_even_past_the_first_page(owner_ui, monkeypatch):
    import ui.api_client as api

    car = await _managed_car(owner_ui)
    await _review_and_confirm(owner_ui, "run", {"month": "2026-09"})
    assert (await owner_ui.api.get(f"{API}/assets/{car}/movements")).json()["total"] > 1
    real = api.fixed_assets_get

    async def one_per_page(token, path, params=None):
        # A page holding only the newest movement, as a long-lived asset's first page holds only recent ones.
        if path.endswith("/movements") and not params:
            params = {"limit": 1}
        return await real(token, path, params)

    monkeypatch.setattr(api, "fixed_assets_get", one_per_page)
    page = (await owner_ui.get(f"/fixed-assets/assets/{car}")).text
    card = page.split(t("fixed_assets.sources"), 1)[1].split("</div>", 1)[0]
    assert t("fixed_assets.origin.contributed") in card and 'href="/accounting?q=' in card


async def test_u23_correct_shows_only_the_chosen_methods_settings_and_request_beside_review(owner_ui):
    car = await _managed_car(owner_ui)
    page = (await owner_ui.get(f"/fixed-assets/assets/{car}/correct")).text
    assert "<fieldset" not in page and "<legend" not in page
    groups = dict(re.findall(r'<div data-method="([a-z_]+)"([^>]*)>', page))
    assert groups, "each method with settings has its own group"
    # The car is on straight line, which has no settings, so every group starts hidden.
    assert all("hidden" in attrs for attrs in groups.values()), groups
    assert 'onchange=' in page.split('name="method"', 1)[1].split(">", 1)[0]
    actions = page.split('class="form-actions"', 1)[1].split("</div>", 1)[0]
    assert t("fixed_assets.review") in actions and t("fixed_assets.request_correction") in actions
    assert "fixed_assets." not in page


async def test_forms_open_on_the_companys_today_not_the_servers(owner_ui):
    """A form's date starts on the company's today in its business timezone. Fourteen hours
    ahead of UTC and twelve behind are never on the same day, so at least one of them is not
    the server's own date whenever this runs."""
    from celerp.services.business_time import business_date_of

    car = await _managed_car(owner_ui)
    for tz in ("Etc/GMT-14", "Etc/GMT+12"):
        r = await owner_ui.api.patch("/companies/me", json={"settings": {"timezone": tz}})
        assert r.status_code == 200, r.text
        today = business_date_of(None, tz)
        for path in (f"/fixed-assets/assets/{car}/dispose", f"/fixed-assets/assets/{car}/sell",
                     f"/fixed-assets/assets/{car}/move", "/fixed-assets/checks"):
            page = await owner_ui.get(path)
            assert page.status_code == 200, (path, page.text)
            assert f'value="{today}"' in page.text or f'value="{today[:8]}01"' in page.text, (tz, path)
        upkeep = (await owner_ui.get(f"/fixed-assets/assets/{car}")).text.split('id="fa-upkeep"', 1)[1]
        assert f'value="{today}"' in upkeep, tz


async def test_runs_page_reviews_posts_and_reverses_a_run(owner_ui):
    await _demo(owner_ui)
    await owner_ui.post("/fixed-assets/start")
    empty = await owner_ui.get("/fixed-assets/runs")
    assert empty.status_code == 200 and t("fixed_assets.runs_empty") in empty.text
    assert "fixed_assets." not in empty.text, "every label on the page reads as words"

    await _managed_car(owner_ui)
    review = await owner_ui.post("/fixed-assets/op/run/review",
                                 data={"month": "2026-09", "idempotency_key": str(uuid.uuid4())})
    assert "2026-08" in review.text and "2026-09" in review.text and "6400" in review.text
    assert "Delivery car" in review.text, "the Review says which asset each month's figure comes from"
    assert await _review_and_confirm(owner_ui, "run", {"month": "2026-09"}) == "/fixed-assets/runs"

    page = await owner_ui.get("/fixed-assets/runs")
    assert t("fixed_assets.run_status.posted") in page.text
    assert page.text.index(">2026-09<") < page.text.index(">2026-08<"), "newest month first"
    assert "Assets held" not in page.text, "a count of held assets beside the total reads as the assets in it"
    detail = await owner_ui.get("/fixed-assets/runs/2026-09")
    assert detail.status_code == 200 and t("fixed_assets.reverse_run") in detail.text
    assert t("fixed_assets.run_assets_title") in detail.text and "Delivery car" in detail.text
    assert "fixed_assets." not in detail.text

    assert await _review_and_confirm(owner_ui, "reverse-run", {"month": "2026-09"}) == "/fixed-assets/runs/2026-09"
    runs = {r["month"]: r["status"] for r in (await owner_ui.api.get(f"{API}/runs")).json()["items"]}
    assert runs == {"2026-08": "posted", "2026-09": "reversed"}


async def test_asset_page_shows_what_holds_it_and_takes_skips_and_readings(owner_ui):
    car = await _managed_car(owner_ui)
    skipped = await owner_ui.post(f"/fixed-assets/assets/{car}/skip",
                                  data={"month": "2026-08", "reason": "In the workshop"})
    assert skipped.headers.get("HX-Redirect") == f"/fixed-assets/assets/{car}", skipped.text
    refused = await owner_ui.post(f"/fixed-assets/assets/{car}/skip", data={"month": "2026-09", "reason": ""})
    assert "HX-Redirect" not in refused.headers and "error" in refused.headers.get("HX-Trigger", "")
    await _review_and_confirm(owner_ui, "run", {"month": "2026-08"})

    page = await owner_ui.get(f"/fixed-assets/assets/{car}")
    assert t("fixed_assets.exception_title") in page.text and t("fixed_assets.action.skip") in page.text
    assert "In the workshop" in page.text and t("fixed_assets.withdraw_skip") in page.text
    assert "fixed_assets." not in page.text
    register = await owner_ui.get("/fixed-assets")
    assert t("fixed_assets.attention_title") in register.text and f"/fixed-assets/assets/{car}" in register.text

    withdrawn = await owner_ui.delete(f"/fixed-assets/assets/{car}/skip/2026-08")
    assert withdrawn.headers.get("HX-Redirect") == f"/fixed-assets/assets/{car}"
    assert (await owner_ui.api.get(f"{API}/assets/{car}")).json().get("skips") == {}

    metered = (await owner_ui.api.post(f"{API}/classes", json={
        "name": "Metered machines", "kind": "tangible", "method": "units_of_use",
        "params": {"total_units": "10000"}})).json()
    href = await _review_and_confirm(owner_ui, "add-asset", {
        "kind": "contributed", "name": "Press", "class_id": metered["id"], "available_date": "2026-08-10",
        "cost": "10000", "credit_account": "3100"})
    press = href.rsplit("/", 1)[1]
    assert t("fixed_assets.record_reading") in (await owner_ui.get(href)).text
    read = await owner_ui.post(f"/fixed-assets/assets/{press}/readings", data={"period": "2026-09", "units": "120"})
    assert read.headers.get("HX-Redirect") == href, read.text
    assert (await owner_ui.api.get(f"{API}/assets/{press}")).json()["readings"] == {"2026-09": "120"}


async def test_settings_page_saves_the_run_day(owner_ui):
    await _demo(owner_ui)
    await owner_ui.post("/fixed-assets/start")
    page = await owner_ui.get("/settings/fixed-assets")
    assert t("fixed_assets.run_day") in page.text
    await owner_ui.post("/settings/fixed-assets", data={"run_day": "5"})
    assert (await owner_ui.api.get(f"{API}/setup")).json()["settings"]["run_day"] == 5
    bad = await owner_ui.post("/settings/fixed-assets", data={"run_day": "31"})
    assert "error" in bad.headers.get("HX-Trigger", "")


async def test_u5_the_class_table_names_accounts_and_keeps_row_actions_in_a_menu(owner_ui):
    await _demo(owner_ui)
    await owner_ui.post("/fixed-assets/start")
    chart = {a["code"]: a["name"] for a in (await owner_ui.api.get("/accounting/chart")).json()["items"]}
    vehicles = next(c for c in (await owner_ui.api.get(f"{API}/classes")).json()["items"] if c["name"] == "Vehicles")
    page = await owner_ui.get("/settings/fixed-assets")
    row = page.text[page.text.index(f'id="cell-{vehicles["id"].replace(":", "-")}-name"'):]
    row = row[:row.index("</tr>")]
    assert f"1210 {chart['1210']}" in row and f"6400 {chart['6400']}" in row, "accounts read as code and name"
    assert 'class="row-menu"' in row and t("fixed_assets.apply_to_assets") in row
    assert "btn btn--sm" not in row, "row actions sit in the row menu, not as buttons widening the table"
    assert t("label.dblclick_to_edit").split()[0] in t("fixed_assets.classes_hint")
    assert t("fixed_assets.tax_regimes_hint") not in page.text, "with no regime offered, no hint repeats the statement"

    saved = await owner_ui.patch(f"/settings/fixed-assets/classes/{vehicles['id']}/depreciation_account",
                                 data={"value": "6400"})
    assert f"6400 {chart['6400']}" in saved.text, "a saved account cell still reads as code and name"


async def _car(ui, name: str) -> str:
    vehicles = next(c for c in (await ui.api.get(f"{API}/classes")).json()["items"] if c["name"] == "Vehicles")
    href = await _review_and_confirm(ui, "add-asset", {
        "kind": "contributed", "name": name, "class_id": vehicles["id"], "available_date": "2025-06-15",
        "cost": "12000", "credit_account": "3100"})
    return href.rsplit("/", 1)[1]


async def test_correct_an_asset_then_the_corrections_pages_and_report(owner_ui):
    await _demo(owner_ui)
    await owner_ui.post("/fixed-assets/start")
    r = await owner_ui.api.patch(f"{API}/settings", json={"first_managed_period": "2025-01-01"})
    assert r.status_code == 200, r.text
    empty = await owner_ui.get("/fixed-assets/corrections")
    assert empty.status_code == 200 and t("fixed_assets.corrections_empty") in empty.text
    car = await _car(owner_ui, "Delivery car")
    await _review_and_confirm(owner_ui, "run", {"month": "2025-12"})
    await _review_and_confirm(owner_ui, "run", {"month": "2026-03"})

    for field in ("settings", "cost", "available_date", "opening_values", "maintenance", "link_source"):
        form = await owner_ui.get(f"/fixed-assets/assets/{car}/correct", params={"field": field})
        assert form.status_code == 200 and t("fixed_assets.request_correction") in form.text, field
        assert "fixed_assets." not in form.text, field

    # An error: restates 2025 and posts the difference on the chosen day.
    review = await owner_ui.post("/fixed-assets/op/correct/review", data={
        "asset_id": car, "field": "cost", "cost": "15000", "posting_date": "2026-04-20",
        "prior_years": "retained_earnings", "idempotency_key": str(uuid.uuid4())})
    assert t("fixed_assets.classification.error") in review.text and t("fixed_assets.prior_years_title") in review.text
    assert "1,750.00" in review.text and "fixed_assets." not in review.text
    href = await _review_and_confirm(owner_ui, "correct", {
        "asset_id": car, "field": "cost", "cost": "15000", "posting_date": "2026-04-20",
        "prior_years": "retained_earnings"})
    assert href.startswith("/fixed-assets/corrections/")
    first = href.rsplit("/", 1)[1]
    detail = await owner_ui.get(href)
    assert detail.status_code == 200 and t("fixed_assets.undo_correction") in detail.text
    assert "fixed_assets." not in detail.text

    # A change in estimate from the settings tab, from the month after the last run.
    policy = (await owner_ui.api.get(f"{API}/assets/{car}")).json()["policy"]
    was = {f"was:{k}": "" if policy.get(k) is None else str(policy[k])
           for k in ("method", "life_months", "residual_percent", "convention")}
    estimate = {"asset_id": car, "field": "settings", **was, "was:params": "{}", "method": policy["method"],
                "life_months": str(int(policy["life_months"]) + 12), "residual_percent": was["was:residual_percent"],
                "convention": policy["convention"]}
    review = await owner_ui.post("/fixed-assets/op/correct/review", data={**estimate,
                                                                          "idempotency_key": str(uuid.uuid4())})
    assert t("fixed_assets.classification.estimate") in review.text and t("fixed_assets.future_title") in review.text
    await _review_and_confirm(owner_ui, "correct", estimate)
    assert (await owner_ui.api.get(f"{API}/assets/{car}")).json()["policy"]["life_months"] == \
        int(policy["life_months"]) + 12

    asset = await owner_ui.get(f"/fixed-assets/assets/{car}")
    assert t("fixed_assets.corrections_title") in asset.text and f"/fixed-assets/corrections/{first}" in asset.text
    listing = await owner_ui.get("/fixed-assets/corrections")
    assert t("fixed_assets.correction_state.applied") in listing.text and "fixed_assets." not in listing.text
    assert listing.text.index(t("fixed_assets.classification.estimate")) < \
        listing.text.index(f"/fixed-assets/corrections/{first}"), "newest first"

    # The immaterial option: the earlier years' effect through this year's profit and loss.
    second = await _car(owner_ui, "Second car")
    immaterial = await _review_and_confirm(owner_ui, "correct", {
        "asset_id": second, "field": "cost", "cost": "15000", "posting_date": "2026-04-20",
        "prior_years": "profit_and_loss", "immaterial_reason": "Below the materiality threshold"})
    report = await owner_ui.get("/fixed-assets/reports/prior-period-adjustments", params={"year": "2025"})
    assert report.status_code == 200 and t("fixed_assets.ppa_title") in report.text and ">2025</h2>" in report.text
    assert "Below the materiality threshold" in report.text and "1,750.00" in report.text
    assert "fixed_assets." not in report.text
    csv_file = await owner_ui.get("/fixed-assets/reports/prior-period-adjustments.csv", params={"year": "2025"})
    assert csv_file.status_code == 200 and csv_file.headers["content-type"].startswith("text/csv")
    assert "1750" in csv_file.text and "Below the materiality threshold" in csv_file.text
    none = await owner_ui.get("/fixed-assets/reports/prior-period-adjustments", params={"year": "2024"})
    assert t("fixed_assets.ppa_empty", year=2024) in none.text

    # Undo the latest correction on an asset: its cost goes back. An earlier one with a later correction
    # on the same asset stays put, so the history is unwound newest first.
    assert await _review_and_confirm(owner_ui, "undo-correction", {
        "correction_id": immaterial.rsplit("/", 1)[1]}) == immaterial
    assert t("fixed_assets.correction_state.undone") in (await owner_ui.get(immaterial)).text
    assert float((await owner_ui.api.get(f"{API}/assets/{second}")).json()["cost"]) == 12000
    blocked = await owner_ui.post("/fixed-assets/op/undo-correction/review", data={
        "correction_id": first, "idempotency_key": str(uuid.uuid4())})
    assert t("fixed_assets.confirm") not in blocked.text and "FA-0001" in blocked.text

    # A request waits for someone to apply it; withdrawing discards it.
    asked = await owner_ui.post(f"/fixed-assets/assets/{second}/request-correction", data={
        "field": "available_date", "available_date": "2025-07-01", "reason": "Delivered in July",
        "idempotency_key": (ask_key := str(uuid.uuid4()))})
    assert asked.headers.get("HX-Redirect", "").startswith("/fixed-assets/corrections/"), asked.text
    # The same form sent again (a lost response, a double click) is the same request.
    again = await owner_ui.post(f"/fixed-assets/assets/{second}/request-correction", data={
        "field": "available_date", "available_date": "2025-07-01", "reason": "Delivered in July",
        "idempotency_key": ask_key})
    assert again.headers.get("HX-Redirect") == asked.headers["HX-Redirect"], again.text
    waiting = await owner_ui.get(asked.headers["HX-Redirect"])
    assert t("fixed_assets.apply_correction") in waiting.text and t("fixed_assets.withdraw_correction") in waiting.text
    assert "fixed_assets." not in waiting.text
    assert t("fixed_assets.notice.corrections.body", count=1) in (await owner_ui.get("/fixed-assets/corrections")).text
    withdrawn = await owner_ui.post(asked.headers["HX-Redirect"] + "/withdraw")
    assert withdrawn.headers.get("HX-Redirect") == asked.headers["HX-Redirect"], withdrawn.text
    assert t("fixed_assets.correction_closed") in (await owner_ui.get(asked.headers["HX-Redirect"])).text

    # Applying a class's settings to its assets has its own page.
    vehicles = next(c for c in (await owner_ui.api.get(f"{API}/classes")).json()["items"] if c["name"] == "Vehicles")
    apply = await owner_ui.get(f"/fixed-assets/classes/{vehicles['id']}/apply")
    assert apply.status_code == 200 and t("fixed_assets.apply_to_assets_hint", name="Vehicles") in apply.text
    assert "fixed_assets." not in apply.text


async def test_u1_dispose_review_lists_what_leaves_the_books(owner_ui):
    car = await _managed_car(owner_ui)
    review = await owner_ui.post("/fixed-assets/op/dispose/review", data={
        "asset_id": car, "date": "2026-10-05", "reason": "scrapped", "idempotency_key": str(uuid.uuid4())})
    for key in ("cost", "accumulated_depreciation"):
        assert t(f"fixed_assets.leaving.{key}") in review.text, key
    assert "12,000.00" in review.text
    # Nothing was impaired, so no impairment row.
    assert t("fixed_assets.leaving.accumulated_impairment") not in review.text
    assert "fixed_assets." not in review.text


async def test_dispose_from_the_asset_page_and_undo_it(owner_ui):
    car = await _managed_car(owner_ui)
    page = await owner_ui.get(f"/fixed-assets/assets/{car}")
    assert f'href="/fixed-assets/assets/{car}/dispose"' in page.text

    form = await owner_ui.get(f"/fixed-assets/assets/{car}/dispose")
    assert form.status_code == 200 and t("fixed_assets.dispose_hint") in form.text
    assert 'name="percent"' in form.text and t("fixed_assets.disposal_reason.donated") in form.text
    assert "fixed_assets." not in form.text

    review = await owner_ui.post("/fixed-assets/op/dispose/review", data={
        "asset_id": car, "date": "2026-10-05", "reason": "scrapped", "idempotency_key": str(uuid.uuid4())})
    assert t("fixed_assets.loss") in review.text and "fixed_assets." not in review.text
    href = await _review_and_confirm(owner_ui, "dispose", {"asset_id": car, "date": "2026-10-05",
                                                           "reason": "scrapped"})
    assert href == f"/fixed-assets/assets/{car}"
    page = await owner_ui.get(href)
    disposal_card = f'<h2 class="section-title">{t("fixed_assets.disposal_title")}</h2>'
    assert disposal_card in page.text and t("fixed_assets.status.disposed") in page.text
    assert t("fixed_assets.movement.disposal") in page.text and t("fixed_assets.undo_disposal") in page.text
    assert f'href="/fixed-assets/assets/{car}/dispose"' not in page.text, "a disposed asset offers no second disposal"

    await _review_and_confirm(owner_ui, "undo-disposal", {"asset_id": car})
    page = await owner_ui.get(href)
    assert disposal_card not in page.text and t("fixed_assets.status.active") in page.text


async def test_dispose_of_several_from_the_register(owner_ui):
    car = await _managed_car(owner_ui)
    vehicles = next(c for c in (await owner_ui.api.get(f"{API}/classes")).json()["items"] if c["name"] == "Vehicles")
    van = (await _review_and_confirm(owner_ui, "add-asset", {
        "kind": "contributed", "name": "Van", "class_id": vehicles["id"], "available_date": "2026-08-10",
        "cost": "8000", "credit_account": "3100"})).rsplit("/", 1)[1]
    register = await owner_ui.get("/fixed-assets")
    assert 'class="bulk-select"' in register.text and "/fixed-assets/dispose/selected" in register.text

    chosen = await owner_ui.post("/fixed-assets/dispose/selected", data={"selected": [car, van]})
    target = chosen.headers["HX-Redirect"]
    assert target.startswith("/fixed-assets/dispose?ids=")
    form = await owner_ui.get(target)
    assert form.status_code == 200 and t("fixed_assets.dispose_several_title", n=2) in form.text
    assert 'name="percent"' not in form.text and "Van" in form.text and "fixed_assets." not in form.text

    href = await _review_and_confirm(owner_ui, "dispose", {"asset_ids": [car, van], "date": "2026-10-05",
                                                           "reason": "donated"})
    assert href == "/fixed-assets"
    for asset_id in (car, van):
        assert (await owner_ui.api.get(f"{API}/assets/{asset_id}")).json()["status"] == "disposed"


async def test_sell_from_the_asset_page_then_credit_the_sale(owner_ui):
    car = await _managed_car(owner_ui)
    page = await owner_ui.get(f"/fixed-assets/assets/{car}")
    assert f'href="/fixed-assets/assets/{car}/sell"' in page.text

    form = await owner_ui.get(f"/fixed-assets/assets/{car}/sell")
    assert form.status_code == 200 and t("fixed_assets.sell_hint") in form.text
    assert "fixed_assets." not in form.text
    review = await owner_ui.post("/fixed-assets/op/sell/review", data={
        "asset_id": car, "date": "2026-10-05", "price": "9000", "contact_id": await _buyer(owner_ui),
        "idempotency_key": str(uuid.uuid4())})
    assert t("fixed_assets.loss") in review.text and "1160" in review.text
    href = await _review_and_confirm(owner_ui, "sell", {"asset_id": car, "date": "2026-10-05", "price": "9000",
                                                       "contact_id": await _buyer(owner_ui)})
    assert href.startswith("/docs/")
    invoice_id = href.removeprefix("/docs/")
    number = (await owner_ui.api.get(f"/docs/{invoice_id}")).json()["ref_id"]

    page = await owner_ui.get(f"/fixed-assets/assets/{car}")
    assert t("fixed_assets.sale_state.pending") in page.text
    assert t("fixed_assets.sale_pending_hint", invoice=number) in page.text
    assert t("fixed_assets.cancel_sale") in page.text and t("fixed_assets.sale_journal_pending") in page.text
    assert f'href="/fixed-assets/assets/{car}/sell"' not in page.text, "a pending sale offers no second sale"
    assert f'href="/fixed-assets/assets/{car}/dispose"' not in page.text
    assert "fixed_assets." not in page.text

    finalized = await owner_ui.api.post(f"/docs/{invoice_id}/finalize")
    assert finalized.status_code == 200, finalized.text
    page = await owner_ui.get(f"/fixed-assets/assets/{car}")
    assert t("fixed_assets.sale_state.sold") in page.text and t("fixed_assets.credit_sale") in page.text
    assert t("fixed_assets.undo_disposal") not in page.text, "a sale is undone through its invoice"
    assert "fixed_assets." not in page.text

    href = await _review_and_confirm(owner_ui, "sale-credit", {"asset_id": car, "kind": "adjust", "amount": "1000",
                                                              "date": "2026-10-20"})
    credit_note_id = href.removeprefix("/docs/")
    assert (await owner_ui.api.post(f"/docs/{credit_note_id}/finalize")).status_code == 200
    page = await owner_ui.get(f"/fixed-assets/assets/{car}")
    assert t("fixed_assets.credit_notes") in page.text and t("fixed_assets.credit_kind.adjust") in page.text
    assert f'href="/docs/{credit_note_id}"' in page.text

    # The invoice and its credit note lead back to the sale on the asset's page.
    sale_href = f'href="/fixed-assets/assets/{car}#fa-sale-{car}"'
    for doc_id in (invoice_id, credit_note_id):
        doc_page = await owner_ui.get(f"/docs/{doc_id}")
        assert doc_page.status_code == 200 and sale_href in doc_page.text
        assert t("fixed_assets.correct_sale") in doc_page.text


def _line_inputs(html: str, marker: str) -> list[str]:
    """The values of a draft's line inputs carrying ``marker`` (an attribute)."""
    return [re.search(r'value="([^"]*)"', tag).group(1) for tag in re.findall(r"<input[^>]*>", html) if marker in tag]


async def test_u36_a_sale_invoice_line_shows_its_price_and_total_as_money(owner_ui):
    car = await _managed_car(owner_ui)
    # The buyer's name as well as the contact, so the same sale also runs on the earlier form.
    href = await _review_and_confirm(owner_ui, "sell", {"asset_id": car, "date": "2026-10-05", "price": "9000",
                                                       "contact_id": await _buyer(owner_ui),
                                                       "contact_name": "Brightwater Retail"})
    page = (await owner_ui.get(href)).text
    # The blank add-a-line row holds "0"; the sale's own line is the only other one.
    assert [v for v in _line_inputs(page, 'data-name="unit_price"') if v != "0"] == ["9000.00"]
    assert [v for v in _line_inputs(page, 'data-name="line_total"') if v != "0"] == ["9000.00"]


async def test_u39_a_toast_says_what_changed(owner_ui):
    car = await _managed_car(owner_ui)
    added = await _confirmed(owner_ui, "add-to-asset", {"asset_id": car, "kind": "improvement", "amount": "1200",
                                                        "credit_account": "3100", "date": "2026-09-15"})
    assert _toast(added) == t("fixed_assets.toast.add_to_asset") != "fixed_assets.toast.add_to_asset"
    reminded = await owner_ui.post(f"/fixed-assets/assets/{car}/reminders",
                                   data={"kind": "maintenance", "due": "2026-12-01", "detail": "Oil change"})
    assert _toast(reminded) == t("fixed_assets.toast.reminder_added") != "fixed_assets.toast.reminder_added"


async def test_u39_every_operation_has_its_own_toast():
    from celerp_fixed_assets.ui_routes import _OPERATIONS
    said = {action: t(f"fixed_assets.toast.{action.replace('-', '_')}") for action in _OPERATIONS}
    assert not [a for a, text in said.items() if text.startswith("fixed_assets.")], said
    assert len(set(said.values())) == len(said), said


async def test_u38_a_sale_credit_note_names_and_links_the_invoice_it_credits(owner_ui):
    car = await _managed_car(owner_ui)
    href = await _review_and_confirm(owner_ui, "sell", {"asset_id": car, "date": "2026-10-05", "price": "9000",
                                                       "contact_id": await _buyer(owner_ui),
                                                       "contact_name": "Brightwater Retail"})
    invoice_id = href.removeprefix("/docs/")
    assert (await owner_ui.api.post(f"/docs/{invoice_id}/finalize")).status_code == 200
    number = (await owner_ui.api.get(f"/docs/{invoice_id}")).json()["ref_id"]
    href = await _review_and_confirm(owner_ui, "sale-credit", {"asset_id": car, "kind": "adjust", "amount": "1000",
                                                              "date": "2026-10-20"})
    page = (await owner_ui.get(href)).text
    bar = page[page.index('class="doc-meta-bar"'):page.index('class="doc-row"')]
    assert doc_type_label("invoice") in bar
    assert re.search(rf'<a href="/docs/{re.escape(invoice_id)}"[^>]*>\s*{re.escape(number)}\s*</a>', bar), bar


async def test_u8_u9_the_sale_card_opens_the_draft_credit_note_and_folds_a_returned_sale(owner_ui):
    car = await _managed_car(owner_ui)
    href = await _review_and_confirm(owner_ui, "sell", {"asset_id": car, "date": "2026-10-05", "price": "9000",
                                                       "contact_id": await _buyer(owner_ui)})
    invoice_id = href.removeprefix("/docs/")
    assert (await owner_ui.api.post(f"/docs/{invoice_id}/finalize")).status_code == 200
    href = await _review_and_confirm(owner_ui, "sale-credit", {"asset_id": car, "kind": "adjust", "amount": "1000",
                                                              "date": "2026-10-20"})
    draft_id = href.removeprefix("/docs/")
    number = (await owner_ui.api.get(f"/docs/{draft_id}")).json()["ref_id"]
    page = await owner_ui.get(f"/fixed-assets/assets/{car}")
    assert t("fixed_assets.credit_open_draft", number=number) in page.text
    assert f'href="/docs/{draft_id}"' in page.text
    assert "/fixed-assets/op/sale-credit/review" not in page.text, "the open draft is finished, not drafted again"
    # Two tabs: the second Review says it opens the draft, and Confirm leads to it.
    form = {"asset_id": car, "kind": "adjust", "amount": "500", "date": "2026-10-21"}
    review = await owner_ui.post("/fixed-assets/op/sale-credit/review",
                                 data={**form, "idempotency_key": str(uuid.uuid4())})
    assert t("fixed_assets.credit_open_draft", number=number) in review.text
    assert await _review_and_confirm(owner_ui, "sale-credit", form) == f"/docs/{draft_id}"

    assert (await owner_ui.api.delete(f"/docs/{draft_id}")).status_code == 200
    href = await _review_and_confirm(owner_ui, "sale-credit", {"asset_id": car, "kind": "returned",
                                                              "date": "2026-10-22"})
    assert (await owner_ui.api.post(f"/docs/{href.removeprefix('/docs/')}/finalize")).status_code == 200
    page = await owner_ui.get(f"/fixed-assets/assets/{car}")
    card = page.text[page.text.index('id="fa-sale-'):]
    assert "<details" in card[:400], "a returned sale folds into history"
    assert t("fixed_assets.sale_returned_summary", invoice=(await owner_ui.api.get(
        f"/docs/{invoice_id}")).json()["doc_number"]) in card
    assert t("fixed_assets.remaining") in card and t("fixed_assets.gain_or_loss") not in card.split("</details>")[0]
    assert "fixed_assets." not in page.text


def _effect(review) -> str:
    found = re.search(r'id="fa-effect"[^>]*>([^<]+)<', review.text)
    assert found, review.text
    return html.unescape(found.group(1))


async def _review(ui, action: str, form: dict):
    review = await ui.post(f"/fixed-assets/op/{action}/review", data={**form, "idempotency_key": str(uuid.uuid4())})
    assert review.status_code == 200, review.text
    return review


async def test_a_sale_review_says_what_confirm_does_to_the_invoice_and_the_asset(owner_ui):
    car = await _managed_car(owner_ui)
    number = (await owner_ui.api.get(f"{API}/assets/{car}")).json()["number"]
    buyer = (await owner_ui.api.get("/crm/contacts", params={"limit": 1})).json()["items"][0]
    href = await _review_and_confirm(owner_ui, "sell", {"asset_id": car, "date": "2026-10-05", "price": "9000",
                                                       "contact_id": buyer["id"]})
    assert (await owner_ui.api.delete(href)).status_code == 200
    reissue = _effect(await _review(owner_ui, "sale-reissue", {"asset_id": car}))
    assert reissue.startswith(t("fixed_assets.effect.sale_reissue", number=number, price="", buyer=buyer["name"],
                                date="2026-10-05").split(" at ")[0])
    assert "9,000.00" in reissue and buyer["name"] in reissue and "2026-10-05" in reissue

    draft = (await _review_and_confirm(owner_ui, "sale-reissue", {"asset_id": car})).removeprefix("/docs/")
    invoice = (await owner_ui.api.get(f"/docs/{draft}")).json()["ref_id"]
    assert _effect(await _review(owner_ui, "sale-cancel", {"asset_id": car})) == t(
        "fixed_assets.effect.sale_cancel_voids", number=number, invoice=invoice)

    assert (await owner_ui.api.post(f"/docs/{draft}/finalize")).status_code == 200
    invoice = (await owner_ui.api.get(f"/docs/{draft}")).json()["doc_number"]
    adjust = _effect(await _review(owner_ui, "sale-credit", {"asset_id": car, "kind": "adjust", "amount": "1000",
                                                             "date": "2026-10-20"}))
    assert invoice in adjust and "1,000.00" in adjust and number in adjust
    assert adjust.endswith(t("fixed_assets.effect.sale_credit.adjust", number=number, invoice="",
                             amount="").split(". ", 1)[1])
    returned = _effect(await _review(owner_ui, "sale-credit", {"asset_id": car, "kind": "returned",
                                                               "date": "2026-10-20"}))
    assert invoice in returned and "9,000.00" in returned
    assert returned.endswith(t("fixed_assets.effect.sale_credit.returned", number=number, invoice="",
                               amount="").split(". ", 1)[1])


async def test_a_deleted_sale_draft_is_reissued_or_cancelled_from_the_asset_page(owner_ui):
    car = await _managed_car(owner_ui)
    href = await _review_and_confirm(owner_ui, "sell", {"asset_id": car, "date": "2026-10-05", "price": "9000",
                                                       "contact_id": await _buyer(owner_ui)})
    assert (await owner_ui.api.delete(href)).status_code == 200
    page = await owner_ui.get(f"/fixed-assets/assets/{car}")
    assert t("fixed_assets.sale_draft_deleted") in page.text and t("fixed_assets.reissue") in page.text

    again = await _review_and_confirm(owner_ui, "sale-reissue", {"asset_id": car})
    assert again.startswith("/docs/") and again != href
    page = await owner_ui.get(f"/fixed-assets/assets/{car}")
    assert t("fixed_assets.sale_draft_deleted") not in page.text and t("fixed_assets.reissue") not in page.text

    assert await _review_and_confirm(owner_ui, "sale-cancel", {"asset_id": car}) == f"/fixed-assets/assets/{car}"
    page = await owner_ui.get(f"/fixed-assets/assets/{car}")
    assert t("fixed_assets.cancel_sale") not in page.text and f'href="/fixed-assets/assets/{car}/sell"' in page.text


async def test_u10_u11_a_sale_that_cannot_complete_says_why_and_what_to_do(owner_ui):
    car = await _managed_car(owner_ui)
    href = await _review_and_confirm(owner_ui, "sell", {"asset_id": car, "date": "2026-10-05", "price": "9000",
                                                       "contact_id": await _buyer(owner_ui)})
    invoice_id = href.removeprefix("/docs/")
    number = (await owner_ui.api.get(href)).json()["ref_id"]
    # Dated before the car was available, the invoice cannot complete the sale: said as what
    # finalizing it would meet and how to fix it, never as an error nobody caused.
    r = await owner_ui.api.patch(href, json={"fields_changed": {"issue_date": {"new": "2026-08-01"}}})
    assert r.status_code == 200, r.text
    page = await owner_ui.get(f"/fixed-assets/assets/{car}")
    card = page.text[page.text.index('id="fa-sale-'):]
    assert "flash--error" not in card
    assert t("fixed_assets.sale_blocked", invoice=number) in card and "before" in card
    assert f'href="/docs/{invoice_id}"' in card
    r = await owner_ui.api.patch(href, json={"fields_changed": {"issue_date": {"new": "2026-10-05"}}})
    assert r.status_code == 200, r.text
    assert (await owner_ui.api.post(f"{href}/finalize")).status_code == 200
    final = (await owner_ui.api.get(href)).json()["doc_number"]
    assert (await owner_ui.api.post(f"{href}/void", json={})).status_code == 200
    page = await owner_ui.get(f"/fixed-assets/assets/{car}")
    assert t("fixed_assets.sale_invoice_void", invoice=final) in page.text
    assert t("fixed_assets.sale_pending_hint", invoice=final) not in page.text
    assert t("fixed_assets.reissue") in page.text and t("fixed_assets.cancel_sale") in page.text
    assert "fixed_assets." not in page.text


def _row_label(label: str, html: str) -> bool:
    """Whether a detail table has a row labelled exactly so."""
    return re.search(rf"<th[^>]*>{re.escape(label)}</th>", html) is not None


async def _class(ui, name: str) -> dict:
    return next(c for c in (await ui.api.get(f"{API}/classes")).json()["items"] if c["name"] == name)


async def test_impair_from_the_asset_page_then_undo_it(owner_ui):
    car = await _managed_car(owner_ui)
    page = await owner_ui.get(f"/fixed-assets/assets/{car}")
    assert f'href="/fixed-assets/assets/{car}/remeasure"' in page.text
    assert f'href="/fixed-assets/assets/{car}/add"' in page.text

    revalue = await owner_ui.get(f"/fixed-assets/assets/{car}/remeasure")
    assert revalue.status_code == 200 and t("fixed_assets.cost_model_hint") in revalue.text
    assert "fixed_assets." not in revalue.text
    form = await owner_ui.get(f"/fixed-assets/assets/{car}/remeasure", params={"op": "impairment"})
    assert t("fixed_assets.remeasure_hint.impairment") in form.text and 'name="recoverable_amount"' in form.text

    data = {"asset_id": car, "date": "2026-09-30", "recoverable_amount": "9000", "basis": "Dealer quote",
            "idempotency_key": str(uuid.uuid4())}
    review = await owner_ui.post("/fixed-assets/op/impair/review", data=data)
    assert t("fixed_assets.remeasure.new_carrying_amount") in review.text and "fixed_assets." not in review.text
    href = await _review_and_confirm(owner_ui, "impair", data)
    assert href == f"/fixed-assets/assets/{car}"
    page = await owner_ui.get(href)
    assert t("fixed_assets.movement.impairment") in page.text and t("fixed_assets.undo_latest") in page.text
    movements = page.text.split(f">{t('fixed_assets.movements')}<", 1)[1]
    assert t("fixed_assets.accumulated_impairment") in movements, "the impairment shows in the movements"
    assert "fixed_assets." not in page.text

    await _review_and_confirm(owner_ui, "undo-latest", {"asset_id": car})
    page = await owner_ui.get(href)
    assert t("fixed_assets.undo_latest") not in page.text
    assert (await owner_ui.api.get(f"{API}/assets/{car}")).json()["accumulated_impairment"] in ("0", "0.00")


def _register(html: str) -> tuple[list[str], list[list[str]]]:
    """The register table's headings and its rows' cell texts."""
    table = re.search(r'<table[^>]*id="fa-register".*?</table>', html, re.S).group(0)

    def text(cell: str) -> str:
        return re.sub(r"<[^>]+>", "", cell).strip()

    head = [text(c) for c in re.findall(r"<th\b.*?</th>", table, re.S)]
    rows = [[text(c) for c in re.findall(r"<td\b.*?</td>", row, re.S)]
            for row in re.findall(r"<tr\b[^>]*data-row.*?</tr>", table, re.S)]
    return head, rows


async def test_u27_the_register_shows_impairment_so_each_row_adds_up_to_its_net_book_value(owner_ui):
    car = await _managed_car(owner_ui)
    await _review_and_confirm(owner_ui, "impair", {
        "asset_id": car, "date": "2026-09-30", "recoverable_amount": "9000", "basis": "Dealer quote",
        "idempotency_key": str(uuid.uuid4())})
    head, rows = _register((await owner_ui.get("/fixed-assets")).text)
    columns = [t(f"fixed_assets.{k}") for k in ("cost", "accumulated_depreciation", "accumulated_impairment", "nbv")]
    assert all(c in head for c in columns), head
    at = [head.index(c) for c in columns]
    assert rows
    for row in rows:
        cost, depreciation, impairment, nbv = (Decimal(re.sub(r"[^0-9.-]", "", row[i]) or "0") for i in at)
        assert cost - depreciation - impairment == nbv, row
    assert any(Decimal(re.sub(r"[^0-9.-]", "", row[at[2]]) or "0") > 0 for row in rows), rows


async def test_add_an_improvement_and_see_the_repair_route(owner_ui):
    car = await _managed_car(owner_ui)
    repair = await owner_ui.get(f"/fixed-assets/assets/{car}/add", params={"kind": "repair"})
    assert t("fixed_assets.add_hint.repair") in repair.text and 'href="/fixed-assets/intake"' in repair.text
    replacement = await owner_ui.get(f"/fixed-assets/assets/{car}/add", params={"kind": "replacement"})
    assert t("fixed_assets.old_part_estimate") in replacement.text and "fixed_assets." not in replacement.text

    form = await owner_ui.get(f"/fixed-assets/assets/{car}/add")
    assert form.status_code == 200 and t("fixed_assets.add_hint.improvement") in form.text
    data = {"asset_id": car, "kind": "improvement", "amount": "1200", "credit_account": "3100",
            "date": "2026-09-15", "idempotency_key": str(uuid.uuid4())}
    review = await owner_ui.post("/fixed-assets/op/add-to-asset/review", data=data)
    assert t("fixed_assets.keep_end_date") in review.text and "fixed_assets." not in review.text
    href = await _review_and_confirm(owner_ui, "add-to-asset", data)
    assert href == f"/fixed-assets/assets/{car}"
    page = await owner_ui.get(href)
    assert t("fixed_assets.movement.improvement") in page.text


async def test_place_construction_in_service(owner_ui):
    await _managed_car(owner_ui)
    site = await _review_and_confirm(owner_ui, "add-asset", {
        "kind": "contributed", "name": "Office fit-out", "class_id": (await _class(owner_ui, "Assets under construction"))["id"],
        "available_date": "2026-08-10", "cost": "20000", "credit_account": "3100"})
    site = site.rsplit("/", 1)[1]
    page = await owner_ui.get(f"/fixed-assets/assets/{site}")
    assert f'href="/fixed-assets/assets/{site}/place-in-service"' in page.text
    assert f'href="/fixed-assets/assets/{site}/remeasure"' not in page.text

    form = await owner_ui.get(f"/fixed-assets/assets/{site}/place-in-service")
    assert form.status_code == 200 and 'name="row:2:amount"' in form.text and "fixed_assets." not in form.text
    row = await owner_ui.get(f"/fixed-assets/assets/{site}/place-in-service/row", params={"n": 3})
    assert 'name="row:3:name"' in row.text

    buildings = await _class(owner_ui, "Buildings")
    data = {"asset_id": site, "date": "2026-09-30", "row:0:name": "Partitions", "row:0:class_id": buildings["id"],
            "row:0:amount": "15000", "row:1:name": "", "idempotency_key": str(uuid.uuid4())}
    review = await owner_ui.post("/fixed-assets/op/place-in-service/review", data=data)
    assert t("fixed_assets.construction.remainder") in review.text and "fixed_assets." not in review.text
    href = await _review_and_confirm(owner_ui, "place-in-service", data)
    assert href == f"/fixed-assets/assets/{site}"
    assert t("fixed_assets.movement.construction_out") in (await owner_ui.get(href)).text


async def test_revalue_a_class_from_settings(owner_ui):
    await _demo(owner_ui)
    await owner_ui.post("/fixed-assets/start")
    r = await owner_ui.api.patch(f"{API}/settings", json={"first_managed_period": "2026-08-01"})
    assert r.status_code == 200, r.text
    land = await _class(owner_ui, "Land")
    plot = await _review_and_confirm(owner_ui, "add-asset", {
        "kind": "contributed", "name": "Yard plot", "class_id": land["id"], "available_date": "2026-08-10",
        "cost": "600", "credit_account": "3100"})
    plot = plot.rsplit("/", 1)[1]

    settings = await owner_ui.get("/settings/fixed-assets")
    assert f'href="/fixed-assets/classes/{land["id"]}/revalue"' in settings.text
    page = await owner_ui.get(f"/fixed-assets/classes/{land['id']}/revalue")
    assert page.status_code == 200 and f'name="fair_value:{plot}"' in page.text and "fixed_assets." not in page.text

    data = {"class_id": land["id"], "date": "2026-09-30", "basis": "Valuer report", f"fair_value:{plot}": "700",
            "idempotency_key": str(uuid.uuid4())}
    review = await owner_ui.post("/fixed-assets/op/revalue-class/review", data=data)
    assert t("label.total") in review.text and "fixed_assets." not in review.text
    assert await _review_and_confirm(owner_ui, "revalue-class", data) == "/settings/fixed-assets"
    assert (await _class(owner_ui, "Land"))["measurement_policy"] == "revaluation"
    assert t("fixed_assets.revaluation_surplus") in (await owner_ui.get(f"/fixed-assets/assets/{plot}")).text


async def test_record_an_impairment_review_that_found_a_loss(owner_ui):
    await _demo(owner_ui)
    await _start(owner_ui)
    other = await _class(owner_ui, "Other intangibles")
    r = await owner_ui.api.patch(f"{API}/classes/{other['id']}", json={"method": "none", "life_months": None})
    assert r.status_code == 200, r.text
    licence = await _review_and_confirm(owner_ui, "add-asset", {
        "kind": "contributed", "name": "Broadcast licence", "class_id": other["id"], "available_date": "2025-09-01",
        "cost": "5000", "credit_account": "3100"})
    licence = licence.rsplit("/", 1)[1]

    register = await owner_ui.get("/fixed-assets")
    assert t("fixed_assets.reviews_due_title") in register.text and 'href="/fixed-assets/reviews"' in register.text
    due = await owner_ui.get("/fixed-assets/reviews")
    assert f'href="/fixed-assets/assets/{licence}"' in due.text and t("fixed_assets.overdue") in due.text
    page = await owner_ui.get(f"/fixed-assets/assets/{licence}")
    assert t("fixed_assets.reviews_title") in page.text and t("fixed_assets.record_review") in page.text
    assert "fixed_assets." not in page.text

    found = await owner_ui.post(f"/fixed-assets/assets/{licence}/reviews", data={
        "date": "2026-10-05", "outcome": "recoverable_amount", "recoverable_amount": "4000", "basis": "Value in use"})
    target = found.headers["HX-Redirect"]
    assert target.startswith(f"/fixed-assets/assets/{licence}/remeasure?op=impairment")
    form = await owner_ui.get(target)
    assert 'value="4000.00"' in form.text and 'value="Value in use"' in form.text and 'value="2026-10-05"' in form.text
    assert t("fixed_assets.reviews_empty") in (await owner_ui.get("/fixed-assets/reviews")).text


async def test_reports_index_schedule_page_print_and_csv(owner_ui):
    await _demo(owner_ui)
    await owner_ui.post("/fixed-assets/start")
    r = await owner_ui.api.patch(f"{API}/settings", json={"first_managed_period": "2025-01-01"})
    assert r.status_code == 200, r.text
    params = {"date_from": "2025-01-01", "date_to": "2025-12-31"}
    empty = await owner_ui.get("/fixed-assets/reports/schedule", params=params)
    assert empty.status_code == 200 and t("fixed_assets.schedule_empty") in empty.text
    await _car(owner_ui, "Owner's car")
    index = await owner_ui.get("/fixed-assets/reports")
    assert index.status_code == 200 and "fixed_assets." not in index.text
    for path in ("schedule", "register", "depreciation", "disposals", "prior-period-adjustments"):
        assert f'href="/fixed-assets/reports/{path}' in index.text, path
    page = await owner_ui.get("/fixed-assets/reports/schedule", params=params)
    assert page.status_code == 200 and "12,000.00" in page.text and "fixed_assets." not in page.text
    assert 'cell--number' in page.text and "date_from=2025-01-01" in page.text
    printed = await owner_ui.get("/fixed-assets/reports/schedule/print", params=params)
    assert printed.status_code == 200 and "12,000.00" in printed.text
    sheet = await owner_ui.get("/fixed-assets/reports/schedule.csv", params=params)
    assert sheet.status_code == 200 and sheet.headers["content-type"].startswith("text/csv")
    assert "Owner's car" in sheet.text and "12000.00" in sheet.text
    for path in ("register", "depreciation", "disposals"):
        got = await owner_ui.get(f"/fixed-assets/reports/{path}", params=params)
        assert got.status_code == 200 and "fixed_assets." not in got.text, path
        assert (await owner_ui.get(f"/fixed-assets/reports/{path}.csv", params=params)).status_code == 200, path
        assert (await owner_ui.get(f"/fixed-assets/reports/{path}/print", params=params)).status_code == 200, path
    refused = await owner_ui.get("/fixed-assets/reports/schedule", params={"date_from": "2024-01-01",
                                                                          "date_to": "2024-12-31"})
    assert refused.status_code == 200 and "first managed period" in refused.text


async def test_u24_the_schedule_is_one_table_per_section_on_screen_and_paper(owner_ui):
    car = await _managed_car(owner_ui)
    assert car
    params = {"date_from": "2026-08-01", "date_to": "2026-12-31"}
    for path in ("/fixed-assets/reports/schedule", "/fixed-assets/reports/schedule/print"):
        page = (await owner_ui.get(path, params=params)).text
        parts = re.split(r"<table\b", page)
        before, tables = parts[:-1], parts[1:]
        assert len(tables) == 4, (path, len(tables))
        for key, lead, table in zip(("cost", "accumulated_depreciation", "accumulated_impairment", "nbv"),
                                    before, tables):
            # Each section's title sits right before its own table.
            assert lead.rstrip().rsplit("<h", 1)[1].count(t(f"fixed_assets.{key}")) == 1, (path, key)
            head = table.split("</thead>", 1)[0]
            columns = len(re.findall(r"<th\b", head))
            assert columns <= 8, (path, key, columns)
            assert "Delivery car" in table and t("fixed_assets.total") in table, (path, key)
        assert "landscape" not in page, path


async def test_u15_u30_intake_names_journals_and_line_kinds_and_every_account_reads_code_and_name(owner_ui):
    car = await _managed_car(owner_ui)
    chart = {a["code"]: a["name"] for a in (await owner_ui.api.get("/accounting/chart")).json()["items"]}
    r = await owner_ui.api.post("/accounting/journal-entries", json={
        "ts": "2026-09-30", "memo": "Monthly depreciation", "idempotency_token": str(uuid.uuid4()), "entries": [
            {"account": "6400", "debit": 100}, {"account": "1220", "credit": 100}]})
    assert r.status_code == 200, r.text
    je_id = r.json()["je_id"]

    page = await owner_ui.get("/fixed-assets/intake")
    at = page.text.index("Monthly depreciation")
    row = page.text[page.text.rindex("<tr", 0, at):page.text.index("</tr>", at) + 5]
    assert f">{je_id}<" not in row and f">{t('fixed_assets.manual_journal')}</a>" in row, \
        "a manual journal reads as a journal entry, not its id"
    assert t("fixed_assets.line_kind.depreciation") in row and f"1220 {chart['1220']}" in row
    assert t("fixed_assets.line_action.link-depreciation") in row and f">{t('fixed_assets.deal_with_line')}<" not in row, \
        "depreciation is linked to its assets, not registered as one"
    line = await owner_ui.get("/fixed-assets/intake/line", params={"source": f"{je_id}:1"})
    assert t("fixed_assets.manual_journal") in line.text and f"1220 {chart['1220']}" in line.text

    review = await owner_ui.post("/fixed-assets/op/impair/review", data={
        "asset_id": car, "date": "2026-09-30", "recoverable_amount": "9000", "basis": "Dealer quote",
        "idempotency_key": str(uuid.uuid4())})
    journal = review.text[review.text.index(t("fixed_assets.debit")):]
    accumulated = (await _class(owner_ui, "Vehicles"))["accumulated_depreciation_account"]
    assert re.search(r"<td>\d{4}</td>", journal) is None, "a previewed journal names every account"
    assert f"<td>{accumulated} {chart[accumulated]}</td>" in journal

    recon = await owner_ui.get("/fixed-assets/reconciliation")
    assert f"1210 {chart['1210']}" in recon.text and "<td>1220</td>" not in recon.text


async def test_u16_a_finalized_bill_leads_to_registering_its_asset_line_then_to_the_asset(owner_ui):
    await _managed_car(owner_ui)
    r = await owner_ui.api.post("/docs", json={
        "doc_type": "bill", "contact_name": "City Motors", "issue_date": "2026-09-05", "total": 4000.0,
        "line_items": [{"name": "Coffee roaster", "quantity": 1, "unit_price": 4000.0, "receive_as": "asset"}]})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    assert t("fixed_assets.register_as_asset") not in (await owner_ui.get(f"/docs/{doc}")).text, \
        "a draft bill has nothing to register yet"
    assert (await owner_ui.api.post(f"/docs/{doc}/finalize")).status_code == 200

    page = await owner_ui.get(f"/docs/{doc}")
    href = re.search(r'href="(/fixed-assets/intake/line\?source=[^"]+)"[^>]*>' + re.escape(t("fixed_assets.register_as_asset")),
                     page.text)
    assert href, "a finalized asset bill says what comes next"
    line = await owner_ui.get(href.group(1).replace("&amp;", "&"))
    assert "Coffee roaster" in line.text and 'name="class_id"' in line.text
    source = re.search(r'name="source" value="([^"]+)"', line.text).group(1)

    vehicles = await _class(owner_ui, "Vehicles")
    asset = await _review_and_confirm(owner_ui, "register", {
        "source": source, "mode": "separate", "class_id": vehicles["id"], "name": "Coffee roaster",
        "available_date": "2026-09-05"})
    asset_id = asset.rsplit("/", 1)[1]
    number = (await owner_ui.api.get(f"{API}/assets/{asset_id}")).json()["number"]
    again = await owner_ui.get(href.group(1).replace("&amp;", "&"))
    assert t("fixed_assets.line_registered", description="Coffee roaster") in again.text
    assert f'href="/fixed-assets/assets/{asset_id}">{number}</a>' in again.text


async def test_u17_a_page_waiting_on_setup_offers_the_step_in_place_and_opening_names_its_journal(owner_ui):
    await _demo(owner_ui)
    await owner_ui.post("/fixed-assets/start")
    page = await owner_ui.get("/fixed-assets/reconciliation")
    assert t("fixed_assets.not_managed") not in page.text and 'id="fa-first-period"' in page.text, \
        "the confirmation is offered where it is needed, not only named"
    assert 'name="back" value="/fixed-assets/reconciliation"' in page.text
    suggested = re.search(r'name="first_managed_period" value="([^"]+)"', page.text).group(1)
    done = await owner_ui.post("/fixed-assets/first-period", data={"first_managed_period": suggested,
                                                                  "back": "/fixed-assets/reconciliation"})
    assert done.headers.get("HX-Redirect") == "/fixed-assets/reconciliation"
    assert 'id="fa-first-period"' not in (await owner_ui.get("/fixed-assets/reconciliation")).text

    cutover = (await owner_ui.api.get(f"{API}/opening")).json()["cutover"]
    r = await owner_ui.api.post("/accounting/journal-entries", json={
        "ts": cutover, "memo": "Generator", "idempotency_token": str(uuid.uuid4()), "entries": [
            {"account": "1210", "debit": 900}, {"account": "3100", "credit": 900}]})
    assert r.status_code == 200, r.text
    journal = f"/accounting/journal/new?ts={cutover}"
    opening = await owner_ui.get("/fixed-assets/opening")
    assert f'href="{journal}"' in opening.text and t("fixed_assets.record_opening_journal") in opening.text
    refused = await owner_ui.post("/fixed-assets/opening/finish", data={})
    assert "flash--error" in refused.text and "HX-Redirect" not in refused.headers
    assert f'<a href="{journal}">{t("fixed_assets.record_opening_journal")}</a>' in refused.text, \
        "the refusal carries the way to record the opening journal"
    form = await owner_ui.get(journal)
    assert f'value="{cutover}"' in form.text, "the journal form opens on the cutover"


async def test_u18_the_asset_page_shows_how_it_depreciates_and_its_next_charge(owner_ui):
    car = await _managed_car(owner_ui)
    policy = (await _class(owner_ui, "Vehicles"))
    assert policy["method"] == "straight_line"
    cost, life = Decimal("12000"), int(policy["life_months"])
    monthly = ((cost - cost * Decimal(str(policy["residual_percent"])) / 100) / life).quantize(Decimal("0.01"))

    def card(html: str) -> str:
        at = html.index('id="fa-depreciation"')
        return html[at:html.index("</table>", at)]

    shown = card((await owner_ui.get(f"/fixed-assets/assets/{car}")).text)
    for words in (t("fixed_assets.method.straight_line"), t("fixed_assets.life_months"), str(life),
                  t("fixed_assets.residual_percent"), t("fixed_assets.convention"), t("fixed_assets.next_charge")):
        assert words in shown, words
    assert "2026-08" in shown and f"{monthly:,}" in shown, "the next run charges August, the first managed month"

    await _review_and_confirm(owner_ui, "run", {"month": "2026-08"})
    shown = card((await owner_ui.get(f"/fixed-assets/assets/{car}")).text)
    assert "2026-09" in shown and "2026-08" not in shown and f"{monthly:,}" in shown, \
        "once August is posted, the next charge is September's"


async def test_u19_u20_every_open_ended_list_is_searchable_and_a_fixed_one_is_a_plain_select(owner_ui):
    car = await _managed_car(owner_ui)

    def searchable(html: str, name: str) -> bool:
        return re.search(rf'<input type="hidden" name="{re.escape(name)}"[^>]*>', html) is not None \
            and f'<select name="{name}"' not in html

    home = await owner_ui.get("/fixed-assets")
    assert searchable(home.text, "class_id"), "classes are added by the company, so the filter is searchable"
    assert t("fixed_assets.all_classes") in home.text
    register = await owner_ui.get("/fixed-assets/reports/register")
    assert searchable(register.text, "class_id")
    move = await owner_ui.get(f"/fixed-assets/assets/{car}/move")
    assert searchable(move.text, "location_id") and searchable(move.text, "custodian_user")
    add = await owner_ui.get(f"/fixed-assets/assets/{car}/add")
    assert searchable(add.text, "source")
    page = await owner_ui.get(f"/fixed-assets/assets/{car}")
    assert re.search(r'<select name="kind"', page.text), "a reminder's kind is one of a fixed few"


async def test_an_action_page_names_the_asset_and_review_is_its_main_button(owner_ui):
    car = await _managed_car(owner_ui)
    for action, title in (("sell", "Sell"), ("move", "Move"), ("dispose", "Dispose of"), ("add", "Add to"),
                          ("reclassify", "Reclassify")):
        soup = BeautifulSoup((await owner_ui.get(f"/fixed-assets/assets/{car}/{action}")).text, "html.parser")
        assert soup.find("h1").get_text(strip=True) == f"{title} FA-0001 Delivery car", action
        review = soup.find("button", attrs={"hx-post": re.compile(r"/review$")})
        assert review.get_text(strip=True) == "Review" and "btn--primary" in review["class"], action


async def test_u21_transfer_says_what_it_needs_when_no_company_can_take_the_asset(owner_ui):
    car = await _managed_car(owner_ui)
    alone = await owner_ui.get(f"/fixed-assets/assets/{car}/transfer")
    assert t("fixed_assets.transfer_only_company") in alone.text
    assert f'<a href="/setup/new-company">{t("fixed_assets.add_company")}</a>' in alone.text

    r = await owner_ui.api.post("/companies", json={"name": "Beta Co"})
    assert r.status_code == 200, r.text
    beta = (await owner_ui.api.get("/companies/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})).json()
    others = (await owner_ui.api.get(f"{API}/transfer-targets")).json()["others"]
    assert others == [{"company_id": beta["id"], "name": "Beta Co", "reason": "not_using"}]
    page = await owner_ui.get(f"/fixed-assets/assets/{car}/transfer")
    assert t("fixed_assets.transfer_other.not_using", company="Beta Co") in page.text
    assert f'href="/switch-company/{beta["id"]}"' in page.text, "the step is taken in the other company"


async def test_r2_21_a_form_sent_after_a_switch_in_another_tab_saves_nothing_and_offers_the_way_back(owner_ui):
    car = await _managed_car(owner_ui)
    home = (await owner_ui.api.get("/companies/me")).json()
    form_page = await owner_ui.get(f"/fixed-assets/assets/{car}/dispose")
    sent = {"X-Celerp-Page-Company": home["id"], "HX-Current-URL": f"http://ui/fixed-assets/assets/{car}/dispose"}
    carried = re.search(r"hx-headers=(['\"])(.+?)\1", form_page.text)
    assert carried and json.loads(html.unescape(carried.group(2))) == {"X-Celerp-Page-Company": home["id"]}, \
        "the page carries the company it was opened in"

    # Another tab switches the session to Beta Co.
    r = await owner_ui.api.post("/companies", json={"name": "Beta Co"})
    assert r.status_code == 200, r.text
    owner_ui.cookies.set("celerp_token", r.json()["access_token"])
    review = await owner_ui.post("/fixed-assets/op/dispose/review", headers=sent, data={
        "asset_id": car, "date": "2026-10-05", "reason": "scrapped", "idempotency_key": str(uuid.uuid4())})
    switch = t("fixed_assets.switch_to", company=home["name"])
    said = t("fixed_assets.switched_company", page=home["name"], now="Beta Co", switch=switch)
    text = html.unescape(re.sub(r"<[^>]+>", "", review.text))
    assert said in text, review.text
    back = f"/switch-company/{home['id']}?next={quote(f'/fixed-assets/assets/{car}/dispose', safe='')}"
    assert f'<a href="{html.escape(back)}">{switch}</a>' in review.text

    # A small form says the same in its toast; nothing is saved in either company.
    settled = await owner_ui.post("/fixed-assets/first-period", headers=sent,
                                  data={"first_managed_period": "2026-09-01"})
    assert said == _toast(settled)
    asset = (await owner_ui.api.get(f"{API}/assets/{car}")).json()
    assert asset["status"] == "active" and not asset.get("left_use")
    assert (await owner_ui.api.get(f"{API}/setup")).json()["settings"]["first_managed_period"] == "2026-08-01"

    # Switch to the page's company leads back to the page.
    again = await owner_ui.get(back)
    assert again.status_code == 302 and again.headers["location"] == f"/fixed-assets/assets/{car}/dispose"


async def test_u22_the_buyer_is_a_contact_and_the_result_says_gain_or_loss(owner_ui):
    car = await _managed_car(owner_ui)
    buyer = (await owner_ui.api.get(f"/crm/contacts/{await _buyer(owner_ui)}")).json()
    form = await owner_ui.get(f"/fixed-assets/assets/{car}/sell")
    assert '<input type="hidden" name="contact_id"' in form.text and "/contacts/search-options" in form.text
    assert 'name="contact_name"' not in form.text

    body = {"asset_id": car, "date": "2026-10-05", "contact_id": buyer["id"]}
    gain = await owner_ui.post("/fixed-assets/op/sell/review", data={**body, "price": "13000",
                                                                    "idempotency_key": str(uuid.uuid4())})
    loss = await owner_ui.post("/fixed-assets/op/sell/review", data={**body, "price": "9000",
                                                                    "idempotency_key": str(uuid.uuid4())})
    for review, said in ((gain, "fixed_assets.gain"), (loss, "fixed_assets.loss")):
        assert _row_label(t(said), review.text) and not _row_label(t("fixed_assets.gain_or_loss"), review.text)
        assert buyer["name"] in review.text
    href = await _review_and_confirm(owner_ui, "sell", {**body, "price": "9000"})
    invoice = (await owner_ui.api.get(f"/docs/{href.removeprefix('/docs/')}")).json()
    assert invoice["contact_id"] == buyer["id"] and invoice["contact_name"] == buyer["name"]
    page = await owner_ui.get(f"/fixed-assets/assets/{car}")
    assert _row_label(t("fixed_assets.loss"), page.text)


async def test_u44_settings_has_a_breadcrumb_one_heading_and_intangibles_on_the_intangible_account(owner_ui):
    await _demo(owner_ui)
    await owner_ui.post("/fixed-assets/start")
    page = (await owner_ui.get("/settings/fixed-assets")).text
    crumbs = page[page.index('class="breadcrumbs"'):].split("</div>", 1)[0]
    assert 'href="/fixed-assets"' in crumbs and t("fixed_assets.settings_title") in crumbs, crumbs
    title = re.escape(t("fixed_assets.settings_title"))
    assert len(re.findall(rf"<h[1-3][^>]*>\s*{title}\s*</h[1-3]>", page)) == 1

    classes = (await owner_ui.api.get(f"{API}/classes")).json()["items"]
    accounts = {c["name"]: (c["kind"], c["asset_account"]) for c in classes}
    assert accounts["Software"] == accounts["Other intangibles"] == ("intangible", "1230"), accounts
    assert accounts["Vehicles"] == ("tangible", "1210"), accounts
    brand = await owner_ui.api.post(f"{API}/classes", json={"name": "Brands", "kind": "intangible",
                                                                   "life_months": 120})
    assert brand.status_code == 200 and brand.json()["asset_account"] == "1230", brand.text


async def test_u48_a_missing_permission_links_to_users(owner_ui):
    from celerp.services.permissions import missing_permission_text

    await _demo(owner_ui)
    r = await owner_ui.api.patch("/companies/me/role-permissions", json={
        "perm_key": "view_fixed_assets", "role_key": "owner", "granted": False})
    assert r.status_code == 200, r.text
    page = (await owner_ui.get("/fixed-assets")).text
    assert missing_permission_text("view_fixed_assets") in html.unescape(page), page
    assert 'href="/settings/general?tab=users"' in page, page
