# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A connected company's record is applied in one transaction: a receiver, or anything it
calls, that commits part of the record before the record is whole is refused, and nothing
it wrote stays."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

from sqlalchemy import select


async def test_a_receiver_that_commits_midway_is_refused_and_leaves_nothing(session, auth, ids, monkeypatch):
    from celerp.models.company_peer import CompanyPeerAttention
    from celerp.services import peer_access, peer_attention, peer_import

    company_id, source = ids["company_id"], uuid.uuid4()
    grant = SimpleNamespace(company_id=company_id, source_company_id=source, last_used_at=None)

    async def _grant(session, peer):
        return grant

    async def _commits_midway(session, **kw):
        await peer_attention.record(session, company_id=company_id, source_company_id=source,
                                    entity_type="contacts", source_entity_id="half-written",
                                    label="half", reason="half")
        await session.commit()
        return "created"

    monkeypatch.setattr(peer_import, "_grant", _grant)
    monkeypatch.setitem(peer_import.RECEIVERS, "contacts", _commits_midway)
    peer = peer_access.PeerCaller(credential="k", source_company_id=source, scope="contacts")
    record = SimpleNamespace(entity_id="C-1", source_revision=1, data={"name": "C"})

    result = await peer_import.receive_records(session, peer, [record])

    assert result["created"] == 0 and [e.split(":")[0] for e in result["errors"]] == ["C-1"]
    kept = (await session.scalars(select(CompanyPeerAttention.source_entity_id).where(
        CompanyPeerAttention.company_id == company_id))).all()
    assert kept == ["C-1"]
