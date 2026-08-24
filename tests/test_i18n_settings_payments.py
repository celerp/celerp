# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the Web Access > Payments settings route.

The payments settings page builds its browser <title> through the shared
``page_title`` helper (R5), which resolves its translation key by calling ``t()``
at render time, so a request in a non-English language gets a translated title
instead of the generic hardcoded "Celerp" default. This test registers a sentinel
language ``xx`` and drives the real ``/settings/payments`` route through the UI
app, asserting the sentinel title reaches the rendered page. It is red against a
tree that omits the title (falling back to the "Celerp" default), and needs no
en.json change (the sentinel language carries the value).

Conversion mechanism covered:
  - the R5 browser-title helper (``page_title("nav.payments")``), resolved at
    render time in the request's language.
"""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient

from ui import i18n
from test_helpers import make_test_token

# Sentinel catalog: an unmistakable value for the key the payments page renders
# into its browser <title>.
_XX = {
    "nav.payments": "XX_PAYMENTS",
}


@pytest.fixture(autouse=True)
def _xx_lang():
    """Register the sentinel language and reset the registry and context language
    afterwards so nothing leaks between tests."""
    i18n.clear_registry()
    i18n._cached_load.cache_clear()
    i18n.register_catalog("xx", _XX)
    i18n.set_lang("xx")
    yield
    i18n.set_lang("en")
    i18n.clear_registry()
    i18n._cached_load.cache_clear()


@pytest_asyncio.fixture
async def ui_client():
    """httpx client against the UI app, in process (no lifespan, no DB pool)."""
    from ui.app import app as ui_app
    async with AsyncClient(
        transport=ASGITransport(app=ui_app),
        base_url="http://ui",
        follow_redirects=False,
    ) as c:
        yield c


async def test_payments_browser_title_translates(ui_client):
    """The <title> the payments settings page renders resolves through page_title
    at render time. The relay/status/company calls are stubbed so the pre-connect
    pitch renders deterministically; the full shell carries the title."""
    with patch("ui.api_client.get_company", new=AsyncMock(return_value={"settings": {}})), \
         patch("ui.api_client.get_relay_status", new=AsyncMock(return_value={"connected": True})), \
         patch("ui.api_client.get_payments_status", new=AsyncMock(return_value={"enabled": False})):
        r = await ui_client.get(
            "/settings/payments",
            cookies={"celerp_token": make_test_token(), "celerp_lang": "xx"},
        )
    assert r.status_code == 200
    # The R5 browser title (my conversion) reaches the <title> element in the
    # active language. The " - Celerp" suffix is what page_title adds, so this is
    # distinct from the page_header H1, which renders the same key without it -
    # asserting the full title string is what makes this red against the generic
    # "Celerp" default at merge-base rather than matching the header echo.
    assert "<title>XX_PAYMENTS - Celerp</title>" in r.text
