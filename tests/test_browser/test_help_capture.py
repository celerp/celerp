"""Every shipped help page's steps match the running UI.

One case per page: each step's capture recipe opens its route on a company filled
with demo data, runs its actions, and the step's selector must be visible with every entry of its
evidence matching a visible element wholly inside the pictured part of it (a match elsewhere on the
page, hidden, or cut by the picture's edge does not count). The pictured part of the
screen must not show the app's own empty-list, Connect sales or nothing-selected components unless
the step is listed in _SHOWS_EMPTY_STATE, nor a record's internal id in place of its name.
With HELP_CAPTURE_WRITE=1 each step
is written as a webp of the selector's element (its top 800 pixels when taller) carrying the
capture marker, the only way a help image is made; the images are put in place when the run ends,
except where the committed picture already shows the same screen (see _RENDER_NOISE).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib.util
import io
import json
import os
import random
import re
import sys
import tempfile
import threading
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone
from functools import cache
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from playwright.sync_api import expect

import celerp.ai.service as ai_service
from celerp.help.bundle import MAX_IMAGE_BYTES
from celerp.help.checks import capture_marker, image_marker, shipped_pages
from celerp.help.schema import PLACEHOLDER
from celerp.services.business_time import business_today
from celerp_ai.ui_routes import QUESTION_POLL_S
from celerp_subscriptions import billing as subscription_billing
from celerp_labels.presets import PRESET_TEMPLATES
from test_helpers import grant_permission
from ui.i18n import t

from .conftest import _test_db, clear_session_registry, htmx_idle, set_session_cookies

pytestmark = pytest.mark.browser

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "default_modules" / "celerp-subscriptions" / "tests"))
from agreement_kit import freeze_day  # noqa: E402

_SEED_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "seed_demo.py"
_spec = importlib.util.spec_from_file_location("seed_demo_help_capture", _SEED_PATH)
seed_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_demo)

_WRITE = os.environ.get("HELP_CAPTURE_WRITE") == "1"
_MAX_WIDTH = 1000
_MAX_HEIGHT = 800  # CSS pixels: a taller element is cut to its top, where its screen starts
_EXIF_DESCRIPTION = 0x010E
# Who and what every picture shows: generic, the same every run.
_CAPTURE_COMPANY = "Example Trading Co"
_CAPTURE_NAME = "Alex Example"
_CAPTURE_EMAIL = "alex@example.com"
_CAPTURE_PASSWORD = "ExampleTrading1!"
# The business day the company is seeded on and every picture is taken on, whatever day the
# capture runs: the app's "today" (business_time) is frozen to it, as the agreement tests do.
_CAPTURE_DAY = date(2026, 6, 15)
_CAPTURE_NOON = datetime(2026, 6, 15, 12, tzinfo=timezone.utc)
_PAGES = [p for p in shipped_pages() if isinstance(p.data, dict)]
# The app's own components for a screen with nothing to show: an empty list or report, the
# Remote Access and Connect sales pages and banners, the AI page shown without an account, and
# a bulk page opened with no rows selected. A step whose picture shows one would show the
# reader a different screen from the one its text describes.
_EMPTY_STATE_SELECTOR = ", ".join((
    ".empty-state", ".empty-state-msg", ".empty-state-cta",
    ".cloud-hero", ".upgrade-banner", ".module-license-upsell", ".ai-showcase__headline",
    ".ai-empty-state__welcome", ".flash--error",
))
# The same screens drawn without those components: every English message whose key names it
# an empty or nothing-to-show message (``no_...``, ``...empty...``, ``...nothing...``).
_EMPTY_KEY = re.compile(r"^no_|(^|_)(empty|nothing)(_|$)")
_EMPTY_MESSAGES = sorted({
    text.split("{", 1)[0].strip()
    for catalog in (_REPO / "ui/locales/en.json", *_REPO.glob("default_modules/*/*/locales/en.json"))
    for key, text in json.loads(catalog.read_text(encoding="utf-8")).items()
    if _EMPTY_KEY.search(key.rsplit(".", 1)[-1]) and len(text.split("{", 1)[0].strip()) >= 12
})
# A record's internal id ("contact:<uuid>", "item:<uuid>"): a screen showing one in place of a
# name shows the reader a random string where its text names a record, and a different one
# every run.
_RAW_ID = re.compile(r"\b[a-z_]+:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
# The capture runs only the bundled modules, each matching the lock, so none reads as sideloaded.
_SIDELOADED = t("modules.src_sideloaded", "en")
# Steps whose text is about that empty or sales screen itself: "<page id>#<step>": why.
_CONNECT_SALES = "the step names what Celerp Connect adds, which its sales page lists"
_NO_TAX_REGIME = "no first-party module ships a tax regime, so the page shows where to get one"
_SHOWS_EMPTY_STATE: dict[str, str] = {
    "ai.ask-questions#2": "the step is about the box a question is typed into, which a new chat shows",
    "fixed-assets.tax-books#1": _NO_TAX_REGIME,
    "fixed-assets.year-end#1": _NO_TAX_REGIME,
    "fixed-assets.year-end#2": _NO_TAX_REGIME,
    "core.settings-payments#1": _CONNECT_SALES,
    "backup.cloud-backup#1": _CONNECT_SALES,
    "connectors.connect-store#1": _CONNECT_SALES,
    "connectors.connect-store#2": _CONNECT_SALES,
    "backup.local-backup#1": "the cloud recovery points beside the local section need Celerp Connect",
    "backup.local-backup#2": "the cloud recovery points beside the local section need Celerp Connect",
    "backup.local-backup#3": "the cloud recovery points beside the local section need Celerp Connect",
    "core.accounting-sync-accounts#1": "a new company's accounts have nothing to fix, the state the step describes",
    "core.accounting-sync-accounts#2": "a new company's accounts have nothing to fix, the state the step describes",
    "fixed-assets.settings#1": _NO_TAX_REGIME,
    "fixed-assets.settings#3": _NO_TAX_REGIME,
}
# Pages whose steps add records (a sample import, an AI conversation and the item it adds):
# each is taken on a company of its own.
_ADDS_RECORDS = {"core.imports", "ai.ask-questions", "ai.review-proposals"}
# Pages open to the installation owner only, which show no company or person: taken as the
# suite's user, who owns the installation; the capture refuses a picture that names it.
_INSTALLATION_PAGES = {"backup.local-backup"}
# Steps about what an active Celerp Connect subscription or Celerp AI account shows: the
# installation is given one through the seams the Connect and AI browser tests use, for that
# step only, so the steps about the sales pages still show them.
_ACTIVE_RELAY = {"connected": True, "relay_status": "active", "public_url": "https://example.celerp.com",
                 "tier": "cloud", "subscription_status": "active", "cloud_disconnected": False,
                 "gateway_token_set": True, "entitlement_known": True, "entitled": True}
_AI_ACCOUNT = {"allowed": True, "used": 4, "base_limit": 200, "topup_balance": 0, "remaining": 196,
               "limit": 200, "topup_credits": 0, "resets_at": None, "tier": "ai"}
_CONNECT, _AI, _COPIES = "connect", "ai", "copies"


async def _backup_status(_token):
    """The backup schedule an active subscription shows: last backup at the capture day's 02:00
    UTC, the next one due six hours after the page reads it (the page shows the time until it)."""
    due = datetime.now(timezone.utc) + timedelta(hours=6, seconds=30)
    return {"db": {"ok": True, "error": None, "size_bytes": 4_200_000,
                   "last_run": f"{_CAPTURE_DAY.isoformat()}T02:00:00+00:00"},
            "next_db_utc": due.isoformat()}
async def _connectors_catalog(_token):
    """The connectors the relay lists to an active subscription, the seeded Shopify store connected."""
    from ui.routes.settings_connectors import _local_connector_entry

    return [{**_local_connector_entry(p), "connected": p == "shopify"}
            for p in ("shopify", "woocommerce", "quickbooks", "xero")], "", False
# A company copy sent to an accountant, as the cloud lists it to the account that sent it.
_SENT_COPY_ID, _SENT_LINK_ID = uuid.UUID(int=0x1C0), uuid.UUID(int=0x11C)
_SENT_COPY = {"id": str(_SENT_COPY_ID), "link_id": str(_SENT_LINK_ID), "status": "sent", "stored": True,
              "can_restore": False, "opened": True, "downloaded": False, "company_opened": False,
              "sent_at": "2026-06-12T09:30:00+00:00", "expires_at": "2026-06-26T09:30:00+00:00",
              "firm_name": "Example Accounting"}
# The free email quota the relay advertises to an installation that is not connected, which
# offers "Send by email" on a document. The app keeps the relay's answer in a process cache it
# fills on the first document opened, so it is pinned: otherwise a picture would show the offer
# or not depending on which pages the run opened before it.
_FREE_EMAIL_QUOTA = 50
# Charts draw with an animation the screenshot's own "animations disabled" cannot stop (it is
# script, not CSS), so a picture would catch a chart part-way through. Chart.js is switched to
# draw at once the moment its script defines it, in the capture's browser only.
# A page an action opens in a new tab (a printable list) opens in the capture's own tab instead,
# so the step pictures it.
_CAPTURE_PAGE_SCRIPT = """Object.defineProperty(window, "Chart", {configurable: true,
  set(chart) { chart.defaults.animation = false;
               Object.defineProperty(window, "Chart", {value: chart, writable: true, configurable: true}); }});
