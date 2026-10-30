# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Online payments, connectors, connecting a store and choosing a deposit account stop
at their boundary while the company cannot use a module they write into, with a message
naming that module, never an import error or a 500 part way through.

Three states: the module on; turned off for one company while another keeps it on
(that company is refused, the other works as before); not started, checked in a fresh
process with the real loader, where nothing of the module is importable, and in this
one for a company that uses it before Celerp restarts. A payment refused either way is
not acknowledged, and once Documents is on it is recorded exactly once."""

from __future__ import annotations

import asyncio

import ast
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from test_channel_link_truth import (  # noqa: F401  (connect_session is a fixture)
    _connect, _item, _set_state, _state, connect_session)
from test_channel_link_truth import _register as _owner
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
    """Hand *payload* to the installation; True when it acknowledged it."""
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
        client, session, payments_on, monkeypatch, caplog, docs_not_started):
    """The company uses Documents but Celerp has not started it: not acknowledged, with
    nothing written; once it runs, the same payment is recorded once, and receiving it
    again changes nothing."""
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
    back on, the same payment is recorded once."""
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


@pytest.mark.asyncio
async def test_a_payment_whose_company_turns_documents_off_after_it_was_admitted_writes_nothing(
        client, session, payments_on, monkeypatch):
    """Documents goes off for the company (as another Celerp process would turn it off)
    after the payment passed its boundary but before it takes the company lock it writes
    under: the write is refused, the payment is not acknowledged and is not kept among
    the unmatched, and the invoice is unchanged. Back on, the same payment is recorded once."""
    import celerp.services.payments as payments
    from sqlalchemy import select, update
    from celerp.models.company import Company
    from celerp.modules.registry import disable_for_company
    mark_running(monkeypatch, "celerp-docs")
    tok = await _register(client)
    eid, _ = await _payable_invoice(client, tok)
    payload = _delivery(tok, eid)
    real = payments._lock_company_of

    async def turned_off_first(s, cid):
        settings = await s.scalar(select(Company.settings).where(Company.id == cid))
        await s.execute(update(Company).where(Company.id == cid)
                        .values(settings=disable_for_company(settings, "celerp-docs")))
        return await real(s, cid)

    monkeypatch.setattr(payments, "_lock_company_of", turned_off_first)
    assert await _acked(payload) is False
    assert await _unmatched(session) == 0
    assert await _paid_with(client, tok, eid) == []

    monkeypatch.setattr(payments, "_lock_company_of", real)
    assert await _acked(payload) is True
    assert len(await _paid_with(client, tok, eid)) == 1


@pytest.mark.asyncio
async def test_a_payment_that_meets_a_module_change_in_progress_is_retried_never_kept_as_refused(
        client, session, payments_on, monkeypatch):
    """The payment's write meets a turn-off holding the company's module settings past the
    request lock timeout (ModuleBusy): the payment is not acknowledged, so it is delivered
    again, and it is never kept among the unmatched as an invoice that refused it. The
    next delivery records it once."""
    import celerp_docs.routes_payments as routes_payments
    from celerp.modules.boundary import ModuleBusy
    mark_running(monkeypatch, "celerp-docs")
    tok = await _register(client)
    eid, _ = await _payable_invoice(client, tok)
    payload = _delivery(tok, eid)
    real = routes_payments.record_stripe_payment

    async def busy(*args, **kwargs):
        raise ModuleBusy("celerp-docs")

    monkeypatch.setattr(routes_payments, "record_stripe_payment", busy)
    assert await _acked(payload) is False
    assert await _unmatched(session) == 0
    assert await _paid_with(client, tok, eid) == []

    monkeypatch.setattr(routes_payments, "record_stripe_payment", real)
    assert await _acked(payload) is True
    assert len(await _paid_with(client, tok, eid)) == 1
    assert await _unmatched(session) == 0


