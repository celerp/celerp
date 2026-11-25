"""Hiding the GitHub star card on an installation whose runtime-state row was never written
creates the row through lock_runtime_state and records the dismissal in it."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select

from celerp.models.auth import SystemRuntimeState


@pytest.mark.asyncio
async def test_dismissing_the_star_card_creates_the_missing_runtime_state_row(client, session):
    suffix = uuid.uuid4().hex[:10]
    r = await client.post("/auth/register", json={
        "company_name": f"Co {suffix}", "email": f"admin_{suffix}@test.example",
        "name": "Admin", "password": "pw123val",
    })
    assert r.status_code == 200, r.text
    await session.execute(delete(SystemRuntimeState))
    await session.commit()

    r = await client.post("/stars/dismiss", headers={"Authorization": f"Bearer {r.json()['access_token']}"})

    assert r.status_code == 200, r.text
    rows = (await session.execute(select(SystemRuntimeState.id, SystemRuntimeState.value))).all()
    assert [(row.id, row.value.get("star_prompt_dismissed")) for row in rows] == [(1, True)], rows