window.open = url => { location.assign(url); return null; };"""
# A step on the assistant's screen is taken with the AI subscription; the two that picture what a
# company without it sees are listed as None.
_SUBSCRIBED: dict[str, str | None] = {
    "core.company-handoff#2": _COPIES, "core.company-handoff#4": _COPIES,
    "core.web-access#2": _CONNECT, "backup.cloud-backup#2": _CONNECT,
    "connectors.connect-store#3": _CONNECT, "connectors.connect-store#4": _CONNECT,
    "ai.ask-questions#1": None, "ai.ask-questions#3": None,
}


@cache
def _sage50_set() -> Path:
    from fixtures.sage50_export import build

    path = Path(tempfile.mkdtemp()) / "sage50.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in build.files(build.simulate()).items():
            zf.writestr(name, data)
    return path


@contextlib.contextmanager
def _store_reads():
    from celerp import connectors
    from celerp.connectors.base import ConnectorContext, RemoteOrder, RemoteProduct

    products = [RemoteProduct(key=str(n), product_id=str(n), variation_id=None, sku=sku, name=name,
                              description=None, price=price, modified=None, stock=stock)
                for n, (sku, name, price, stock) in enumerate((
                    ("MUG-001", "Stoneware Mug", 12.0, 40), ("TEE-001", "Cotton T-Shirt", 18.0, 25),
                    ("CAP-001", "Canvas Cap", 15.0, 12)), 1)]
    orders = [RemoteOrder(id=str(n), number=f"#10{n:02d}", status="paid",
                          created=datetime(2026, 6, n + 8, 10, tzinfo=timezone.utc), total=total,
                          currency="USD", posts=True)
              for n, total in enumerate((36.0, 18.0, 45.0), 1)]

    async def context(company_id, *_a, **_k):
        return ConnectorContext(company_id=str(company_id), access_token="t",
                                store_handle="https://example-store.myshopify.com")

    async def read_products(_self, _ctx):
        return list(products)

    async def read_orders(_self, _ctx, _orders_from):
        return list(orders)

    store = type(connectors.get("shopify"))
    with patch("celerp.connectors.relay_token.fetch_context", context), \
            patch.object(store, "preview_products", read_products), \
            patch.object(store, "preview_orders", read_orders):
        yield


@contextlib.contextmanager
def _subscription(kind: str | None):
    from celerp.gateway.state import set_session_token

    with contextlib.ExitStack() as stack:
        if kind:
            # Both subscriptions sign this installation in to Celerp, as connecting an account does.
            set_session_token("help-capture-session-token")
            stack.callback(set_session_token, "")
        if kind == _CONNECT:
            stack.enter_context(patch("ui.api_client.get_relay_status", AsyncMock(return_value=_ACTIVE_RELAY)))
            stack.enter_context(patch("ui.api_client.get_backup_status", _backup_status))
            stack.enter_context(patch("ui.api_client.get_connectors_catalog", _connectors_catalog))
        elif kind == _COPIES:
            async def cloud(method, path, *, params=None, **_kw):
                assert (method, path) == ("GET", "/handoffs"), (method, path)
                return httpx.Response(200, json={"items": [{**_SENT_COPY, "company_id": params["company_id"]}]})
            stack.enter_context(patch("celerp.services.cloud_entitlement.authenticated_request", cloud))
        elif kind == _AI:
            stack.enter_context(patch("celerp.ai.quota.get_quota_status", AsyncMock(return_value=_AI_ACCOUNT)))
            stack.enter_context(patch.object(ai_service, "complete", _scripted_model()))
            # The answer lands at the question's next poll, after the API schema is built.
            stack.callback(expect.set_options, timeout=5000)
            expect.set_options(timeout=5000 + 4 * QUESTION_POLL_S * 1000)
        yield


def _scripted_model(*lamps: tuple[str, str]):
    """The model of an AI step, as tests/test_browser/test_ai_chat_journey.py scripts it: each
    request in turn is answered with a proposal to add one of ``lamps`` (SKU, name), so the
    step pictures its card."""
    from celerp.ai.llm import ModelResult
    from celerp.ai.tools import compile_agent_capabilities
    from celerp.main import app

    create_item = next(name for name, cap in compile_agent_capabilities(app, {}).items()
                       if (cap["method"], cap["path"]) == ("POST", "/items"))
    turns = [{"role": "assistant", "content": f"I'll add the {name.lower()} once you confirm.", "tool_calls": [{
        "id": f"call-help-capture-{sku}", "type": "function", "function": {"name": create_item, "arguments": json.dumps(
            {"body": {"sku": sku, "name": name, "category": "Electronics", "retail_price": 45,
                      "sell_by": "piece"}})}}]} for sku, name in lamps or (("LAMP-001", "Desk Lamp"),)]

    async def complete(messages, **_kw) -> ModelResult:
        message = turns.pop(0) if turns else {"role": "assistant", "content": "Desk Lamp is now in your stock."}
        return ModelResult(message=message, model_used="help-capture", usage={}, reservation_id=None,
                           remaining=None)
    return complete


@contextlib.contextmanager
def _answer_never_came():
    """A confirmed change whose answer never came, as the executor reports a step past its
    deadline: the card keeps the change open with the reason and Retry."""
    from celerp.ai import tools as ai_tools

    seconds = int(ai_tools.deadlines.TOOL_STEP_S)
    timeout = ai_tools._agent_error("capability_timeout", "ai.err_capability_timeout",
                                    f"The operation did not finish within {seconds} seconds.", seconds=seconds)
    with patch.object(ai_tools, "execute_agent_capability", AsyncMock(return_value=timeout)):
        yield


_LAMPS = (("LAMP-001", "Desk Lamp"), ("LAMP-002", "Floor Lamp"), ("LAMP-003", "Reading Lamp"),
          ("LAMP-004", "Wall Lamp"))


@contextlib.contextmanager
def _older_proposals():
    """Four questions each left with an open proposal, in a conversation kept to two messages
    as tests/test_protected_work.py caps it: reopened, it shows the newest message and the two
    cards before it under Show older actions."""
    from celerp.ai import conversations

    with patch.object(conversations, "MAX_MESSAGES_PER_CONVERSATION", 2), \
            patch.object(ai_service, "complete", _scripted_model(*_LAMPS)):
        yield


# A step whose state the capture company does not reach alone is taken inside its own seam:
# - core.imports#5: the sample books leave nothing to decide, so they go from coverage straight
#   to review; the question a file can leave open is taken with the sample swapped for a Sage 50
#   export set, which asks the currency its books are kept in.
# - connect-store#4: a store's first sync preview reads the store, which is given a few products
#   and orders through the seams tests/test_first_sync_preview_receipt.py uses.
# - review-proposals#3 and #4: a change that can be tried again, and proposals past the first page.
_STEP_STATE = {
    "core.imports#5": lambda: patch("celerp.importers.sample.SAMPLE_ARTIFACT", _sage50_set()),
    "connectors.connect-store#4": _store_reads,
    "ai.review-proposals#3": _answer_never_came,
    "ai.review-proposals#4": _older_proposals,
}


async def _ok(response: httpx.Response) -> dict:
    assert response.status_code in (200, 201), response.text
    return response.json()


async def seed_funds_and_deferred_revenue(client: httpx.AsyncClient, auth: dict, contacts: list[dict],
                                          today: date) -> None:
    """Funds and revenue recognition show their controls only once they are in use: funds
    on, a restricted fund, an archived fund, a transfer and two imported invoices, one waiting
    for a fund choice and one posted on a fund; an invoice deferring revenue over
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
    # Two invoices from a file import: one waits for a fund choice, one posted on the reserve.
    for ref in ("IMP-0001", "IMP-0002"):
        await _ok(await client.post("/docs/import", headers=auth, json={
            "entity_id": f"doc:{ref}", "event_type": "doc.created", "source": "csv", "idempotency_key": key(),
            "data": {"doc_type": "invoice", "status": "draft", "ref_id": ref, "contact_id": contacts[3]["id"],
                     "issue_date": today.isoformat(), "total": 300.0, "line_items": [
                         {"item_id": None, "name": "Workshop", "quantity": 1, "unit_price": 300.0, "line_total": 300.0}]}}))
    version = (await _ok(await client.get("/docs/doc:IMP-0002", headers=auth)))["version"]
    await _ok(await client.post("/docs/doc:IMP-0002/fund-choices", headers=auth, json={
        "expected_version": version, "document": reserve["id"]}))
    await _ok(await client.post("/docs/doc:IMP-0002/finalize", headers=auth, json={}))


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