async def _recorded_elsewhere(client, tok: str, eid: str) -> dict:
    """A payment whose company is gone, recorded by a person on the invoice *eid*: the
    deliveries for it that follow still name the gone company. Returns them."""
    import uuid
    from celerp.services.payments import receive_payment
    gone = str(uuid.uuid4())
    names = {"company_id": gone, "entity_id": "invoice-gone", "reference": "pi_moved"}
    assert await receive_payment({**names, "amount_minor": 107000, "currency": "usd", "paid_at": PAID.isoformat(),
                                  "context": BOOKS, "managed": True})
    r = await client.post("/payments/unmatched/record", headers=_h(tok),
                          json={"reference": "pi_moved", "entity_id": eid})
    assert r.status_code == 200, r.text
    return {
        "receive_payment": {**names, "amount_minor": 107000, "currency": "usd", "paid_at": PAID.isoformat(),
                            "context": BOOKS, "managed": True},
        "receive_refund": {**names, "refund_id": "re_moved", "transition": "applied", "cycle": 1,
                           "amount_minor": 1000, "currency": "usd", "occurred_at": PAID.isoformat()},
        "receive_fees": {**names, "occurred_at": PAID.isoformat(), "fees": {
            "version": 1, "currency": "usd", "transaction_id": "txn_moved", "total_minor": 520,
            "processor_minor": 270, "platform_minor": 250, "other_minor": 0}},
        "receive_release": {**names, "released_at": PAID.isoformat()},
    }


async def _kept(session, reference: str):
    from sqlalchemy import select
    from celerp.models.payment_closure import UnmatchedPayment
    session.expire_all()
    return await session.scalar(select(UnmatchedPayment).where(UnmatchedPayment.reference == reference))


