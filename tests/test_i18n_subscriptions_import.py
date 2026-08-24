# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the subscriptions CSV import routes.

The subscriptions import routes build every user-facing page title, section
header, expiry error, upsert label, and result-panel entity label by calling
``t()`` at render time, so a request in a non-English language gets translated
output. These tests prove that by registering a sentinel language ``xx`` and
asserting the sentinel text reaches the rendered output while ``xx`` is the
active language. They are red against a tree that hardcodes the English
literals in ``ui/routes/subscriptions_import.py``.
"""

import pytest

from fasthtml.common import to_xml, Div, Span

from ui import i18n
from ui.routes import subscriptions_import as si


# Sentinel catalog: unmistakable values for the keys these routes render.
# ``nav.subscriptions`` flows into ``import_result_panel`` which calls
# ``str.title()`` on it, so the sentinel is chosen to survive ``.title()``.
_XX = {
    "subscriptions_import.hdr_import": "XX_IMPORT_SUBS",
    "subscriptions_import.upsert_label": "XX_UPSERT_LBL",
    "inventory.csv_expired": "XX_CSV_EXPIRED",
    "nav.subscriptions": "Zzsub",
    "import.upsert_hint": "HINT {label}",
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
    monkeypatch.setattr(si, "base_shell", _fake_base_shell)
    monkeypatch.setattr(si, "_token", lambda request: "tok")

    handler = _routes()[("GET", "/subscriptions/import")]
    html = to_xml(await handler(_FormReq({})))

    # page_header(t(...)) renders the sentinel as the header text.
    assert "XX_IMPORT_SUBS" in html
    # page_title(...) composes "<sentinel> - Celerp" for the browser title.
    assert "XX_IMPORT_SUBS - Celerp" in html


@pytest.mark.asyncio
async def test_result_panel_entity_label_translates(monkeypatch):
    async def _fake_create(token, payload):
        return {"id": 1}

    monkeypatch.setattr(si, "_token", lambda request: "tok")
    monkeypatch.setattr(si.api, "create_subscription", _fake_create)

    handler = _routes()[("POST", "/subscriptions/import/confirm")]
    req = _FormReq({"csv_data": "name,frequency,start_date\nMonthly,monthly,2026-01-01\n"})
    html = to_xml(await handler(req))

    # entity_label=t("nav.subscriptions"), title-cased inside the panel.
    assert "Zzsub" in html


@pytest.mark.asyncio
async def test_revalidate_upsert_label_translates(monkeypatch):
    monkeypatch.setattr(si, "_token", lambda request: "tok")

    handler = _routes()[("POST", "/subscriptions/import/revalidate")]
    req = _FormReq({"csv_data": "name,frequency,start_date\nMonthly,monthly,2026-01-01\n"})
    html = to_xml(await handler(req))

    # upsert_label=t("subscriptions_import.upsert_label") flows into the upsert hint.
    assert "XX_UPSERT_LBL" in html