async def seed_documents(client: httpx.AsyncClient, auth: dict, contacts: list[dict], today: date) -> None:
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
                "currency": "USD", "issue_date": (today - timedelta(days=13)).isoformat(),
                "due_date": (today + timedelta(days=17)).isoformat(),
                "line_items": [{"description": "Packing boxes", "sku": "BOX-L", "quantity": 40,
                                "unit_price": 2.5}]}})
    assert r.status_code == 302, r.text
    # Moved in from another system: an unpaid invoice, and a bill for goods not yet received,
    # that the opening balances hold.
    vendor = next(c for c in contacts if c.get("contact_type") in ("vendor", "both"))
    goods = {"item_id": lines[0]["entity_id"], "sku": lines[0]["sku"], "name": lines[0]["name"]}
    for doc_type, contact, number, line in (("invoice", customer, "OLD-INV-118", {"name": "Office supplies"}),
                                            ("bill", vendor, "OLD-BILL-207", goods)):
        await _ok(await client.post("/docs/import", headers=auth, json={
            "entity_id": f"doc:{number}", "event_type": "doc.created", "source": "csv",
            "idempotency_key": f"help-capture-{number}", "data": {
                "doc_type": doc_type, "ref_id": number, "contact_id": contact["id"], "currency": "USD",
                "issue_date": f"{today.year - 1}-12-15", "status": "awaiting_payment",
                "import_treatment": "opening_balances",
                "line_items": [{**line, "quantity": 1, "unit_price": 400}],
                "subtotal": 400, "total": 400, "amount_outstanding": 400}}))


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
    # A run's reference (WO- and the start of its id) follows the build's key: fixed keys give
    # the same references in every capture.
    runs = [(await _ok(await client.post(f"/manufacturing/items/{product}/build", headers=auth, json={
        "quantity": qty, "idempotency_key": f"capture-run-{n}"})))["id"] for n, qty in enumerate((2, 3, 1, 2), 1)]
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


