# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Undoing an import locks only a batch of the caller's company, after the company's lock: a
batch id of another company is refused without its row ever being locked, and the lock
checker's census finds no import batch taken outside the company lock."""
from __future__ import annotations

import sys
import uuid

from test_helpers import company_auth


def _ungated() -> set[str]:
    return sys.modules["lock_rank"].RECORDER.ungated_sites({"row:import_batches"})


async def _batch(session, company_id) -> uuid.UUID:
    from celerp.models.import_batch import ImportBatch

    batch = ImportBatch(company_id=company_id, entity_type="item", row_count=0, entity_ids=[],
                        idempotency_keys=[], reversible=True)
    session.add(batch)
    await session.commit()
    return batch.id


async def test_undoing_an_import_locks_only_the_callers_batch(client, session, auth, ids):
    other = await company_auth(session, uuid.uuid4(), uuid.uuid4())
    foreign = await _batch(session, other["company_id"])
    own = await _batch(session, ids["company_id"])
    before = _ungated()

    r = await client.post(f"/items/import/batches/{foreign}/undo", headers=auth["headers"])
    assert r.status_code == 404, r.text
    r = await client.post(f"/items/import/batches/{own}/undo", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert _ungated() - before == set()
