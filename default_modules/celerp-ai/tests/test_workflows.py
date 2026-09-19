# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Product-level workflow acceptance tests for the capability agent.

These are the proof that the generic capability surface is sufficient: the agent
composes the real read and write operations into whole business tasks without a
single bespoke tool. Every test drives the real app - real routes, real
capabilities, real seeded data - through the public conversation endpoints
(``/ai/conversations`` + ``/query`` + ``/confirm``). Only the model is scripted:
``celerp.ai.service.complete`` is replaced with a deterministic responder so no
network call is ever made and the loop is exercised against the exact wire shape
the relay sends (tool-call arguments as a JSON string).

The scripted model is intentionally dynamic where the workflow demands chaining:
the import case reads the ``preview_hash`` back out of the fed tool result and
echoes it into the commit call, proving the model composes preview then commit
from live data rather than a canned value.
"""

from __future__ import annotations

import io
import json
import os
import secrets
import uuid

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import openpyxl
import pytest
from fastapi import FastAPI
from unittest.mock import patch

from celerp.ai import tools as ai_tools
from celerp.ai.files import upload_dir
from celerp.models.ledger import LedgerEntry
from celerp.modules.loader import (
    _BUNDLED_MODULES_DIRS,
    is_core_folded,
    load_all,
    register_api_routes,
)
from test_helpers import perm_setup

from _fixtures import tool_call, tool_result


# ── Scripted model ────────────────────────────────────────────────────────────

def _bundled_pluggable_names() -> set[str]:
    root = _BUNDLED_MODULES_DIRS[0]
    return {
        p.name for p in root.iterdir()
        if p.is_dir() and (p / "__init__.py").exists() and not is_core_folded(p.name)
    }


def _last_tool_data(messages: list[dict]) -> dict:
    """The parsed content of the most recent tool result the loop fed the model."""
    for m in reversed(messages):
        if m.get("role") == "tool":
            return json.loads(m["content"])
    raise AssertionError("no tool result has been fed to the model yet")


class ScriptedModel:
    """Stand-in for ``complete`` that plays a fixed list of turns.

    Each step is a callable taking the messages built so far and returning a
    ``ModelResult``. Recording the messages lets a test read back the tool result
    the loop fed the model - the same bytes a direct API read returns.
    """

    def __init__(self, *steps):
        self._steps = list(steps)
        self._i = 0
        self.seen: list[list[dict]] = []

    async def __call__(self, messages, *, tools=None, reservation_id=None):
        self.seen.append([dict(m) for m in messages])
        step = self._steps[self._i]
        self._i += 1
        return step(messages)


def _answer(text: str):
    """A final turn with no tool calls: the loop returns this as the answer."""
    return lambda _messages: tool_result(content=text)


def _call(name: str, arguments: dict, call_id: str):
    """A turn that emits exactly one tool call."""
    return lambda _messages: tool_result(calls=[tool_call(name, arguments, call_id)])


# ── Capability + upload fixtures ──────────────────────────────────────────────

@pytest.fixture
def agent_caps():
    """Populate the loader registry so the compiler recognises module routes as
    first-party, then return the compiled capabilities. The autouse loader-reset
    fixture restores registry state at teardown."""
    from celerp.main import app

    loaded = load_all(_BUNDLED_MODULES_DIRS[0], _bundled_pluggable_names())
    register_api_routes(FastAPI(docs_url=None, redoc_url=None), loaded)
    return ai_tools.compile_agent_capabilities(app, {})


def _op(caps: dict, method: str, path: str) -> str:
    """Resolve the capability name (operationId) for a (method, path)."""
    for name, cap in caps.items():
        if cap["method"] == method and cap["path"] == path:
            return name
    raise AssertionError(f"no capability compiled for {method} {path}")


@pytest.fixture
def xlsx_upload():
    """Write an ai_uploads xlsx file pair the import routes can read; cleaned up
    afterwards. Returns ``(company_id, {sheet: rows}) -> file_id``."""
    created: list[str] = []
    d = upload_dir()

    def _write(company_id, sheets: dict[str, list[list]], *, filename: str = "catalog.xlsx") -> str:
        workbook = openpyxl.Workbook()
        workbook.remove(workbook.active)
        for name, rows in sheets.items():
            worksheet = workbook.create_sheet(title=name)
            for row in rows:
                worksheet.append(row)
        buffer = io.BytesIO()
        workbook.save(buffer)
        file_id = f"ai_up_{uuid.uuid4().hex}"
        (d / f"{file_id}.bin").write_bytes(buffer.getvalue())
        (d / f"{file_id}.meta").write_text(json.dumps({
            "filename": filename,
            "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "company_id": str(company_id),
        }))
        created.append(file_id)
        return file_id

    yield _write

    for file_id in created:
        (d / f"{file_id}.bin").unlink(missing_ok=True)
        (d / f"{file_id}.meta").unlink(missing_ok=True)


# ── Conversation helpers ──────────────────────────────────────────────────────

@pytest.fixture
def ai_session(client):
    """Seat a live gateway session token so the cloud-gated ``/ai/*`` routes pass
    ``require_session_token``, which reads the in-process gateway state directly.
    The ``client`` fixture restores the prior token at teardown."""
    from celerp.gateway.state import set_session_token
    set_session_token(secrets.token_hex(32))


async def _new_conversation(client, headers) -> str:
    r = await client.post("/ai/conversations", headers=headers, json={"title": None})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _company_id(client, headers) -> str:
    r = await client.get("/companies/me", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_inventory_search(client, session, agent_caps, ai_session):
    """The agent answers a stock question by searching inventory through the
    generic list capability, and the result it reads is byte-identical to the
    same search over the public API."""
    ctx = await perm_setup(client, session)
    admin = ctx["admin_h"]
    await client.post("/items", headers=admin, json={
        "sku": "WIDGET-BLUE", "name": "Blue Widget", "quantity": 8,
        "location_id": ctx["location_id"], "cost_price": 50.0,
        "sell_by": "piece", "status": "available",
    })

    list_op = _op(agent_caps, "GET", "/items")
    scripted = ScriptedModel(
        _call(list_op, {"query": {"q": "Widget"}}, "c1"),
        _answer("You have 8 Blue Widget in stock."),
    )
    conv_id = await _new_conversation(client, admin)
    with patch("celerp.ai.service.complete", scripted):
        r = await client.post(
            f"/ai/conversations/{conv_id}/query", headers=admin,
            json={"query": "how many widgets do I have?"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pending_actions"] == []
    assert list_op in body["tools_called"]
    assert body["answer"] == "You have 8 Blue Widget in stock."

    # Read parity: the tool result fed to the model equals a direct API read.
    fed = _last_tool_data(scripted.seen[-1])
    direct = await client.get("/items", params={"q": "Widget"}, headers=admin)
    assert fed["ok"] is True
    assert fed["status"] == direct.status_code
    assert fed["data"] == direct.json()


@pytest.mark.asyncio
async def test_restock_with_reorder_suggestion(client, session, agent_caps, ai_session):
    """A restock request composes a read (the velocity-based reorder suggestion)
    with a write (a draft purchase order for the suggested quantity). The write
    is proposed as a pending action and only lands once the user confirms."""
    ctx = await perm_setup(client, session)
    admin = ctx["admin_h"]
    item_id = ctx["item_id"]
    company_id = await _company_id(client, admin)

    # Seed trailing outbound history so a positive reorder quantity is suggested:
    # 90 units over the 90-day window averages one per day -> reorder_qty 14.
    session.add(LedgerEntry(
        company_id=uuid.UUID(company_id), entity_id=item_id, entity_type="item",
        event_type="item.fulfilled", data={"quantity_fulfilled": 90},
        source="test", idempotency_key=f"seed-{uuid.uuid4().hex}",
    ))
    await session.commit()

    reorder_op = _op(agent_caps, "GET", "/items/{entity_id}/reorder-suggestion")
    create_doc_op = _op(agent_caps, "POST", "/docs")

    def _propose_po(messages):
        suggestion = _last_tool_data(messages)["data"]
        qty = suggestion["reorder_qty"]
        args = {"body": {
            "doc_type": "purchase_order", "status": "draft",
            "line_items": [{
                "item_id": item_id, "sku": "SKU-PERM", "name": "Perm Item",
                "quantity": qty, "unit_price": 100,
            }],
        }}
        return tool_result(calls=[tool_call(create_doc_op, args, "p1")])

    scripted = ScriptedModel(
        _call(reorder_op, {"path": {"entity_id": item_id}}, "r1"),
        _propose_po,
    )
    conv_id = await _new_conversation(client, admin)
    with patch("celerp.ai.service.complete", scripted):
        r = await client.post(
            f"/ai/conversations/{conv_id}/query", headers=admin,
            json={"query": "restock what is running low"},
        )
    assert r.status_code == 200, r.text
    body = r.json()

    # The reorder read really ran and returned a positive, velocity-derived number.
    fed = _last_tool_data(scripted.seen[-1])
    assert fed["data"]["reorder_qty"] == 14
    assert reorder_op in body["tools_called"]

    # The write is proposed, not executed.
    assert len(body["pending_actions"]) == 1
    pa = body["pending_actions"][0]
    assert pa["name"] == create_doc_op

    # Confirming lands the draft purchase order.
    r2 = await client.post(
        f"/ai/conversations/{conv_id}/confirm", headers=admin,
        json={"message_id": pa["message_id"], "tool_call_id": pa["id"]},
    )
    assert r2.status_code == 200, r2.text
    confirmed = r2.json()
    assert confirmed["ok"] is True
    assert confirmed["data"]["id"].startswith("doc:")


@pytest.mark.asyncio
async def test_draft_document_requires_confirmation(client, session, agent_caps, ai_session):
    """A proposed document write never executes on the query turn: it comes back
    as a pending action, nothing is persisted, and only the explicit confirm
    creates the document."""
    ctx = await perm_setup(client, session)
    admin = ctx["admin_h"]
    create_doc_op = _op(agent_caps, "POST", "/docs")

    before = (await client.get("/docs", headers=admin)).json()["total"]

    scripted = ScriptedModel(
        _call(create_doc_op, {"body": {
            "doc_type": "invoice", "status": "draft",
            "line_items": [{"name": "Consulting", "quantity": 1, "unit_price": 250}],
        }}, "d1"),
    )
    conv_id = await _new_conversation(client, admin)
    with patch("celerp.ai.service.complete", scripted):
        r = await client.post(
            f"/ai/conversations/{conv_id}/query", headers=admin,
            json={"query": "draft an invoice for 250 of consulting"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["pending_actions"]) == 1
    pa = body["pending_actions"][0]
    assert pa["name"] == create_doc_op

    # Nothing was written on the query turn.
    assert (await client.get("/docs", headers=admin)).json()["total"] == before

    # Confirming writes exactly one document.
    r2 = await client.post(
        f"/ai/conversations/{conv_id}/confirm", headers=admin,
        json={"message_id": pa["message_id"], "tool_call_id": pa["id"]},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["ok"] is True
    assert (await client.get("/docs", headers=admin)).json()["total"] == before + 1


@pytest.mark.asyncio
async def test_stock_discrepancy_audit_read_only(client, session, agent_caps, ai_session):
    """An audit chains several reads across turns and answers in prose, proposing
    no write - a read-only task leaves the ledger untouched."""
    ctx = await perm_setup(client, session)
    admin = ctx["admin_h"]
    item_id = ctx["item_id"]

    list_op = _op(agent_caps, "GET", "/items")
    get_op = _op(agent_caps, "GET", "/items/{entity_id}")
    docs_before = (await client.get("/docs", headers=admin)).json()["total"]

    scripted = ScriptedModel(
        _call(list_op, {"query": {"q": ""}}, "a1"),
        _call(get_op, {"path": {"entity_id": item_id}}, "a2"),
        _answer("No discrepancies: on-hand matches the ledger."),
    )
    conv_id = await _new_conversation(client, admin)
    with patch("celerp.ai.service.complete", scripted):
        r = await client.post(
            f"/ai/conversations/{conv_id}/query", headers=admin,
            json={"query": "audit my stock for discrepancies"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pending_actions"] == []
    assert list_op in body["tools_called"]
    assert get_op in body["tools_called"]
    assert body["answer"] == "No discrepancies: on-hand matches the ledger."

    # A read-only audit writes nothing.
    assert (await client.get("/docs", headers=admin)).json()["total"] == docs_before


@pytest.mark.asyncio
async def test_supplier_catalog_xlsx_preview_then_commit(client, session, agent_caps, xlsx_upload, ai_session):
    """Importing a supplier catalog composes preview then commit: the model reads
    the preview hash out of the live preview result and echoes it into the commit
    call, which is confirmed to land the rows."""
    ctx = await perm_setup(client, session)
    admin = ctx["admin_h"]
    company_id = await _company_id(client, admin)
    file_id = xlsx_upload(company_id, {"Catalog": [
        ["sku", "name", "sell_by", "quantity", "retail_price"],
        ["CAT-1", "Catalog Widget", "piece", 4, 12],
    ]})

    preview_op = _op(agent_caps, "GET", "/items/import/preview")
    commit_op = _op(agent_caps, "POST", "/items/import/commit")

    def _propose_commit(messages):
        preview = _last_tool_data(messages)["data"]
        args = {"body": {
            "file_id": file_id, "sheet": "Catalog",
            "preview_hash": preview["preview_hash"],
        }}
        return tool_result(calls=[tool_call(commit_op, args, "cm1")])

    scripted = ScriptedModel(
        _call(preview_op, {"query": {"file_id": file_id, "sheet": "Catalog"}}, "pv1"),
        _propose_commit,
    )
    conv_id = await _new_conversation(client, admin)
    with patch("celerp.ai.service.complete", scripted):
        r = await client.post(
            f"/ai/conversations/{conv_id}/query", headers=admin,
            json={"query": "import this supplier catalog"},
        )
    assert r.status_code == 200, r.text
    body = r.json()

    # The commit call carries the hash the preview produced - the model chained on
    # live data, not a canned value.
    preview_fed = _last_tool_data(scripted.seen[-1])["data"]
    assert len(preview_fed["preview_hash"]) == 64
    assert preview_op in body["tools_called"]
    assert len(body["pending_actions"]) == 1
    pa = body["pending_actions"][0]
    assert pa["name"] == commit_op
    assert pa["arguments"]["body"]["preview_hash"] == preview_fed["preview_hash"]

    # Confirming commits the import: one item created.
    r2 = await client.post(
        f"/ai/conversations/{conv_id}/confirm", headers=admin,
        json={"message_id": pa["message_id"], "tool_call_id": pa["id"]},
    )
    assert r2.status_code == 200, r2.text
    confirmed = r2.json()
    assert confirmed["ok"] is True
    assert confirmed["data"]["created"] == 1
