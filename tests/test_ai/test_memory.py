# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Tests for celerp/ai/memory.py — 100% line coverage.

Covers:
  - get_memory: existing company, missing company
  - add_note: basic append, trimming to MAX_NOTES
  - set_kv: basic set, eviction at MAX_KV_KEYS, update existing key
  - clear_memory
  - set_memory with missing company (no-op)
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from celerp.models.base import Base
from celerp.models.company import Company
from celerp.ai.memory import (
    MAX_KV_KEYS,
    MAX_NOTES,
    add_note,
    clear_memory,
    get_memory,
    set_kv,
    set_memory,
)

_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine(_DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


@pytest_asyncio.fixture
async def company(session: AsyncSession) -> Company:
    """Create and persist a test company."""
    c = Company(name="TestCo", slug="testco", settings={})
    session.add(c)
    await session.commit()
    await session.refresh(c)
    return c


# ── get_memory ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_memory_empty_for_new_company(session, company):
    mem = await get_memory(session, company.id)
    assert mem == {"notes": [], "kv": {}}


@pytest.mark.asyncio
async def test_get_memory_missing_company(session):
    """Returns empty dict for unknown company_id (no crash)."""
    mem = await get_memory(session, uuid.uuid4())
    assert mem == {"notes": [], "kv": {}}


# ── add_note ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_note_basic(session, company):
    await add_note(session, company.id, "Supplier A has 30-day terms")
    await session.commit()
    mem = await get_memory(session, company.id)
    assert len(mem["notes"]) == 1
    assert mem["notes"][0]["content"] == "Supplier A has 30-day terms"
    assert "added_at" in mem["notes"][0]


@pytest.mark.asyncio
async def test_add_note_trims_to_max(session, company):
    """Notes beyond MAX_NOTES are trimmed (oldest dropped)."""
    for i in range(MAX_NOTES + 5):
        await add_note(session, company.id, f"note-{i}")
    await session.commit()
    mem = await get_memory(session, company.id)
    assert len(mem["notes"]) == MAX_NOTES
    # The last MAX_NOTES notes should be the newest ones
    assert mem["notes"][-1]["content"] == f"note-{MAX_NOTES + 4}"


# ── set_kv ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_kv_basic(session, company):
    await set_kv(session, company.id, "default_currency", "THB")
    await session.commit()
    mem = await get_memory(session, company.id)
    assert mem["kv"]["default_currency"] == "THB"


@pytest.mark.asyncio
async def test_set_kv_update_existing(session, company):
    """Updating an existing key doesn't add a new entry."""
    await set_kv(session, company.id, "currency", "THB")
    await set_kv(session, company.id, "currency", "USD")
    await session.commit()
    mem = await get_memory(session, company.id)
    assert mem["kv"]["currency"] == "USD"
    assert len(mem["kv"]) == 1


@pytest.mark.asyncio
async def test_set_kv_evicts_oldest_at_max(session, company):
    """At MAX_KV_KEYS, inserting a new key evicts the oldest."""
    for i in range(MAX_KV_KEYS):
        await set_kv(session, company.id, f"key-{i}", f"val-{i}")
    await session.commit()

    # Now insert one more (new key, not updating)
    await set_kv(session, company.id, "brand-new-key", "brand-new-value")
    await session.commit()

    mem = await get_memory(session, company.id)
    assert len(mem["kv"]) == MAX_KV_KEYS
    assert "brand-new-key" in mem["kv"]
    # Oldest key (key-0) should be evicted
    assert "key-0" not in mem["kv"]


# ── clear_memory ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_clear_memory(session, company):
    await add_note(session, company.id, "some note")
    await set_kv(session, company.id, "k", "v")
    await session.commit()

    await clear_memory(session, company.id)
    await session.commit()

    mem = await get_memory(session, company.id)
    assert mem == {"notes": [], "kv": {}}


# ── set_memory no-op on missing company ───────────────────────────────────────

@pytest.mark.asyncio
async def test_set_memory_missing_company_noop(session):
    """set_memory does nothing and doesn't raise for unknown company_id."""
    await set_memory(session, uuid.uuid4(), {"notes": [], "kv": {"k": "v"}})
    # No exception and no commit needed — just ensuring it's a no-op
