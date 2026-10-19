# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""No message tells the user to refresh or reload the page (the desktop app has no visible
reload). A notice that needs one says "Select Try again" instead, and only those messages get
the Try again control, which opens the page again with GET when it is clicked."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from test_ui import ui_client  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parent.parent
LOCALE_DIRS = [ROOT / "ui" / "locales", ROOT / "default_modules/celerp-fixed-assets/celerp_fixed_assets/locales"]
LANGS = ["am", "ar", "de", "en", "es", "fr", "id", "it", "ja", "pt", "th", "vi"]

# Sentences that talk about the app restarting or a shell command, not an instruction to reload.
_NOT_AN_INSTRUCTION = (
    "The app will reload.",
    "this page will reload when it is back",
    "This page reloads when the server is back",
    "uvicorn celerp.main:app --reload",
    "Celerp couldn't refresh it",
    "the list did not refresh",
)
_INSTRUCTION = re.compile(r"\b(?:refresh|reload)\b", re.I)

# What the same instruction reads like in each other language (a page reload, never a restart).
_OTHER_LANGUAGE_RELOAD = {
    "am": r"ገጹን ያድሱ|ገጹን እንደገና ይጫኑ",
    "ar": r"حدّث (?:الصفحة|القائمة|الشاشة)|أعد تحميل",
    "de": r"Seite (?:neu laden|aktualisieren)|(?:Laden Sie|Aktualisieren Sie) (?:die )?(?:Seite|Liste|Ansicht)|neu laden",
    "es": r"(?:Actualice|Recargue) (?:la|esta) (?:página|pantalla|lista)|[Rr]ecargue",
    "fr": r"Actualisez|Rechargez",
    "id": r"[Mm]uat ulang (?:halaman|daftar)|[Ss]egarkan",
    "it": r"(?:Aggiorna|Ricarica) (?:la|questa) (?:pagina|schermata|lista)",
    "ja": r"再読み込み|ページを更新|一覧を更新|画面を更新",
    "pt": r"(?:Atualize|Recarregue) (?:a|esta) (?:página|tela|lista)|[Rr]ecarregue",
    "th": r"รีเฟรช|โหลดหน้า(?:นี้)?ใหม่",
    "vi": r"[Tt]ải lại (?:trang|danh sách)|[Ll]àm mới",
}
# Catalog keys whose text is about a restart or a list that did not refresh, kept as they are.
_KEPT_KEYS = {
    "modules.restart_confirm", "modules.restarting", "settings.restarting_automatically",
    "msg.restart_api", "marketplace.from_cache", "documents.scan_reload_needed",
}


def _catalog(lang: str) -> dict:
    merged: dict = {}
    for d in LOCALE_DIRS:
        path = d / f"{lang}.json"
        if path.exists():
            merged.update(json.loads(path.read_text(encoding="utf-8")))
    return merged


def _sans_kept_phrases(text: str) -> str:
    for phrase in _NOT_AN_INSTRUCTION:
        text = text.replace(phrase, "")
    return text


def test_no_english_message_tells_the_user_to_refresh_or_reload():
    offenders = {k: v for k, v in _catalog("en").items() if _INSTRUCTION.search(_sans_kept_phrases(v))}
    assert offenders == {}


@pytest.mark.parametrize("lang", [lang for lang in LANGS if lang != "en"])
def test_no_translation_tells_the_user_to_reload_the_page(lang):
    pattern = re.compile(_OTHER_LANGUAGE_RELOAD[lang])
    offenders = [k for k, v in _catalog(lang).items()
                 if k not in _KEPT_KEYS and pattern.search(v)]
    assert offenders == []


@pytest.mark.parametrize("lang", LANGS)
def test_a_message_that_names_try_again_names_it_the_way_the_catalog_labels_the_button(lang):
    en, cat = _catalog("en"), _catalog(lang)
    label = cat["btn.try_again"]
    wrong = [k for k, v in en.items()
             if re.search(r"[Ss]elect Try again", v) and label not in cat.get(k, "")]
    assert wrong == []


def test_the_reload_only_button_label_is_gone_because_try_again_is_the_one_action():
    assert "documents.save_reload_action" not in _catalog("en")


_SENTENCE_START = re.compile(r"(?:^|[.;,:] |\band )(?:[Rr]efresh|[Rr]eload)\b(?=\s+\w)(?! token)")


def _python_sources():
    for root in ("celerp", "ui", "default_modules"):
        for path in (ROOT / root).rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if "/tests/" in rel or "/migrations/" in rel or "/fixtures/" in rel:
                continue
            yield rel, path


def test_no_message_in_code_tells_the_user_to_refresh_or_reload():
    found = []
    for rel, path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docs = {id(n.body[0].value) for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and n.body and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant)}
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Constant) and isinstance(n.value, str)) or id(n) in docs:
                continue
            for line in n.value.splitlines():
                if "//" not in line and "#" not in line and "*/" not in line and _SENTENCE_START.search(line):
                    found.append(f"{rel}:{n.lineno}: {line.strip()[:90]}")
    assert found == []


