# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Taking back a production run's steps from the product's Manufacturing tab and the In
Production queue: which run offers which undo, what each sends, and what the user is told."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from fasthtml.common import to_xml

from test_ui import _authed, ui_client  # noqa: F401  (ui_client is a fixture)
from ui import i18n
from ui.api_client import APIError
from ui.routes.inventory import _production_block

pytestmark = pytest.mark.asyncio

LOT = {"lot_item_id": "item:lot1", "sku": "RING-L1", "quantity": 2.0, "value": "100.00"}
ISSUED = [{"item_id": "item:gold", "sku": "GOLD", "quantity": 10, "issued_qty": 10.0}]


@pytest.fixture(autouse=True)
def _lang():
    yield
    i18n.set_lang("en")


def _options(run: dict) -> list[str]:
    html = to_xml(_production_block("item:ring", {"id": "item:ring"}, {"runs": [{"id": "mfg:1", **run}]}, "$"))
    return [part.split('"')[0] for part in html.split('<option value="')[1:]]


def test_an_open_run_offers_to_undo_each_receipt_and_return_its_materials():
    opts = _options({"status": "in_progress", "inputs": ISSUED, "receipts": [LOT]})
    assert opts == ["", "complete", "hold", "undo:item:lot1", "return", "cancel"]
    html = to_xml(_production_block("item:ring", {}, {"runs": [{"id": "mfg:1", "status": "in_progress",
                                                               "inputs": ISSUED, "receipts": [LOT]}]}, "$"))
    assert "Undo receipt: RING-L1" in html and "Return materials" in html


def test_a_run_holding_nothing_offers_no_undo():
    assert _options({"status": "planned", "inputs": [{**ISSUED[0], "issued_qty": 0.0}]}) == ["", "start", "cancel"]
    assert _options({"status": "on_hold", "inputs": ISSUED}) == ["", "resume", "return", "cancel"]


def test_a_completed_run_offers_reopen_and_a_cancelled_run_nothing():
    assert _options({"status": "completed", "inputs": ISSUED, "receipts": [LOT]}) == ["", "reopen"]
    html = to_xml(_production_block("item:ring", {}, {"runs": [{"id": "mfg:1", "status": "cancelled"}]}, "$"))
    assert "wo-action-select" not in html


def _hub(**more):
    return (patch("ui.api_client.get_item", new=AsyncMock(return_value={"id": "item:p", "sku": "P"})),
            patch("ui.api_client.get_company", new=AsyncMock(return_value={"currency": "THB"})),
            patch("ui.api_client.manufacturing_item_hub", new=AsyncMock(return_value={})))


@pytest.mark.parametrize("action, call, args, said", [
    ("undo:item:lot1", "undo_mfg_receipt", ("mfg:1", "item:lot1"), "Receipt undone. The lot is no longer in stock."),
    ("return", "return_mfg_materials", ("mfg:1",), "Materials returned to stock."),
    ("reopen", "reopen_mfg_order", ("mfg:1",), "Run reopened."),
])
async def test_each_undo_calls_its_operation_and_says_what_it_did(ui_client, action, call, args, said):
    a, b, c = _hub()
    with a, b, c, patch(f"ui.api_client.{call}", new=AsyncMock(return_value={})) as op:
        r = await ui_client.post("/api/items/item:p/runs/mfg:1/act", data={"action": action}, cookies=_authed())
    assert r.status_code == 200, r.text
    assert op.await_args.args[1:] == args
    assert said in r.text and "flash--success" in r.text


async def test_a_refused_undo_says_why_in_the_users_language(ui_client):
    refused = APIError(409, "changed", {
        "message": "RING-L1 has changed since this run produced it.", "message_key": "mfg.output_changed",
        "params": {"lot": "RING-L1"}})
    a, b, c = _hub()
    with a, b, c, patch("ui.api_client.undo_mfg_receipt", new=AsyncMock(side_effect=refused)):
        r = await ui_client.post("/api/items/item:p/runs/mfg:1/act", data={"action": "undo:item:lot1"},
                                 cookies={**_authed(), "celerp_lang": "de"})
    assert r.status_code == 200, r.text
    assert "RING-L1 wurde verändert, seit dieser Produktionslauf es hergestellt hat" in r.text
    assert "flash--error" in r.text


async def test_the_in_production_queue_returns_materials_in_bulk(ui_client):
    with (
        patch("ui.api_client.manufacturing_bulk_run_action",
              new=AsyncMock(return_value={"done": ["mfg:1", "mfg:2"], "skipped": []})) as bulk,
        patch("ui.api_client.list_mfg_orders", new=AsyncMock(return_value={"items": []})),
    ):
        r = await ui_client.post("/manufacturing/runs/bulk/return?status=active",
                                 content=b"selected=mfg%3A1&selected=mfg%3A2",
                                 headers={"content-type": "application/x-www-form-urlencoded"}, cookies=_authed())
    assert r.status_code == 200, r.text
    assert bulk.await_args.args[1:] == (["mfg:1", "mfg:2"], "return")
    assert json.loads(r.headers["HX-Trigger"])["celerpToast"]["message"] == "Materials returned for runs: 2"


async def test_the_in_production_queue_offers_return_materials(ui_client):
    with patch("ui.api_client.list_mfg_orders", new=AsyncMock(return_value={"items": []})):
        r = await ui_client.get("/manufacturing/production", cookies=_authed())
    assert r.status_code == 200, r.text
    assert "/manufacturing/runs/bulk/return?status=" in r.text and "Return materials" in r.text