@pytest.mark.asyncio
async def test_a_delivery_for_a_payment_recorded_in_another_company_follows_that_companys_documents(
        client, session, payments_on, monkeypatch):
    """The deliveries for a payment a person recorded on another company's invoice name
    the company the payment came from, which is gone; they write to the company it was
    recorded in, so that company's Documents decides. Turned off there: every delivery
    is refused naming Documents and changes nothing, the other company still works.
    Back on: the refund is recorded once."""
    from celerp.modules.boundary import ModuleOff
    from celerp.services import payments
    mark_running(monkeypatch, "celerp-docs")
    tok = await _register(client)
    eid, _ = await _payable_invoice(client, tok)
    deliveries = await _recorded_elsewhere(client, tok, eid)
    other = await _second_company(client, tok)
    other_eid, _ = await _payable_invoice(client, other)
    before = await _doc_state(client, tok, eid)
    assert (before["status"], before["amount_paid"]) == ("paid", 1070.0)

    await _documents(client, tok, "disable")
    for name, payload in deliveries.items():
        with pytest.raises(ModuleOff) as refused:
            await getattr(payments, name)(dict(payload))
        assert str(refused.value) == _OFF.format("Documents"), name
    assert (await _kept(session, "pi_moved")).released_at is None
    assert await _acked(_delivery(other, other_eid)) is True
    assert len(await _paid_with(client, other, other_eid)) == 1

    await _documents(client, tok, "enable")
    assert await _doc_state(client, tok, eid) == before
    assert await payments.receive_refund(dict(deliveries["receive_refund"])) is True
    assert await payments.receive_refund(dict(deliveries["receive_refund"])) is True
    after = await _doc_state(client, tok, eid)
    assert (after["status"], after["amount_paid"]) == ("partial", 1060.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("named", [None, ""])
async def test_work_for_no_company_is_refused_even_while_the_module_runs(monkeypatch, named):
    """The boundary judges the company the work is for. With none to read it refuses,
    never falling back to whether the module runs in this process; a company that is
    named goes ahead while it may use the module."""
    from celerp.modules.boundary import ModuleOff, needs_modules
    mark_running(monkeypatch, "celerp-docs")
    ran = []

    @needs_modules("celerp-docs", company=lambda company: company)
    async def work(company):
        ran.append(company)

    with pytest.raises(ModuleOff) as refused:
        await work(named)
    assert refused.value.module == "celerp-docs" and ran == []

    monkeypatch.setattr("celerp.modules.boundary.company_settings", _no_settings)
    await work("a-company")
    assert ran == ["a-company"]


async def _no_settings(company_id):
    return {}


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
async def test_a_sync_run_before_its_module_starts_fails_with_the_refusal_alone(client, monkeypatch, docs_not_started):
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


async def _product_deleted(monkeypatch, company_id: str):
    """A WooCommerce product-deleted delivery for a connected store: the store's sync
    history it leaves, and the connector that received it."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import celerp.connectors as connector_registry
    from celerp.connectors import sync_runner
    from celerp.connectors.base import ConnectorContext, SyncDirection
    from celerp.connectors.webhooks import WebhookEvent, handle_webhook
    from celerp.connectors.woocommerce import WooCommerceConnector

    history = []

    async def finish(run_id, company_id, connector, entity, result, started, ended, status):
        history.append({"entity": entity, "errors": result.errors, "status": status})
    ctx = ConnectorContext(company_id=company_id, access_token="t", store_handle="https://shop.test")
    config = SimpleNamespace(id="config-1", webhook_secret="s", direction="both")
    monkeypatch.setattr(sync_runner, "_begin_run", AsyncMock(return_value="run-1"))
    monkeypatch.setattr(sync_runner, "_finish_run", finish)
    monkeypatch.setattr(sync_runner, "_last_success_watermark", AsyncMock(return_value=None))
    monkeypatch.setattr("celerp.connectors.ownership.lock_connector_operation", AsyncMock(return_value=config))
    monkeypatch.setattr("celerp.connectors.ownership.bind_connector_store", AsyncMock())
    monkeypatch.setattr("celerp.connectors.relay_token.fetch_context", AsyncMock(return_value=ctx))
    woo = WooCommerceConnector()
    monkeypatch.setattr(woo, "_paginate", _not_fetched())
    monkeypatch.setattr(connector_registry, "get", lambda name: woo)
    await handle_webhook(WebhookEvent(platform="woocommerce", topic="product.deleted", payload={"id": 88}),
                         ctx, SyncDirection.BOTH)
    return history, woo


@pytest.mark.asyncio
async def test_a_deleted_store_product_before_inventory_starts_is_recorded(client, monkeypatch):
    """The deletion is refused before anything changes and left on the store's sync
    history in the refusal's words, never dropped unseen; nothing is fetched."""
    tok = await _register(client)
    history, _ = await _product_deleted(monkeypatch, _company_id(tok))
    assert history == [{"entity": "products", "errors": [_STARTS.format("Inventory")], "status": "failed"}]


@pytest.mark.asyncio
async def test_a_deleted_store_product_with_inventory_off_is_recorded(client, monkeypatch):
    mark_running(monkeypatch, "celerp-inventory")
    tok = await _register(client)
    r = await client.post("/companies/me/modules/celerp-inventory/disable", headers=_h(tok))
    assert r.status_code == 200, r.text
    history, _ = await _product_deleted(monkeypatch, _company_id(tok))
    assert history == [{"entity": "products", "errors": [_OFF.format("Inventory")], "status": "failed"}]


@pytest.mark.asyncio
async def test_a_deleted_store_product_with_inventory_on_is_handled_alone(client, monkeypatch):
    """With Inventory on, the targeted handler takes the deletion and no product sync runs."""
    from unittest.mock import AsyncMock
    mark_running(monkeypatch, "celerp-inventory")
    tok = await _register(client)
    handled = AsyncMock()
    monkeypatch.setattr("celerp.connectors.woocommerce.WooCommerceConnector.handle_product_deleted", handled)
    history, _ = await _product_deleted(monkeypatch, _company_id(tok))
    assert history == [] and handled.await_count == 1


async def _order_webhook(monkeypatch, company_id: str, store):
    """A WooCommerce order-updated delivery for a connected store whose orders *store*
    answers, handled with every later attempt it leads to: the store's sync history."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import celerp.connectors as connector_registry
    from celerp.connectors import sync_runner, webhooks
    from celerp.connectors.base import ConnectorContext, SyncDirection
    from celerp.connectors.woocommerce import WooCommerceConnector

    history = []

    async def finish(run_id, company_id, connector, entity, result, started, ended, status):
        history.append({"entity": entity, "errors": result.errors, "status": status})
    ctx = ConnectorContext(company_id=company_id, access_token="t", store_handle="https://shop.test")
    monkeypatch.setattr(sync_runner, "_begin_run", AsyncMock(return_value="run-1"))
    monkeypatch.setattr(sync_runner, "_finish_run", finish)
    monkeypatch.setattr(sync_runner, "_last_success_watermark", AsyncMock(return_value=None))
    monkeypatch.setattr("celerp.connectors.ownership.lock_connector_operation",
                        AsyncMock(return_value=SimpleNamespace(id="config-1", webhook_secret="s", direction="both")))
    monkeypatch.setattr("celerp.connectors.ownership.bind_connector_store", AsyncMock())
    monkeypatch.setattr("celerp.connectors.relay_token.fetch_context", AsyncMock(return_value=ctx))
    monkeypatch.setattr(webhooks, "MODULE_SETTLE_SECONDS", 0)
    woo = WooCommerceConnector()
    monkeypatch.setattr(woo, "_paginate", store)
    monkeypatch.setattr(connector_registry, "get", lambda name: woo)
    await webhooks.handle_webhook(webhooks.WebhookEvent(platform="woocommerce", topic="order.updated"),
                                  ctx, SyncDirection.BOTH)
    retrying = webhooks._RETRYING
    while retrying:
        await asyncio.gather(*list(retrying))
    return history


def _busy_then(answers: int):
    """The store's orders, after a sync that met a change to Documents in progress *answers* times."""
    from unittest.mock import AsyncMock
    from celerp.modules.boundary import ModuleBusy
    return AsyncMock(side_effect=[ModuleBusy("celerp-docs")] * answers + [[]])


@pytest.mark.asyncio
async def test_an_order_webhook_refused_while_modules_change_is_handled_again(client, monkeypatch):
    """A store sends a webhook once and is never asked again, so one refused as busy is
    handled again after the wait the refusal names, and the order is pulled then."""
    from celerp.modules.boundary import ModuleBusy
    mark_running(monkeypatch, "celerp-docs")
    tok = await _register(client)
    store = _busy_then(1)
    history = await _order_webhook(monkeypatch, _company_id(tok), store)
    assert history == [{"entity": "orders", "errors": [str(ModuleBusy("celerp-docs"))], "status": "failed"},
                       {"entity": "orders", "errors": None, "status": "success"}]
    assert store.await_count == 2


@pytest.mark.asyncio
async def test_an_order_webhook_refused_while_modules_keep_changing_is_left_to_the_scheduled_sync(
        client, monkeypatch):
    """Still busy every time: handled again a bounded number of times, each failed run kept
    on the store's history (the watermark stays, so the next scheduled sync pulls it)."""
    from celerp.connectors import webhooks
    mark_running(monkeypatch, "celerp-docs")
    tok = await _register(client)
    history = await _order_webhook(monkeypatch, _company_id(tok), _busy_then(10))
    assert [run["status"] for run in history] == ["failed"] * (1 + webhooks._BUSY_RETRIES)


@pytest.mark.asyncio
async def test_an_order_webhook_for_a_company_that_turned_documents_off_is_not_handled_again(
        client, monkeypatch):
    """Turned off is final until the company turns it back on: recorded once, never retried."""
    mark_running(monkeypatch, "celerp-docs")
    tok = await _register(client)
    await _documents(client, tok, "disable")
    history = await _order_webhook(monkeypatch, _company_id(tok), _not_fetched())
    assert history == [{"entity": "orders", "errors": [_OFF.format("Documents")], "status": "failed"}]


# ── Choosing a deposit account ───────────────────────────────────────────────

async def _choose_deposit(client, tok: str):
    return await client.patch("/companies/me/books", json={"stripe_deposit_account": "9999"},
                              headers=_h(tok))


def _key(r) -> str | None:
    detail = r.json().get("detail")
    return detail.get("message_key") if isinstance(detail, dict) else None


@pytest.mark.asyncio
async def test_choosing_a_deposit_account_follows_the_company_documents_state(client, monkeypatch, docs_not_started):
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
            r = await ui.post("/settings/connectors/woocommerce/oauth-redirect")
    assert r.status_code == 200
    assert _OFF.format("Inventory") in r.text and "API 403" not in r.text


# ── Store links while Inventory has not started ─────────────────────────────

def _inventory_not_importable(monkeypatch) -> None:
    """Nothing of Inventory importable, as in a process that never loaded it."""
    for name in ("celerp_inventory", "celerp_inventory.services"):
        monkeypatch.setitem(sys.modules, name, None)


async def _linked_item(client, session, owner, sku: str) -> str:
    """An item still linked to a WooCommerce product."""
    headers, cid = owner
    eid = await _item(client, headers, sku)
    await _set_state(session, cid, eid, idempotency_key="woocommerce:88")
    return eid


async def _two_companies(client) -> tuple[tuple[dict, str], tuple[dict, str]]:
    first = await _owner(client)
    r = await client.post("/companies", json={"name": "Second Co"}, headers=first[0])
    assert r.status_code == 200, r.text
    tok = r.json()["access_token"]
    return first, (_h(tok), _company_id(tok))


async def _is_active(session, cid: str) -> bool:
    import uuid
    from celerp.models.company import Company
    return (await session.get(Company, uuid.UUID(cid), populate_existing=True)).is_active


@pytest.mark.asyncio
async def test_deactivating_a_company_before_inventory_starts(client, session, monkeypatch):
    """With no store link left there is nothing to detach, so the company is deactivated.
    A company whose items still link to store products is refused, naming Inventory,
    and keeps both its active state and its links."""
    from unittest.mock import AsyncMock, patch
    plain, linked = await _two_companies(client)
    eid = await _linked_item(client, session, linked, "LINKED-1")
    _inventory_not_importable(monkeypatch)

    with patch("celerp.connectors.remote_state.revoke_connector_remote_state", AsyncMock()):
        r = await client.delete("/companies/me", headers=plain[0])
        assert r.status_code == 200, r.text
        assert not await _is_active(session, plain[1])

        r = await client.delete("/companies/me", headers=linked[0])
    assert (r.status_code, _key(r)) == (403, "modules.starts_after_restart"), r.text
    assert r.json()["detail"]["message"] == _STARTS.format("Inventory")
    assert await _is_active(session, linked[1])
    assert (await _state(session, linked[1], eid))["idempotency_key"] == "woocommerce:88"


@pytest.mark.asyncio
async def test_disconnecting_a_store_before_inventory_starts(client, session, monkeypatch, connect_session):
    """Refused before the store is told anything while links are left to turn into
    history; with none left the store is disconnected."""
    import sqlalchemy as sa
    from unittest.mock import AsyncMock, patch
    from celerp.models.connector_config import ConnectorConfig
    plain, linked = await _two_companies(client)
    for _, cid in (plain, linked):
        await _connect(session, cid, "woocommerce")
    await _linked_item(client, session, linked, "LINKED-2")
    _inventory_not_importable(monkeypatch)

    revoke = AsyncMock()
    with patch("celerp.connectors.remote_state.revoke_connector_remote_state", revoke):
        r = await client.delete("/connectors/woocommerce/credentials", headers=linked[0])
        assert (r.status_code, _key(r)) == (403, "modules.starts_after_restart"), r.text
        assert revoke.await_count == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(ConnectorConfig).where(
            ConnectorConfig.company_id == linked[1])) == 1

        r = await client.delete("/connectors/woocommerce/credentials", headers=plain[0])
    assert r.status_code == 200 and r.json() == {"ok": True}, r.text