async def seed_ai_usage(company_id: str, user_id: str) -> None:
    """Questions to Celerp AI the service has charged for this month, recorded as the app
    records every reply, so the usage table lists who asked."""
    from celerp.ai.usage import record_usage
    from celerp.db import LifecycleSessionLocal

    async with LifecycleSessionLocal() as session:
        for credits in (1, 2, 1):
            await record_usage(session, company_id=uuid.UUID(company_id), user_id=uuid.UUID(user_id),
                               operation_id=uuid.uuid4(), credits=credits, known=True)
        await session.commit()


async def seed_store_connection(company_id: str) -> None:
    """A Shopify store connected to the company, as connecting it records, so a connection's
    own page shows its status, sync and disconnect."""
    from celerp.connectors.ownership import claim_connector_ownership
    from celerp.db import LifecycleSessionLocal

    async with LifecycleSessionLocal() as session:
        await claim_connector_ownership(session, company_id, "shopify", default_sync_frequency="realtime")
        await session.commit()


async def seed_company_copies(company_id: str) -> None:
    """What this installation keeps of the copy in _SENT_COPY (who it went to, its link's key),
    as sending records it, and a copy of the company saved when a copy replaced it."""
    from celerp.services import company_backup, company_backup_files

    company_backup_files.remember_sent_copy(company_id, _SENT_COPY_ID, {
        "recipient": "client@example.com", "key": base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")})

    class _Noon(datetime):
        @classmethod
        def now(cls, tz=None):
            return _CAPTURE_NOON

    with patch.object(company_backup, "datetime", _Noon):
        await company_backup._keep_backup(uuid.UUID(company_id), _CAPTURE_COMPANY)


@contextlib.contextmanager
def _capture_clock():
    """The business day is _CAPTURE_DAY and the wall clock subscriptions read (what their
    summary looks back from) is that day's evening, after every record _date_records dates."""
    with pytest.MonkeyPatch.context() as mp:
        freeze_day(mp, _CAPTURE_DAY.isoformat())
        mp.setattr(subscription_billing, "now", lambda: _CAPTURE_NOON + timedelta(hours=6))
        yield


def _sign_in(api_server: str, email: str) -> str:
    """An access token for ``email``; the installation's one-session slot is left free, as
    test_partner_claim_gate leaves it, so the suite's own sign-ins still go through."""
    clear_session_registry()
    try:
        r = httpx.post(f"{api_server}/auth/login", json={"email": email, "password": _CAPTURE_PASSWORD},
                       timeout=10)
        assert r.status_code == 200, r.text
        return r.json()["access_token"]
    finally:
        clear_session_registry()


def capture_company(api_server: str, api, email: str, *, connect_store: bool = False) -> dict:
    """The capture company, filled with the same demo data every run, and an access token of
    its owner: a member named _CAPTURE_NAME who belongs to no other company, so a picture shows
    the same company, person and company list wherever it is taken. A store connects to one
    company of an installation, so only the shared capture company is given one."""
    r = api.post("/companies/me/users", json={"email": email, "name": _CAPTURE_NAME, "role": "viewer",
                                              "password": _CAPTURE_PASSWORD})
    assert r.status_code == 200, r.text
    user_id = r.json()["id"]
    r = httpx.post(f"{api_server}/companies", json={"name": _CAPTURE_COMPANY}, timeout=30,
                   headers={"Authorization": f"Bearer {_sign_in(api_server, email)}"})
    assert r.status_code == 200, r.text
    # Added to the suite's company only to reach the companies route, then taken out of it.
    r = api.patch(f"/companies/me/users/{user_id}", json={"is_active": False})
    assert r.status_code == 200, r.text
    token = _sign_in(api_server, email)
    started = datetime.now(timezone.utc)

    async def seed():
        auth = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(base_url=api_server, timeout=30.0) as client:
            # The app's own sample records (the fixed asset bills) are added only while the
            # company keeps no books of its own, so before anything below posts.
            reseeded = await _ok(await client.post("/companies/me/demo/reseed", headers=auth))
            assert reseeded["ok"], reseeded
            await seed_demo.seed_items(client, token)
            contacts = await seed_demo.seed_contacts(client, token)
            await seed_demo.seed_docs(client, token, contacts, "invoice", 12, _CAPTURE_DAY)
            await seed_demo.seed_docs(client, token, contacts, "purchase_order", 6, _CAPTURE_DAY)
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
            await seed_fixed_assets(client, auth, user_id, business_today(None))
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
            await seed_documents(client, auth, contacts, date.fromisoformat(today))
            await seed_manufacturing(client, auth)
            await seed_company_details(client, auth, date.fromisoformat(today))
            company_id = (await _ok(await client.get("/companies/me", headers=auth)))["id"]
            await seed_ai_usage(company_id, user_id)
            if connect_store:
                await seed_store_connection(company_id)
            await seed_company_copies(company_id)
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
    with _capture_clock():
        thread.start()
        thread.join()
    if error:
        raise error[0]
    r = httpx.get(f"{api_server}/companies/me", headers={"Authorization": f"Bearer {token}"}, timeout=10)
    assert r.status_code == 200, r.text
    _date_records(r.json()["id"], started)
    return {"token": token, "id": r.json()["id"]}


_ISO = re.compile(r"\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?")


def _run_days(started: datetime) -> set[date]:
    """The days the run's real time falls on, in UTC and in local time."""
    return {started.date(), datetime.now(timezone.utc).date(), started.astimezone().date(), date.today()}


def _dated(value, started: datetime, until: datetime):
    """``value`` with every date or time the run wrote at its real time (from ``started`` to
    ``until``) moved to _CAPTURE_DAY."""
    if isinstance(value, dict):
        return {k: _dated(v, started, until) for k, v in value.items()}
    if isinstance(value, list):
        return [_dated(v, started, until) for v in value]
    if not isinstance(value, str) or not _ISO.fullmatch(value):
        return value
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return value
    if len(value) == 10:
        return _CAPTURE_DAY.isoformat() if when.date() in _run_days(started) else value
    when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    return _CAPTURE_NOON.isoformat() if started <= when <= until else value


def _date_records(company_id: str, started: datetime) -> None:
    """Dates what the seed wrote at the run's real time (record times, upload, receipt and
    approval times in the records' data) on _CAPTURE_DAY, so no picture depends on the day the
    capture ran. Record times keep their order: the n-th becomes noon plus n seconds."""
    until = datetime.now(timezone.utc)
    conn = _test_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT c.table_name, c.column_name, c.data_type FROM information_schema.columns c"
                        " JOIN information_schema.columns k ON k.table_schema = c.table_schema"
                        "  AND k.table_name = c.table_name AND k.column_name = 'company_id'"
                        " JOIN information_schema.tables t ON t.table_schema = c.table_schema"
                        "  AND t.table_name = c.table_name AND t.table_type = 'BASE TABLE'"
                        " WHERE c.table_schema = 'public' AND c.data_type IN"
                        " ('timestamp with time zone', 'timestamp without time zone', 'date', 'json', 'jsonb')")
            for table, column, kind in cur.fetchall():
                where = f"company_id::text = %s AND {column} IS NOT NULL"
                if kind.startswith("timestamp"):
                    aware = kind.endswith("with time zone")
                    noon, start, end = ((t if aware else t.replace(tzinfo=None)) for t in (_CAPTURE_NOON, started, until))
                    cur.execute(f"UPDATE {table} SET {column} = %s + n * interval '1 second' FROM"
                                f" (SELECT ctid AS row, row_number() OVER (ORDER BY {column}, ctid) AS n"
                                f"  FROM {table} WHERE {where} AND {column} BETWEEN %s AND %s) d"
                                f" WHERE {table}.ctid = d.row", (noon, company_id, start, end))
                elif kind == "date":
                    cur.execute(f"UPDATE {table} SET {column} = %s WHERE {where} AND {column} = ANY(%s)",
                                (_CAPTURE_DAY, company_id, list(_run_days(started))))
                else:
                    cur.execute(f"SELECT ctid, {column} FROM {table} WHERE {where}", (company_id,))
                    for row, value in cur.fetchall():
                        if (moved := _dated(value, started, until)) != value:
                            cur.execute(f"UPDATE {table} SET {column} = %s::{kind} WHERE ctid = %s",
                                        (json.dumps(moved), row))
    finally:
        conn.close()


@pytest.fixture
def core_sample_records_only():
    """The capture companies keep the sample records modules add (the asset bills the fixed
    asset pages register), whichever test creates them."""


@pytest.fixture
def _mock_get_modules_default():
    """The modules page lists the modules the app finds, not the suite's default of none."""


@pytest.fixture(scope="session")
def demo_company(api_server, api):
    return capture_company(api_server, api, _CAPTURE_EMAIL, connect_store=True)


@pytest.fixture(scope="session")
def suite_owner(api) -> dict:
    """The suite's own company, as a capture company, and the names no picture may show: its
    name and its owners' names and emails."""
    me = api.get("/companies/me").json()
    owners = [u for u in api.get("/companies/me/users").json()["items"] if u["role"] == "owner"]
    return {"token": api.headers["Authorization"].removeprefix("Bearer "), "id": me["id"],
            "names": {me["name"], *(u[k] for u in owners for k in ("name", "email"))}}


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


def _records(company_id: str) -> int:
    """How many records the company holds: its ledger and its AI conversations."""
    conn = _test_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT (SELECT count(*) FROM ledger WHERE company_id = %s)"
                        " + (SELECT count(*) FROM ai_conversations WHERE company_id = %s)", (company_id,) * 2)
            return cur.fetchone()[0]
    finally:
        conn.close()


