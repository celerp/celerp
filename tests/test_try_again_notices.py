# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""No message tells the user to refresh or reload the page (the desktop app has no visible
reload). A notice that needs one names the Try again action instead, and one shared piece of
the shell turns that name into a button that reloads the page when it is clicked."""

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


def test_the_shell_carries_one_try_again_button_for_every_notice_that_names_it():
    from ui.components.shell import client_scripts, _shell_js_i18n
    labels = _shell_js_i18n("en")["tryAgainLabels"]
    assert sorted(labels) == sorted({_catalog(lang)["btn.try_again"] for lang in LANGS})
    js = "".join(str(s) for s in client_scripts("en"))
    assert js.count("function celerpTryAgain") == 1 and "location.reload()" in js
    assert js.count("try-again") >= 1


def test_the_contact_notice_has_no_second_try_again_implementation():
    import ui.routes.contacts as contacts
    assert not hasattr(contacts, "_reload_contact_link")


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
