"""Delivery marks held in memory, and a well-formed accountant key, for connector tests
that stub the network and the readers but have no database."""
from __future__ import annotations

import pytest

KEY = "cp1.6f1c2a8e-4b3d-4e2a-9c1f-0d5e7b8a9c10.2b7e4f1a-8c3d-4a5b-9e6f-1d2c3b4a5e6f.AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"


@pytest.fixture
def memory_marks(monkeypatch):
    """What each record's last delivery was, keyed like the real table."""
    from celerp.services import peer_journals

    marks: dict = {}

    async def sent_marks(company_id, grant_id, entity, ids):
        return {i: marks[(company_id, grant_id, entity, i)] for i in ids
                if (company_id, grant_id, entity, i) in marks}, None

    async def note_delivery(company_id, grant_id, entity, entity_id, revision, refused, record=None):
        marks[(company_id, grant_id, entity, entity_id)] = (revision, refused)

    monkeypatch.setattr(peer_journals, "sent_marks", sent_marks)
    monkeypatch.setattr(peer_journals, "note_delivery", note_delivery)
    return marks
