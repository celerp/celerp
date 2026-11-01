# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Changes waiting to be sent to the store are shown on the connection's card: how many
wait (parked, or backing off after a failure) and why, in plain words, with Retry to try
them now. Retry makes them due through execution.resume_reruns, so the queue sends them
at its next pass, and the card no longer counts them.

These run on the real database, through the app's own requests."""
from __future__ import annotations

import pytest
import sqlalchemy as sa

from celerp.db import get_session_ctx
from connector_test_support import connected
from migration_support import maker, real_client, real_engine  # noqa: F401
from pacing_guard import seeding_pacing
from test_first_sync_requests import PLATFORM, store_answers, store_owner  # noqa: F401 - fixture

pytestmark = pytest.mark.asyncio


async def _queue(company_id, entity_id: str, retry_at: str | None, error: str | None) -> None:
    from celerp.models.connector_config import OutboundQueue

    async with get_session_ctx() as s, seeding_pacing(s):
        s.add(OutboundQueue(company_id=company_id, connector=PLATFORM, entity_type="item_stock",
                            entity_id=entity_id, payload_json="{}", error_message=error,
                            next_retry_at=sa.literal_column(retry_at) if retry_at else None))
        await s.commit()


async def test_changes_waiting_to_be_sent_show_on_the_card_and_retry_sends_them_now(store_owner, store_answers):
    from fasthtml.common import to_xml

    from celerp.models.connector_config import OutboundQueue
    from ui.i18n import t
    from ui.routes.settings_connectors_first_sync import first_sync_summary_card

    await connected(store_owner.cid, PLATFORM, {"products": "outbound"})
    await _queue(store_owner.cid, "parked", "CAST('infinity' AS TIMESTAMPTZ)", "store_changed")
    await _queue(store_owner.cid, "backing-off", "now() + interval '10 minutes'", "store_changed")
    await _queue(store_owner.cid, "due", None, None)

    r = await store_owner.api.get(f"/connectors/{PLATFORM}/first-sync/summary",
                                  headers={"Authorization": f"Bearer {store_owner.tok}"})
    assert r.status_code == 200, r.text
    assert r.json()["outbound"] == {"waiting": 2, "reason": "store_changed"}
    card = to_xml(first_sync_summary_card(PLATFORM, r.json(), "en"))
    assert t("connectors.outbound_waiting", "en", service="WooCommerce", n=2,
             reason=t("connectors.fail_store_changed", "en", service="WooCommerce")) in card
    assert "store_changed" not in card
    assert f'hx-post="/settings/connectors/{PLATFORM}/outbound/retry"' in card

    page = await store_owner.ui.get(f"/settings/connectors/{PLATFORM}")
    assert f'hx-post="/settings/connectors/{PLATFORM}/outbound/retry"' in page.text

    r = await store_owner.ui.post(f"/settings/connectors/{PLATFORM}/outbound/retry",
                                  headers={"HX-Request": "true"})
    assert r.status_code == 200, r.text
    assert "outbound/retry" not in r.text, "nothing waits once Retry made it due"
    async with get_session_ctx() as s:
        rows = (await s.execute(sa.select(
            OutboundQueue.entity_id, OutboundQueue.retry_count,
            OutboundQueue.next_retry_at <= sa.func.now()).where(
            OutboundQueue.company_id == store_owner.cid, OutboundQueue.next_retry_at.is_not(None)))).all()
    assert sorted(rows) == [("backing-off", 0, True), ("parked", 0, True)]


async def test_a_card_with_nothing_waiting_has_no_outbound_retry(store_owner, store_answers):
    await connected(store_owner.cid, PLATFORM, {"products": "outbound"})
    await _queue(store_owner.cid, "due", None, None)
    r = await store_owner.api.get(f"/connectors/{PLATFORM}/first-sync/summary",
                                  headers={"Authorization": f"Bearer {store_owner.tok}"})
    assert r.json()["outbound"] == {"waiting": 0, "reason": None}
    page = await store_owner.ui.get(f"/settings/connectors/{PLATFORM}")
    assert "outbound/retry" not in page.text
