# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Connector entity sets are defined once, every entity has a label, and records
waiting on a person are named the way the user knows them."""
from __future__ import annotations

import importlib.util
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from celerp.connectors import base, sync_runner
from celerp.connectors.base import ConnectorContext
from peer_marks_support import KEY, memory_marks  # noqa: F401

_SEED_PATH = Path(__file__).resolve().parent.parent / "scripts" / "seed_demo.py"
_spec = importlib.util.spec_from_file_location("seed_demo_entities", _SEED_PATH)
seed_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_demo)


def test_entity_set_defined_once():
    from ui.routes import settings_connectors

    assert base.OUTBOUND_ENTITIES == (
        "products_out", "contacts_out", "journals_out", "invoices_out", "documents_out",
        "inventory_out", "attachments_out")
    assert base.INBOUND_ENTITIES == ("products", "orders", "contacts", "inventory", "journals", "period_lock")
    for copy in ("_OUTBOUND_ENTITIES", "_INBOUND_ENTITIES", "_OUTBOUND_ENTITY_METHODS"):
        assert not hasattr(sync_runner, copy) and not hasattr(base, copy), copy
    assert settings_connectors._ENTITY_ORDER == [*base.INBOUND_ENTITIES, *base.OUTBOUND_ENTITIES]


def test_peer_connector_kinds_defined_once():
    import ast
    import inspect

    from celerp.connectors import celerp as peer

    assert {kind + "_out" for kind in peer._KINDS} <= set(base.OUTBOUND_ENTITIES)
    # Each kind's event, sync entity and label live in _KINDS and nowhere else.
    keyed_by_kind = [node for node in ast.walk(ast.parse(inspect.getsource(peer)))
                     if isinstance(node, ast.Dict)
                     and any(isinstance(k, ast.Constant) and k.value == "journals" for k in node.keys)]
    assert len(keyed_by_kind) == 1


@pytest.mark.parametrize("lang", ["en", "th"])
def test_every_entity_has_a_label(lang):
    from ui.routes.settings_connectors import _entity_label

    for entity in (*base.INBOUND_ENTITIES, *base.OUTBOUND_ENTITIES):
        assert _entity_label(entity, lang) != entity, entity
    assert _entity_label("documents_out", lang) != _entity_label("attachments_out", lang)


@pytest.mark.asyncio
async def test_attention_items_show_document_numbers(client, session, monkeypatch, memory_marks):
    from celerp import db
    from celerp.connectors import celerp as peer
    from jose import jwt
    from celerp.config import settings

    r = await client.post("/auth/register", json={
        "company_name": "Demo Trading", "email": f"ent-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Admin", "password": "pwvalid1"})
    token = r.json()["access_token"]
    company = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])["company_id"]
    await seed_demo.seed_items(client, token)
    contacts = await seed_demo.seed_contacts(client, token)
    await seed_demo.seed_docs(client, token, contacts, "invoice", 3)
    docs = (await client.get("/docs", params={"limit": 500},
                             headers={"Authorization": f"Bearer {token}"})).json()["items"]
    numbers = {d["id"]: d["ref_id"] for d in docs if d.get("ref_id")}
    assert numbers

    @asynccontextmanager
    async def shared():
        yield session

    class Refusing:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            return SimpleNamespace(status_code=409, json=lambda: {})

    async def no_prior_sync(*args):
        return None

    monkeypatch.setattr(db, "get_session_ctx", shared)
    monkeypatch.setattr(sync_runner, "_last_success_watermark", no_prior_sync)
    monkeypatch.setattr(peer, "RateLimitedClient", Refusing)
    result = await peer.CelerpConnector().sync_documents_out(ConnectorContext(
        company_id=company, store_handle="https://firm.example", access_token=KEY))
    assert result.attention
    for entry in result.attention:
        assert entry["label"] == numbers[entry["id"]], entry


def test_status_view_names_records_and_aligns_numbers():
    from fasthtml.common import to_xml
    from ui.routes.settings_connectors import _attention_list, _entity_status_table

    entry = [{"id": "doc:1", "label": "INV-0001", "reason": "Receiving installation needs attention"}]
    peer = to_xml(_attention_list("celerp", entry))
    assert "Records needing attention" in peer and "Orders" not in peer
    assert "Orders needing attention" in to_xml(_attention_list("woocommerce", entry))
    from types import SimpleNamespace
    from datetime import datetime, timezone
    run = SimpleNamespace(finished_at=datetime.now(timezone.utc), status="success",
                          created_count=1, updated_count=0, errors=[])
    table = to_xml(_entity_status_table({"documents_out": run}))
    headers = table.split("<thead>")[1].split("</thead>")[0]
    # Changes are words: their header is a plain label, left like the cells under it;
    # only Issues stays a number column.
    assert ">Changes<" in headers and "{" not in headers, headers
    assert headers.count('class="cell--number"') == 1, headers
    assert table.count('class="cell--number"') == 2, table
    # Changes read as words, in the table and on the last-sync card alike.
    from ui.routes.settings_connectors import _last_sync_info
    assert "1 added, 0 updated" in table and "+1" not in table
    assert "1 added, 0 updated" in to_xml(_last_sync_info(run))


def test_disconnect_confirms_say_what_happens_in_plain_words():
    """The accountant connection has no store and no webhooks, so its Disconnect asks in
    its own words; no confirm talks about tokens."""
    from fasthtml.common import to_xml
    from ui.routes.settings_connectors import _disconnect_confirm, _disconnect_failed

    for platform in ("celerp", "shopify"):
        for anyway in (False, True):
            assert "token" not in _disconnect_confirm(platform, "en", anyway=anyway).lower()
    peer = _disconnect_confirm("celerp", "en") + _disconnect_confirm("celerp", "en", anyway=True)
    assert "accountant" in peer and "store" not in peer and "webhook" not in peer
    assert "store" in _disconnect_confirm("shopify", "en", anyway=True)
    failed = to_xml(_disconnect_failed({"ok": False}, "en", platform="celerp", force=False,
                                       retry="/r", target="#t", swap="innerHTML"))
    assert "webhook" not in failed and "accountant" in failed
