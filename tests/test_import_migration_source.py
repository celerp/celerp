# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A lot created with source "migration" keeps the books of the system it came from, so only a
data migration may write that source. The raw import route refuses a caller claiming it, before
anything is written, and the books stay settled."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from celerp.models.ledger import LedgerEntry
from stock_books import assert_settled
from test_cost_restatement import auth, ids  # noqa: F401  (auth and ids are fixtures)


def _record(source: str) -> dict:
    return {"entity_id": f"item:{uuid.uuid4()}", "event_type": "item.created", "source": source,
            "idempotency_key": uuid.uuid4().hex,
            "data": {"sku": f"MIG-{uuid.uuid4().hex[:6]}", "name": "Lot", "quantity": 2, "sell_by": "piece",
                     "status": "available", "cost_total": 200.0}}


async def _events(session, auth) -> int:
    return (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"]))).scalar_one()


@pytest.mark.asyncio
async def test_a_caller_cannot_claim_the_migration_source(client, session, auth):
    local, claimed = _record("import"), _record("migration")
    before = await _events(session, auth)
    r = await client.post("/items/import/batch", headers=auth["headers"], json={"records": [local, claimed]})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "import.migration_source_reserved", detail
    assert detail["params"] == {"entity_id": claimed["entity_id"]}, detail
    assert detail["message"] == (
        f'{claimed["entity_id"]} claims source "migration": only a data migration writes that source.')
    assert await _events(session, auth) == before
    await assert_settled(client, session, auth)


@pytest.mark.asyncio
async def test_an_imported_lot_is_booked_as_opening_stock(client, session, auth):
    r = await client.post("/items/import/batch", headers=auth["headers"], json={"records": [_record("import")]})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
    await assert_settled(client, session, auth)


def test_the_refusal_is_shown_in_the_users_language():
    from ui import i18n

    i18n.set_lang("de")
    try:
        text = i18n.refusal_text({"message": "x", "message_key": "import.migration_source_reserved",
                                  "params": {"entity_id": "item:1"}})
        assert text == 'item:1 gibt die Quelle "migration" an: Nur eine Datenmigration schreibt diese Quelle.', text
    finally:
        i18n.set_lang("en")
