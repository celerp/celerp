"""A refusal names permissions and pages in the reader's language, and the period lock refusal
links to the page that moves the lock."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fasthtml.common import to_xml

from celerp.accounting_roles import period_locked_refusal, refusal
from celerp.services.permissions import PERMISSIONS, missing_permission_refusal
from ui.components.shell import refusal_fix, toast_header
from ui.i18n import refusal_text, set_lang, t


@pytest.fixture
def spanish():
    set_lang("es")
    yield
    set_lang("en")


def test_every_permission_is_named_in_the_catalog():
    assert all(t(p.label_key, "en") != p.label_key for p in PERMISSIONS)


def test_a_missing_permission_is_named_through_the_catalog():
    refusal = missing_permission_refusal("manage_accounting")
    assert refusal["params"]["label"]["message_key"] == "perm.name.manage_accounting"
    assert '"Manage accounting"' in refusal["message"]


def test_the_period_lock_refusal_names_its_permission_and_page_through_the_catalog(spanish):
    refusal = period_locked_refusal("2026-10-31")
    assert refusal["params"]["permission"]["message_key"] == "perm.name.manage_accounting"
    assert refusal["params"]["place"]["message_key"] == "page.period_lock"
    text = refusal_text(refusal)
    assert t("page.period_lock", "es") in text and "Finance" not in text
    with_english_name = t("error.period_locked", "es", date="x", permission="Manage accounting",
                          place=t("page.period_lock", "es"))
    assert "Manage accounting" in with_english_name  # the name is a param, not baked into the text


@pytest.mark.parametrize("lang", ["en", "es", "ja", "de", "fr", "pt", "it", "th", "vi", "id", "ar", "am"])
def test_no_catalog_bakes_the_names_into_the_period_lock_text(lang):
    text = json.loads(Path(f"ui/locales/{lang}.json").read_text(encoding="utf-8"))["error.period_locked"]
    assert "{permission}" in text and "{place}" in text and "Manage accounting" not in text and "Finance" not in text


def test_the_period_lock_refusal_toast_links_to_the_lock_page():
    toast = json.loads(toast_header(period_locked_refusal("2026-10-31"), "error")["HX-Trigger"])["celerpToast"]
    assert toast["action"] == {"href": "/settings/accounting?tab=period-lock", "label": t("page.period_lock", "en")}
    assert refusal_fix({"message_key": "error.unexpected_error_body"}) is None


def test_every_period_lock_refusal_links_to_the_lock_page():
    for key in ("error.period_locked", "mfg.period_locked"):
        assert refusal_fix(refusal(key, ""))["href"] == "/settings/accounting?tab=period-lock"


# A catalog string that only looks like an app path, and why it is not one.
_NOT_A_PAGE = {"settings_cloud.per_mo": "a price per month", "settings_cloud.per_year": "a price per year"}
_PATH = re.compile(r'(?:(?<=\s)|^|(?<=["(]))(/[a-z][a-z0-9_-]*(?:/[A-Za-z0-9_{}.-]+)*)')


def _ui_page(path: str) -> bool:
    from starlette.routing import Match

    from ui.app import app
    scope = {"type": "http", "method": "GET", "path": re.sub(r"\{\w+\}", "x", urlsplit(path).path)}
    return any(r.matches(scope)[0] == Match.FULL for r in app.routes)


def test_every_page_a_refusal_or_catalog_text_names_is_a_page():
    """A way out names a page the reader can open: every refusal link, and every app path
    written in the English catalog."""
    from celerp.accounting_roles import REFUSAL_FIXES

    catalog = json.loads(Path("ui/locales/en.json").read_text(encoding="utf-8"))
    named = {f"fix {key}": href for key, (href, _label, _permission) in REFUSAL_FIXES.items()}
    named |= {f"text {key}": path for key, text in catalog.items() if key not in _NOT_A_PAGE
              for path in _PATH.findall(text if isinstance(text, str) else "")}
    assert [f"{where} -> {path}" for where, path in named.items() if not _ui_page(path)] == []
    assert all(_PATH.findall(catalog[key]) for key in _NOT_A_PAGE)  # each exception still reads that way


def test_a_refused_call_on_a_page_links_to_the_page_that_resolves_it():
    from ui.api_client import APIError
    from ui.components.shell import error_flash

    refusal = period_locked_refusal("2026-10-31")
    html = to_xml(error_flash(APIError(409, refusal_text(refusal), refusal)))
    assert 'href="/settings/accounting?tab=period-lock"' in html and t("page.period_lock", "en") in html
    assert "href" not in to_xml(error_flash(APIError(409, "plain", None)))
