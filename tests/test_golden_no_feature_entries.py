# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A company that uses no income accounts posts every entry exactly as it did before them.

The sample company (the demo items and contacts) sells, buys, takes payment and credits
through the normal routes: invoices finalized, sent and paid, purchase orders finalized
into bills, credit notes linked to an invoice line and unlinked. Every posted entry's
lines (account, debit, credit, roles) must match the golden file recorded before item and
category income accounts existed. Entries are keyed by document reference, so the file
does not depend on generated ids or on the day the test runs.
"""
from __future__ import annotations

import importlib.util
import json
import random
import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection

_HERE = Path(__file__).resolve().parent
_GOLDEN = _HERE / "fixtures" / "golden_no_feature_entries.json"
_spec = importlib.util.spec_from_file_location("seed_demo", _HERE.parent / "scripts" / "seed_demo.py")
seed_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_demo)

pytestmark = pytest.mark.asyncio


async def _sample_company(client) -> tuple[dict, list[dict]]:
    r = await client.post("/auth/register", json={
        "company_name": "Golden Co", "email": f"golden-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Admin", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    await seed_demo.seed_items(client, token)
    contacts = await seed_demo.seed_contacts(client, token)
    return {"Authorization": f"Bearer {token}"}, sorted(contacts, key=lambda c: c["name"])


async def _ok(r, what: str) -> dict:
    assert r.status_code == 200, f"{what}: {r.text}"
    return r.json()


async def _documents(client, headers: dict, contacts: list[dict]) -> None:
    items = sorted((await _ok(await client.get("/items", params={"limit": 500}, headers=headers), "items"))["items"],
                   key=lambda i: i["sku"])
    customers = [c for c in contacts if c.get("contact_type") in ("customer", "both")]
    vendors = [c for c in contacts if c.get("contact_type") in ("vendor", "both")]
    for doc_type, count, pool in (("invoice", 6, customers), ("purchase_order", 4, vendors)):
        for i in range(count):
            key = f"golden:{doc_type}:{i + 1}"
            rng = random.Random(key)
            contact = rng.choice(pool)
            price_key = "wholesale_price" if doc_type == "purchase_order" else "retail_price"
            lines = [{"item_id": item["id"], "name": item["name"], "quantity": qty,
                      "unit_price": float(item.get(price_key) or 100),
                      "line_total": qty * float(item.get(price_key) or 100)}
                     for item in rng.sample(items, rng.randint(1, 3)) for qty in (rng.randint(1, 3),)]
            doc = await _ok(await client.post("/docs", headers=headers, json={
                "doc_type": doc_type, "contact_id": contact["id"],
                "contact_name": contact["name"], "line_items": lines,
                "total": sum(li["line_total"] for li in lines), "idempotency_key": key}), key)
            steps = seed_demo._LIFECYCLES[doc_type][1 + i % (len(seed_demo._LIFECYCLES[doc_type]) - 1)]
            await seed_demo._advance(client, headers, doc["id"], steps, key)
            if doc_type == "invoice":
                await _credit(client, headers, doc["id"], i)


async def _credit(client, headers: dict, invoice: str, i: int) -> None:
    """A credit note on the invoice's first line (linked on even invoices, unlinked on odd)."""
    first = (await _ok(await client.get(f"/docs/{invoice}", headers=headers), "invoice"))["line_items"][0]
    amount = round(first["line_total"] / 2, 2)
    line = {"name": first["name"], "quantity": 1, "unit_price": amount, "line_total": amount}
    if i % 2 == 0:
        line["source_line_id"] = first["line_id"]
    cn = await _ok(await client.post("/docs", headers=headers, json={
        "doc_type": "credit_note", "original_doc_id": invoice,
        "line_items": [line], "total": amount}), "credit note")
    await _ok(await client.post(f"/docs/{cn['id']}/finalize", headers=headers), "credit note finalize")


async def posted_entries(session, company_id) -> dict[str, list[list]]:
    """Every posted entry's lines, keyed by its id with each document id replaced by the
    document's type and its place among that type's references (references carry the month)."""
    session.expire_all()
    rows = (await session.execute(select(Projection).where(Projection.company_id == company_id))).scalars().all()
    docs = sorted((r.state.get("doc_type"), str(r.state.get("ref_id")), r.entity_id) for r in rows if r.entity_type == "doc")
    labels = {eid: f"{kind}#{sum(1 for k, ref2, _ in docs if k == kind and ref2 <= ref)}" for kind, ref, eid in docs}
    pattern = re.compile("|".join(re.escape(d) for d in sorted(labels, key=len, reverse=True)))
    return {pattern.sub(lambda m: labels[m.group(0)], r.entity_id): sorted(
                [e["account"], round(float(e.get("debit") or 0), 2), round(float(e.get("credit") or 0), 2),
                 sorted(e.get("account_roles") or [])] for e in r.state.get("entries") or [])
            for r in rows if r.entity_type == "journal_entry" and r.state.get("status") == "posted"
            and r.entity_id.startswith("je:auto:doc:")}


async def test_golden_no_feature_company_entries_identical(client, session):
    from celerp.models.company import Company

    headers, contacts = await _sample_company(client)
    await _documents(client, headers, contacts)
    company_id = (await session.execute(select(Company.id).where(Company.name == "Golden Co"))).scalars().one()
    assert await posted_entries(session, company_id) == json.loads(_GOLDEN.read_text())