@pytest.mark.asyncio
async def test_connecting_a_store_or_choosing_items_to_sync_needs_inventory(client, session, connect_session):
    """Both write links on Inventory's items, so both are refused, naming it, before
    anything is stored."""
    import sqlalchemy as sa
    from celerp.models.connector_config import ConnectorConfig
    owner = await _owner(client)
    r = await client.post("/connectors/woocommerce/credentials", headers=owner[0], json={
        "consumer_key": "ck_x", "consumer_secret": "cs_x", "store_url": "https://shop.test"})
    assert (r.status_code, _key(r)) == (403, "modules.starts_after_restart"), r.text
    assert await session.scalar(sa.select(sa.func.count()).select_from(ConnectorConfig).where(
        ConnectorConfig.company_id == owner[1])) == 0

    r = await client.post("/connector-items/woocommerce/sync", headers=owner[0],
                          json={"entity_ids": ["any"], "enable": True})
    assert (r.status_code, _key(r)) == (403, "modules.starts_after_restart"), r.text


@pytest.mark.asyncio
async def test_reconciling_a_store_order_needs_documents(client, connect_session, docs_not_started):
    """Marking an order change as reconciled writes on the order, so it is refused,
    naming Documents, while the company cannot use it."""
    headers, _ = await _owner(client)
    r = await client.post("/connectors/woocommerce/orders/101/reconciled", headers=headers,
                          json={"signature": "s"})
    assert (r.status_code, _key(r)) == (403, "modules.starts_after_restart"), r.text
    assert r.json()["detail"]["message"] == _STARTS.format("Documents")


