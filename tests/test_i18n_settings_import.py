# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the settings import routes.

The location/tax/payment-term import routes build every user-facing page
title, section header, and result-panel label by calling ``t()`` at render
time, so a request in a non-English language gets translated output. These
tests prove that by registering a sentinel language ``xx`` and asserting the
sentinel text reaches the rendered output while ``xx`` is the active language.
They are red against a tree that hardcodes the English literals in
``ui/routes/settings_import.py``.
"""

import pytest

from fasthtml.common import to_xml, Div, Span

from ui import i18n
from ui.routes import settings_import as si


# Sentinel catalog: unmistakable values for the keys these routes render.
# ``settings.tab_locations`` flows into ``import_result_panel`` which calls
# ``str.title()`` on it, so the sentinel is chosen to survive ``.title()``.
_XX = {
    "settings_import.hdr_locations": "XX_HDR_LOC",
    "settings_import.records_failed": "XX_FAILED {n}",
    "settings.tab_locations": "Zzloc",
}


@pytest.fixture(autouse=True)
def _xx_lang():
    i18n.clear_registry()
    i18n._cached_load.cache_clear()
    i18n.register_catalog("xx", _XX)
    i18n.set_lang("xx")
    yield
    i18n.set_lang("en")
    i18n.clear_registry()
    i18n._cached_load.cache_clear()


class _CaptureApp:
    """Minimal stand-in for the FastHTML app: records the route handlers that
    ``setup_routes`` registers so a test can invoke one directly."""

    def __init__(self):
        self.handlers: dict = {}

    def get(self, path):
        def deco(fn):
            self.handlers[("GET", path)] = fn
            return fn
        return deco

    def post(self, path):
        def deco(fn):
            self.handlers[("POST", path)] = fn
            return fn
        return deco


def _routes():
    app = _CaptureApp()
    si.setup_routes(app)
    return app.handlers


class _FormReq:
    """Fake request exposing a cookies dict and an async ``form()``."""

    def __init__(self, form: dict):
        self._form = form
        self.cookies: dict = {}

    async def form(self):
        return self._form


async def _fake_base_shell(*content, title="", **kwargs):
    return Div(Span(title), Div(*content))


@pytest.mark.asyncio
async def test_import_page_header_and_title_translate(monkeypatch):
    async def _perm(request, key):
        return None

    monkeypatch.setattr(si, "base_shell", _fake_base_shell)
    monkeypatch.setattr(si, "_token", lambda request: "tok")
    monkeypatch.setattr(si, "_check_permission", _perm)

    handler = _routes()[("GET", "/settings/import/locations")]
    html = to_xml(await handler(_FormReq({})))

    # page_header(t(...)) renders the sentinel as the H1 text.
    assert "XX_HDR_LOC" in html
    # page_title(...) composes "<sentinel> - Celerp" for the browser title.
    assert "XX_HDR_LOC - Celerp" in html


@pytest.mark.asyncio
async def test_import_result_panel_labels_translate(monkeypatch):
    async def _fake_batch(token, path, records):
        return {"created": 1, "skipped": 0, "failed": 2}

    monkeypatch.setattr(si, "_token", lambda request: "tok")
    monkeypatch.setattr(si.api, "batch_import", _fake_batch)

    handler = _routes()[("POST", "/settings/import/locations/confirm")]
    req = _FormReq({"csv_data": "name,type\nMain,store\n"})
    html = to_xml(await handler(req))

    # errors=[t("settings_import.records_failed", n=failed)] with {n} interpolation.
    assert "XX_FAILED 2" in html
    # entity_label=t("settings.tab_locations"), title-cased inside the panel.
    assert "Zzloc" in html
