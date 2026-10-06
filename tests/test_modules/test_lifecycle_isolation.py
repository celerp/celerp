# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""A failing lifecycle hook leaves the caller's transaction as it found it.

fire_lifecycle is best-effort: a hook that fails is logged and its siblings and
the caller carry on. That only holds if the failed hook's own writes go with it,
so each hook runs in its own savepoint on the caller's session. Each test uses
its own database (committed_engine) so the caller's commit is real.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.models.company import Company
from celerp.modules import slots


def _company(name: str, slug: str | None = None) -> Company:
    return Company(name=name, slug=slug or f"{name}-{uuid.uuid4().hex[:6]}", settings={})


async def _writes_then_raises(*, session, **_):
    session.add(_company("hook-partial"))
    await session.flush()
    raise RuntimeError("hook failed after writing")


async def _adds_then_raises(*, session, **_):
    session.add(_company("hook-pending"))
    raise RuntimeError("hook failed before flushing")


async def _duplicates_parent(*, session, parent_slug, **_):
    session.add(_company("hook-duplicate", parent_slug))
    await session.flush()


async def _sibling(*, session, **_):
    session.add(_company("sibling"))


@pytest.fixture(autouse=True)
def _clean_slots():
    slots.clear()
    yield
    slots.clear()


def _register(*handlers) -> None:
    for fn in handlers:
        slots.register("on_modules_ready", {"handler": f"{__name__}:{fn.__name__}",
                                            "_module": "acme-test"})


async def _names(factory) -> set[str]:
    async with factory() as s:
        return set((await s.execute(select(Company.name))).scalars())


@pytest.fixture
def factory(committed_engine):
    return async_sessionmaker(committed_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.mark.parametrize("failing", [_writes_then_raises, _adds_then_raises])
async def test_failed_hook_writes_are_not_committed_by_the_caller(factory, failing):
    _register(failing, _sibling)
    async with factory() as s:
        s.add(_company("parent"))
        await slots.fire_lifecycle("on_modules_ready", session=s)
        await s.commit()

    names = await _names(factory)
    assert "parent" in names and "sibling" in names
    assert not names & {"hook-partial", "hook-pending"}, "a failed hook's writes were committed"


async def test_hook_database_error_leaves_the_caller_able_to_commit(factory):
    _register(_duplicates_parent, _sibling)
    parent = _company("parent")
    async with factory() as s:
        s.add(parent)
        await slots.fire_lifecycle("on_modules_ready", session=s, parent_slug=parent.slug)
        await s.commit()

    names = await _names(factory)
    assert "parent" in names and "sibling" in names
    assert "hook-duplicate" not in names