# ── Core code reaches a module only past its boundary ───────────────────────

def _core_files():
    """Code that runs whichever modules a company uses: celerp/, ui/ and the modules
    folded into core."""
    from celerp.modules.loader import CORE_FOLDED
    roots = [REPO_ROOT / "celerp", REPO_ROOT / "ui",
             *(REPO_ROOT / "default_modules" / name for name in sorted(CORE_FOLDED))]
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT)
            if "tests" not in rel.parts and not path.name.startswith("test_"):
                yield str(rel), ast.parse(path.read_text())


def _packages() -> dict[str, str]:
    """Import name to module name, for every module a company can turn off."""
    from celerp.modules.loader import CORE_FOLDED
    return {p.name.replace("-", "_"): p.name for p in sorted((REPO_ROOT / "default_modules").iterdir())
            if p.name.startswith("celerp-") and p.is_dir() and p.name not in CORE_FOLDED}


_PACKAGES = _packages()
_CONNECTORS = REPO_ROOT / "celerp" / "connectors"


def _module_imports(node: ast.AST) -> set[str]:
    """Modules *node* imports, as ``import x`` or ``from x import y``."""
    names = []
    for n in ast.walk(node):
        if isinstance(n, ast.Import):
            names += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            names.append(n.module)
    return {_PACKAGES[name.split(".")[0]] for name in names if name.split(".")[0] in _PACKAGES}


