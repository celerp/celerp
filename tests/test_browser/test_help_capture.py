"""Every shipped help page's steps match the running UI.

One case per page: each step's capture recipe opens its route on a company filled
with demo data, runs its actions, and the step's selector and the English text of
every label it names must be visible (as text or as a field's placeholder; a hidden
copy elsewhere on the page, such as in a closed menu, does not count). With HELP_CAPTURE_WRITE=1 each step that
declares an image is written as a cropped webp carrying the capture marker, the
only way a help image is made.
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import os
import random
import threading
import uuid
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect

from celerp.help.bundle import MAX_IMAGE_BYTES
from celerp.help.checks import CAPTURE_MARKER, shipped_pages
from celerp.services.business_time import business_today
from celerp_labels.presets import PRESET_TEMPLATES
from test_helpers import grant_permission
from ui.i18n import t

from .conftest import set_session_cookies

pytestmark = pytest.mark.browser

_SEED_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "seed_demo.py"
_spec = importlib.util.spec_from_file_location("seed_demo_help_capture", _SEED_PATH)
seed_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_demo)

_WRITE = os.environ.get("HELP_CAPTURE_WRITE") == "1"
_MAX_WIDTH = 1000
_EXIF_DESCRIPTION = 0x010E
_PAGES = [p for p in shipped_pages() if isinstance(p.data, dict)]


async def _ok(response: httpx.Response) -> dict:
    assert response.status_code in (200, 201), response.text
    return response.json()


async def seed_funds_and_deferred_revenue(client: httpx.AsyncClient, auth: dict, contacts: list[dict],
                                          today: date) -> None:
    """Funds and revenue recognition show their controls only once they are in use: funds
    on, a restricted fund, an archived fund and a transfer; an invoice deferring revenue over
    the last three ended months with the oldest month posted, and a voided one."""
    def key() -> str:
        return uuid.uuid4().hex

    await _ok(await client.put("/accounting/funds/setting", headers=auth, json={"enabled": True}))
    await _ok(await client.post("/accounting/funds", headers=auth, json={
        "request_key": key(), "name": "Roof appeal", "restricted": True, "purpose": "Repair the roof"}))
    reserve = await _ok(await client.post("/accounting/funds", headers=auth, json={
        "request_key": key(), "name": "Reserve"}))
    fair = await _ok(await client.post("/accounting/funds", headers=auth, json={
        "request_key": key(), "name": "Summer fair"}))
    await _ok(await client.post(f"/accounting/funds/{fair['id']}/archive", headers=auth))
    await _ok(await client.post("/accounting/funds/transfer", headers=auth, json={
        "request_key": key(), "date": today.isoformat(), "reason": "Set aside a reserve",
        "from_fund": None, "to_fund": reserve["id"], "amount": 500.0}))

    item = await _ok(await client.post("/items", headers=auth, json={
        "name": "Support plan", "sell_by": "piece", "inventory_type": "service", "quantity": 0,
        "status": "available", "retail_price": 900.0, "recognize_over_service_period": True}))
    this_month = today.replace(day=1)
    start = (this_month - timedelta(days=85)).replace(day=1)
    line = {"item_id": item["id"], "name": "Support plan", "quantity": 1, "unit_price": 900.0,
            "line_total": 900.0, "service_start": start.isoformat(),
            "service_end": (this_month - timedelta(days=1)).isoformat()}
    docs = []
    for _ in range(2):
        doc = await _ok(await client.post("/docs", headers=auth, json={
            "doc_type": "invoice", "contact_id": contacts[2]["id"], "line_items": [line], "total": 900.0}))
        await _ok(await client.post(f"/docs/{doc['id']}/finalize", headers=auth, json={}))
        docs.append(doc["id"])
    await _ok(await client.post(f"/accounting/recognition/{start:%Y-%m}/post", headers=auth, json={}))
    await _ok(await client.post(f"/docs/{docs[1]}/void", headers=auth, json={"reason": "Sent twice"}))


@pytest.fixture(scope="module")
def demo_token(api_server, api):
    """A company of its own, filled with the same demo data every run."""
    r = api.post("/companies", json={"name": f"Help capture {uuid.uuid4().hex[:6]}"})
    assert r.status_code in (200, 201), r.text
    token = r.json()["access_token"]

    async def seed():
        auth = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(base_url=api_server, timeout=30.0) as client:
            await seed_demo.seed_items(client, token)
            contacts = await seed_demo.seed_contacts(client, token)
            await seed_demo.seed_docs(client, token, contacts, "invoice", 12)
            await seed_demo.seed_docs(client, token, contacts, "purchase_order", 6)
            # The fixed assets register shows nothing until its one-time start is
            # confirmed; help pages for it capture the register in its normal,
            # already-started state.
            r = await client.post("/fixed-assets/api/start", headers={"Authorization": f"Bearer {token}"})
            assert r.status_code in (200, 201), r.text
            # Opening balances and Reconciliation open only once the first managed period is
            # confirmed: confirm the period the app suggests, as the user does.
            r = await client.get("/fixed-assets/api/setup", headers=auth)
            assert r.status_code == 200, r.text
            r = await client.patch("/fixed-assets/api/settings", headers=auth,
                                   json={"first_managed_period": r.json()["first_period"]["suggested"]})
            assert r.status_code == 200, r.text
            # Donors and Subscribers show a donor's statement and a subscriber's quantities only
            # once there is one: a gift by bank transfer from a contact to a gift category's income
            # account, and an active agreement.
            r = await client.post("/accounting/accounts", headers=auth, json={
                "code": "4250", "name": "Donations", "account_type": "revenue", "parent_code": "4000"})
            assert r.status_code == 200, r.text
            r = await client.post("/companies/me/categories", headers=auth, json={"name": "Donation"})
            assert r.status_code == 200, r.text
            donation = r.json()["key"]
            for path, body in (("income-account", {"income_account": "4250"}), ("gift", {"gift": True})):
                r = await client.put(f"/companies/me/categories/{donation}/{path}", headers=auth, json=body)
                assert r.status_code == 200, r.text
            # Dated the company's today, the day the Donors page's default period ends on.
            r = await client.get("/companies/me", headers=auth)
            assert r.status_code == 200, r.text
            today = business_today(r.json()["settings"].get("timezone")).isoformat()
            r = await client.post("/accounting/journal-entries", headers=auth, json={
                "ts": today, "memo": "Gift by bank transfer", "idempotency_token": uuid.uuid4().hex,
                "entries": [{"account": "1111", "debit": 250.0, "credit": 0.0},
                            {"account": "4250", "debit": 0.0, "credit": 250.0, "contact": contacts[0]["id"]}]})
            assert r.status_code == 200, r.text
            r = await client.post("/docs", headers=auth, json={
                "doc_type": "subscription_invoice", "contact_id": contacts[1]["id"], "frequency": "monthly",
                "start_date": today,
                "line_items": [{"description": "Seats", "quantity": 5, "unit_price": 20.0, "line_total": 100.0}]})
            assert r.status_code in (200, 201), r.text
            r = await client.post(f"/subscriptions/{r.json()['id']}/activate", headers=auth)
            assert r.status_code == 200, r.text
            await seed_funds_and_deferred_revenue(client, auth, contacts, date.fromisoformat(today))
            await seed_bank_statement(client, auth, date.fromisoformat(today))
            # The default label templates are created through their write route, so no
            # page's first read seeds them: which labels page reads first depends on test order.
            for preset in PRESET_TEMPLATES:
                r = await client.post("/api/labels/templates", json=preset,
                                      headers={"Authorization": f"Bearer {token}"})
                assert r.status_code == 201, r.text

    random.seed(20261022)
    # Playwright's sync API keeps its own event loop running on this thread for the whole
    # session (greenlet-trampolined), so asyncio.run() here would always raise "cannot be
    # called from a running event loop". Run the seed coroutine on a fresh thread, same as
    # api_server/ui_server above run their own event loop off the main thread.
    error: list[BaseException] = []

    def _run():
        try:
            asyncio.run(seed())
        except BaseException as exc:  # noqa: BLE001 - re-raised on the calling thread below
            error.append(exc)

    thread = threading.Thread(target=_run)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return token


async def seed_bank_statement(client: httpx.AsyncClient, auth: dict, today: date) -> None:
    """One month's CSV statement on the default bank, read and imported through the bank
    page's own routes, so the bank help pages capture its lines, views and answers."""
    banks = (await client.get("/accounting/bank-accounts", headers=auth)).json()["items"]
    bank_id = banks[0]["id"]
    end = today.replace(day=1) - timedelta(days=1)
    start = end.replace(day=1)
    rows = [(start + timedelta(days=2), "OFFICE RENT", "-800.00"), (start + timedelta(days=9), "ATM WITHDRAWAL", "-100.00"),
            (start + timedelta(days=15), "CUSTOMER PAYMENT", "1500.00")]
    csv = "Date,Description,Amount\n" + "".join(f"{d.isoformat()},{desc},{amt}\n" for d, desc, amt in rows)
    r = await client.post("/accounting/reconciliation/files", headers=auth,
                          files={"files": ("statement.csv", csv.encode(), "text/csv")})
    assert r.status_code == 201, r.text
    r = await client.post("/accounting/reconciliation/read-statement", headers=auth, json={
        "file_ids": r.json()["file_ids"], "bank_id": bank_id, "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    for _ in range(100):
        preview = await client.get(f"/accounting/reconciliation/statement-preview/{job_id}", headers=auth)
        if preview.status_code == 200 and preview.json()["statements"]:
            break
        await asyncio.sleep(0.2)
    assert preview.status_code == 200, preview.text
    header = {"opening_balance": "1000.00", "closing_balance": "1600.00",
              "period_start": start.isoformat(), "period_end": end.isoformat()}
    r = await client.post("/accounting/reconciliation/import-statement", headers=auth, json={
        "job_id": job_id, "bank_id": bank_id, "idempotency_key": uuid.uuid4().hex,
        "statements": [{"header": {**st["header"], **header}, "lines": st["lines"]}
                       for st in preview.json()["statements"]]})
    assert r.status_code == 200, r.text
    # A client who answers questions without keeping the books, asked about the ATM line,
    # so the questions page shows a card with its answer form.
    await grant_permission(client, auth, "answer_bank_questions", "viewer")
    r = await client.post("/companies/me/users", headers=auth, json={
        "email": "client@example.com", "name": "Client", "role": "viewer", "password": "userpw123"})
    assert r.status_code == 200, r.text
    table = (await client.get(f"/accounting/reconciliation/bank/{bank_id}/groups", headers=auth)).json()
    atm = next(line for g in table["groups"] for line in g["lines"] if "ATM" in line["description"])
    r = await client.post(f"/accounting/reconciliation/bank/{bank_id}/ask", headers=auth, json={
        "line_ids": [atm["id"]], "note": "What was this cash for?", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text


@pytest.fixture
def demo_page(page, browser_context, demo_token):
    set_session_cookies(browser_context, demo_token)
    return page


def _run(page, action: dict) -> None:
    if "click" in action:
        page.locator(action["click"]).first.click()
    elif "fill" in action:
        page.locator(action["fill"]).first.fill(action["value"])
    elif "press" in action:
        page.keyboard.press(action["press"])
    else:
        expect(page.locator(action["wait"]).first).to_be_visible()


def _visible_label(key: str) -> str:
    """The English label as it shows on screen; a label with a {param} is matched up to it."""
    return t(key, "en").split("{", 1)[0].strip()


def _write_image(target, path: Path) -> None:
    from PIL import Image

    with Image.open(io.BytesIO(target.screenshot())) as shot:
        image = shot.convert("RGB")
    if image.width > _MAX_WIDTH:
        image = image.resize((_MAX_WIDTH, round(image.height * _MAX_WIDTH / image.width)))
    exif = Image.Exif()
    exif[_EXIF_DESCRIPTION] = CAPTURE_MARKER
    for quality in (80, 65, 50, 35, 20):
        buf = io.BytesIO()
        image.save(buf, "WEBP", quality=quality, method=6, exif=exif)
        if buf.tell() <= MAX_IMAGE_BYTES:
            break
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.getvalue())


@pytest.mark.parametrize("help_page", _PAGES, ids=[p.data.get("id", p.path.name) for p in _PAGES])
def test_help_capture_every_page(demo_page, help_page):
    for n, step in enumerate(help_page.data["steps"], 1):
        capture = step["capture"]
        response = demo_page.goto(capture["route"])
        assert response is not None and response.ok, f"step {n}: {capture['route']} did not load"
        for action in capture["actions"]:
            _run(demo_page, action)
        target = demo_page.locator(capture["selector"]).first
        expect(target).to_be_visible()
        for key in step["labels"].values():
            label = _visible_label(key)
            if label:
                shown = demo_page.get_by_text(label).or_(demo_page.get_by_placeholder(label))
                expect(shown.filter(visible=True).first).to_be_visible()
        if _WRITE and step["image"]:
            _write_image(target, help_page.help_dir / step["image"])
