#!/usr/bin/env python3
# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Seed demo data via the Celerp API.
Idempotent: skips items and contacts already present and replays documents
by idempotency key.

Usage:
    cd <repo-root>/core
    .venv/bin/python scripts/seed_demo.py
"""
from __future__ import annotations

import asyncio
import os
import random
import sys
import uuid
from datetime import date, timedelta

import httpx

API_BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")
EMAIL = "admin@demo.test"
PASSWORD = "demo-password"

# ── Data templates ─────────────────────────────────────────────────────────

_CATEGORIES = ["Electronics", "Furniture", "Apparel", "Food & Bev", "Raw Materials", "Packaging", "Tools", "Office"]

_ITEMS = [
    # (sku, name, category, qty, cost, retail, wholesale)
    ("ELEC-001", "Laptop Pro 15", "Electronics", 25, 800, 1500, 1200),
    ("ELEC-002", "Wireless Keyboard", "Electronics", 50, 20, 60, 45),
    ("ELEC-003", "USB-C Hub 7-Port", "Electronics", 80, 12, 35, 28),
    ("ELEC-004", "Monitor 27in 4K", "Electronics", 15, 250, 600, 480),
    ("ELEC-005", "Noise Cancel Headphones", "Electronics", 30, 80, 220, 175),
    ("ELEC-006", "Webcam HD 1080p", "Electronics", 45, 25, 75, 60),
    ("ELEC-007", "Desk Lamp LED", "Electronics", 60, 15, 45, 36),
    ("FURN-001", "Ergonomic Chair", "Furniture", 10, 150, 350, 280),
    ("FURN-002", "Standing Desk Adjust", "Furniture", 8, 200, 500, 400),
    ("FURN-003", "Filing Cabinet 3-Draw", "Furniture", 12, 80, 180, 140),
    ("FURN-004", "Bookshelf Pine 5-Tier", "Furniture", 20, 60, 140, 110),
    ("FURN-005", "Meeting Table 6-Seat", "Furniture", 5, 300, 700, 560),
    ("APPL-001", "Cotton T-Shirt S", "Apparel", 200, 5, 25, 18),
    ("APPL-002", "Cotton T-Shirt M", "Apparel", 200, 5, 25, 18),
    ("APPL-003", "Cotton T-Shirt L", "Apparel", 150, 5, 25, 18),
    ("APPL-004", "Denim Jeans Slim 32", "Apparel", 100, 18, 70, 55),
    ("APPL-005", "Fleece Jacket XL", "Apparel", 60, 22, 85, 68),
    ("FOOD-001", "Organic Coffee 1kg", "Food & Bev", 300, 8, 22, 16),
    ("FOOD-002", "Green Tea 200g", "Food & Bev", 250, 3, 12, 9),
    ("FOOD-003", "Energy Bar Box 12", "Food & Bev", 400, 6, 18, 14),
    ("FOOD-004", "Sparkling Water 1L", "Food & Bev", 500, 1, 4, 3),
    ("RAW-001", "Aluminum Sheet 1mm", "Raw Materials", 1000, 2, 8, 6),
    ("RAW-002", "Steel Rod 10mm", "Raw Materials", 800, 3, 10, 8),
    ("RAW-003", "PVC Pipe 2in", "Raw Materials", 600, 4, 12, 9),
    ("PACK-001", "Cardboard Box S", "Packaging", 2000, 0.5, 2, 1.5),
    ("PACK-002", "Cardboard Box M", "Packaging", 1500, 0.8, 2.5, 2),
    ("PACK-003", "Bubble Wrap Roll 50m", "Packaging", 200, 5, 15, 12),
    ("TOOL-001", "Hammer 16oz", "Tools", 40, 8, 25, 20),
    ("TOOL-002", "Drill Cordless 18V", "Tools", 20, 60, 150, 120),
    ("TOOL-003", "Screwdriver Set 12pc", "Tools", 50, 10, 30, 24),
    ("OFFC-001", "A4 Paper Ream 500", "Office", 300, 2, 8, 6),
    ("OFFC-002", "Ballpoint Pens Box", "Office", 200, 3, 10, 8),
    ("OFFC-003", "Sticky Notes Pack", "Office", 400, 2, 6, 5),
    ("OFFC-004", "Stapler Heavy Duty", "Office", 30, 5, 18, 14),
    ("OFFC-005", "Whiteboard 90x60", "Office", 15, 25, 70, 55),
]

_CONTACTS = [
    ("Acme Corp", "customer", "+1-555-0100", "orders@acme.com"),
    ("Global Supplies Ltd", "supplier", "+1-555-0200", "supply@global.com"),
    ("TechStart Inc", "customer", "+1-555-0300", "purchasing@techstart.io"),
    ("Metro Retail Group", "customer", "+1-555-0400", "accounts@metro.com"),
    ("Pacific Imports", "supplier", "+1-555-0500", "sales@pacific.com"),
    ("Horizon Manufacturing", "customer", "+1-555-0600", "buy@horizon.co"),
    ("Delta Logistics", "supplier", "+1-555-0700", "ops@delta.com"),
    ("Sunrise Foods Co", "customer", "+1-555-0800", "order@sunrise.com"),
    ("Northern Textiles", "supplier", "+1-555-0900", "export@north.com"),
    ("City Office Hub", "customer", "+1-555-1000", "procurement@cityhub.com"),
    ("Apex Electronics", "customer", "+1-555-1100", "buy@apex.com"),
    ("Valley Traders", "supplier", "+1-555-1200", "trade@valley.com"),
    ("Summit Health", "customer", "+1-555-1300", "orders@summit.com"),
    ("BlueOcean Partners", "customer", "+1-555-1400", "ap@blueocean.com"),
    ("Forest Wood Products", "supplier", "+1-555-1500", "sell@forest.com"),
    ("Urban Living", "customer", "+1-555-1600", "buyer@urban.com"),
    ("East Coast Pharma", "customer", "+1-555-1700", "purch@ecp.com"),
    ("Pioneer Solutions", "customer", "+1-555-1800", "orders@pioneer.com"),
    ("West End Wholesale", "supplier", "+1-555-1900", "wholesale@west.com"),
    ("Capital Ventures", "customer", "+1-555-2000", "cfo@capitalv.com"),
    ("Mountain Fresh", "supplier", "+1-555-2100", "fresh@mtn.com"),
    ("Coastal Designs", "customer", "+1-555-2200", "accounts@coastal.com"),
    ("RedBrick Studio", "customer", "+1-555-2300", "studio@redbrick.com"),
    ("Northgate Retail", "customer", "+1-555-2400", "buy@northgate.com"),
    ("Epsilon Tech", "customer", "+1-555-2500", "purchases@eps.io"),
]


async def login(client: httpx.AsyncClient) -> str:
    r = await client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})
    if r.is_error:
        print(f"Login failed: {r.status_code} {r.text}", file=sys.stderr)
        sys.exit(1)
    token = r.json()["access_token"]
    print(f"Logged in as {EMAIL}")
    return token


def _require(r: httpx.Response, what: str) -> httpx.Response:
    """Stop the whole seed on the first failed call, so `celerp demo` exits non-zero
    instead of reporting success over a half-seeded company."""
    if r.is_error:
        print(f"Seeding failed at {what}: {r.status_code} {r.text[:200]}", file=sys.stderr)
        sys.exit(1)
    return r


def _rows(r: httpx.Response) -> list[dict]:
    return r.json()["items"]


async def get_existing_skus(client: httpx.AsyncClient, token: str) -> set:
    r = _require(await client.get("/items", params={"limit": 500}, headers={"Authorization": f"Bearer {token}"}), "listing items")
    return {it.get("sku") for it in _rows(r) if it.get("sku")}


async def get_existing_contacts(client: httpx.AsyncClient, token: str) -> set:
    r = _require(await client.get("/crm/contacts", params={"limit": 500}, headers={"Authorization": f"Bearer {token}"}), "listing contacts")
    return {c.get("email") for c in _rows(r) if c.get("email")}


async def seed_items(client: httpx.AsyncClient, token: str) -> list[str]:
    existing = await get_existing_skus(client, token)
    headers = {"Authorization": f"Bearer {token}"}
    entity_ids = []
    created = skipped = 0
    for sku, name, category, qty, cost, retail, wholesale in _ITEMS:
        if sku in existing:
            skipped += 1
            continue
        eid = f"item:{uuid.uuid4()}"
        records = [{
            "entity_id": eid,
            "event_type": "item.created",
            "data": {
                "sku": sku, "name": name, "category": category,
                "quantity": qty, "cost_total": cost * qty, "retail_price": retail,
                "wholesale_price": wholesale, "status": "available",
            },
            "source": "seed",
            "idempotency_key": f"seed:item:{sku}",
        }]
        _require(await client.post("/items/import/batch", json={"records": records}, headers=headers), f"item {sku}")
        entity_ids.append(eid)
        created += 1
    print(f"Items: {created} created, {skipped} skipped")
    return entity_ids


async def seed_contacts(client: httpx.AsyncClient, token: str) -> list[dict]:
    existing = await get_existing_contacts(client, token)
    headers = {"Authorization": f"Bearer {token}"}
    created = skipped = 0
    for name, ctype, phone, email in _CONTACTS:
        if email in existing:
            skipped += 1
            continue
        _require(await client.post("/crm/contacts", json={
            "name": name, "contact_type": ctype, "phone": phone, "email": email,
            "credit_limit": random.choice([5000, 10000, 20000, 50000]),
        }, headers=headers), f"contact {name}")
        created += 1
    r = _require(await client.get("/crm/contacts", params={"limit": 500}, headers=headers), "listing contacts")
    print(f"Contacts: {created} created, {skipped} skipped")
    return _rows(r)


# The actions that take each demo document to where it ends up. Every document
# starts as a draft and moves on only through the actions a user takes: an
# invoice is finalized, sent and paid; a purchase order is converted to a bill.
_LIFECYCLES = {
    "invoice": ((), ("finalize", "send"), ("finalize", "send", "pay")),
    "purchase_order": ((), ("finalize",)),
}


async def _advance(client: httpx.AsyncClient, headers: dict, doc_id: str, steps: tuple, key: str) -> None:
    for step in steps:
        if step == "finalize":
            _require(await client.post(f"/docs/{doc_id}/finalize", headers=headers), f"finalizing {key}")
        elif step == "send":
            _require(await client.post(f"/docs/{doc_id}/send", json={"idempotency_key": f"{key}:send"},
                                       headers=headers), f"sending {key}")
        elif step == "pay":
            doc = _require(await client.get(f"/docs/{doc_id}", headers=headers), f"reading {key}").json()
            outstanding = float(doc.get("amount_outstanding") or 0)
            if outstanding > 0:
                _require(await client.post(f"/docs/{doc_id}/payment", json={
                    "amount": outstanding, "payment_date": doc.get("date") or date.today().isoformat(),
                    "bank_account": "1110", "idempotency_key": f"{key}:payment",
                }, headers=headers), f"recording payment on {key}")


async def seed_docs(client: httpx.AsyncClient, token: str, contacts: list[dict], doc_type: str, count: int):
    headers = {"Authorization": f"Bearer {token}"}
    wanted = "supplier" if doc_type == "purchase_order" else "customer"
    pool = [c for c in contacts if c.get("contact_type") in (wanted, None)] or contacts
    if not pool:
        print(f"Seeding failed: no contacts to use for {doc_type}", file=sys.stderr)
        sys.exit(1)
    all_items = _rows(_require(await client.get("/items", params={"limit": 500}, headers=headers), "listing items"))
    if not all_items:
        print(f"Seeding failed: no items to use for {doc_type}", file=sys.stderr)
        sys.exit(1)
    lifecycles = _LIFECYCLES[doc_type]
    today = date.today()
    for i in range(count):
        doc_date = (today - timedelta(days=random.randint(0, 180))).isoformat()
        due_date = (today + timedelta(days=random.randint(7, 60))).isoformat()
        contact = random.choice(pool)
        line_items = []
        for item in random.sample(all_items, min(random.randint(1, 4), len(all_items))):
            qty = random.randint(1, 10)
            price_key = "wholesale_price" if doc_type == "purchase_order" else "retail_price"
            price = float(item.get(price_key) or 100)
            line_items.append({
                "item_id": item.get("entity_id", ""),
                "name": item.get("name", ""),
                "quantity": qty,
                "unit_price": price,
                "line_total": qty * price,
            })
        # The key makes a re-run replay the same draft instead of adding another,
        # and the actions below skip or replay whatever step already happened.
        key = f"seed:{doc_type}:{i + 1}"
        r = _require(await client.post("/docs", json={
            "doc_type": doc_type,
            "contact_id": contact.get("entity_id", ""),
            "contact_name": contact.get("name", ""),
            "date": doc_date, "due_date": due_date,
            "line_items": line_items, "total": sum(li["line_total"] for li in line_items),
            "idempotency_key": key,
        }, headers=headers), f"creating {key}")
        await _advance(client, headers, r.json()["id"], lifecycles[i % len(lifecycles)], key)
    print(f"{doc_type}: {count} seeded")


async def main():
    async with httpx.AsyncClient(base_url=API_BASE, timeout=30.0) as client:
        token = await login(client)
        print("Seeding items...")
        await seed_items(client, token)
        print("Seeding contacts...")
        contacts = await seed_contacts(client, token)
        print("Seeding invoices...")
        await seed_docs(client, token, contacts, "invoice", random.randint(10, 20))
        print("Seeding purchase orders...")
        await seed_docs(client, token, contacts, "purchase_order", random.randint(5, 10))
        print("Done!")


if __name__ == "__main__":
    asyncio.run(main())
