# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the documents CSV import routes.

The documents import routes build every user-facing page title, section
header, expired-data error, and import-result label by calling ``t()`` at
render time, so a request in a non-English language gets translated output.
These tests prove that by registering a sentinel language ``xx`` and asserting
the sentinel text reaches the rendered output while ``xx`` is the active
language. They are red against a tree that hardcodes the English literals in
``ui/routes/docs_import.py``.
"""

import pytest

from fasthtml.common import to_xml, Div, Span

from ui import i18n
from ui.routes import docs_import as di


# Sentinel catalog: unmistakable values for the keys these routes render.
# ``docs_import.entity_documents`` flows into ``import_result_panel`` which
# calls ``str.title()`` on it, so its sentinel is chosen to survive ``.title()``.
_XX = {
    "docs_import.import_documents": "XX_IMPORT_DOCS",
    "import.csv_expired": "XX_CSV_EXPIRED",
    "docs_import.upsert_label": "XX_UPSERT",
    "docs_import.entity_documents": "Zzdocs",
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
    di.setup_routes(app)
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
    monkeypatch.setattr(di, "base_shell", _fake_base_shell)
    monkeypatch.setattr(di, "_token", lambda request: "tok")

    handler = _routes()[("GET", "/docs/import")]
    html = to_xml(await handler(_FormReq({})))

    # page_header(t(...)) renders the sentinel as the H1 text.
    assert "XX_IMPORT_DOCS" in html
    # page_title(...) composes "<sentinel> - Celerp" for the browser title.
    assert "XX_IMPORT_DOCS - Celerp" in html


@pytest.mark.asyncio
async def test_expired_csv_error_translates(monkeypatch):
    monkeypatch.setattr(di, "base_shell", _fake_base_shell)
    monkeypatch.setattr(di, "_token", lambda request: "tok")

    # No csv_ref / csv_data in the form -> the "expired" branch renders
    # upload_form(error=t("import.csv_expired")).
    handler = _routes()[("POST", "/docs/import/mapped")]
    html = to_xml(await handler(_FormReq({})))

    assert "XX_CSV_EXPIRED" in html


@pytest.mark.asyncio
async def test_upsert_label_translates(monkeypatch):
    monkeypatch.setattr(di, "_token", lambda request: "tok")

    # Clean rows (required doc_type/doc_number present) -> the confirm panel,
    # whose upsert hint interpolates upsert_label=t("docs_import.upsert_label").
    handler = _routes()[("POST", "/docs/import/revalidate")]
    req = _FormReq({"csv_data": "doc_type,doc_number\ninvoice,INV-1\n"})
    html = to_xml(await handler(req))

    assert "XX_UPSERT" in html


@pytest.mark.asyncio
async def test_result_panel_entity_label_translates(monkeypatch):
    async def _fake_batch(token, path, records, upsert=False):
        return {"created": 1, "skipped": 0, "updated": 0, "errors": []}

    monkeypatch.setattr(di, "_token", lambda request: "tok")
    monkeypatch.setattr(di.api, "batch_import", _fake_batch)

    # entity_label=t("docs_import.entity_documents"), title-cased inside the
    # result panel's "View {label}" button.
    handler = _routes()[("POST", "/docs/import/confirm")]
    req = _FormReq({"csv_data": "doc_type,doc_number\ninvoice,INV-1\n"})
    html = to_xml(await handler(req))

    assert "Zzdocs" in html
