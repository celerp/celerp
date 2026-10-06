# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Connection and billing messages say what happened and what to do next."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ui.i18n import t

_LOCALES = Path(__file__).resolve().parents[1] / "ui" / "locales"


def test_unsupported_assistant_file_says_which_files_work():
    from celerp.ai.llm import _build_user_content
    with pytest.raises(ValueError) as exc:
        _build_user_content("hi", [{"media_type": "application/zip", "data": ""}])
    assert str(exc.value) == t("error.ai_file_type", "en")
    assert "PDF" in str(exc.value)


def test_missing_company_says_how_to_recover():
    from ui.routes.settings_connectors import _request_company_id
    with pytest.raises(RuntimeError) as exc:
        _request_company_id(SimpleNamespace(state=SimpleNamespace()))
    assert str(exc.value) == t("error.company_unavailable", "en")


def test_restore_refusal_names_who_and_where():
    from celerp_backup import routes
    assert not hasattr(routes, "_ALREADY_SET_UP")
    assert "System Recovery" in t("error.restore_already_set_up", "en")


def test_one_connector_display_name_helper():
    from ui.routes import settings_connectors
    assert not hasattr(settings_connectors, "_service_name")


@pytest.mark.parametrize("key, says", [
    ("connectors.authorize_error", "Xero"),
    ("connectors.connect_check_failed", "Check the store address and keys"),
    ("connectors.store_url_required", "Enter your store's web address."),
    ("connectors.store_url_must_use_https", "keep your store keys safe"),
    ("connectors.missing_credentials", "consumer secret"),
    ("connectors.fetch_error", "Refresh the page."),
    ("connectors.connect_failed", "restart Celerp"),
    ("connectors.deposit_unknown_account", "Choose a bank account from the list."),
    ("stars.claim_unavailable_body", "online service"),
])
def test_connect_messages_say_what_to_do(key, says):
    assert says in t(key, "en", service="Xero", detail="x", code="1000")


def test_no_relay_jargon_in_star_claim():
    for path in _LOCALES.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "relay" not in data["stars.claim_unavailable_body"].lower(), path.name