def _today_written(day: date) -> set[str]:
    """``day`` as the app writes a date, and its month unless that is also _CAPTURE_DAY's."""
    if day == _CAPTURE_DAY:
        return set()
    days = {day.isoformat(), f"{day.month:02d}/{day.day:02d}/{day.year}", f"{day.month}/{day.day}/{day.year}",
            f"{day.day:02d}/{day.month:02d}/{day.year}", f"{day.day}/{day.month}/{day.year}",
            f"{day:%b} {day.day}, {day.year}", f"{day:%B} {day.day}, {day.year}",
            f"{day.day} {day:%b} {day.year}", f"{day.day} {day:%B} {day.year}"}
    if (day.year, day.month) != (_CAPTURE_DAY.year, _CAPTURE_DAY.month):
        days |= {f"{day:%B} {day.year}", f"{day:%b} {day.year}", f"{day.year}-{day.month:02d}"}
    return days


def _pictured_text(target) -> str:
    """The text of the part of ``target`` a picture keeps."""
    return target.evaluate("""(el, maxHeight) => {
        const top = el.getBoundingClientRect().top, walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
        const kept = [];
        for (let n = walker.nextNode(); n; n = walker.nextNode()) {
            const range = document.createRange();
            range.selectNodeContents(n);
            const box = range.getBoundingClientRect();
            if (box.height && box.top - top < maxHeight) kept.push(n.textContent);
        }
        return kept.join(" ");
    }""", _MAX_HEIGHT)


