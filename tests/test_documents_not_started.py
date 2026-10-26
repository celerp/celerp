# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Core work that reaches into Documents waits for it to run, not only for the company to use it:
a list write, the startup tax notice and the activity wording each judge both, and refuse or skip
cleanly, without importing Documents, in a process where it has not started."""
from __future__ import annotations

import sys
import uuid

import pytest
from sqlalchemy import func, select

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.modules.boundary import ModuleOff

pytestmark = pytest.mark.asyncio


@pytest.fixture
def docs_cannot_import(docs_not_started, monkeypatch):
    """Not started, and its code cannot be imported: a list write must refuse before reaching it."""
    for name in ("celerp_docs.doc_projections", "celerp_docs.doc_money"):
        monkeypatch.setitem(sys.modules, name, None)


async def _ledger_count(session, company_id) -> int:
    return (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == company_id))).scalar_one()


@pytest.mark.parametrize("event_type", ["list.created", "list.updated"])
async def test_a_list_write_is_refused_while_documents_has_not_started(session, auth, docs_cannot_import, event_type):
    from celerp.events.engine import emit_event

    entity_id = f"list:{uuid.uuid4()}"
    before = await _ledger_count(session, auth["company_id"])
    with pytest.raises(ModuleOff) as refused:
        await emit_event(session, company_id=auth["company_id"], entity_id=entity_id, entity_type="list",
                         event_type=event_type, data={"list_type": "quotation", "line_items": []},
                         actor_id=None, location_id=None, source="system", idempotency_key=uuid.uuid4().hex)
    assert "starts after Celerp restarts" in str(refused.value.detail)
    await session.rollback()
    assert await _ledger_count(session, auth["company_id"]) == before
    assert await session.get(Projection, (auth["company_id"], entity_id)) is None


async def test_the_startup_tax_notice_skips_while_documents_has_not_started(session, auth, docs_cannot_import):
    from celerp.services.tax_seed_notice import reconcile_list_tax_notice

    await reconcile_list_tax_notice(session, auth["company_id"], {})


_TAX_CHANGE = {"line_items": {"old": [{"sku": "A-1", "taxes": []}],
                              "new": [{"sku": "A-1", "taxes": [{"code": "VAT", "rate": 7}]}]}}


@pytest.mark.parametrize("docs_on", [True, False])
async def test_the_activity_wording_follows_each_companys_documents(docs_on):
    """Two companies on one installation: the one using Documents reads the line's tax change, the
    other does not, whatever is installed."""
    from ui.components.activity import detail_from_entry

    shown = detail_from_entry({"fields_changed": _TAX_CHANGE}, "list.updated", "USD", docs_on=docs_on)
    assert ("Tax 0% → 7%" in shown) is docs_on


async def test_the_activity_wording_never_imports_documents_for_a_company_without_it(docs_cannot_import):
    from ui.components.activity import activity_table

    activity_table([{"event_type": "list.updated", "entity_id": "list:x", "entity_type": "list",
                     "data": {"fields_changed": _TAX_CHANGE}}], docs_on=False)
