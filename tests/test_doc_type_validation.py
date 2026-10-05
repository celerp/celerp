# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A document is numbered by its type, so creating one of a type the app does not have is
refused as invalid input, naming the type, before anything is written."""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from celerp.models.ledger import LedgerEntry
from test_cost_restatement import auth, ids  # noqa: F401  (auth and ids are fixtures)


async def _events(session, auth) -> int:
    return (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"]))).scalar_one()


@pytest.mark.asyncio
async def test_an_unknown_document_type_is_refused_by_name(client, session, auth):
    before = await _events(session, auth)
    r = await client.post("/docs", headers=auth["headers"], json={"doc_type": "sales_order", "total": 0})
    assert r.status_code == 422, r.text
    assert "'sales_order' is not a document type" in r.text, r.text
    assert await _events(session, auth) == before


@pytest.mark.asyncio
async def test_a_known_document_type_is_created(client, session, auth):
    r = await client.post("/docs", headers=auth["headers"], json={"doc_type": "quotation", "total": 0})
    assert r.status_code == 200, r.text