def _run(page, action: dict) -> None:
    if "click" in action:
        page.locator(action["click"]).first.click()
    elif "fill" in action:
        page.locator(action["fill"]).first.fill(action["value"])
    elif "select" in action:
        page.locator(action["select"]).first.select_option(action["value"])
    elif "upload" in action:
        page.locator(action["upload"]).first.set_input_files(
            {"name": "import.csv", "mimeType": "text/csv", "buffer": action["value"].encode()})
    elif "press" in action:
        page.keyboard.press(action["press"])
    else:
        expect(page.locator(action["wait"]).first).to_be_visible()


def _visible_label(key: str) -> str:
    """The English label as it shows on screen; a label with a {param} is matched up to it."""
    return t(key, "en").split("{", 1)[0].strip()


def _evidence_locator(page, entry: str, labels: dict):
    """What one evidence entry matches: a {name} alone is the English text of that label (or a
    field showing it as placeholder), anything else a selector, with each {name} in it standing
    for that text (``.step-label--active:text-is('{map}')``)."""
    if name := PLACEHOLDER.fullmatch(entry):
        label = _visible_label(labels[name.group(1)])
        return page.get_by_text(label).or_(page.get_by_placeholder(label))
    return page.locator(PLACEHOLDER.sub(
        lambda m: _visible_label(labels[m.group(1)]).replace("\\", "\\\\").replace("'", "\\'"), entry))


