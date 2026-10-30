# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Turning Documents and Manufacturing off never changes what a company's stored records mean.

A company keeps its documents, subscriptions and production runs when it turns those modules
off, and a restart then stops loading them. A change to the company's units is still checked
against every one of those records, in a process that has never imported either module, with
the same refusal it gives while they run; a change no record stands in the way of still goes
through; and once the modules are back, the same records work under the same rules.
"""
from __future__ import annotations

import time

import httpx
import pytest

pytestmark = pytest.mark.browser

_DOCS, _MFG, _SUBS, _BOOKS = "celerp-docs", "celerp-manufacturing", "celerp-subscriptions", "celerp-accounting"
_MODULES = (_DOCS, _MFG, _SUBS, _BOOKS)


class _Api:
    def __init__(self, base: str, token: str) -> None:
        self.base, self.headers = base, {"Authorization": f"Bearer {token}"}

    def call(self, method: str, path: str, body: dict | None = None, expect: int | None = 200):
        r = httpx.request(method, self.base + path, json=body, headers=self.headers, timeout=60)
        if expect is not None:
            assert r.status_code == expect, f"{method} {path}: {r.status_code} {r.text}"
        return r

    def modules(self) -> dict[str, dict]:
        return {m["name"]: m for m in self.call("GET", "/companies/me/modules").json()}

    def restart(self) -> None:
        """Restart Celerp as the Modules page does, and wait until the new process serves the
        module routes (it holds them until the UI process reports the modules it started)."""
        boot = httpx.get(self.base + "/health", timeout=10).json()["boot"]
        self.call("POST", "/system/restart")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                if httpx.get(self.base + "/health", timeout=5).json().get("boot") != boot \
                        and self.call("GET", "/items?limit=1", expect=None).status_code == 200:
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.5)
        raise AssertionError("Celerp did not come back within 120 s of the restart")

    def units(self, **decimals: int) -> httpx.Response:
        units = self.call("GET", "/companies/me/units").json()
        changed = [{**u, "decimals": decimals.get(u["name"], u["decimals"])} for u in units]
        return self.call("PUT", "/companies/me/units", {"units": changed}, expect=None)


def _kg_item(api: _Api, sku: str) -> str:
    return api.call("POST", "/items", {"sku": sku, "name": sku, "quantity": 10, "sell_by": "kg",
                                       "status": "available", "allow_splitting": True}).json()["id"]


def _line(item_id: str, sku: str, qty: float) -> dict:
    return {"item_id": item_id, "sku": sku, "name": sku, "quantity": qty, "unit": "kg", "unit_price": 10}


def _refusal(api: _Api) -> dict:
    r = api.units(kg=2)
    assert r.status_code == 422, r.text
    return r.json()["detail"]


def test_units_check_holds_every_stored_record_with_its_modules_off(fresh):
    api = _Api(fresh.api, httpx.post(fresh.api + "/auth/register", json={
        "company_name": "Units Co", "email": "owner@example.com", "name": "Owner",
        "password": "UnitsCheck123!"}, timeout=60).json()["access_token"])
    for name in (_SUBS, _BOOKS, _MFG):
        api.call("POST", f"/companies/me/modules/{name}/enable")
    api.restart()
    assert all(api.modules()[n]["running"] for n in _MODULES)

    # Records made through the API: an open invoice, a draft purchase order received in part, an
    # active subscription and an unfinished production run, each still moving a quantity
    # kg allows at 3 decimal places and not at 2.
    stock, made = _kg_item(api, "FLOUR"), _kg_item(api, "BREAD")
    invoice = api.call("POST", "/docs", {"doc_type": "invoice", "contact_id": "customer:1",
                                         "line_items": [_line(stock, "FLOUR", 0.275)]}).json()["id"]
    order = api.call("POST", "/docs", {"doc_type": "purchase_order", "contact_id": "supplier:1",
                                       "line_items": [_line(stock, "FLOUR", 1.275)]}).json()["id"]
    api.call("POST", f"/docs/{order}/receive", {"location_id": "", "received_items": [
        {"receive_as": "stock", "po_line_index": 0, "item_id": stock, "quantity_received": 0.1}]})
    subscription = api.call("POST", "/docs", {
        "doc_type": "subscription_invoice", "contact_id": "customer:1", "frequency": "monthly",
        "start_date": "2026-01-01", "name": "Weekly flour",
        "line_items": [_line(stock, "FLOUR", 0.275)]}).json()["id"]
    api.call("POST", f"/subscriptions/{subscription}/activate")
    api.call("POST", "/manufacturing", {"description": "Bake", "output_item_id": made, "quantity": 1,
                                        "inputs": [{"item_id": stock, "quantity": 0.275}]})
    running = _refusal(api)
    assert running["message_key"] == "units.precision_lowered_under_partly_received_and_templates", running
    number = {d: api.call("GET", f"/docs/{d}").json()["ref_id"] for d in (invoice, order, subscription)}
    assert running["params"]["templates"] == number[subscription], running
    # The run is known by what it makes.
    assert set(running["params"]["examples"].split(", ")) == {number[invoice], number[order], "BREAD"}, running

    # Off for this company, so no company uses them and the restart loads neither.
    for name in (_SUBS, _BOOKS, _DOCS, _MFG):
        api.call("POST", f"/companies/me/modules/{name}/disable")
    api.restart()
    off = api.modules()
    assert not any(off[n]["running"] or off[n]["enabled"] for n in _MODULES), off

    assert _refusal(api) == running
    assert api.units(gram=1).status_code == 200

    for name in (_SUBS, _BOOKS, _MFG):
        api.call("POST", f"/companies/me/modules/{name}/enable")
    api.restart()
    assert _refusal(api) == running
    # The same records work again under the same rules: the order takes in the rest, so it moves
    # nothing more and drops out of the refusal, while the flour it brought in, 11.275 kg, is in it.
    api.call("POST", f"/docs/{order}/receive", {"location_id": "", "received_items": [
        {"receive_as": "stock", "po_line_index": 0, "item_id": stock, "quantity_received": 1.175}]})
    after = _refusal(api)
    assert after["message_key"] == "units.precision_lowered_under_quantities_and_templates", after
    assert set(after["params"]["examples"].split(", ")) == {number[invoice], "FLOUR", "BREAD"}, after
    assert api.call("GET", f"/docs/{invoice}").json()["status"] == "draft"
