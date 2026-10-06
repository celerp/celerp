# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Messages that tell the user to disconnect, reconnect or sign in again say where:
Web Access (the sidebar item), or the button right below or beside the message."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ui.i18n import t

_LOCALES = Path(__file__).resolve().parents[1] / "ui" / "locales"
_WEB_ACCESS = {lang: json.loads((_LOCALES / f"{lang}.json").read_text(encoding="utf-8"))["settings_cloud.web_access"]
               for lang in ("en", "th")}

# Every message that sends the user to Web Access names it, in English and Thai.
_NAMES_WEB_ACCESS = [
    "error.connect_account_first", "error.relay_sign_in_failed", "error.billing_portal_failed",
    "error.connector_not_here", "error.relay_signed_out", "error.connector_multi_company",
    "error.connector_company_invalid", "error.connector_other_company", "error.connector_legacy_conflict",
    "error.connector_not_connected", "error.woocommerce_credentials", "error.connector_sync_error",
    "error.cloud_backup_disconnected", "error.cloud_backup_no_account", "error.ai_signed_out",
    "doc.send_relay_error_notice", "doc.send_relay_error_link", "ai.service_unavailable", "pay.settings_revoked",
    "connectors.ambiguous", "connectors.unassigned", "company_backup.integrations_disconnected",
]


@pytest.mark.parametrize("key", _NAMES_WEB_ACCESS)
@pytest.mark.parametrize("lang", ["en", "th"])
def test_message_names_web_access(key, lang):
    assert _WEB_ACCESS[lang] in t(key, lang, service="Shopify", detail="x")


def test_connection_failed_names_the_buttons_below_it():
    """Shown next to its own buttons: it names them and says they are below."""
    assert "Connect automatically below" in t("settings.connection_failed", "en")
    assert "Disconnect" in t("settings.connection_failed", "en")


def test_unused_reconnect_token_key_is_gone():
    for path in _LOCALES.glob("*.json"):
        assert "settings.reconnect_token_missing" not in json.loads(path.read_text(encoding="utf-8")), path.name


def test_linked_twice_names_the_connector_and_where():
    from celerp.connectors.ownership import ConnectorOwnershipAmbiguousError, _resolve_connector_owner
    rows = [SimpleNamespace(company_id="a", connector="shopify"), SimpleNamespace(company_id="b", connector="shopify")]
    with patch("celerp.connectors.ownership.ensure_instance_id", return_value="legacy"):
        with pytest.raises(ConnectorOwnershipAmbiguousError) as exc:
            _resolve_connector_owner(rows, "a")
    assert str(exc.value) == t("error.connector_multi_company", "en", service="Shopify")


def test_other_company_names_the_connector():
    from celerp.connectors.ownership import ConnectorOwnershipError, _resolve_connector_owner
    rows = [SimpleNamespace(company_id="b", connector="xero")]
    with patch("celerp.connectors.ownership.ensure_instance_id", return_value="legacy"):
        with pytest.raises(ConnectorOwnershipError) as exc:
            _resolve_connector_owner(rows, "a")
    assert str(exc.value) == t("error.connector_not_here", "en", service="Xero")


@pytest.mark.asyncio
async def test_unconfirmed_disconnect_names_the_connector(monkeypatch):
    from celerp.connectors import remote_state
    monkeypatch.setattr("celerp.gateway.state.relay_http_url", lambda: "http://127.0.0.1:9")
    with pytest.raises(remote_state.ConnectorRemoteCleanupError) as exc:
        await remote_state.revoke_connector_remote_state("c1", "woocommerce")
    assert str(exc.value) == t("error.connector_disconnect_unconfirmed", "en", service="WooCommerce")


def test_ai_signed_out_says_where():
    from celerp.ai.service import RelayError, _user_error
    assert _user_error(RelayError("no_session", "no session")) == t("error.ai_signed_out", "en")