def _units(tree: ast.Module):
    """(name, node) of each function, each method, and the module's own statements."""
    functions = (ast.FunctionDef, ast.AsyncFunctionDef)
    yield "<module>", ast.Module(body=[n for n in tree.body if not isinstance(n, (*functions, ast.ClassDef))],
                                 type_ignores=[])
    for node in tree.body:
        if isinstance(node, functions):
            yield node.name, node
        elif isinstance(node, ast.ClassDef):
            yield from ((f"{node.name}.{f.name}", f) for f in node.body if isinstance(f, functions))


def _file_names(tree: ast.Module) -> dict[str, set[str]]:
    """Module-level names that stand for modules: a string constant, or a decorator
    made by needs_modules."""
    names = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = node.value
            if isinstance(value, ast.Constant) and value.value in _PACKAGES.values():
                names[node.targets[0].id] = {value.value}
            elif isinstance(value, ast.Call) and getattr(value.func, "id", None) == "needs_modules":
                names[node.targets[0].id] = _literals(value.args, {})
    return names


def _literals(args, names: dict[str, set[str]]) -> set[str]:
    out = set()
    for a in args:
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            out.add(a.value)
        elif isinstance(a, ast.Name):
            out |= names.get(a.id, set())
    return out


# Calls that refuse unless a module runs for the company: the modules named in their
# arguments, or (a shared check) the module it always names.
# Only checks that judge both count: the company may use the module, and this process
# has started it. A check of the company alone (require_company_uses) leaves the import
# to fail in a process that has not.
_CHECKS: dict[str, set[str] | None] = {"require_on": None}


