"""Diagnostic: check what's in the database for demo items.

Run: cd /path/to/celerp/core && python tests/diagnose_prices.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker


async def diagnose():
    db_url = os.environ.get("DATABASE_URL", "postgresql+asyncpg://celerp:celerp@localhost:5432/celerp")
    print(f"DB: {db_url.split('@')[1] if '@' in db_url else db_url}")
    
    engine = create_async_engine(db_url)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    
    async with factory() as session:
        # 1. Check company settings
        print("\n=== Company Settings ===")
        rows = await session.execute(text("SELECT id, name, settings FROM companies LIMIT 1"))
        for row in rows:
            import json
            settings = row[2] if isinstance(row[2], dict) else json.loads(row[2]) if row[2] else {}
            print(f"  Company: {row[1]} (id: {row[0]})")
            print(f"  price_lists: {settings.get('price_lists', 'NOT SET')}")
            print(f"  default_price_list: {settings.get('default_price_list', 'NOT SET')}")
            print(f"  vertical: {settings.get('vertical', 'NOT SET')}")
            print(f"  item_schema present: {'item_schema' in settings}")
            if 'item_schema' in settings:
                schema = settings['item_schema']
                price_fields = [f for f in schema if 'price' in f.get('key', '').lower()]
                print(f"  item_schema price fields: {price_fields}")
            print(f"  column_prefs: {settings.get('column_prefs', 'NOT SET')}")
        
        # 2. Check ledger for demo events
        print("\n=== Demo Ledger Events ===")
        rows = await session.execute(text(
            "SELECT entity_id, event_type, data FROM ledger WHERE source = 'demo' LIMIT 3"
        ))
        count = 0
        for row in rows:
            count += 1
            import json
            data = row[2] if isinstance(row[2], dict) else json.loads(row[2]) if row[2] else {}
            print(f"\n  Entity: {row[0]}")
            print(f"  Event: {row[1]}")
            print(f"  SKU: {data.get('sku')}")
            print(f"  Name: {data.get('name')}")
            attrs = data.get('attributes', {})
            print(f"  attributes keys: {list(attrs.keys())}")
            print(f"  retail_price in attrs: {attrs.get('retail_price')}")
            print(f"  wholesale_price in attrs: {attrs.get('wholesale_price')}")
            print(f"  cost_price in attrs: {attrs.get('cost_price')}")
            # Check for old-format keys
            print(f"  'Retail' in attrs: {attrs.get('Retail')}")
        
        if count == 0:
            print("  NO DEMO EVENTS FOUND!")
        
        total = (await session.execute(text("SELECT count(*) FROM ledger WHERE source = 'demo'"))).scalar()
        print(f"\n  Total demo events: {total}")
        
        # 3. Check projections
        print("\n=== Demo Projections ===")
        rows = await session.execute(text(
            "SELECT entity_id, state FROM projections WHERE entity_id LIKE 'item:demo-%' LIMIT 3"
        ))
        count = 0
        for row in rows:
            count += 1
            import json
            state = row[1] if isinstance(row[1], dict) else json.loads(row[1]) if row[1] else {}
            print(f"\n  Entity: {row[0]}")
            print(f"  SKU: {state.get('sku')}")
            print(f"  Top-level retail_price: {state.get('retail_price')}")
            print(f"  Top-level wholesale_price: {state.get('wholesale_price')}")
            print(f"  Top-level cost_price: {state.get('cost_price')}")
            attrs = state.get('attributes', {})
            print(f"  attributes.retail_price: {attrs.get('retail_price')}")
            print(f"  attributes.wholesale_price: {attrs.get('wholesale_price')}")
            print(f"  attributes.cost_price: {attrs.get('cost_price')}")
            # Check for old-format keys
            print(f"  attributes.'Retail': {attrs.get('Retail')}")
        
        if count == 0:
            print("  NO DEMO PROJECTIONS FOUND!")
        
        total = (await session.execute(text("SELECT count(*) FROM projections WHERE entity_id LIKE 'item:demo-%'"))).scalar()
        print(f"\n  Total demo projections: {total}")
        
        # 4. Check what the API item-schema endpoint would return
        print("\n=== Item Schema Check ===")
        rows = await session.execute(text("SELECT settings FROM companies LIMIT 1"))
        for row in rows:
            import json
            settings = row[0] if isinstance(row[0], dict) else json.loads(row[0]) if row[0] else {}
            stored_schema = settings.get("item_schema")
            if stored_schema:
                print("  STORED item_schema found! Price columns may be missing.")
                keys = [f.get("key") for f in stored_schema]
                has_price = any("price" in k for k in keys if k)
                print(f"  Schema keys: {keys}")
                print(f"  Has price columns: {has_price}")
            else:
                print("  No stored item_schema - will use DEFAULT_ITEM_SCHEMA (includes price columns)")
    
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(diagnose())
