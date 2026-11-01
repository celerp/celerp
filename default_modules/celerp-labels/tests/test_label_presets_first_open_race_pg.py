# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A company's label templates are seeded on their first read: the default presets, and a record
kind's own template. Two first reads at once would each find no template and each seed them, so a
seeder takes the company's lock and reads again under it: the second waits for the first to commit
and then finds the templates.

Each case runs on real PostgreSQL across two connections."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.models.company import Company

pytestmark = pytest.mark.asyncio

ROUNDS = 10
_KIND = {"entity_type": "x_widget", "label_key": "label.name", "keys": ["name"],
         "preset": {"fields": [{"key": "name", "label": "label.name", "type": "text"}]}}


@pytest.fixture
def factory(committed_engine):
    return async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)


async def _seed(s, cid):
    from celerp_labels.routes import _seed_presets
    await _seed_presets(s, cid)


async def _source(s, cid):
    from celerp_labels.routes import _source_templates
    await _source_templates(s, cid, _KIND)


@pytest.mark.parametrize("first_read", [_seed, _source], ids=["presets", "source"])
async def test_two_first_reads_at_once_seed_one_set(factory, first_read):
    from celerp_labels.models import LabelTemplate
    from celerp_labels.presets import PRESET_TEMPLATES

    expected = len(PRESET_TEMPLATES) + (first_read is _source)

    async def read(cid) -> None:
        async with factory() as s:
            await first_read(s, cid)
            await s.commit()

    for _ in range(ROUNDS):
        cid = uuid.uuid4()
        async with factory() as s:
            s.add(Company(id=cid, name="Labels Co", slug=f"lb-{cid.hex[:8]}", settings={}))
            await s.commit()
        await asyncio.wait_for(asyncio.gather(read(cid), read(cid)), 10)
        async with factory() as s:
            assert (await s.execute(select(func.count()).select_from(LabelTemplate)
                                    .where(LabelTemplate.company_id == cid))).scalar_one() == expected
