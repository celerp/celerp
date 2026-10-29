# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Every writer of a company's peer attention items and delivery marks takes the company's
lock before the row, also when the transaction holds nothing yet (a record refused after its
rollback, a delivery noted in its own session), so the lock checker's census finds no such row
taken outside the company lock."""
from __future__ import annotations

import sys
import uuid

_CLASSES = ("row:company_peer_attention", "row:company_peer_deliveries")


async def test_attention_and_delivery_writers_take_the_company_lock_first(session, auth, ids):
    from celerp.services import peer_attention, peer_journals

    company_id, source = ids["company_id"], uuid.uuid4()
    await session.commit()
    recorder = sys.modules["lock_rank"].RECORDER
    before = recorder.ungated_sites(_CLASSES)
    where = dict(company_id=company_id, source_company_id=source, entity_type="journals",
                 source_entity_id="J-1")
    await peer_attention.record(session, **where, label="J-1", reason="Refused")
    await session.commit()
    await peer_attention.clear(session, **where)
    await session.commit()
    await peer_journals.mark(session, company_id=company_id, grant_id=uuid.uuid4(), entity="journals",
                             entity_id="J-1", revision=1, refused=None)
    await session.commit()
    assert recorder.ungated_sites(_CLASSES) - before == set()
