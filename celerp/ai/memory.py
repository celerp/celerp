# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""AI memory — per-company context stored in company.settings['ai_memory'].

Memory is a JSON object stored in the Company.settings column under the key
"ai_memory". It persists across sessions and is passed as context on every
AI query for that company.

Schema (stored in settings["ai_memory"]):
  {
    "notes": [{"content": str, "added_at": ISO8601}],  # max MAX_NOTES
    "kv": {"key": "value", ...}                         # max MAX_KV_KEYS
  }

The AI can read and update this memory via tool calls (update_memory, clear_memory).
The router exposes GET/DELETE endpoints for human management.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.company import Company

MAX_NOTES = 50
MAX_KV_KEYS = 100
_MEM_KEY = "ai_memory"


def _empty() -> dict[str, Any]:
    """Fresh empty memory structure. Returns a new dict each time (safe to mutate)."""
    return {"notes": [], "kv": {}}


async def get_memory(session: AsyncSession, company_id: str | uuid.UUID) -> dict[str, Any]:
    """Return the AI memory dict for a company (empty if not set)."""
    row = await session.get(Company, company_id)
    if row is None:
        return _empty()
    return dict(row.settings.get(_MEM_KEY, _empty()))


async def set_memory(
    session: AsyncSession,
    company_id: str | uuid.UUID,
    memory: dict[str, Any],
) -> None:
    """Persist AI memory. Caller is responsible for committing the session."""
    row = await session.get(Company, company_id)
    if row is None:
        return
    settings = dict(row.settings)
    settings[_MEM_KEY] = memory
    row.settings = settings
    session.add(row)


async def add_note(
    session: AsyncSession,
    company_id: str | uuid.UUID,
    content: str,
) -> None:
    """Append a note to AI memory, trimming to MAX_NOTES oldest."""
    mem = await get_memory(session, company_id)
    notes = list(mem.get("notes", []))
    notes.append({"content": content, "added_at": datetime.now(timezone.utc).isoformat()})
    if len(notes) > MAX_NOTES:
        notes = notes[-MAX_NOTES:]
    mem["notes"] = notes
    await set_memory(session, company_id, mem)


async def set_kv(
    session: AsyncSession,
    company_id: str | uuid.UUID,
    key: str,
    value: str,
) -> None:
    """Set a key-value fact in AI memory."""
    mem = await get_memory(session, company_id)
    kv = dict(mem.get("kv", {}))
    if len(kv) >= MAX_KV_KEYS and key not in kv:
        # Drop oldest key to stay within limit (dict preserves insertion order in Python 3.7+)
        oldest = next(iter(kv))
        del kv[oldest]
    kv[key] = value
    mem["kv"] = kv
    await set_memory(session, company_id, mem)


async def clear_memory(
    session: AsyncSession,
    company_id: str | uuid.UUID,
) -> None:
    """Wipe all AI memory for a company."""
    await set_memory(session, company_id, _empty())
