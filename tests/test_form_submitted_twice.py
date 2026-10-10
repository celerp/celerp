# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A form on a document page, submitted twice, is recorded once.

A person who presses Save, sees nothing happen and presses it again, sends the same
form twice. Each case renders the real form, submits the same fields twice through
the page's own route, and checks the payment, refund, credit, receipt or return was
recorded once. A page opened again brings a new form that records a new one.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from html.parser import HTMLParser

import pytest
from fasthtml.common import to_xml

import ui.api_client as api_client
from test_cost_restatement import _state
from test_helpers import sell_item
from test_operation_retries import DATE, _final, _pay

BANK = [{"chart_account_code": "1111", "bank_name": "Main", "account_name": "Main"}]


class _Routes:
    def __init__(self):
        self.routes: dict = {}

    def __getattr__(self, method):
        def register(path, *args, **kwargs):
            def deco(fn):
                self.routes[(method, path)] = fn
                return fn
            return deco
        return register


class _Request:
    cookies: dict = {}

    def __init__(self, form: list[tuple[str, str]] | None = None, query: dict | None = None):
        self._form = form or []
        self.query_params = query or {}

    async def form(self):
        from starlette.datastructures import FormData
        return FormData(self._form)


class _Form(HTMLParser):
    """The name/value pairs a browser submits from the form that posts to ``target``."""

    def __init__(self, target: str):
        super().__init__()
        self.target = target
        self.found: list[list[tuple[str, str]]] = []
        self._fields: list[tuple[str, str]] | None = None
        self._posts = False
        self._select = None
        self._picked = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._fields, self._posts = [], False
        if self._fields is None:
            return
        if a.get("hx-post") == self.target:
            self._posts = True
        if tag == "input" and a.get("name"):
            self._fields.append((a["name"], a.get("value") or ""))
        elif tag == "select":
            self._select, self._picked = a.get("name"), False
        elif tag == "option" and self._select and not self._picked:
            self._fields.append((self._select, a.get("value") or ""))
            self._picked = True

    def handle_endtag(self, tag):
        if tag == "select":
            self._select = None
        elif tag == "form" and self._fields is not None:
            if self._posts:
                self.found.append(self._fields)
            self._fields = None


def _form(page, target: str, **typed) -> list[tuple[str, str]]:
    """The one form on ``page`` that posts to ``target``, with what the user typed in."""
    parser = _Form(target)
    parser.feed(to_xml(page))
    [fields] = parser.found
    fields = [(k, v) for k, v in fields if k not in typed]
    return fields + [(k, str(v)) for k, v in typed.items()]


@pytest.fixture
def routes(client, auth, monkeypatch):
    """The document page's routes, talking to the test server as the signed-in user."""
    from ui.routes import documents

    class _Signed:
        def __getattr__(self, name):
            call = getattr(client, name)

            async def signed(*args, **kwargs):
                kwargs["headers"] = {**auth["headers"], **(kwargs.get("headers") or {})}
                return await call(*args, **kwargs)
            return signed

    @asynccontextmanager
    async def _backend(token, timeout=10.0):
        yield _Signed()

    monkeypatch.setattr(api_client, "_api_client", _backend)
    monkeypatch.setattr(documents, "_token", lambda request: "tok")
    monkeypatch.setattr(documents, "_get_role", lambda request: "owner")
    table = _Routes()
    documents.setup_routes(table)
    return table.routes


async def _twice(routes, path: str, fields, *args):
    handler = routes[("post", path)]
    for _ in range(2):
        resp = await handler(_Request(fields), *args)
        # A refusal comes back as a toast; the same form sent again gets the first answer.
        headers = getattr(resp, "headers", None) or {}
        assert "HX-Reswap" not in headers, headers


async def _payments_page(client, auth, doc_id: str):
    from ui.routes import documents
    doc = (await client.get(f"/docs/{doc_id}", headers=auth["headers"])).json()
    return documents._payment_section(doc, bank_accounts=BANK)


@pytest.mark.asyncio
async def test_a_refund_sent_twice_from_one_form_is_refunded_once(client, session, auth, routes):
    inv = await _final(client, auth, "invoice")
    await _pay(client, auth, inv, 100.0)
    fields = _form(await _payments_page(client, auth, inv), f"/docs/{inv}/refund", amount=30, payment_date=DATE)

    await _twice(routes, "/docs/{entity_id}/refund", fields, inv)

    [payment] = (await _state(session, auth, inv))["payments"]
    assert payment["amount"] - payment["refunded"] == 70.0


@pytest.mark.asyncio
async def test_a_payment_saved_twice_from_one_form_is_recorded_once(client, session, auth, routes):
    inv = await _final(client, auth, "invoice")
    fields = _form(await _payments_page(client, auth, inv), f"/docs/{inv}/payment", amount=40, payment_date=DATE)

    await _twice(routes, "/docs/{entity_id}/payment", fields, inv)

    assert [p["amount"] for p in (await _state(session, auth, inv))["payments"]] == [40.0]


