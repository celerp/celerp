# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The core 404/500 error pages route their chrome through t().

These pages are rendered by the core app (not a module), so their strings live
in the central catalog under the ``error.*`` namespace. The test registers a
sentinel catalog for those keys, drives each handler with a passthrough shell,
and asserts the sentinel text reaches the response body. It is red against a
tree that hardcodes the English strings.
"""

import pytest

from fasthtml.common import Div

_XX = {
    "error.page_not_found": "XX_404_TITLE",
    "error.page_not_found_body": "XX_404_BODY",
    "error.go_to_settings": "XX_GO_SETTINGS",
    "error.not_found_title": "XX_404_BROWSER",
    "error.something_went_wrong": "XX_500_TITLE",
    "error.unexpected_error_body": "XX_500_BODY",
    "error.back_to_dashboard": "XX_BACK_DASH",
    "error.server_error_title": "XX_500_BROWSER",
}


@pytest.fixture
def _xx_lang():
    from ui import i18n
    i18n.clear_registry()
    i18n._cached_load.cache_clear()
    i18n.register_catalog("xx", _XX)
    i18n.set_lang("xx")
    yield
    i18n.set_lang("en")
    i18n.clear_registry()
    i18n._cached_load.cache_clear()


async def _passthrough_shell(*content, title="", **kwargs):
    """Stand in for base_shell: keep the rendered content and the browser title
    visible so the test can assert on both without an app/DB context."""
    return Div(*content, Div(title))


@pytest.mark.asyncio
async def test_error_pages_use_translation(_xx_lang, monkeypatch):
    import ui.app as app_mod
    import ui.components.shell as shell_mod
    monkeypatch.setattr(shell_mod, "base_shell", _passthrough_shell)

    resp404 = await app_mod.ui_404_handler(None, Exception("nope"))
    body404 = resp404.body.decode()
    assert resp404.status_code == 404
    for token in ("XX_404_TITLE", "XX_404_BODY", "XX_GO_SETTINGS", "XX_404_BROWSER"):
        assert token in body404, f"{token} missing from 404 body"

    resp500 = await app_mod.ui_500_handler(None, Exception("boom"))
    body500 = resp500.body.decode()
    assert resp500.status_code == 500
    for token in ("XX_500_TITLE", "XX_500_BODY", "XX_BACK_DASH", "XX_500_BROWSER"):
        assert token in body500, f"{token} missing from 500 body"
