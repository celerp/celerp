# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A company whose recipes list no extras makes things exactly as it did before extras existed.

The sample company (the demo items) runs production through every door: a run created by
the API and imported from a spreadsheet, a build completed at once, a run completed in bulk,
a run made from the to-make list, one completed when an invoice is finalized, and the
movements in between (issue, receive, waste, reopen, undo receipt, cancel). Every run's
events and stored requests, its posted journal lines, the run as read back, each product's
hub and its production block on screen must match the golden file recorded before extras.
Ids come from a seeded generator so stored requests compare exactly; dates are masked.
"""
from __future__ import annotations

import importlib.util
import json
import os
import random
import re
import uuid
from pathlib import Path

import pytest
from fasthtml.common import to_xml
from sqlalchemy import select

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection

_HERE = Path(__file__).resolve().parent
_GOLDEN = _HERE / "fixtures" / "golden_no_extras.json"
_spec = importlib.util.spec_from_file_location("seed_demo", _HERE.parent / "scripts" / "seed_demo.py")
seed_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_demo)

pytestmark = pytest.mark.asyncio

MFG = "/manufacturing"
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}([T ][\d:.]+(Z|[+-]\d{2}:?\d{2})?)?")


def _mask(value):
    """Dates become a marker; integer ids that come from a shared sequence are dropped."""
    if isinstance(value, dict):
        return {k: _mask(v) for k, v in value.items()
                if not (isinstance(v, int) and not isinstance(v, bool) and (k == "id" or k.endswith("event_id")))}
    if isinstance(value, list):
        return [_mask(v) for v in value]
    if isinstance(value, str):
        return _DATE.sub("<date>", value)
    return value


async def _ok(r, what: str) -> dict:
    assert r.status_code == 200, f"{what}: {r.text}"
    return r.json()


async def _company(client, monkeypatch) -> dict:
    rng = random.Random("golden-no-extras")
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(int=rng.getrandbits(128), version=4))
    token = (await _ok(await client.post("/auth/register", json={
        "company_name": "Golden Mfg", "email": "golden-mfg@test.example",
        "name": "Admin", "password": "pwvalid1"}), "register"))["access_token"]
    await seed_demo.seed_items(client, token)
    return {"Authorization": f"Bearer {token}"}


async def _product(client, h, sku: str, components: list[tuple[str, float]], output_qty: float = 1) -> str:
    item = (await _ok(await client.post("/items", headers=h, json={
        "sku": sku, "name": sku, "quantity": 0, "sell_by": "piece", "status": "available"}), sku))["id"]
    await _ok(await client.put(f"{MFG}/items/{item}/recipe", headers=h, json={
        "output_qty": output_qty, "components": [{"item_id": c, "quantity": q} for c, q in components],
        "labor": [], "overhead": []}), f"recipe {sku}")
    return item


async def _act(client, h, run: str, action: str, key: str, **body) -> dict:
    return await _ok(await client.post(f"{MFG}/{run}/{action}", headers=h,
                                       json={**body, "idempotency_key": key}), f"{action} {key}")


async def _build(client, h, item: str, qty: float, key: str, **body) -> str:
    return (await _ok(await client.post(f"{MFG}/items/{item}/build", headers=h, json={
        "quantity": qty, "idempotency_key": key, **body}), f"build {key}"))["id"]


async def _invoice(client, h, item: str, sku: str, qty: float) -> str:
    r = await client.post("/docs", headers=h, json={"doc_type": "invoice", "total": 0, "line_items": [
        {"item_id": item, "sku": sku, "name": sku, "quantity": qty, "unit_price": 100}]})
    assert r.status_code in (200, 201), r.text
    doc = r.json()["id"]
    await _ok(await client.post(f"/docs/{doc}/finalize", headers=h), f"finalize {sku}")
    return doc


async def _receipts(client, h, run: str) -> list[dict]:
    return (await _ok(await client.get(f"{MFG}/{run}", headers=h), run)).get("receipts") or []


async def _every_door(client, h) -> list[str]:
    """Runs through every door without extras; returns the products made."""
    items = {i["sku"]: i["id"] for i in (await _ok(await client.get("/items", params={"limit": 500}, headers=h),
                                                   "items"))["items"]}
    raw1, raw2 = items["RAW-001"], items["RAW-002"]
    ring = await _product(client, h, "GOLD-RING", [(raw1, 2), (raw2, 1)])
    chain = await _product(client, h, "GOLD-CHAIN", [(raw1, 3)], output_qty=2)
    made = await _product(client, h, "GOLD-MADE", [(raw2, 2)])
    auto = await _product(client, h, "GOLD-AUTO", [(raw1, 1)])

    # API create: issue part, receive part, complete with waste, reopen, undo a receipt, complete.
    run = (await _ok(await client.post(MFG, headers=h, json={
        "description": "Rings", "inputs": [{"item_id": raw1, "quantity": 4}, {"item_id": raw2, "quantity": 2}],
        "output_item_id": ring, "quantity": 2, "idempotency_key": "g:create"}), "create"))["id"]
    await _act(client, h, run, "issue", "g:issue", items=[{"item_id": raw1, "quantity": 2}])
    await _act(client, h, run, "issue", "g:issue-rest")
    await _act(client, h, run, "receive", "g:receive", quantity=1)
    await _act(client, h, run, "complete", "g:complete",
               waste_items=[{"item_id": raw2, "quantity": 0.5}], waste_reason="spilled")
    await _act(client, h, run, "reopen", "g:reopen")
    lot = (await _receipts(client, h, run))[-1]["lot_item_id"]
    await _act(client, h, run, "undo-receipt", "g:undo", lot_item_id=lot)
    await _act(client, h, run, "complete", "g:complete2")

    # Build completed at once; build then bulk complete; build then return and cancel.
    await _build(client, h, chain, 4, "g:build-now", complete=True)
    bulk = await _build(client, h, ring, 1, "g:build-bulk")
    await _ok(await client.post(f"{MFG}/bulk-action", headers=h, json={
        "run_ids": [bulk], "action": "complete", "idempotency_key": "g:bulk"}), "bulk")
    gone = await _build(client, h, chain, 2, "g:build-cancel")
    await _act(client, h, gone, "issue", "g:issue-cancel")
    await _act(client, h, gone, "return", "g:return-cancel")
    await _act(client, h, gone, "cancel", "g:cancel", reason="not needed")

    # Import, then complete with no body.
    imported = await _ok(await client.post(f"{MFG}/import/batch", headers=h, json={"records": [{
        "entity_id": "mfg:golden-import", "event_type": "mfg.order.created", "idempotency_key": "g:import", "source": "golden",
        "data": {"description": "Imported rings", "output_item_id": ring, "quantity": 1,
                 "inputs": [{"item_id": raw1, "quantity": 2}, {"item_id": raw2, "quantity": 1}]}}]}), "import")
    assert imported["created"] == 1, imported
    await _ok(await client.post(f"{MFG}/mfg:golden-import/complete", headers=h, json={}), "import complete")

    # To-make list, made and completed.
    doc = await _invoice(client, h, made, "GOLD-MADE", 2)
    await _ok(await client.post(f"{MFG}/to-make/make", headers=h, json={
        "lines": [{"item_id": made, "doc_id": doc}], "complete": True, "idempotency_key": "g:make"}), "make")

    # Finalizing an invoice creates and completes the run.
    await _ok(await client.patch("/companies/me", headers=h, json={"settings": {"manufacturing": {
        "auto_create_work_orders": True, "auto_complete_work_orders": True}}}), "settings")
    await _invoice(client, h, auto, "GOLD-AUTO", 3)
    return [ring, chain, made, auto]


async def _snapshot(client, session, h, products: list[str]) -> dict:
    from ui.routes.inventory import _production_block

    session.expire_all()
    runs = sorted((await _ok(await client.get(MFG, params={"limit": 500}, headers=h), "runs"))["items"],
                  key=lambda r: r["id"])
    out: dict = {"runs": {}, "products": {}}
    for listed in runs:
        rid = listed["id"]
        events = (await session.execute(select(LedgerEntry).where(LedgerEntry.entity_id == rid)
                                        .order_by(LedgerEntry.id))).scalars().all()
        cid = events[0].company_id
        jes = (await session.execute(select(Projection).where(
            Projection.company_id == cid, Projection.entity_type == "journal_entry",
            Projection.entity_id.contains(rid)))).scalars().all()
        out["runs"][rid] = {
            "events": [[e.event_type, _mask(e.data), (e.metadata_ or {}).get("request")] for e in events],
            "journal": {j.entity_id: [j.state.get("status"), sorted(
                [x["account"], round(float(x.get("debit") or 0), 2), round(float(x.get("credit") or 0), 2),
                 sorted(x.get("account_roles") or [])] for x in j.state.get("entries") or [])]
                for j in sorted(jes, key=lambda j: j.entity_id)},
            "read": _mask(await _ok(await client.get(f"{MFG}/{rid}", headers=h), rid)),
        }
    for p in products:
        item = await _ok(await client.get(f"/items/{p}", headers=h), p)
        mine = [r for r in runs if r.get("output_item_id") == p]
        recipes = (await session.execute(select(LedgerEntry).where(
            LedgerEntry.entity_id == p, LedgerEntry.event_type.like("item.recipe%")).order_by(LedgerEntry.id))).scalars()
        out["products"][p] = {
            "recipe_events": [[e.event_type, _mask(e.data)] for e in recipes],
            "hub": _mask(await _ok(await client.get(f"{MFG}/items/{p}/hub", headers=h), f"hub {p}")),
            "block": _DATE.sub("<date>", to_xml(_production_block(p, item, {"runs": mine}, "$"))),
        }
    return out


async def test_golden_no_extras(client, session, monkeypatch):
    h = await _company(client, monkeypatch)
    products = await _every_door(client, h)
    got = json.loads(json.dumps(await _snapshot(client, session, h, products), sort_keys=True, default=str))
    if os.environ.get("CELERP_RECORD_GOLDEN"):
        _GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    assert got == json.loads(_GOLDEN.read_text())