@pytest.mark.asyncio
async def test_a_credit_applied_twice_from_one_form_is_applied_once(client, session, auth, routes):
    inv = await _final(client, auth, "invoice")
    cn = await _final(client, auth, "credit_note", 30.0)
    fields = _form(await _payments_page(client, auth, cn), f"/docs/{cn}/apply-credit",
                   target_doc_id=inv, amount=10, date=DATE)

    await _twice(routes, "/docs/{entity_id}/apply-credit", fields, cn)

    assert [p["amount"] for p in (await _state(session, auth, inv))["payments"]] == [10.0]


@pytest.mark.asyncio
async def test_a_credit_refunded_twice_from_one_form_is_refunded_once(client, session, auth, routes):
    cn = await _final(client, auth, "credit_note", 30.0)
    fields = _form(await _payments_page(client, auth, cn), f"/docs/{cn}/refund-credit", amount=10, date=DATE)

    await _twice(routes, "/docs/{entity_id}/refund-credit", fields, cn)

    assert [p["amount"] for p in (await _state(session, auth, cn))["payments"]] == [10.0]


@pytest.mark.asyncio
async def test_a_bulk_payment_saved_twice_from_one_form_is_paid_once(client, session, auth, routes):
    docs = [await _final(client, auth, "invoice", 60.0) for _ in range(2)]
    panel = await routes[("get", "/docs/bulk-payment-panel")](_Request(query={"doc_ids": ",".join(docs)}))
    fields = _form(panel, "/docs/bulk-payment", amount=90, payment_date=DATE, bank_account="1111")

    await _twice(routes, "/docs/bulk-payment", fields)

    paid = [sum(p["amount"] for p in (await _state(session, auth, d))["payments"]) for d in docs]
    assert sum(paid) == 90.0


@pytest.mark.asyncio
async def test_goods_received_twice_from_one_form_are_received_once(client, session, auth, routes):
    from ui.routes import documents
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "purchase_order", "line_items": [
            {"sku": f"RT-{uuid.uuid4().hex[:6]}", "name": "Beads", "quantity": 10, "unit_price": 14.0}]})
    assert r.status_code == 200, r.text
    po = r.json()["id"]
    doc = (await client.get(f"/docs/{po}", headers=auth["headers"])).json()
    locations = (await client.get("/companies/me/locations", headers=auth["headers"])).json()["items"]
    page = documents._li_bulk_toolbar(po, False, show_fulfill=True, is_inbound=True,
                                      inbound_line_items=doc["line_items"], locations=locations)
    fields = _form(page, f"/docs/{po}/receive", qty_0=4)

    await _twice(routes, "/docs/{entity_id}/receive", fields, po)

    [line] = (await _state(session, auth, po))["line_items"]
    assert line["quantity_received"] == 4


@pytest.mark.asyncio
async def test_a_return_received_twice_from_one_form_is_received_once(client, session, auth, routes, monkeypatch):
    from celerp.modules import loader
    from ui.routes import documents
    monkeypatch.setattr(loader, "loaded_modules", lambda: [{"name": "celerp-inventory"}])
    sku = f"RR-{uuid.uuid4().hex[:6]}"
    r = await client.post("/items", headers=auth["headers"], json={
        "status": "available", "sku": sku, "name": "Widget", "quantity": 2, "cost_price": 40.0,
        "sell_by": "piece"})
    assert r.status_code == 200, r.text
    inv = await sell_item(client, auth["headers"], r.json()["id"], unit_price=50.0)
    line = {"name": "Widget", "sku": sku, "quantity": 2, "unit_price": 50.0}
    cn = await _final(client, auth, "credit_note", 100.0, original_doc_id=inv, line_items=[line])
    doc = (await client.get(f"/docs/{cn}", headers=auth["headers"])).json()
    fields = _form(documents._render_receive_return_section(doc), f"/docs/{cn}/receive-return")

    await _twice(routes, "/docs/{entity_id}/receive-return", fields, cn)

    received = (await _state(session, auth, cn))["return_received_items"]
    assert sum(i["quantity"] for i in received) == 2


@pytest.mark.asyncio
async def test_a_document_voided_twice_from_one_form_reports_the_first_answer(client, session, auth, routes):
    from ui.routes import documents
    inv = await _final(client, auth, "invoice")
    doc = (await client.get(f"/docs/{inv}", headers=auth["headers"])).json()
    fields = _form(documents._doc_detail(doc), f"/docs/{inv}/action/void", reason="Cancelled")

    await _twice(routes, "/docs/{entity_id}/action/{action}", fields, inv, "void")

    assert (await _state(session, auth, inv))["status"] == "void"


@pytest.mark.asyncio
async def test_each_page_opened_brings_a_form_that_records_a_new_one(client, session, auth, routes):
    inv = await _final(client, auth, "invoice")
    first = _form(await _payments_page(client, auth, inv), f"/docs/{inv}/payment", amount=40, payment_date=DATE)
    again = _form(await _payments_page(client, auth, inv), f"/docs/{inv}/payment", amount=40, payment_date=DATE)
    assert dict(first)["idempotency_key"] != dict(again)["idempotency_key"]

    await _twice(routes, "/docs/{entity_id}/payment", first, inv)
    await _twice(routes, "/docs/{entity_id}/payment", again, inv)

    assert [p["amount"] for p in (await _state(session, auth, inv))["payments"]] == [40.0, 40.0]
