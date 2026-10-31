# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A first sync started from its preview page runs through the app's own requests: the
settings page asks the API to start it, the API saves the choices with an import batch for
the items it may create, and the first sync runs in the background under its execution
lease, renewing it while it works and releasing it when done. An import batch the first
sync left empty is removed, so Import History never lists a batch with nothing to undo.

These run on the real database (each request commits), as the background execution takes
its own connection and waits for the connector-owner key the starting request holds."""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient

from company_backup_support import company, owner, token
from connector_test_support import connected
from migration_support import maker, real_client, real_engine  # noqa: F401

pytestmark = pytest.mark.asyncio

PLATFORM = "woocommerce"


async def _settled(tasks: set) -> None:
    while pending := {t for t in tasks if not t.done()}:
        await asyncio.wait(pending)


@pytest_asyncio.fixture
async def store_owner(real_engine, real_client, connector_modules_running):
    """The owner of a company with a store connection, signed in to the settings pages,
    which talk to the real API in process."""
    from celerp.main import app
    from ui.app import app as ui_app

    user = await owner(real_engine)
    cid = await company(real_engine, user, "Store Trading", "store-marker")
    tok = await token(real_engine, user, cid)

    def _bridged(t, timeout=10.0):
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Authorization": f"Bearer {t}"}, follow_redirects=True)

    with patch("ui.api_client._client", _bridged):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                               follow_redirects=False, cookies={"celerp_token": tok}) as ui:
            yield SimpleNamespace(cid=str(cid), tok=tok, ui=ui, api=real_client)


@pytest.fixture
def store_answers(signed_in_to_cloud, monkeypatch):
    """The relay hands out a store token and the store's webhooks need nothing."""
    from celerp.connectors.base import ConnectorContext

    monkeypatch.setattr("celerp.connectors.relay_token.fetch_context", AsyncMock(
        side_effect=lambda cid, *_a, **_k: ConnectorContext(company_id=str(cid), access_token="t")))
    monkeypatch.setattr("celerp.connectors.webhook_reconcile.reconcile_webhooks", AsyncMock())


async def test_starting_the_first_sync_from_its_preview_runs_it_under_a_renewed_lease(
        store_owner, store_answers, real_engine, monkeypatch):
    from celerp.connectors import execution, sync_runner
    from celerp.connectors.base import SyncEntity, SyncResult
    from celerp.models.connector_first_sync import ConnectorFirstSync
    from celerp.models.import_batch import ImportBatch
    from celerp.services import background

    await connected(store_owner.cid, PLATFORM, {"products": "inbound"}, first_sync="pending")

    # The store answers slowly: the lease is renewed at least once while the sync runs.
    renewed = asyncio.Event()
    renew = execution._renew_once

    async def _renewing(ex):
        ok = await renew(ex)
        renewed.set()
        return ok

    async def _store(*_a, **_k):
        await renewed.wait()
        return [SyncResult(entity=SyncEntity.PRODUCTS)]  # nothing in the store to bring in

    monkeypatch.setattr(execution, "RENEW_EVERY", 0.01)
    monkeypatch.setattr(execution, "_renew_once", _renewing)
    monkeypatch.setattr(sync_runner, "run_connector_sync", _store)

    before = set(background._BG_TASKS)
    r = await store_owner.ui.post(f"/settings/connectors/{PLATFORM}/first-sync",
                                  data={"match": "keep"}, headers={"HX-Request": "true"})
    assert r.status_code == 204, r.text
    assert r.headers["HX-Redirect"] == f"/settings/connectors/{PLATFORM}"
    await asyncio.wait_for(_settled(set(background._BG_TASKS) - before), 30)

    assert renewed.is_set()
    async with maker(real_engine)() as s:
        record = await s.get(ConnectorFirstSync, (store_owner.cid, PLATFORM))
        assert record.state == "completed", record.error
        assert record.batch_id is None
        assert (await s.scalars(sa.select(ImportBatch).where(
            ImportBatch.company_id == uuid.UUID(store_owner.cid)))).all() == []
        assert await execution.live_executions(s, store_owner.cid, PLATFORM) == []
