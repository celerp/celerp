"""Every shipped help page's steps match the running UI.

One case per page: each step's capture recipe opens its route on a company filled
with demo data, runs its actions, and the step's selector and the English text of
every label it names must be visible (as text or as a field's placeholder; a hidden
copy elsewhere on the page, such as in a closed menu, does not count). The pictured part of the
screen must not show the app's own empty-list, Connect sales or nothing-selected components unless
the step is listed in _SHOWS_EMPTY_STATE. With HELP_CAPTURE_WRITE=1 each step
is written as a webp of the selector's element (its top 800 pixels when taller) carrying the
capture marker, the only way a help image is made.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import os
import random
import threading
import uuid
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from playwright.sync_api import expect

from celerp.help.bundle import MAX_IMAGE_BYTES
from celerp.help.checks import CAPTURE_MARKER, shipped_pages
from celerp.services.business_time import business_today
from celerp_labels.presets import PRESET_TEMPLATES
from test_helpers import grant_permission
from ui.i18n import t

from .conftest import _TEST_EMAIL, set_session_cookies

pytestmark = pytest.mark.browser

_SEED_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "seed_demo.py"
_spec = importlib.util.spec_from_file_location("seed_demo_help_capture", _SEED_PATH)
seed_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_demo)

_WRITE = os.environ.get("HELP_CAPTURE_WRITE") == "1"
_MAX_WIDTH = 1000
_MAX_HEIGHT = 800  # CSS pixels: a taller element is cut to its top, where its screen starts
_EXIF_DESCRIPTION = 0x010E
_PAGES = [p for p in shipped_pages() if isinstance(p.data, dict)]
# The app's own components for a screen with nothing to show: an empty list or report, the
# Remote Access and Connect sales pages and banners, the AI page shown without an account, and
# a bulk page opened with no rows selected. A step whose picture shows one would show the
# reader a different screen from the one its text describes.
_EMPTY_STATE_SELECTOR = ", ".join((
    ".empty-state", ".empty-state-msg", ".empty-state-cta",
    ".cloud-hero", ".upgrade-banner", ".module-license-upsell", ".ai-showcase__headline",
))
# Steps whose text is about that empty or sales screen itself: "<page id>#<step>": why.
_CONNECT_SALES = "the step names what Celerp Connect adds, which its sales page lists"
_NO_TAX_REGIME = "no first-party module ships a tax regime, so the page shows where to get one"
_SHOWS_EMPTY_STATE: dict[str, str] = {
    "fixed-assets.tax-books#1": _NO_TAX_REGIME,
    "fixed-assets.tax-books#2": _NO_TAX_REGIME,
    "fixed-assets.year-end#1": _NO_TAX_REGIME,
    "fixed-assets.year-end#2": _NO_TAX_REGIME,
    "fixed-assets.year-end#3": _NO_TAX_REGIME,
    "core.settings-payments#1": _CONNECT_SALES,
    "backup.cloud-backup#1": _CONNECT_SALES,
    "connectors.connect-store#1": _CONNECT_SALES,
    "connectors.connect-store#2": _CONNECT_SALES,
    "backup.local-backup#1": "the cloud recovery points beside the local section need Celerp Connect",
    "backup.local-backup#2": "the cloud recovery points beside the local section need Celerp Connect",
    "backup.local-backup#3": "the cloud recovery points beside the local section need Celerp Connect",
    "core.accounting-sync-accounts#1": "a new company's accounts have nothing to fix, the state the step describes",
    "core.accounting-sync-accounts#2": "a new company's accounts have nothing to fix, the state the step describes",
}
# Steps about what an active Celerp Connect subscription or Celerp AI account shows: the
# installation is given one through the seams the Connect and AI browser tests use, for that
# step only, so the steps about the sales pages still show them.
_ACTIVE_RELAY = {"connected": True, "relay_status": "active", "public_url": "https://example.celerp.com",
                 "tier": "cloud", "subscription_status": "active", "cloud_disconnected": False,
                 "gateway_token_set": True, "entitlement_known": True, "entitled": True}
_AI_ACCOUNT = {"allowed": True, "used": 0, "base_limit": 200, "topup_balance": 0, "remaining": 200,
               "limit": 200, "topup_credits": 0, "resets_at": None, "tier": "ai"}
_CONNECT, _AI = "connect", "ai"
_SUBSCRIBED: dict[str, str] = {
    "core.web-access#2": _CONNECT,
    "backup.cloud-backup#2": _CONNECT, "backup.cloud-backup#3": _CONNECT,
    "connectors.connect-store#3": _CONNECT, "connectors.connect-store#4": _CONNECT,
    "connectors.connect-store#5": _CONNECT,
    "ai.ask-questions#2": _AI, "ai.ask-questions#4": _AI,
    **{f"ai.review-proposals#{n}": _AI for n in range(1, 5)},
}


@contextlib.contextmanager
def _subscription(kind: str | None):
    from celerp.gateway.state import set_session_token

    with contextlib.ExitStack() as stack:
        if kind == _CONNECT:
            stack.enter_context(patch("ui.api_client.get_relay_status", AsyncMock(return_value=_ACTIVE_RELAY)))
        elif kind == _AI:
            set_session_token("help-capture-session-token")
            stack.callback(set_session_token, "")
            stack.enter_context(patch("celerp.ai.quota.get_quota_status", AsyncMock(return_value=_AI_ACCOUNT)))
        yield


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


async def _fa(client: httpx.AsyncClient, auth: dict, path: str, body: dict) -> dict:
    """A fixed-assets change as its page makes it: the review, then the change confirmed
    with the fingerprint the review returned."""
    url, key = f"/fixed-assets/api{path}", uuid.uuid4().hex
    seen = await _ok(await client.post(url, headers=auth, json={**body, "preview": True, "op_key": key}))
    return await _ok(await client.post(url, headers=auth, json={**body, "op_key": key, "fingerprint": seen["fingerprint"]}))


async def seed_fixed_assets(client: httpx.AsyncClient, auth: dict, user_id: str, today: date) -> None:
    """The fixed asset pages in use: the sample bills' van, laptops and forklift registered
    (the office fit-out still waits on the intake list), the last ended month depreciated, one
    correction applied and one waiting, reminders and an impairment review falling due, the
    laptops in the capture user's care, the van seen on an asset check and one finished
    register import."""
    async def get(path: str) -> dict:
        return await _ok(await client.get(f"/fixed-assets/api{path}", headers=auth))

    classes = {c["name"]: c["id"] for c in (await get("/classes"))["items"]}
    lines = (await get("/intake"))["items"]
    for name, cls in (("Delivery van", "Vehicles"), ("Laptop", "Computer equipment"), ("Forklift", "Plant and machinery")):
        line = next(i for i in lines if name in i["description"])
        await _fa(client, auth, "/register", {"source": line["source"], "class_id": classes[cls]})
    assets = {a["name"]: a["id"] for a in (await get("/assets"))["items"]}
    last_month = today.replace(day=1) - timedelta(days=1)
    await _fa(client, auth, "/runs", {"month": f"{last_month:%Y-%m}"})
    van, laptops, forklift = assets["Delivery van"], assets["Laptop"], assets["Forklift"]
    await _fa(client, auth, f"/assets/{forklift}/correct", {
        "changes": {"life_months": 96}, "effective_date": today.replace(day=1).isoformat(),
        "reason": "The supplier's service plan keeps it running eight years"})
    await _ok(await client.post(f"/fixed-assets/api/assets/{van}/corrections", headers=auth, json={
        "changes": {"life_months": 72}, "reason": "Fleet policy keeps vans six years", "op_key": uuid.uuid4().hex}))
    for body in ({"kind": "maintenance", "work": "Annual service", "due": (today + timedelta(days=5)).isoformat()},
                 {"kind": "insurance", "insurer": "Harbour Insurance", "due": (today - timedelta(days=3)).isoformat()}):
        await _ok(await client.post(f"/fixed-assets/api/assets/{van}/reminders", headers=auth, json=body))
    numbers = {a["id"]: a["number"] for a in (await get("/assets"))["items"]}
    await _ok(await client.post("/fixed-assets/api/checks", headers=auth, json={"tag": numbers[van]}))
    await _fa(client, auth, f"/assets/{laptops}/move", {"custodian": {"user_id": user_id}, "date": today.isoformat()})
    # An intangible with an indefinite life is reviewed for impairment each year.
    await _ok(await client.patch(f"/fixed-assets/api/classes/{classes['Other intangibles']}", headers=auth,
                                 json={"method": "none", "life_months": None}))
    setup = await get("/setup")
    await _fa(client, auth, "/imports", {"as_at": setup["settings"]["cutover"], "rows": [
        {"name": "Warehouse", "class": "Buildings", "available_date": "2020-01-01", "cost": "250000",
         "accumulated_depreciation": "40000"},
        {"name": "Trading licence", "class": "Other intangibles", "available_date": "2021-03-01", "cost": "8000",
         "accumulated_depreciation": "4000"}]})


async def seed_subscriptions(client: httpx.AsyncClient, auth: dict, contacts: list[dict], today: date) -> None:
    """The agreement pages in use: a SaaS plan with a subscriber, a seat subscription, a
    retainer, a membership billed for its first year, an agreement that bills itself and has
    billed today, and one so far behind that its periods wait on Catch-up."""
    from celerp.db import LifecycleSessionLocal
    from celerp.events.engine import unattended
    from celerp_docs import periodic

    customers = [c for c in contacts if c.get("contact_type") in ("customer", "both")]

    async def agreement(contact: dict, start: date, lines: list[dict], **fields) -> str:
        doc = await _ok(await client.post("/docs", headers=auth, json={
            "doc_type": "subscription_invoice", "contact_id": contact["id"], "frequency": "monthly",
            "start_date": start.isoformat(), "line_items": lines, **fields}))
        await _ok(await client.post(f"/subscriptions/{doc['id']}/activate", headers=auth))
        return doc["id"]

    def line(name: str, price: float, quantity: int = 1, **extra) -> dict:
        return {"description": name, "quantity": quantity, "unit_price": price, "line_total": quantity * price, **extra}

    await _ok(await client.post("/accounting/accounts", headers=auth, json={
        "code": "4190", "name": "Plan income", "account_type": "revenue", "parent_code": "4000"}))
    plans = (await _ok(await client.post("/companies/me/categories", headers=auth, json={"name": "saas_plan"})))["key"]
    await _ok(await client.put(f"/companies/me/categories/{plans}/income-account", headers=auth,
                               json={"income_account": "4190"}))
    team = (await _ok(await client.post("/items", headers=auth, json={
        "name": "Team plan", "sku": "PLAN-TEAM", "sell_by": "piece", "inventory_type": "service", "quantity": 0,
        "status": "available", "category": plans, "retail_price": 20.0})))["id"]
    await agreement(customers[0], today, [line("Team plan", 20.0, 3, entity_id=team)])
    await agreement(customers[1], today, [line("Seats", 20.0, 5)])
    await agreement(customers[2], today, [line("Monthly advisory retainer", 2500.0)], kind="retainer")
    member = await agreement(customers[3], today.replace(month=1, day=1), [line("Annual membership", 60.0)],
                             kind="membership", frequency="annually", grace_days=30,
                             beneficiary_contact_id=customers[4]["id"])
    await _ok(await client.post(f"/subscriptions/{member}/generate-due", headers=auth,
                                json={"request_key": uuid.uuid4().hex}))
    unattended_ones = (await agreement(customers[0], today, [line("Support plan", 150.0)]),
                       await agreement(customers[1], (today.replace(day=1) - timedelta(days=100)).replace(day=1),
                                       [line("Hosting", 80.0)]))
    for eid, behind in zip(unattended_ones, (None, 1)):
        await _ok(await client.patch(f"/subscriptions/{eid}/schedule", headers=auth, json={
            "generation_mode": "auto_invoice", **({"catchup_periods": behind} if behind else {})}))
    # One pass of the app's own billing loop, as it runs unattended in the background.
    company = await _ok(await client.get("/companies/me", headers=auth))
    await unattended(lambda: periodic.run_company(LifecycleSessionLocal, uuid.UUID(company["id"]), today=today))


async def seed_documents(client: httpx.AsyncClient, auth: dict, contacts: list[dict]) -> None:
    """The document pages in use: a quotation, a stock count, a draft memo with lines and one
    issued, and an invoice another company sent, waiting in Received."""
    customer = next(c for c in contacts if c.get("contact_type") in ("customer", "both"))
    stock = {i["sku"]: i for i in (await _ok(await client.get(
        "/items", headers=auth, params={"limit": 500})))["items"]}
    lines = [{"sku": sku, "name": name, "entity_id": stock[sku]["id"], "quantity": 1, "unit_price": retail}
             for sku, name, _category, _qty, _cost, retail, _wholesale in seed_demo._ITEMS[:2]]
    # The lists page shows issued lists; drafts sit under their own view.
    quotation = await _ok(await client.post("/lists", headers=auth, json={
        "list_type": "quotation", "contact_id": customer["id"], "line_items": lines}))
    await _ok(await client.post(f"/lists/{quotation['id']}/finalize", headers=auth))
    locations = await _ok(await client.get("/companies/me/locations", headers=auth))
    location = (locations.get("items") or locations.get("locations") or locations)[0]
    audit = await _ok(await client.post("/lists/audit", headers=auth, json={"location_id": location["id"]}))
    await _ok(await client.post(f"/lists/{audit['id']}/finalize", headers=auth))
    for issue in (False, True):
        memo = await _ok(await client.post("/docs", headers=auth, json={
            "doc_type": "memo", "contact_id": customer["id"], "line_items": lines}))
        if issue:  # issued, with its first item gone out to the customer
            await _ok(await client.post(f"/docs/{memo['id']}/finalize", headers=auth))
            await _ok(await client.post(f"/docs/{memo['id']}/fulfill-lines", headers=auth,
                                        json={"line_entity_ids": [lines[0]["entity_id"]]}))
    r = await client.post("/docs/import-bundle", headers=auth, follow_redirects=False, json={
        "version": 1,
        "source": {"installation": "example-supplier", "document": "doc:INV-0042", "revision": 1},
        "doc": {"doc_type": "invoice", "ref_id": "INV-0042", "company_name": "Example Supplier Ltd",
                "company_email": "billing@supplier.example.com", "contact_name": "Example Trading Co",
                "currency": "USD", "issue_date": "2026-11-02", "due_date": "2026-12-02",
                "line_items": [{"description": "Packing boxes", "sku": "BOX-L", "quantity": 40,
                                "unit_price": 2.5}]}})
    assert r.status_code == 302, r.text


async def seed_manufacturing(client: httpx.AsyncClient, auth: dict) -> None:
    """A product with a recipe, a production order for more of it than its runs cover, and
    its work orders at every stage of the queue: planned, in progress with its components
    issued, on hold, and completed."""
    stock = {i["sku"]: i["id"] for i in (await _ok(await client.get(
        "/items", headers=auth, params={"limit": 500})))["items"]}
    product = (await _ok(await client.post("/items", headers=auth, json={
        "sku": "BNDL-001", "name": "Home Office Bundle", "category": "Furniture", "quantity": 0,
        "retail_price": 950, "status": "available", "sell_by": "piece"})))["id"]
    await _ok(await client.put(f"/manufacturing/items/{product}/recipe", headers=auth, json={
        "output_qty": 1, "labor": [], "overhead": [], "components": [
            {"item_id": stock[sku], "quantity": 1} for sku in ("FURN-002", "ELEC-002", "ELEC-007")]}))
    order = await _ok(await client.post("/docs", headers=auth, json={"doc_type": "production_order", "line_items": [
        {"sku": "BNDL-001", "name": "Home Office Bundle", "entity_id": product, "quantity": 10, "unit_price": 950}]}))
    await _ok(await client.post(f"/docs/{order['id']}/finalize", headers=auth))
    runs = [(await _ok(await client.post(f"/manufacturing/items/{product}/build", headers=auth,
                                         json={"quantity": qty})))["id"] for qty in (2, 3, 1, 2)]
    _planned, issued, held, done = runs
    for run, steps in ((issued, ("start", "issue")), (held, ("start", "hold")), (done, ("complete",))):
        for step in steps:
            await _ok(await client.post(f"/manufacturing/{run}/{step}", headers=auth, json={}))


async def seed_company_details(client: httpx.AsyncClient, auth: dict, today: date) -> None:
    """The company's own record with its billing and shipping addresses and a document on
    file, and the opening balance date the tie-out report checks against."""
    company = await _ok(await client.get("/companies/me", headers=auth))
    own = company["settings"]["self_contact_id"]
    for kind in ("billing", "shipping"):
        await _ok(await client.post(f"/crm/contacts/{own}/addresses", headers=auth, json={
            "address_type": kind, "line1": "1 Example Street", "city": "Springfield",
            "postal_code": "10110", "country": "US"}))
    r = await client.post(f"/crm/contacts/{own}/files", headers=auth, files={
        "file": ("business-registration.txt", b"Example Trading Co business registration\n", "text/plain")})
    assert r.status_code == 200, r.text
    await _ok(await client.patch("/companies/me/books", headers=auth,
                                 json={"opening_balance_date": f"{today.year}-01-01"}))


@pytest.fixture(scope="module")
def demo_token(api_server, api):
    """A company of its own, filled with the same demo data every run."""
    r = api.post("/companies", json={"name": f"Help capture {uuid.uuid4().hex[:6]}"})
    assert r.status_code in (200, 201), r.text
    token = r.json()["access_token"]

    async def seed():
        auth = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(base_url=api_server, timeout=30.0) as client:
            # The app's own sample records (the fixed asset bills) are added only while the
            # company keeps no books of its own, so before anything below posts.
            reseeded = await _ok(await client.post("/companies/me/demo/reseed", headers=auth))
            assert reseeded["ok"], reseeded
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
            users = (await _ok(await client.get("/companies/me/users", headers=auth)))
            me = next(u for u in users.get("items", users) if u["email"] == _TEST_EMAIL)
            await seed_fixed_assets(client, auth, me["id"], business_today(None))
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
            await seed_subscriptions(client, auth, contacts, date.fromisoformat(today))
            await seed_funds_and_deferred_revenue(client, auth, contacts, date.fromisoformat(today))
            await seed_bank_statement(client, auth, date.fromisoformat(today))
            await seed_documents(client, auth, contacts)
            await seed_manufacturing(client, auth)
            await seed_company_details(client, auth, date.fromisoformat(today))
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
    # A rule that posts the monthly rent to the rent account.
    chart = (await _ok(await client.get("/accounting/chart", headers=auth)))["items"]
    rent = next(a["code"] for a in chart if a["name"] == "Rent")
    await _ok(await client.post("/accounting/rules", headers=auth, json={
        "match_pattern": "OFFICE RENT", "target_account_code": rent}))


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


def _empty_state_shown(target) -> list[str]:
    """The empty-state components visible in the part of ``target`` a picture keeps."""
    return target.evaluate("""(el, [sel, maxHeight]) => {
        const top = el.getBoundingClientRect().top;
        return [el, ...el.querySelectorAll(sel)]
            .filter(e => e.matches(sel) && e.checkVisibility()
                         && e.getBoundingClientRect().top - top < maxHeight)
            .map(e => `${e.className}: ${e.innerText.trim().slice(0, 80)}`);
    }""", [_EMPTY_STATE_SELECTOR, _MAX_HEIGHT])


def _write_image(target, path: Path) -> None:
    from PIL import Image

    with Image.open(io.BytesIO(target.screenshot())) as shot:
        image = shot.convert("RGB")
    image = image.crop((0, 0, image.width, min(image.height, _MAX_HEIGHT)))
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
    empty_states = []
    for n, step in enumerate(help_page.data["steps"], 1):
        capture = step["capture"]
        step_id = f"{help_page.data['id']}#{n}"
        with _subscription(_SUBSCRIBED.get(step_id)):
            response = demo_page.goto(capture["route"])
            assert response is not None and response.ok, f"step {n}: {capture['route']} did not load"
            for action in capture["actions"]:
                _run(demo_page, action)
        target = demo_page.locator(capture["selector"]).first
        expect(target).to_be_visible()
        if step_id not in _SHOWS_EMPTY_STATE and (shown := _empty_state_shown(target)):
            empty_states.append(f"{step_id} {shown}")
            continue
        for key in step["labels"].values():
            label = _visible_label(key)
            if label:
                shown = demo_page.get_by_text(label).or_(demo_page.get_by_placeholder(label))
                expect(shown.filter(visible=True).first).to_be_visible()
        if _WRITE:
            _write_image(target, help_page.help_dir / step["image"])
    assert not empty_states, "steps picturing an empty state:\n" + "\n".join(empty_states)
