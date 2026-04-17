#!/usr/bin/env python3
# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Seed demo data via the Celerp API.
Idempotent: skips entities with the same SKU/ref already present.

Usage:
    cd <repo-root>/core
    .venv/bin/python scripts/seed_demo.py
"""
from __future__ import annotations

import asyncio
import random
import sys
import uuid

import httpx

API_BASE = "http://127.0.0.1:8000"
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


async def get_existing_skus(client: httpx.AsyncClient, token: str) -> set:
    r = await client.get("/items", params={"limit": 500}, headers={"Authorization": f"Bearer {token}"})
    if r.is_error:
        return set()
    return {it.get("sku") for it in r.json() if it.get("sku")}


async def get_existing_contacts(client: httpx.AsyncClient, token: str) -> set:
    r = await client.get("/crm/contacts", params={"limit": 500}, headers={"Authorization": f"Bearer {token}"})
    if r.is_error:
        return set()
    return {c.get("email") for c in r.json() if c.get("email")}


async def get_existing_docs(client: httpx.AsyncClient, token: str) -> set:
    r = await client.get("/docs", params={"limit": 500}, headers={"Authorization": f"Bearer {token}"})
    if r.is_error:
        return set()
    return {d.get("doc_number") for d in r.json() if d.get("doc_number")}


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
                "quantity": qty, "cost_price": cost, "retail_price": retail,
                "wholesale_price": wholesale, "status": "active",
            },
            "source": "seed",
            "idempotency_key": f"seed:item:{sku}",
        }]
        r = await client.post("/items/import/batch", json={"records": records}, headers=headers)
        if r.is_error:
            print(f"  WARN item {sku}: {r.text[:80]}")
        else:
            entity_ids.append(eid)
            created += 1
    print(f"Items: {created} created, {skipped} skipped")
    return entity_ids


async def seed_contacts(client: httpx.AsyncClient, token: str) -> list[dict]:
    existing = await get_existing_contacts(client, token)
    headers = {"Authorization": f"Bearer {token}"}
    contacts = []
    created = skipped = 0
    for name, ctype, phone, email in _CONTACTS:
        if email in existing:
            skipped += 1
            continue
        r = await client.post("/crm/contacts", json={
            "name": name, "contact_type": ctype, "phone": phone, "email": email,
            "credit_limit": random.choice([5000, 10000, 20000, 50000]),
        }, headers=headers)
        if r.is_error:
            print(f"  WARN contact {name}: {r.text[:80]}")
        else:
            contacts.append(r.json())
            created += 1
    # Need actual IDs; refetch
    r2 = await client.get("/crm/contacts", params={"limit": 500}, headers=headers)
    all_contacts = r2.json() if not r2.is_error else []
    print(f"Contacts: {created} created, {skipped} skipped")
    return all_contacts


async def seed_docs(client: httpx.AsyncClient, token: str, contacts: list[dict], doc_type: str, count: int):
    existing_refs = await get_existing_docs(client, token)
    headers = {"Authorization": f"Bearer {token}"}
    customer_contacts = [c for c in contacts if c.get("contact_type") in ("customer", None)]
    supplier_contacts = [c for c in contacts if c.get("contact_type") in ("supplier", None)]
    pool = supplier_contacts if doc_type == "purchase_order" else customer_contacts
    if not pool:
        pool = contacts
    items_r = await client.get("/items", params={"limit": 500}, headers=headers)
    all_items = items_r.json() if not items_r.is_error else []
    if not all_items:
        print(f"  WARN no items to use for {doc_type}")
        return
    created = skipped = 0
    for i in range(count):
        from datetime import date, timedelta
        today = date.today()
        doc_date = (today - timedelta(days=random.randint(0, 180))).isoformat()
        due_date = (today + timedelta(days=random.randint(7, 60))).isoformat()
        prefix = "INV" if doc_type == "invoice" else "PO"
        doc_num = f"{prefix}-DEMO-{i+1:04d}"
        if doc_num in existing_refs:
            skipped += 1
            continue
        contact = random.choice(pool)
        n_items = random.randint(1, 4)
        line_items = []
        for item in random.sample(all_items, min(n_items, len(all_items))):
            qty = random.randint(1, 10)
            price = float(item.get("retail_price") or item.get("wholesale_price") or 100)
            line_items.append({
                "item_id": item.get("entity_id", ""),
                "name": item.get("name", ""),
                "quantity": qty,
                "unit_price": price,
                "line_total": qty * price,
            })
        total = sum(l["line_total"] for l in line_items)
        payload = {
            "doc_type": doc_type, "doc_number": doc_num,
            "contact_id": contact.get("entity_id", ""),
            "contact_name": contact.get("name", ""),
            "date": doc_date, "due_date": due_date,
            "line_items": line_items, "total": total,
            "status": random.choice(["draft", "sent", "paid"]),
        }
        r = await client.post("/docs", json=payload, headers=headers)
        if r.is_error:
            print(f"  WARN doc {doc_num}: {r.text[:80]}")
        else:
            created += 1
    print(f"{doc_type}: {created} created, {skipped} skipped")


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
