# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the accounting CSV import routes.

The chart-of-accounts import routes build every user-facing page title, section
header, upload hint, expired-data error, and import-result label by calling
``t()`` at render time, so a request in a non-English language gets translated
output. These tests prove that by registering a sentinel language ``xx`` and
asserting the sentinel text reaches the rendered output while ``xx`` is the
active language. They are red against a tree that hardcodes the English literals
in ``ui/routes/accounting_import.py``.
"""

import pytest

from fasthtml.common import to_xml, Div, Span

from ui import i18n
from ui.routes import accounting_import as ai


# Sentinel catalog: unmistakable values for the keys these routes render.
# ``accounting_import.entity_accounts`` flows into ``import_result_panel`` which
# calls ``str.title()`` on it, so its sentinel is chosen to survive ``.title()``.
_XX = {
    "accounting_import.header_chart": "XX_HEADER_CHART",
    "accounting_import.title_chart": "XX_TITLE_CHART",
    "accounting_import.chart_hint": "XX_CHART_HINT",
    "accounting_import.entity_accounts": "Zzaccounts",
    "import.csv_expired": "XX_CSV_EXPIRED",
    "settings_import.records_failed": "XX_RECORDS_FAILED {n}",
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
    ai.setup_routes(app)
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
    monkeypatch.setattr(ai, "base_shell", _fake_base_shell)
    monkeypatch.setattr(ai, "_token", lambda request: "tok")

    handler = _routes()[("GET", "/accounting/import/chart")]
    html = to_xml(await handler(_FormReq({})))

    # page_header(t(...)) renders the sentinel as the H1 text.
    assert "XX_HEADER_CHART" in html
    # page_title(...) composes "<sentinel> - Celerp" for the browser title.
    assert "XX_TITLE_CHART - Celerp" in html


@pytest.mark.asyncio
async def test_expired_csv_error_translates(monkeypatch):
    monkeypatch.setattr(ai, "base_shell", _fake_base_shell)
    monkeypatch.setattr(ai, "_token", lambda request: "tok")

    # No csv_ref / csv_data in the form -> the "expired" branch renders
    # upload_form(error=t("import.csv_expired")).
    handler = _routes()[("POST", "/accounting/import/chart/mapped")]
    html = to_xml(await handler(_FormReq({})))

    assert "XX_CSV_EXPIRED" in html


@pytest.mark.asyncio
async def test_result_panel_entity_label_and_errors_translate(monkeypatch):
    async def _fake_batch(token, path, records):
        return {"created": 1, "skipped": 0, "failed": 2}

    monkeypatch.setattr(ai, "_token", lambda request: "tok")
    monkeypatch.setattr(ai.api, "batch_import", _fake_batch)

    # entity_label=t("accounting_import.entity_accounts"), title-cased inside the
    # result panel's "View {label}" button; the failed count flows through
    # t("settings_import.records_failed", n=failed).
    handler = _routes()[("POST", "/accounting/import/chart/confirm")]
    req = _FormReq({"csv_data": "code,name,account_type\n1000,Assets,asset\n"})
    html = to_xml(await handler(req))

    assert "Zzaccounts" in html
    assert "XX_RECORDS_FAILED 2" in html