def _literal_text(node) -> str | None:
    """A string literal as written, an f-string with each placeholder as ``{}``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
    return None


def test_every_select_try_again_message_in_code_is_a_catalog_message():
    """A message that tells the user to select Try again only gets the control (and a translation)
    when it is the English of a catalog key, so no API refusal may carry the sentence as plain text."""
    catalogued = {re.sub(r"\{\w+\}", "{}", v) for k, v in _catalog("en").items()
                  if re.search(r"[Ss]elect Try again", v)}
    plain = []
    for rel, path in _python_sources():
        if rel.startswith("ui/"):
            continue
        nodes = list(ast.walk(ast.parse(path.read_text(encoding="utf-8"))))
        parts = {id(v) for n in nodes if isinstance(n, ast.JoinedStr) for v in n.values}
        for n in nodes:
            text = None if id(n) in parts else _literal_text(n)
            if text and re.search(r"[Ss]elect Try again", text) and text not in catalogued:
                plain.append(f"{rel}:{n.lineno}: {text[:90]}")
    assert not plain, "\n".join(plain)


@pytest.mark.parametrize("lang", ["en", "th"])
def test_a_refusal_the_api_sends_with_a_key_reaches_the_reader_with_the_try_again_control(lang, module_catalogs):
    """The WooCommerce order-changed refusal and a fixed assets one, rendered the way the page shows them."""
    from fastapi import HTTPException
    from celerp_connectors.routes import _order_changed
    from celerp_fixed_assets.operations import _same_intent
    from ui.i18n import _current_lang, refusal_text

    with pytest.raises(HTTPException) as err:
        _same_intent({"intent_hash": "a"}, "b")
    token = _current_lang.set(lang)
    try:
        shown = [refusal_text(d) for d in (_order_changed(), err.value.detail)]
    finally:
        _current_lang.reset(token)
    assert all(shown) and all(_says_try_again(lang, text) for text in shown)
    assert (shown[0] == _order_changed()["message"]) == (lang == "en")


# Messages that ask the user to do the action again (or that sit beside a Retry control of
# their own): they never get the Try again control, in any language.
_RETRY_THE_ACTION = (
    "shell.request_failed", "api.unreachable", "error.backup_running", "auth.sign_ins_paused_one",
    "settings.reset_not_done", "connectors.connect_failed", "inventory.err_merge_stores_unchecked",
    "acct.err_je_zero", "acct.err_void_none_selected", "acct.err_writeoff_too_large",
    "contacts.err_none_selected", "modules.unavailable_retry", "msg.files_load_failed",
    "error.company_settings_unavailable", "older_stock.failed", "btn.try_again", "btn.retry",
)


def _filled(template: str) -> str:
    return re.sub(r"\{\w+\}", "X", template)


@pytest.fixture
def module_catalogs():
    """The module catalogs a running app registers, so module messages are in the pattern too."""
    from ui.i18n import register_catalog
    for d in LOCALE_DIRS[1:]:
        for path in d.glob("*.json"):
            register_catalog(path.stem, json.loads(path.read_text(encoding="utf-8")))


def _says_try_again(lang: str, text: str) -> bool:
    from ui.i18n import try_again_pattern
    return re.search(try_again_pattern(lang), text) is not None


@pytest.mark.parametrize("lang", LANGS)
def test_every_message_that_says_select_try_again_is_recognised_in_its_own_language(lang, module_catalogs):
    from ui.i18n import t
    en = _catalog("en")
    keys = [k for k, v in en.items() if re.search(r"[Ss]elect Try again", v)]
    assert len(keys) > 100
    missed = [k for k in keys if not _says_try_again(lang, _filled(t(k, lang)))]
    assert missed == []


def test_a_long_notice_is_checked_in_time_proportional_to_its_length(module_catalogs):
    """Every message with its Try again sentence cut, run together: words the pattern starts on
    again and again, so a pattern that backtracks over the whole notice would hang the page."""
    import time
    from ui.i18n import t
    en = _catalog("en")
    cut = " ".join(re.sub(r"[Ss]elect Try again", "", _filled(t(k, "en")))
                   for k in en if re.search(r"[Ss]elect Try again", en[k]))
    notice = cut * 4
    started = time.perf_counter()
    assert not _says_try_again("en", notice)
    assert time.perf_counter() - started < 1


@pytest.mark.parametrize("lang", ["en", "th", "ja", "de"])
def test_a_message_that_means_do_it_again_gets_no_try_again_control(lang, module_catalogs):
    from ui.i18n import t
    en = _catalog("en")
    assert [k for k in _RETRY_THE_ACTION if k not in en] == []
    matched = [k for k in _RETRY_THE_ACTION if _says_try_again(lang, _filled(t(k, lang)))]
    assert matched == []


def _request(method: str, path: str, query: str = ""):
    from starlette.requests import Request
    from ui.app import app
    return Request({"type": "http", "method": method, "path": path, "root_path": "",
                    "query_string": query.encode(), "headers": [], "app": app})


# A page that came from GET has no address of its own: the button opens the one the browser shows.
@pytest.mark.parametrize("method, path, query, address", [
    ("GET", "/inventory", "q=ring", ""),
    ("GET", "/contacts/c1", "", ""),
    ("POST", "/inventory/import/preview", "", "/inventory/import"),
    ("POST", "/inventory/import/mapped", "", "/inventory/import"),
    ("POST", "/docs/import/mapped", "", "/docs/import"),
    ("POST", "/setup/import-backup", "", "/setup/import-backup"),
])
def test_try_again_opens_the_page_with_get_never_sending_a_form_again(method, path, query, address):
    from ui.components.shell import try_again_address
    assert try_again_address(_request(method, path, query)) == address


def _shell_config(html: str) -> dict:
    return json.loads(re.search(r"window\.__shellI18n = (\{.*?\});</script>", html, re.S).group(1))


@pytest.fixture
def import_backup_page(ui_client):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, patch
    import httpx

    @asynccontextmanager
    async def timed_out(*_a, **_k):
        raise httpx.TimeoutException("slow")
        yield

    async def open_(method, lang="en"):
        cookies = {"celerp_lang": lang}
        with patch("ui.routes.auth.bootstrap_status", new=AsyncMock(return_value=False)), \
             patch("ui.api_client.setup_code_required", new=AsyncMock(return_value=False)), \
             patch("ui.api_client._local_client", new=timed_out):
            if method == "GET":
                return await ui_client.get("/setup/import-backup", cookies=cookies)
            return await ui_client.post("/setup/import-backup", cookies=cookies,
                                        files={"backup_file": ("b.celerp-backup", b"x", "application/octet-stream")})
    return open_


@pytest.fixture
def import_backup_page_pattern(ui_client):
    async def fetch(src):
        r = await ui_client.get(src)
        assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
        return json.loads(r.text.split(" = ", 1)[1].rstrip(";"))
    return fetch


@pytest.mark.asyncio
@pytest.mark.parametrize("lang", ["en", "th"])
async def test_the_restore_page_that_says_select_try_again_carries_it_and_it_opens_with_get(
        import_backup_page, import_backup_page_pattern, lang):
    from ui.i18n import t
    r = await import_backup_page("POST", lang)
    assert t("auth.import_timed_out", lang) in r.text
    config = _shell_config(r.text)
    assert config["tryAgainHref"] == "/setup/import-backup"
    assert config["tryAgain"] == t("btn.try_again", lang)
    src = re.search(r'<script src="(/static/try-again/[^"]+)"', r.text).group(1)
    assert f"/{lang}.js?v=" in src
    pattern = await import_backup_page_pattern(src)
    assert re.search(pattern, t("auth.import_timed_out", lang))


@pytest.mark.asyncio
async def test_a_try_again_address_from_before_a_restart_is_served_but_never_kept(ui_client):
    """A page drawn before the messages changed asks for the old version: it gets the current
    pattern, never cached for good under the old address."""
    r = await ui_client.get("/static/try-again/en.js?v=000000000000")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_repricing_without_a_version_says_so_in_the_readers_language(ui_client):
    from unittest.mock import AsyncMock, patch
    from test_ui import _authed
    from ui.i18n import t
    with patch("ui.api_client.get_doc", new=AsyncMock(return_value={"doc_type": "invoice"})), \
         patch("ui.api_client.get_price_lists", new=AsyncMock(return_value=[{"name": "Wholesale"}])), \
         patch("ui.api_client.patch_doc", new=AsyncMock(return_value={})), \
         patch("ui.api_client.reprice_doc", new=AsyncMock(return_value={})):
        r = await ui_client.patch("/docs/d1/field/price_list", data={"value": "Wholesale"},
                                  cookies={**_authed(), "celerp_lang": "th"})
    toast = json.loads(r.headers["HX-Trigger"])["celerpToast"]
    assert toast["message"] == t("documents.reprice_version_unknown", "th")
    assert toast["message"] != "documents.reprice_version_unknown"


def test_the_japanese_close_label_says_close():
    assert _catalog("ja")["btn.close"] == "閉じる"


@pytest.mark.asyncio
async def test_a_failed_contact_read_says_to_select_try_again_and_never_to_reload(ui_client_for_notices):
    r = await ui_client_for_notices("/contacts/c1")
    assert "Select Try again." in r.text
    assert "reload" not in r.text.split('class="flash flash--error"', 1)[1].split("</div>", 1)[0].lower()


@pytest.fixture
def ui_client_for_notices(ui_client):
    from unittest.mock import AsyncMock, patch
    from ui.api_client import APIError
    from test_ui import _authed

    async def get(path):
        with patch("ui.api_client.list_contact_docs", new=AsyncMock(return_value={"items": []})), \
             patch("ui.api_client.get_contact", new=AsyncMock(side_effect=APIError(503, "down"))):
            return await ui_client.get(path, cookies=_authed())
    return get