def _evidence_missing(page, target, step: dict) -> list[str]:
    """The step's evidence entries with no visible match lying wholly inside the part of
    ``target`` a picture keeps (its top _MAX_HEIGHT pixels): a match elsewhere on the page,
    hidden, or cut by the picture's edge does not count."""
    clip = target.evaluate("""(el, maxHeight) => {
        const r = el.getBoundingClientRect();
        return [r.left, r.top, r.right, r.top + Math.min(r.height, maxHeight)];
    }""", _MAX_HEIGHT)
    return [entry for entry in step["capture"]["evidence"]
            if not _evidence_locator(page, entry, step["labels"]).evaluate_all("""(els, [l, t, r, b]) =>
                els.some(e => {
                    const box = e.getBoundingClientRect();
                    return e.checkVisibility() && box.width > 0 && box.height > 0 && box.left >= l - 1
                        && box.top >= t - 1 && box.right <= r + 1 && box.bottom <= b + 1;
                })""", clip)]


def _empty_state_shown(target) -> list[str]:
    """The empty-state components visible in the part of ``target`` a picture keeps."""
    return target.evaluate("""(el, [sel, maxHeight, emptyMessages]) => {
        const top = el.getBoundingClientRect().top;
        const components = [el, ...el.querySelectorAll(sel)]
            .filter(e => e.matches(sel) && e.checkVisibility()
                         && e.getBoundingClientRect().top - top < maxHeight)
            .map(e => `${e.className}: ${e.innerText.trim().slice(0, 80)}`);
        const messages = [el, ...el.querySelectorAll("*")]
            .filter(e => e.checkVisibility() && e.getBoundingClientRect().top - top < maxHeight)
            .map(e => [...e.childNodes].filter(n => n.nodeType === Node.TEXT_NODE)
                                      .map(n => n.textContent).join("").trim())
            .filter(text => text && emptyMessages.some(m => text.startsWith(m)));
        return [...components, ...messages.map(text => `message: ${text.slice(0, 80)}`)];
    }""", [_EMPTY_STATE_SELECTOR, _MAX_HEIGHT, _EMPTY_MESSAGES])


# Pictures a write run has taken, put in place when the run ends: an image under a module
# changes that module's digest, which the app checks against the lock it loaded at start, so
# an image written mid-run would make every later picture show its module as sideloaded.
_TAKEN: dict[Path, bytes] = {}


@pytest.fixture(scope="session", autouse=True)
def _put_pictures_in_place():
    yield
    for path, data in _TAKEN.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


# Chromium anti-aliases some thin lines (a dotted underline at a fractional pixel offset) a
# little differently from one page load to the next, the same page and data giving one of a few
# pictures that differ by a few shades. A pixel counts as changed past this many shades of 255
# (the noise measured stays under 30; any changed text, line or colour is far past it).
_RENDER_NOISE = 40


def _same_picture(committed: bytes, taken: bytes) -> bool:
    """True when ``taken`` shows what ``committed`` shows, apart from anti-aliasing noise."""
    from PIL import Image, ImageChops

    with Image.open(io.BytesIO(committed)) as old, Image.open(io.BytesIO(taken)) as new:
        if old.size != new.size:
            return False
        diff = ImageChops.difference(old.convert("RGB"), new.convert("RGB"))
    return max(high for _, high in diff.getextrema()) <= _RENDER_NOISE


def _write_image(target, path: Path, marker: str) -> None:
    from PIL import Image

    with Image.open(io.BytesIO(target.screenshot(animations="disabled"))) as shot:
        image = shot.convert("RGB")
    image = image.crop((0, 0, image.width, min(image.height, _MAX_HEIGHT)))
    if image.width > _MAX_WIDTH:
        image = image.resize((_MAX_WIDTH, round(image.height * _MAX_WIDTH / image.width)))
    exif = Image.Exif()
    exif[_EXIF_DESCRIPTION] = marker
    for quality in (80, 65, 50, 35, 20):
        buf = io.BytesIO()
        image.save(buf, "WEBP", quality=quality, method=6, exif=exif)
        if buf.tell() <= MAX_IMAGE_BYTES:
            break
    # A retake that looks the same keeps the committed file only while it carries this marker.
    if not (path.is_file() and _same_picture(path.read_bytes(), buf.getvalue()) and image_marker(path) == marker):
        _TAKEN[path] = buf.getvalue()


