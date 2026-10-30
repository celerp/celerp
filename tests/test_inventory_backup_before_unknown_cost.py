# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A backup taken before unknown goods costs were kept restores unchanged.

``fixtures/inv_unknown_cost`` was captured by driving the older release's own API
(``fixtures/inv_unknown_cost/capture.py``, run from a checkout of that release): a cleared goods
cost on a lot carrying freight, a merge of a known and an unknown source, a merge of a lot
carrying a bill's freight, a priced receipt into a lot, a split and a sold-out lot. Restored
today, every lot keeps the state the older release gave it, and rebuilding the company from its
events gives the same states."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from migration_support import maker, real_client, real_engine  # noqa: F401
from test_company_backup import _BK_UUID, _bk_local, _bk_restore_new, _bk_setup
from test_company_backup_snapshot import _replayed_states

pytestmark = pytest.mark.asyncio
FIXTURE = Path(__file__).parent / "fixtures" / "inv_unknown_cost"


def _normalized(states) -> list[str]:
    return sorted(_BK_UUID.sub("<id>", json.dumps(s, sort_keys=True, default=str)) for s in states)


async def test_backup_from_before_restores_and_replays_unchanged(real_engine, real_client, tmp_path, monkeypatch):
    from celerp.models.projections import Projection

    _bk_local(monkeypatch, tmp_path)
    _user, _cid, tok = await _bk_setup(real_engine)
    cid = await _bk_restore_new(real_client, tok, (FIXTURE / "company.celerp-company").read_bytes())
    async with maker(real_engine)() as s:
        items = (await s.execute(select(Projection.state).where(
            Projection.company_id == cid, Projection.entity_type == "item"))).scalars().all()
    captured = json.loads((FIXTURE / "items.json").read_text())["items"]
    assert _normalized(items) == _normalized(captured.values())

    stored, replayed = await _replayed_states(real_engine, cid)
    diff = {k: (stored.get(k), replayed.get(k)) for k in stored.keys() | replayed.keys()
            if stored.get(k) != replayed.get(k)}
    assert not diff, diff
