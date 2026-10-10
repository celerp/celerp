# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The demo seeder builds its documents through the normal lifecycle and fails loudly."""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient

_SEED_PATH = Path(__file__).resolve().parent.parent / "scripts" / "seed_demo.py"
_spec = importlib.util.spec_from_file_location("seed_demo", _SEED_PATH)
seed_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_demo)


async def _token(client: AsyncClient) -> str:
    r = await client.post("/auth/register", json={
        "company_name": "Demo Co", "email": f"seed-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Admin", "password": "pwvalid1",
    })
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _docs(client: AsyncClient, token: str, doc_type: str) -> list[dict]:
    r = await client.get("/docs", params={"doc_type": doc_type, "limit": 500},
                         headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return r.json()["items"]


@pytest.mark.asyncio
async def test_demo_documents_reach_their_states_through_the_lifecycle(client: AsyncClient):
    token = await _token(client)
    await seed_demo.seed_items(client, token)
    contacts = await seed_demo.seed_contacts(client, token)
    await seed_demo.seed_docs(client, token, contacts, "invoice", 3)
    await seed_demo.seed_docs(client, token, contacts, "purchase_order", 3)

    invoices = await _docs(client, token, "invoice")
    assert sorted(d["status"] for d in invoices) == ["draft", "paid", "sent"]
    paid = next(d for d in invoices if d["status"] == "paid")
    assert paid["amount_paid"] == pytest.approx(paid["total"])
    assert paid["amount_outstanding"] == pytest.approx(0)
    assert paid["payments"], "a paid demo invoice carries the payment that settled it"
    assert all(d["finalized"] for d in invoices if d["status"] != "draft")

    # A purchase order moves on only by converting to a bill; the rest stay drafts.
    orders = await _docs(client, token, "purchase_order")
    assert [d["status"] for d in orders] == ["draft", "draft"]
    bills = await _docs(client, token, "bill")
    assert len(bills) == 1 and bills[0]["finalized"] and bills[0]["source_po_ref"]

    # A second run adds no more documents.
    await seed_demo.seed_docs(client, token, contacts, "invoice", 3)
    assert len(await _docs(client, token, "invoice")) == 3


@pytest.mark.asyncio
async def test_demo_seeding_stops_with_an_error_when_a_call_fails(client: AsyncClient):
    with pytest.raises(SystemExit) as exit_info:
        await seed_demo.seed_items(client, "not-a-valid-token")
    assert exit_info.value.code == 1


async def _skus(client: AsyncClient, token: str) -> list[str]:
    r = await client.get("/items", params={"limit": 500}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return [it["sku"] for it in r.json()["items"]]


@pytest.mark.asyncio
async def test_demo_items_land(client: AsyncClient):
    """Every demo item the seeder reports as created is in the company's inventory."""
    token = await _token(client)
    wanted = sorted(row[0] for row in seed_demo._ITEMS)
    ids = await seed_demo.seed_items(client, token)
    assert len(ids) == len(wanted)
    landed = await _skus(client, token)
    assert sorted(s for s in landed if s in wanted) == wanted, landed
    # A second run adds nothing and still succeeds.
    assert await seed_demo.seed_items(client, token) == []
    assert sorted(await _skus(client, token)) == sorted(landed)


class _RejectingImport:
    """Passes every call through, but strips sell_by from item import rows so the
    import refuses each row inside a 200 response."""

    def __init__(self, client: AsyncClient):
        self._c = client

    def __getattr__(self, name):
        return getattr(self._c, name)

    async def post(self, url, json=None, **kw):
        if url == "/items/import/batch" and json:
            for rec in json.get("records", []):
                rec["data"].pop("sell_by", None)
        return await self._c.post(url, json=json, **kw)


@pytest.mark.asyncio
async def test_demo_seeding_stops_when_the_import_refuses_an_item(client: AsyncClient, capsys):
    """A row the import refuses inside a 200 response stops the seed with the reason,
    instead of being counted as created over an empty company."""
    token = await _token(client)
    with pytest.raises(SystemExit) as exit_info:
        await seed_demo.seed_items(_RejectingImport(client), token)
    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert "Seeding failed at item" in err and "sell_by" in err, err
    assert "created" not in capsys.readouterr().out