def _capture_step(demo_page, help_page, step: dict, step_id: str, foreign: set[str]) -> list[str]:
    """Opens one step's screen and checks it; the empty states, the run's own day or the
    ``foreign`` company and people it pictures, if any, else takes its picture when writing."""
    capture = step["capture"]
    response = demo_page.goto(capture["route"])
    assert response is not None and response.ok, f"{step_id}: {capture['route']} did not load"
    for action in capture["actions"]:
        _run(demo_page, action)
    demo_page.mouse.move(0, 0)  # no hover state where the last click left the pointer
    htmx_idle(demo_page)
    target = demo_page.locator(capture["selector"]).first
    expect(target).to_be_visible()
    if step_id not in _SHOWS_EMPTY_STATE and (shown := _empty_state_shown(target)):
        return shown
    text = _pictured_text(target)
    if dated := sorted(d for day in _run_days(datetime.now(timezone.utc)) for d in _today_written(day) if d in text):
        return [f"the day the capture ran: {dated}"]
    if named := sorted(n for n in foreign if n in text):
        return [f"not the capture company or user: {named}"]
    if ids := sorted(set(_RAW_ID.findall(text))):
        return [f"a record id in place of its name: {ids}"]
    if _SIDELOADED in text:
        return ["a bundled module shown as sideloaded: its files differ from the module lock"]
    if missing := _evidence_missing(demo_page, target, step):
        return [f"no visible match wholly inside the picture for evidence {missing}"]
    if _WRITE:
        page_id, n = step_id.rsplit("#", 1)
        _write_image(target, help_page.help_dir / step["image"], capture_marker(page_id, int(n), step["capture"]))
    return []


@pytest.mark.parametrize("help_page", _PAGES, ids=[p.data.get("id", p.path.name) for p in _PAGES])
def test_help_capture_every_page(page, browser_context, api_server, api, demo_company, suite_owner, help_page,
                                 tools_open_when_called):
    page_id = help_page.data["id"]
    # A page that adds records is taken on a company of its own, so no other page's picture
    # depends on which pages ran before it.
    company = (capture_company(api_server, api, _CAPTURE_EMAIL.replace("@", f"+{page_id}@"))
               if page_id in _ADDS_RECORDS
               else suite_owner if page_id in _INSTALLATION_PAGES else demo_company)
    set_session_cookies(browser_context, company["token"])
    page.add_init_script(_CAPTURE_PAGE_SCRIPT)
    records = _records(company["id"])
    wrong = []
    for n, step in enumerate(help_page.data["steps"], 1):
        step_id = f"{page_id}#{n}"
        # The installed modules as the app finds them, from the bundled tree (the suite's
        # process runs with none, which the modules page would picture as none installed).
        with _capture_clock(), _subscription(_SUBSCRIBED.get(step_id, _AI if step["capture"]["route"].startswith("/ai") else None)), \
                patch.dict(os.environ, {"MODULE_DIR": "default_modules"}), \
                patch("ui.routes.documents._free_send_quota", return_value=_FREE_EMAIL_QUOTA), \
                _STEP_STATE.get(step_id, contextlib.nullcontext)():
            try:
                if shown := _capture_step(page, help_page, step, step_id, suite_owner["names"]):
                    wrong.append(f"{step_id} {shown}")
            finally:
                # Leave the page while the step's subscription still holds, so a request it
                # has in flight never lands on the next step's state.
                page.goto("about:blank")
    assert not wrong, "steps picturing a screen other than the one their text describes:\n" + "\n".join(wrong)
    assert page_id in _ADDS_RECORDS or _records(company["id"]) == records, (
        f"{page_id} added records to the capture company: list it in _ADDS_RECORDS")


def test_a_retaken_picture_differing_only_by_anti_aliasing_keeps_the_committed_one():
    from PIL import Image, ImageDraw

    def webp(draw, height: int = 60) -> bytes:
        image = Image.new("RGB", (200, height), "white")
        draw(ImageDraw.Draw(image))
        buf = io.BytesIO()
        image.save(buf, "WEBP", quality=80)
        return buf.getvalue()

    committed = webp(lambda d: (d.text((10, 10), "INV-2606-0011", fill="black"), d.line((10, 30, 120, 30), fill=(40, 40, 40))))
    # The same line anti-aliased across two pixel rows, as Chromium draws it on some loads.
    noise = webp(lambda d: (d.text((10, 10), "INV-2606-0011", fill="black"), d.line((10, 30, 120, 30), fill=(40, 40, 40)),
                            d.line((10, 29, 120, 29), fill=(232, 232, 232)), d.line((10, 31, 120, 31), fill=(232, 232, 232))))
    changed = webp(lambda d: (d.text((10, 10), "INV-2606-0012", fill="black"), d.line((10, 30, 120, 30), fill=(40, 40, 40))))
    assert _same_picture(committed, noise)
    assert not _same_picture(committed, changed)
    assert not _same_picture(committed, webp(lambda d: None, height=61))
