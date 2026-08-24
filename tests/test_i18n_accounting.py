# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the accounting route.

The accounting workspace builds its browser <title> through the shared
``page_title`` helper (R5), which resolves its translation key by calling
``t()`` at render time, so a request in a non-English language gets a translated
title instead of a hardcoded "Accounting - Celerp" string. This test registers a
sentinel language ``xx`` and drives the real ``/accounting`` route through the UI
app, asserting the sentinel title reaches the rendered page. It is red against a
tree that hardcodes the English title, and needs no en.json change (the sentinel
language carries the value).

Conversion mechanism covered:
  - the R5 browser-title helper (``page_title("page.accounting")``), resolved at
    render time in the request's language.
"""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch
from httpx import ASGITransport, AsyncClient

from ui import i18n
from ui.api_client import APIError
from test_helpers import make_test_token

# Sentinel catalog: unmistakable values for the keys the accounting page renders
# on the path exercised below (the browser title and the error banner it lands on
# when the books cannot be read).
_XX = {
    "page.accounting": "XX_ACCT_TITLE",
    "acct.not_authorized": "XX_NOT_AUTH",
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


async def test_accounting_browser_title_translates(ui_client):
    """The <title> the accounting page renders resolves through page_title at
    render time. get_company is forced to 403 so the page renders its error branch
    (no journal/fiscal fetch needed); the full shell still carries the title."""
    boom = AsyncMock(side_effect=APIError(403, "forbidden"))
    with patch("ui.api_client.get_company", new=boom):
        r = await ui_client.get(
            "/accounting",
            cookies={"celerp_token": make_test_token(), "celerp_lang": "xx"},
        )
    assert r.status_code == 200
    # The R5 browser title (my conversion) reaches the <title> element in the
    # active language. The " - Celerp" suffix is what page_title adds, so this is
    # distinct from the page_header H1, which renders the same key without it -
    # asserting the full title string is what makes this red against the hardcoded
    # "Accounting - Celerp" at merge-base rather than matching the header echo.
    assert "<title>XX_ACCT_TITLE - Celerp</title>" in r.text
    # And the error-banner label resolves at render time on the same page.
    assert "XX_NOT_AUTH" in r.text
