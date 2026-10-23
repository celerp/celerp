# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Online payments, connectors, connecting a store and choosing a deposit account stop
at their boundary while the company cannot use a module they write into, with a message
naming that module, never an import error or a 500 part way through.

Three states: the module on; turned off for one company while another keeps it on
(that company is refused, the other works as before); not started, checked in a fresh
process with the real loader, where nothing of the module is importable, and in this
one for a company that uses it before Celerp restarts. A payment refused either way is
not acknowledged, so Celerp Cloud keeps it and delivers it again; once Documents is on
it is recorded exactly once."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from test_helpers import REPO_ROOT, mark_running
from test_payments_stripe import (  # noqa: F401  (payments_on is a fixture)
    BOOKS, PAID, _company_id, _doc_state, _h, _payable_invoice, _register, payments_on)

_OFF = "The {} module is turned off for your company."
_STARTS = "The {} module starts after Celerp restarts."

# ── Not started: a fresh process with the real loader ────────────────────────

_FRESH = textwrap.dedent("""
    import asyncio, json, sys, uuid
    from fastapi import HTTPException

    def outcome(exc):
        if isinstance(exc, HTTPException) and isinstance(exc.detail, dict):
            return {"status": exc.status_code, "text": str(exc), "key": exc.detail.get("message_key"),
                    "params": exc.detail.get("params")}
        return {"error": repr(exc)}

    async def attempt(call):
        try:
            await call()
        except Exception as exc:
            return outcome(exc)
        return {"ran": True}

    async def main():
        from celerp.modules.loader import loaded_modules
        assert loaded_modules() == []
        assert not any(m.split(".")[0] in ("celerp_docs", "celerp_inventory", "celerp_contacts")
                       for m in sys.modules)
        from celerp.services import payments
        delivery = {"company_id": str(uuid.uuid4()), "entity_id": "invoice-1", "reference": "pi_1",
                    "amount_minor": 1000, "currency": "USD", "paid_at": "2026-07-13T09:00:00+00:00",
                    "refund_id": "re_1", "transition": "succeeded", "cycle": 1,
                    "released_at": "2026-07-14T09:00:00+00:00",
                    "fees": {"currency": "USD", "amount_minor": 30}}
        out = {}
        for name in ("receive_payment", "receive_fees", "receive_refund", "receive_release"):
            out[name] = await attempt(lambda: getattr(payments, name)(dict(delivery)))

        from celerp.connectors.base import ConnectorContext
        from celerp.connectors.registry import get
        ctx = ConnectorContext(company_id=str(uuid.uuid4()), access_token="t", store_handle="https://shop.test")
        for platform, method in (("woocommerce", "sync_products"), ("woocommerce", "sync_orders"),
                                 ("woocommerce", "sync_contacts"), ("shopify", "sync_orders"),
                                 ("xero", "sync_products"), ("quickbooks", "sync_orders")):
            out[f"{platform}.{method}"] = await attempt(lambda: getattr(get(platform), method)(ctx))

        from celerp.config import settings
        from celerp.routers.health import connector_authorize_url
        settings.gateway_token = "gateway-token"
        out["authorize.woocommerce"] = await attempt(lambda: connector_authorize_url(
            "woocommerce", shop="https://shop.test", company_id=uuid.uuid4(), session=None))
        print("RESULT " + json.dumps(out))

    asyncio.run(main())
""")


