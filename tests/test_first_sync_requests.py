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
import re
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import sqlalchemy as sa

from company_backup_support import company, owner, token
from connector_test_support import connected
from migration_support import maker, real_client, real_engine  # noqa: F401
from test_helpers import ui_signed_in

pytestmark = pytest.mark.asyncio

PLATFORM = "woocommerce"


async def _settled(tasks: set) -> None:
    while pending := {t for t in tasks if not t.done()}:
        await asyncio.wait(pending)


@pytest_asyncio.fixture
async def store_owner(real_engine, real_client, connector_modules_running):
    """The owner of a company with a store connection, signed in to the settings pages,
    which talk to the real API in process."""
    user = await owner(real_engine)
    cid = await company(real_engine, user, "Store Trading", "store-marker")
    tok = await token(real_engine, user, cid)
    async with ui_signed_in(tok) as ui:
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
        return SyncResult(entity=SyncEntity.PRODUCTS)  # nothing in the store to bring in

    monkeypatch.setattr(execution, "RENEW_EVERY", 0.01)
    monkeypatch.setattr(execution, "_renew_once", _renewing)
    monkeypatch.setattr(sync_runner, "run_sync", _store)

    from celerp.connectors import first_sync

    monkeypatch.setattr(first_sync, "_preview_products", AsyncMock())  # the store has no products
    page = await store_owner.ui.get(f"/settings/connectors/{PLATFORM}/first-sync")
    version = re.search(r'name="preview_version" value="([0-9a-f]{64})"', page.text).group(1)
    before = set(background._BG_TASKS)
    r = await store_owner.ui.post(f"/settings/connectors/{PLATFORM}/first-sync",
                                  data={"match": "keep", "preview_version": version}, headers={"HX-Request": "true"})
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


async def test_the_first_sync_preview_its_summary_and_the_status_poll_read_through_their_requests(
        store_owner, store_answers, monkeypatch):
    """The read pages of a pending first sync, each through its own request: the preview page
    asks the API for the preview, the summary is read from the API, and the settings page polls
    the connection's status. The write observer holds each of them to writing nothing."""
    from celerp.connectors import first_sync

    await connected(store_owner.cid, PLATFORM, {"products": "inbound"}, first_sync="pending")
    monkeypatch.setattr(first_sync, "_preview_products", AsyncMock())  # the store has no products

    r = await store_owner.ui.get(f"/settings/connectors/{PLATFORM}/first-sync")
    assert r.status_code == 200, r.text
    assert f'hx-post="/settings/connectors/{PLATFORM}/first-sync"' in r.text

    r = await store_owner.api.get(f"/connectors/{PLATFORM}/first-sync/summary",
                                  headers={"Authorization": f"Bearer {store_owner.tok}"})
    assert r.status_code == 200, r.text
    assert r.json()["batch"] is None

    r = await store_owner.ui.get(f"/settings/connectors/{PLATFORM}/status")
    assert r.status_code == 200, r.text


async def test_a_stopped_first_sync_shows_why_and_its_retry_runs_it_to_the_end(
        store_owner, store_answers, real_engine, monkeypatch):
    """A first sync the store stopped shows the reason and Retry on the connection's summary;
    Retry starts it again through the API and it runs in the background to completion. A
    second Retry is refused with the reason on the card, as there is nothing left to retry."""
    from datetime import datetime, timezone

    from celerp.connectors import sync_runner
    from celerp.connectors.base import SyncEntity, SyncResult
    from celerp.models.connector_first_sync import ConnectorFirstSync
    from celerp.services import background

    await connected(store_owner.cid, PLATFORM, {"products": "inbound"}, first_sync="failed")
    async with maker(real_engine)() as s:
        record = await s.get(ConnectorFirstSync, (store_owner.cid, PLATFORM))
        record.directions, record.match = {"products": "inbound"}, "keep"
        record.started_at = record.watermark = datetime.now(timezone.utc)
        record.error = {"key": "connectors.err_read_only_key"}
        await s.commit()
    monkeypatch.setattr(sync_runner, "run_sync", AsyncMock(return_value=SyncResult(entity=SyncEntity.PRODUCTS)))

    r = await store_owner.api.get(f"/connectors/{PLATFORM}/first-sync/summary",
                                  headers={"Authorization": f"Bearer {store_owner.tok}"})
    assert (r.json()["state"], r.json()["error"]) == ("failed", {"key": "connectors.err_read_only_key"})
    from ui.routes.settings_connectors_first_sync import first_sync_summary_card
    from fasthtml.common import to_xml
    card = to_xml(first_sync_summary_card(PLATFORM, r.json(), "en"))
    assert "This API key is read-only" in card
    assert f'hx-post="/settings/connectors/{PLATFORM}/first-sync/retry"' in card

    before = set(background._BG_TASKS)
    r = await store_owner.ui.post(f"/settings/connectors/{PLATFORM}/first-sync/retry",
                                  headers={"HX-Request": "true"})
    assert r.status_code == 200, r.text
    await asyncio.wait_for(_settled(set(background._BG_TASKS) - before), 30)
    async with maker(real_engine)() as s:
        record = await s.get(ConnectorFirstSync, (store_owner.cid, PLATFORM))
        assert (record.state, record.error) == ("completed", None)

    r = await store_owner.ui.post(f"/settings/connectors/{PLATFORM}/first-sync/retry",
                                  headers={"HX-Request": "true"})
    assert "This first sync has not stopped, so there is nothing to retry." in r.text
    assert "first-sync/retry" not in r.text
