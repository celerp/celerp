# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A store this company never connected opens on a page that says so and offers Connect,
not Sync now, Disconnect and directions that would only refuse. Its breadcrumb names the
page it leads back to, Web Access.

These run on the real database, through the app's own requests."""
from __future__ import annotations

import pytest

from connector_test_support import connected
from migration_support import maker, real_client, real_engine  # noqa: F401
from test_first_sync_requests import PLATFORM, store_answers, store_owner  # noqa: F401 - fixture

pytestmark = pytest.mark.asyncio


def _crumb(label: str, tab: str) -> str:
    return f'<a href="/settings/cloud?tab={tab}" class="breadcrumb-link">{label}</a>'


async def test_a_never_connected_store_page_offers_connect_only(store_owner):
    from ui.i18n import t

    page = await store_owner.ui.get(f"/settings/connectors/{PLATFORM}")
    assert page.status_code == 200, page.text
    assert t("connectors.not_connected", "en") in page.text
    assert f'hx-post="/settings/connectors/{PLATFORM}/connect-apikey"' in page.text
    assert f'/settings/connectors/{PLATFORM}/sync"' not in page.text
    assert f'/settings/connectors/{PLATFORM}/disconnect' not in page.text
    assert f'/settings/connectors/{PLATFORM}/direction' not in page.text
    assert _crumb(t("settings_cloud.web_access", "en"), "website") in page.text


async def test_a_connected_store_page_keeps_its_actions(store_owner, store_answers):
    from ui.i18n import t

    await connected(store_owner.cid, PLATFORM, {"products": "outbound"})
    page = await store_owner.ui.get(f"/settings/connectors/{PLATFORM}")
    assert f'/settings/connectors/{PLATFORM}/disconnect' in page.text
    assert f'hx-post="/settings/connectors/{PLATFORM}/connect-apikey"' not in page.text
    assert _crumb(t("settings_cloud.web_access", "en"), "website") in page.text