@pytest.fixture(scope="module")
def fresh_process(_db_engine) -> dict:
    """What each boundary does in a process that never loaded a module, for a company
    nothing says turned it off."""
    import os
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    env.update(MODULE_DIR="", ALLOW_INSECURE_JWT="true", PYTHONPATH=str(REPO_ROOT))
    done = subprocess.run([sys.executable, "-c", _FRESH], cwd=REPO_ROOT, env=env,
                          capture_output=True, text=True, timeout=120)
    line = next((ln for ln in done.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert line, done.stdout[-2000:] + done.stderr[-4000:]
    return json.loads(line.removeprefix("RESULT "))


def _refused(outcome: dict, label: str, *, starting: bool = True) -> None:
    text, key = ((_STARTS, "modules.starts_after_restart") if starting
                 else (_OFF, "modules.off_for_company"))
    assert outcome == {"status": 403, "text": text.format(label), "key": key,
                       "params": {"module": label}}, outcome


@pytest.mark.parametrize("name", ["receive_payment", "receive_fees", "receive_refund", "receive_release"])
def test_a_payment_delivery_without_documents_is_refused_naming_it(fresh_process, name):
    _refused(fresh_process[name], "Documents")


@pytest.mark.parametrize("call,label", [
    ("woocommerce.sync_products", "Inventory"), ("woocommerce.sync_orders", "Documents"),
    ("woocommerce.sync_contacts", "Contacts"), ("shopify.sync_orders", "Documents"),
    ("xero.sync_products", "Inventory"), ("quickbooks.sync_orders", "Documents"),
])
def test_a_connector_sync_without_its_module_is_refused_naming_it(fresh_process, call, label):
    _refused(fresh_process[call], label)


def test_connecting_a_store_without_inventory_is_refused_before_anything_is_claimed(fresh_process):
    """The refusal comes before the connector is claimed: the fresh process has no
    database session, so getting any further would fail on it."""
    _refused(fresh_process["authorize.woocommerce"], "Inventory")


# ── In this process: on, off for one company, not started ────────────────

def _delivery(tok: str, eid: str, delivery_id: str = "delivery-1") -> dict:
    return {"company_id": _company_id(tok), "entity_id": eid, "reference": "pi_boundary",
            "amount_minor": 107000, "currency": "usd", "paid_at": PAID.isoformat(),
            "context": BOOKS, "managed": True, "delivery_id": delivery_id}


async def _acked(payload: dict) -> bool:
    """Deliver *payload* as Celerp Cloud does; True when the installation acknowledged it."""
    from celerp.gateway.client import GatewayClient
    gateway = GatewayClient(gateway_token="t", instance_id="i", gateway_url="wss://relay.invalid/ws")
    gateway._ws = object()
    acked = []

    async def send(ws, message):
        acked.append(message["payload"]["delivery_id"])
    gateway._send = send
    await gateway._handle_invoice_payment(dict(payload))
    return acked == [payload["delivery_id"]]


async def _paid_with(client, tok, eid) -> list:
    return [p for p in (await _doc_state(client, tok, eid)).get("payments", []) if p.get("reference") == "pi_boundary"]


async def _unmatched(session) -> int:
    from sqlalchemy import func, select
    from celerp.models.payment_closure import UnmatchedPayment
    return await session.scalar(select(func.count()).select_from(UnmatchedPayment))


async def _second_company(client, tok: str) -> str:
    r = await client.post("/companies", json={"name": "Second Co"}, headers=_h(tok))
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _documents(client, tok: str, action: str) -> None:
    r = await client.post(f"/companies/me/modules/celerp-docs/{action}", headers=_h(tok))
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_a_payment_waits_until_documents_starts_and_is_then_recorded_once(
        client, session, payments_on, monkeypatch, caplog):
    """The company uses Documents but Celerp has not started it: not acknowledged, with
    nothing written; delivered again once it runs, it is recorded once, and a further
    delivery changes nothing."""
    tok = await _register(client)
    eid, _ = await _payable_invoice(client, tok)
    payload = _delivery(tok, eid)

    assert await _acked(payload) is False
    assert _STARTS.format("Documents") in caplog.text
    assert await _unmatched(session) == 0
    assert await _paid_with(client, tok, eid) == []

    mark_running(monkeypatch, "celerp-docs")
    assert await _acked(payload) is True
    assert await _acked(payload) is True
    assert len(await _paid_with(client, tok, eid)) == 1
    assert (await _doc_state(client, tok, eid))["status"] == "paid"


@pytest.mark.asyncio
async def test_a_payment_for_a_company_that_turned_documents_off_waits_until_it_is_back_on(
        client, session, payments_on, monkeypatch, caplog):
    """Documents off for this company while another keeps it on: this company's payment
    is not acknowledged and writes nothing, the other's is recorded as before. Turned
    back on, the payment delivered again is recorded once."""
    mark_running(monkeypatch, "celerp-docs")
    tok = await _register(client)
    eid, _ = await _payable_invoice(client, tok)
    other = await _second_company(client, tok)
    other_eid, _ = await _payable_invoice(client, other)
    await _documents(client, tok, "disable")
    payload = _delivery(tok, eid)

    assert await _acked(payload) is False
    assert _OFF.format("Documents") in caplog.text
    assert await _unmatched(session) == 0

    assert await _acked(_delivery(other, other_eid, "delivery-2")) is True
    assert len(await _paid_with(client, other, other_eid)) == 1

    await _documents(client, tok, "enable")
    assert await _acked(payload) is True
    assert await _acked(payload) is True
    assert len(await _paid_with(client, tok, eid)) == 1
    assert (await _doc_state(client, tok, eid))["status"] == "paid"


@pytest.mark.asyncio
async def test_a_payment_for_a_company_with_documents_on_is_recorded(client, payments_on, monkeypatch):
    mark_running(monkeypatch, "celerp-docs")
    tok = await _register(client)
    eid, _ = await _payable_invoice(client, tok)
    assert await _acked(_delivery(tok, eid)) is True
    assert len(await _paid_with(client, tok, eid)) == 1


# ── Connector syncs: the run records the refusal, whatever calls it ──────────

async def _orders_run(monkeypatch, company_id: str, orders):
    """Run a WooCommerce order sync as the scheduler does, the store answering *orders*;
    returns the run's result and what it recorded on the run."""
    from unittest.mock import AsyncMock
    from celerp.connectors import sync_runner
    from celerp.connectors.base import ConnectorContext, SyncDirection
    from celerp.connectors.woocommerce import WooCommerceConnector

    finished = {}

    async def finish(run_id, company_id, connector, entity, result, started, ended, status):
        finished.update(errors=result.errors, status=status)
    monkeypatch.setattr(sync_runner, "_begin_run", AsyncMock(return_value="run-1"))
    monkeypatch.setattr(sync_runner, "_finish_run", finish)
    monkeypatch.setattr(sync_runner, "_last_success_watermark", AsyncMock(return_value=None))
    monkeypatch.setattr("celerp.connectors.ownership.lock_connector_operation", AsyncMock(return_value=None))
    monkeypatch.setattr("celerp.connectors.ownership.bind_connector_store", AsyncMock())
    woo = WooCommerceConnector()
    monkeypatch.setattr(woo, "_paginate", orders)
    ctx = ConnectorContext(company_id=company_id, access_token="t", store_handle="https://shop.test")
    return await sync_runner.run_sync(woo, ctx, "orders", direction=SyncDirection.INBOUND), finished


def _not_fetched():
    from unittest.mock import AsyncMock
    return AsyncMock(side_effect=AssertionError("fetched from the store"))


@pytest.mark.asyncio
async def test_a_sync_run_before_its_module_starts_fails_with_the_refusal_alone(client, monkeypatch):
    """One sentence on the run, nothing fetched from the store, so nothing is lost and
    the watermark stays where it was."""
    tok = await _register(client)
    result, finished = await _orders_run(monkeypatch, _company_id(tok), _not_fetched())

    assert result.errors == [_STARTS.format("Documents")]
    assert finished == {"errors": [_STARTS.format("Documents")], "status": "failed"}


@pytest.mark.asyncio
async def test_a_company_that_turned_documents_off_has_its_order_sync_refused(client, monkeypatch):
    """Documents off for this company while another keeps it on: this company's sync
    is refused before anything is fetched, as a document sent to it by a connected
    company is; the other company's sync runs as before."""
    from unittest.mock import AsyncMock
    mark_running(monkeypatch, "celerp-docs")
    tok = await _register(client)
    other = await _second_company(client, tok)
    await _documents(client, tok, "disable")

    result, finished = await _orders_run(monkeypatch, _company_id(tok), _not_fetched())
    assert finished == {"errors": [_OFF.format("Documents")], "status": "failed"}

    store = AsyncMock(return_value=[])
    result, finished = await _orders_run(monkeypatch, _company_id(other), store)
    assert store.await_count == 1
    assert not result.errors and finished["status"] == "success", finished


# ── Choosing a deposit account ───────────────────────────────────────────────

async def _choose_deposit(client, tok: str):
    return await client.patch("/companies/me/books", json={"stripe_deposit_account": "9999"},
                              headers=_h(tok))


def _key(r) -> str | None:
    detail = r.json().get("detail")
    return detail.get("message_key") if isinstance(detail, dict) else None


@pytest.mark.asyncio
async def test_choosing_a_deposit_account_follows_the_company_documents_state(client, monkeypatch):
    """Not started: the module starts after a restart. On: the account itself is judged.
    Off for this company while another keeps it on: turned off, naming Documents."""
    tok = await _register(client)
    r = await _choose_deposit(client, tok)
    assert (r.status_code, _key(r)) == (403, "modules.starts_after_restart"), r.text
    assert r.json()["detail"]["message"] == _STARTS.format("Documents")

    mark_running(monkeypatch, "celerp-docs")
    r = await _choose_deposit(client, tok)
    assert r.status_code == 422 and not (_key(r) or "").startswith("modules."), r.text

    other = await _second_company(client, tok)
    await _documents(client, tok, "disable")
    r = await _choose_deposit(client, tok)
    assert (r.status_code, _key(r)) == (403, "modules.off_for_company"), r.text
    assert r.json()["detail"]["message"] == _OFF.format("Documents")
    r = await _choose_deposit(client, other)
    assert r.status_code == 422 and not (_key(r) or "").startswith("modules."), r.text


@pytest.mark.asyncio
async def test_connecting_a_store_without_inventory_shows_the_refusal():
    """The Connect button shows the sentence alone, not an "API 403:" prefix."""
    from unittest.mock import AsyncMock, patch
    from httpx import ASGITransport, AsyncClient
    from test_helpers import make_test_token
    from ui.api_client import _api_error
    from ui.app import app as ui_app
    refused = _api_error(403, {"detail": {"message": _OFF.format("Inventory"),
                                          "message_key": "modules.off_for_company",
                                          "params": {"module": "Inventory"}}}, "")
    with patch("ui.api_client.get_connector_authorize_url", AsyncMock(side_effect=refused)), \
         patch("ui.api_client.get_company", AsyncMock(return_value={
             "id": "00000000-0000-0000-0000-000000000002", "settings": {}, "current_role": "owner"})):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as ui:
            ui.cookies.update({"celerp_token": make_test_token(role="owner")})
            r = await ui.get("/settings/connectors/woocommerce/oauth-redirect")
    assert r.status_code == 200
    assert _OFF.format("Inventory") in r.text and "API 403" not in r.text


# ── Every connector method that reaches a module says so ────────────────────

_PACKAGES = {"celerp_docs": "celerp-docs", "celerp_inventory": "celerp-inventory",
             "celerp_contacts": "celerp-contacts"}
_CONNECTORS = REPO_ROOT / "celerp" / "connectors"


def _module_imports(node: ast.AST) -> set[str]:
    return {_PACKAGES[n.module.split(".")[0]] for n in ast.walk(node)
            if isinstance(n, ast.ImportFrom) and n.module and n.module.split(".")[0] in _PACKAGES}


def _upsert_needs() -> dict[str, set[str]]:
    tree = ast.parse((_CONNECTORS / "upsert.py").read_text())
    return {f.name: _module_imports(f) for f in tree.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _declared(fn: ast.AST) -> set[str]:
    return {a.value for d in fn.decorator_list if isinstance(d, ast.Call)
            and getattr(d.func, "id", None) == "needs_modules" for a in d.args}


def test_every_connector_method_reaching_a_module_refuses_without_it():
    """Class closure: a connector method that imports from a module, or calls an
    upsert helper that does, is marked needs_modules with that module, so whichever
    path calls it (a sync run, the outbound queue, a webhook, a route) stops at the
    same boundary."""
    helpers = _upsert_needs()
    missing = {}
    for path in sorted(_CONNECTORS.glob("*.py")):
        if path.name == "upsert.py":
            continue
        for cls in ast.parse(path.read_text()).body:
            if not isinstance(cls, ast.ClassDef):
                continue
            for fn in cls.body:
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                needs = _module_imports(fn) | {
                    pkg for n in ast.walk(fn) if isinstance(n, ast.Attribute)
                    and getattr(n.value, "id", None) == "_upsert" for pkg in helpers.get(n.attr, ())}
                if needs - _declared(fn):
                    missing[f"{path.name}:{cls.name}.{fn.name}"] = sorted(needs - _declared(fn))
    assert not missing, missing