def _needed_for(name: str) -> set[str]:
    """Every module the mapping *name* given to needs_modules_for can name."""
    from celerp.connectors import ownership
    return set().union(*getattr(ownership, name).values())


def _guarded(fn: ast.AST, names: dict[str, set[str]]) -> set[str]:
    """Modules *fn* refuses without: marked needs_modules or needs_modules_for, or
    checked by the boundary or the loader before it imports them."""
    out = set()
    for d in getattr(fn, "decorator_list", ()):
        if isinstance(d, ast.Call) and getattr(d.func, "id", None) == "needs_modules":
            out |= _literals(d.args, names)
        elif isinstance(d, ast.Call) and getattr(d.func, "id", None) == "needs_modules_for":
            out |= _needed_for(d.args[1].id)
        elif isinstance(d, ast.Name):
            out |= names.get(d.id, set())
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            called = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            if called in _CHECKS:
                out |= _CHECKS[called] or _literals(n.args, names)
    return out


_UPSERT = "a connector helper, called only from connector methods marked needs_modules (checked below)"
_OWN_UI = ("a page or helper of the module's own screens, or of a module that depends on it, "
           "mounted by that module, so it runs only while the module runs")
_PEER = ("receives a connected company's records; each grant scope opens one route, of the "
         "module the records belong to (peer_access.PEER_ROUTES)")
_PAYMENT_STEP = "a step of a payment function marked needs_modules, or of a Documents route"
_UPSERT_PY = "celerp/connectors/upsert.py"
_DOCS_UI = "ui/routes/documents.py"

# Each reviewed use of a module from core code that does not refuse at its own
# boundary, with why it cannot run while that module has not started.
_REVIEWED: dict[tuple[str, str], str] = {
    **{(_UPSERT_PY, name): _UPSERT for name in (
        "upsert_item", "upsert_order_from_shopify", "upsert_invoice_from_quickbooks",
        "upsert_invoice_from_xero", "upsert_order_from_woocommerce",
        "woocommerce_reconciliation_signature", "list_imported_woocommerce_order_ids",
        "hold_missing_woocommerce_order", "upsert_contact_from_shopify",
        "upsert_contact_from_quickbooks", "upsert_contact_from_xero",
        "upsert_contact_from_woocommerce", "list_items_with_external_id",
        "list_item_for_external_identity", "list_items_modified_since_last_sync",
        "list_unsynced_invoices", "invoice_for_push", "mark_doc_pushed")},
    ("celerp/connectors/product_links.py", "detach_product_links"):
        "imports Inventory only while it runs in this process, after "
        "refuse_while_links_cannot_detach has judged the company",
    ("celerp/connectors/product_links.py", "woocommerce_order_anchor_ids"):
        "reached from WooCommerce item sync, marked needs_modules for Inventory, and from "
        "Documents, which needs Inventory",
    ("celerp/migrations/env.py", "<module>"):
        "registers each module's tables for schema migrations, skipping a module that is not there",
    ("celerp/services/auto_je.py", "doc_lot_pool"):
        "costs a document line from its lots: reached from Documents, which needs Inventory, and "
        "from the boot COGS backfill, which fails without writing and retries at a later boot",
    ("celerp/services/auto_je.py", "create_for_online_fees"): _PAYMENT_STEP,
    ("celerp/services/payments.py", "_apply_refund"): _PAYMENT_STEP,
    ("celerp/services/payments.py", "_apply_release"): _PAYMENT_STEP,
    ("celerp/services/payments.py", "record_unmatched_payment"): _PAYMENT_STEP,
    ("celerp/services/peer_documents.py", "receive_document"): _PEER,
    ("celerp/services/peer_documents.py", "_replay_receipts"): _PEER,
    ("celerp/services/peer_documents.py", "_replay_receipt"): _PEER,
    ("celerp/services/peer_documents.py", "_replay_fulfilment"): _PEER,
    ("celerp/services/peer_identity.py", "receive_contact"): _PEER,
    ("celerp/services/peer_identity.py", "receive_product"): _PEER,
    ("celerp/services/peer_inventory.py", "receive_stock_audit"): _PEER,
    ("celerp/services/peer_payments.py", "validate_source_payments"): _PEER,
    ("celerp/services/peer_payments.py", "apply_source_payments"): _PEER,
    ("celerp/services/peer_refunds.py", "replay_source_refunds"): _PEER,
    ("ui/routes/contacts.py", "setup_routes"): _OWN_UI,
    **{(_DOCS_UI, name): _OWN_UI for name in (
        "_hold_refusal", "_physical_lots", "_drawable_lots", "_doc_line_actions",
        "_writeoff_account_choices", "_payment_section", "_doc_detail", "setup_routes")},
    ("celerp/output/doc_print.py", "render_doc_print_html"):
        "its list-totals step runs only for a list; its callers are Documents' share route "
        "(celerp_docs.routes_share) and Documents' own print page, so it runs only while Documents runs",
    ("ui/components/activity.py", "_line_tax_change_summary"):
        "only shows the tax lines of an activity entry and writes nothing; it shows none where "
        "Documents cannot be imported",
    ("ui/routes/inventory.py", "<module>"): _OWN_UI,
    ("ui/routes/inventory.py", "_item_files_section"): _OWN_UI,
}


