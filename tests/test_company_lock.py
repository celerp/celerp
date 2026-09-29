# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Company settings are one JSON value, so every change to them is made on a company
loaded with locked_company() in the same transaction."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm.attributes import flag_modified

from celerp.models.company import Company
from celerp.services.company_lock import locked_company

pytestmark = pytest.mark.asyncio


async def _company(session) -> uuid.UUID:
    company_id = uuid.uuid4()
    session.add(Company(id=company_id, name="LockCo", slug=f"lock-{company_id.hex[:8]}", settings={"a": 1}))
    await session.commit()
    return company_id


async def test_settings_change_without_the_lock_is_refused(session):
    company_id = await _company(session)
    company = await session.get(Company, company_id)
    company.settings = {**company.settings, "b": 2}
    with pytest.raises(RuntimeError, match="locked_company"):
        await session.flush()
    await session.rollback()


async def test_in_place_settings_change_without_the_lock_is_refused(session):
    company_id = await _company(session)
    company = await session.get(Company, company_id)
    company.settings["b"] = 2
    flag_modified(company, "settings")
    with pytest.raises(RuntimeError, match="locked_company"):
        await session.flush()
    await session.rollback()


async def test_settings_change_under_the_lock_is_saved(session):
    company_id = await _company(session)
    company = await locked_company(session, company_id)
    company.settings = {**company.settings, "b": 2}
    await session.commit()
    assert (await session.get(Company, company_id)).settings == {"a": 1, "b": 2}


async def test_the_lock_ends_with_its_transaction(session):
    company_id = await _company(session)
    await locked_company(session, company_id)
    await session.commit()
    company = await session.get(Company, company_id)
    company.settings = {**company.settings, "b": 2}
    with pytest.raises(RuntimeError, match="locked_company"):
        await session.flush()
    await session.rollback()


async def test_other_company_columns_need_no_lock(session):
    company_id = await _company(session)
    company = await session.get(Company, company_id)
    company.name = "Renamed"
    await session.commit()
    assert (await session.get(Company, company_id)).name == "Renamed"


async def test_numbering_without_the_lock_is_refused(session):
    from celerp_docs.sequences import next_doc_ref

    company_id = await _company(session)
    company = await session.get(Company, company_id)
    next_doc_ref(company, "invoice")
    with pytest.raises(RuntimeError, match="locked_company"):
        await session.flush()
    await session.rollback()
