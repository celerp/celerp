# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A change run under one_unit commits whole or not at all: a step that commits the
outer transaction part way through is refused, while savepoints still release and the
caller's own commit after the unit goes through."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.db import one_unit


@pytest.fixture
def factory(_db_engine):
    return async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.mark.asyncio
async def test_a_commit_inside_a_unit_is_refused(factory):
    async with factory() as s:
        await s.execute(text("SELECT 1"))
        with one_unit(s):
            with pytest.raises(RuntimeError, match="committed part of a change"):
                await s.commit()


@pytest.mark.asyncio
async def test_a_nested_unit_keeps_the_outer_one_open(factory):
    async with factory() as s:
        with one_unit(s):
            with one_unit(s):
                pass
            with pytest.raises(RuntimeError, match="committed part of a change"):
                await s.commit()


@pytest.mark.asyncio
async def test_savepoints_release_inside_a_unit_and_the_caller_commits_after(factory):
    async with factory() as s:
        with one_unit(s):
            async with s.begin_nested():
                await s.execute(text("SELECT 1"))
        await s.commit()
        assert "one_unit_open" not in s.info