def test_core_code_reaches_a_module_only_past_its_boundary():
    """Class closure: core code that imports a module package, in either form, is
    marked needs_modules with it, checks it at the boundary or the loader first, or
    is a reviewed use that only runs while the module runs. A reviewed use that no
    longer imports the module leaves the list."""
    missing, used = {}, set()
    for rel, tree in _core_files():
        names = _file_names(tree)
        for name, unit in _units(tree):
            needs = _module_imports(unit) - _guarded(unit, names)
            if not needs:
                continue
            if (rel, name) in _REVIEWED:
                used.add((rel, name))
            else:
                missing[f"{rel}:{name}"] = sorted(needs)
    assert not missing, missing
    assert set(_REVIEWED) == used, sorted(set(_REVIEWED) - used)
    assert all(reason.strip() for reason in _REVIEWED.values())


# ── Every connector method that reaches a module says so ────────────────────

def _upsert_needs() -> dict[str, set[str]]:
    tree = ast.parse((_CONNECTORS / "upsert.py").read_text())
    return {f.name: _module_imports(f) for f in tree.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}


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
                if needs - _guarded(fn, {}):
                    missing[f"{path.name}:{cls.name}.{fn.name}"] = sorted(needs - _guarded(fn, {}))
    assert not missing, missing


@pytest.mark.asyncio
async def test_the_connect_button_posts_the_shop_it_authorizes():
    """Connecting claims the connector, so the button posts: the shop typed
    into its form reaches the claim, and a GET of the same path is refused."""
    from unittest.mock import AsyncMock, patch

    from httpx import ASGITransport, AsyncClient
    from test_helpers import make_test_token
    from ui.app import app as ui_app
    authorize = AsyncMock(return_value={"authorize_url": "https://shop.example/oauth"})
    with patch("ui.api_client.get_connector_authorize_url", authorize), \
         patch("ui.api_client.get_company", AsyncMock(return_value={
             "id": "00000000-0000-0000-0000-000000000002", "settings": {}, "current_role": "owner"})):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as ui:
            ui.cookies.update({"celerp_token": make_test_token(role="owner")})
            r = await ui.post("/settings/connectors/shopify/oauth-redirect",
                              data={"shop": "my-store.myshopify.com"})
            read = await ui.get("/settings/connectors/shopify/oauth-redirect")
    assert r.status_code == 200 and "https://shop.example/oauth" in r.text
    assert authorize.await_args.kwargs["shop"] == "my-store.myshopify.com"
    assert read.status_code in (404, 405)
