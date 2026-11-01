"""A refusal names permissions and pages in the reader's language, and the period lock refusal
links to the page that moves the lock."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from celerp.accounting_roles import period_locked_refusal
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
