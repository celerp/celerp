# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests pinning the connector relay-call boundary: the UI process holds no relay
session (the gateway WebSocket client lives in the API process), so every relay
credential/token operation must go through the API process proxy endpoints."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

UI_DIR = Path(__file__).resolve().parent.parent / "ui"


def test_ui_never_uses_relay_session_headers():
    """relay_session_headers() reads gateway state that is only populated in the
    API process; calling it from UI code sends an empty session token and the
    relay rejects the request. Pin: UI code must proxy via ui.api_client instead."""
    offenders = [
        str(p.relative_to(UI_DIR.parent))
        for p in UI_DIR.rglob("*.py")
        if "relay_session_headers" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


@pytest.mark.asyncio
async def test_connector_sync_kickoff_uses_api_process():
    from ui.routes.settings_connectors import _kickoff_connector_sync
    start = AsyncMock(return_value={"ok": True})
    with patch("ui.api_client.start_connector_sync", start):
        await _kickoff_connector_sync("company-1", "woocommerce", "jwt")
    start.assert_awaited_once_with("jwt", "woocommerce")


@pytest.mark.asyncio
async def test_connectors_tab_shows_trial_cta_when_relay_gates_on_plan():
    """A relay 402 (free account) must render the trial CTA on the connectors
    tab, not the generic could-not-load network message."""
    from ui.routes.settings_connectors import connectors_tab_content

    with patch("ui.routes.settings_connectors._fetch_catalog",
               AsyncMock(return_value=([], "Connectors need an active plan.", True))), \
         patch("ui.routes.settings_connectors._owned_connector_configs",
               AsyncMock(return_value=[])):
        panel = await connectors_tab_content("en", token="tok", category="website", company_id="co-test")

    from fasthtml.common import to_xml
    html = to_xml(panel)
    # The CTA points at the same-origin in-app mint route, which resolves the
    # destination and mints the single-use handoff token server-side at click.
    assert "/commercial/checkout" in html                  # the same-origin mint route
    assert "intent=subscribe" in html
    assert "sku=cloud" in html
    assert 'target="_blank"' in html
    # Fail-closed: the pre-click URL carries no named instance and no celerp.com
    # host - no instance_id leaks and no cross-host handoff is baked in at render.
    assert "instance_id=" not in html
    assert "celerp.com" not in html
    assert "connector-entitlement-cta" in html
    assert "Could not load connectors" not in html


@pytest.mark.asyncio
async def test_connectors_tab_shows_fetch_error_detail():
    """A non-entitlement failure shows the actual error detail (e.g. relay
    unreachable), so it reads as what it is instead of a generic string."""
    from ui.routes.settings_connectors import connectors_tab_content

    with patch("ui.routes.settings_connectors._fetch_catalog",
               AsyncMock(return_value=([], "Relay timed out.", False))):
        panel = await connectors_tab_content("en", token="tok", category="website", company_id="co-test")

    from fasthtml.common import to_xml
    html = to_xml(panel)
    assert "Relay timed out." in html
    assert "connector-entitlement-cta" not in html

@pytest.mark.asyncio
async def test_connectors_tab_renders_single_category():
    """Each Web Access tab shows only its own category's connectors; the tab
    label names the category, so no in-panel section heading renders."""
    from ui.routes.settings_connectors import connectors_tab_content

    catalog = [
        {"id": "shopify", "name": "Shopify", "category": "website"},
        {"id": "xero", "name": "Xero", "category": "accounting"},
    ]
    with patch("ui.routes.settings_connectors._fetch_catalog",
               AsyncMock(return_value=(catalog, None, False))), \
         patch("ui.routes.settings_connectors._get_last_runs",
               AsyncMock(return_value={})), \
         patch("ui.routes.settings_connectors._get_connector_config",
               AsyncMock(return_value=None)), \
         patch("ui.routes.settings_connectors._awaiting_reconnect",
               AsyncMock(return_value=[])):
        panel = await connectors_tab_content("en", token="tok", category="website", company_id="co-test")

    from fasthtml.common import to_xml
    html = to_xml(panel)
    assert "Shopify" in html
    assert "Xero" not in html
    assert "connector-section-title" not in html


@pytest.mark.asyncio
async def test_connectors_tab_empty_category_degrades_honestly():
    """A category with no catalog entries shows a neutral hint, not an error
    and not another category's cards."""
    from ui.routes.settings_connectors import connectors_tab_content

    catalog = [{"id": "shopify", "name": "Shopify", "category": "website"}]
    with patch("ui.routes.settings_connectors._fetch_catalog",
               AsyncMock(return_value=(catalog, None, False))):
        panel = await connectors_tab_content("en", token="tok", category="accounting", company_id="co-test")

    from fasthtml.common import to_xml
    html = to_xml(panel)
    assert "No connectors available here yet." in html
    assert "Shopify" not in html


@pytest.mark.asyncio
async def test_connector_backlinks_target_cloud_tab():
    """Connector detail back-links land on the live Connect settings tabs
    (/settings/cloud?tab={category}); the legacy /settings?tab=connectors target
    no longer exists anywhere in the module."""
    from httpx import ASGITransport, AsyncClient
    from test_helpers import make_test_token

    from ui.app import app as ui_app

    cookies = {"celerp_token": make_test_token(role="owner")}
    async with AsyncClient(
        transport=ASGITransport(app=ui_app),
        base_url="http://ui",
        follow_redirects=False,
    ) as ui_client:
        with patch("ui.routes.settings_connectors._fetch_catalog",
                   AsyncMock(return_value=([], None, False))), \
             patch("ui.routes.settings_connectors._get_connector_config",
                   AsyncMock(return_value={})), \
             patch("ui.routes.settings_connectors._entity_runs",
                   AsyncMock(return_value={})), \
             patch("ui.api_client.get_company",
                   AsyncMock(return_value={"id": "00000000-0000-0000-0000-000000000002", "settings": {}, "current_role": "owner"})), \
             patch("ui.api_client.get_bank_accounts", AsyncMock(return_value={"items": []})), \
             patch("celerp.config.ensure_instance_id", return_value="iid-x"):
            r = await ui_client.get("/settings/connectors/woocommerce", cookies=cookies)
            r_invalid = await ui_client.get("/settings/connectors/not-a-platform",
                                            cookies=cookies)

    assert r.status_code == 200
    html = r.content.decode()
    assert "/settings/cloud?tab=" in html
    assert "/settings?tab=connectors" not in html

    assert r_invalid.status_code == 302
    assert "/settings/cloud?tab=website" in r_invalid.headers.get("location", "")

    src = (UI_DIR / "routes" / "settings_connectors.py").read_text(encoding="utf-8")
    assert "/settings?tab=connectors" not in src


@pytest.mark.asyncio
async def test_disconnect_keeps_local_owner_when_relay_revoke_fails():
    from httpx import ASGITransport, AsyncClient
    from test_helpers import make_test_token
    from ui.app import app as ui_app

    token = make_test_token(role="owner")
    with patch(
        "ui.api_client.get_company",
        AsyncMock(return_value={
            "id": "00000000-0000-0000-0000-000000000002",
            "settings": {},
            "current_role": "owner",
        }),
    ), patch(
        "ui.routes.settings_connectors._get_connector_config",
        AsyncMock(return_value=None),
    ), patch(
        "ui.api_client.delete_connector_credentials",
        AsyncMock(return_value={"ok": False, "error": "relay_error"}),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=ui_app), base_url="http://ui"
        ) as client:
            response = await client.delete(
                "/settings/connectors/woocommerce/disconnect",
                cookies={"celerp_token": token},
            )
    assert response.status_code == 200


def _card_html(catalog: list[dict], config=None, awaiting=None, category="accounting"):
    from fasthtml.common import to_xml
    from ui.routes.settings_connectors import connectors_tab_content

    async def _render():
        with patch("ui.routes.settings_connectors._fetch_catalog",
                   AsyncMock(return_value=(catalog, None, False))), \
             patch("ui.routes.settings_connectors._get_last_runs", AsyncMock(return_value={})), \
             patch("ui.routes.settings_connectors._get_connector_config", AsyncMock(return_value=config)), \
             patch("ui.routes.settings_connectors._awaiting_reconnect", AsyncMock(return_value=awaiting or [])):
            return to_xml(await connectors_tab_content("en", token="tok", category=category, company_id="co-test"))
    return _render()


_OAUTH = {"id": "quickbooks", "name": "QuickBooks", "category": "accounting", "auth_type": "oauth"}


@pytest.mark.asyncio
async def test_pending_oauth_claim_renders_finish_or_cancel():
    """A connection whose authorization never finished is shown as in progress
    with Finish and Cancel, never as connected and never as a bare Connect."""
    from types import SimpleNamespace
    cfg = SimpleNamespace(sync_frequency="manual", direction="both", connector="quickbooks")
    html = await _card_html([{**_OAUTH, "connected": False, "ownership": "owned", "local_claim": True}], config=cfg)
    assert "Connection in progress" in html
    assert "authorization was started but not finished" in html
    assert ">Finish<" in html or "Finish</button>" in html
    assert 'hx-delete="/settings/connectors/quickbooks/disconnect"' in html
    assert "Cancel" in html
    assert "hx-confirm" not in html.split("Cancel")[0].rsplit("<button", 1)[-1]


@pytest.mark.asyncio
async def test_connector_linked_twice_shows_banner_and_disconnect():
    """A connector linked to more than one company is shown, never resolved on
    its own: a linked company can disconnect it; any other sees the explanation
    and no action it cannot perform."""
    own = await _card_html([{**_OAUTH, "connected": True, "ownership": "ambiguous", "local_claim": True}])
    assert "This connector is linked to more than one company" in own
    assert 'hx-delete="/settings/connectors/quickbooks/disconnect"' in own
    assert "Connection in progress" not in own
    assert "oauth-redirect" not in own

    none = await _card_html([{**_OAUTH, "connected": False, "ownership": "ambiguous", "local_claim": False}])
    assert "This connector is linked to more than one company" in none
    assert "hx-delete" not in none
    assert "oauth-redirect" not in none


@pytest.mark.asyncio
async def test_connector_owned_elsewhere_is_labelled():
    html = await _card_html([{**_OAUTH, "connected": False, "ownership": "other", "local_claim": False}])
    assert "Connected to another company." in html
    assert "oauth-redirect" in html


@pytest.mark.asyncio
async def test_reconnect_banner_after_deactivation_names_lost_connectors():
    catalog = [
        {"id": "shopify", "name": "Shopify", "category": "website", "auth_type": "oauth"},
        {"id": "woocommerce", "name": "WooCommerce", "category": "website", "auth_type": "api_key"},
    ]
    html = await _card_html(catalog, awaiting=["shopify", "woocommerce"], category="website")
    assert "Connectors were disconnected when this company was deactivated: Shopify, WooCommerce." in html
    quiet = await _card_html(catalog, awaiting=[], category="website")
    assert "disconnected when this company was deactivated" not in quiet


def test_woocommerce_detail_offers_deposit_account_choice():
    from fasthtml.common import to_xml
    from ui.routes.settings_connectors import _connector_detail_body

    c = {"id": "woocommerce", "name": "WooCommerce", "category": "website", "auth_type": "api_key", "connected": True}
    deposit = {"current": "1200", "bank_accounts": [
        {"chart_account_code": "1200", "bank_name": "Main"},
        {"chart_account_code": "1210", "bank_name": "Savings"},
    ]}
    html = to_xml(_connector_detail_body(c, {}, None, "en", deposit=deposit))
    assert "Deposit payments to" in html
    assert "Online payments default" in html
    assert "1200 - Main" in html and "1210 - Savings" in html
    assert 'hx-post="/settings/connectors/woocommerce/deposit-account"' in html
    assert 'id="connector-deposit-woocommerce"' in html
    assert "Deposit payments to" not in to_xml(_connector_detail_body(c, {}, None, "en"))


@pytest.fixture
def deposit_ui():
    from httpx import ASGITransport, AsyncClient
    from test_helpers import make_test_token
    from ui.app import app as ui_app

    patch_company = AsyncMock(return_value={"ok": True})
    with patch("ui.api_client.get_company", AsyncMock(return_value={
                "id": "00000000-0000-0000-0000-000000000002", "settings": {}, "current_role": "owner",
            })), \
         patch("ui.api_client.get_bank_accounts", AsyncMock(return_value={"items": [
                {"chart_account_code": "1200", "bank_name": "Main"}]})), \
         patch("ui.api_client.patch_company", patch_company):
        yield AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui"), \
            {"celerp_token": make_test_token(role="owner")}, patch_company


@pytest.mark.asyncio
async def test_deposit_account_route_saves_a_known_account(deposit_ui):
    client, cookies, patch_company = deposit_ui
    async with client:
        r = await client.post("/settings/connectors/woocommerce/deposit-account",
                              data={"woocommerce_deposit_account": "1200"}, cookies=cookies)
    assert r.status_code == 200
    patch_company.assert_awaited_once_with(cookies["celerp_token"], {"woocommerce_deposit_account": "1200"})
    assert "flash--success" in r.text and 'id="connector-deposit-woocommerce"' in r.text


@pytest.mark.asyncio
async def test_deposit_account_route_rejects_an_unknown_account(deposit_ui):
    client, cookies, patch_company = deposit_ui
    async with client:
        r = await client.post("/settings/connectors/woocommerce/deposit-account",
                              data={"woocommerce_deposit_account": "9999"}, cookies=cookies)
    assert r.status_code == 200
    assert "9999 is not an active bank account." in r.text
    patch_company.assert_not_awaited()


@pytest.mark.asyncio
async def test_deposit_account_route_add_new_redirects_to_bank_accounts(deposit_ui):
    client, cookies, patch_company = deposit_ui
    async with client:
        r = await client.post("/settings/connectors/woocommerce/deposit-account",
                              data={"woocommerce_deposit_account": "__new__"}, cookies=cookies)
    assert r.status_code == 204
    assert r.headers["HX-Redirect"] == "/settings/accounting/bank-accounts/new"
    patch_company.assert_not_awaited()


@pytest.mark.asyncio
async def test_deposit_account_route_is_woocommerce_only(deposit_ui):
    client, cookies, patch_company = deposit_ui
    async with client:
        r = await client.post("/settings/connectors/shopify/deposit-account",
                              data={"woocommerce_deposit_account": "1200"}, cookies=cookies)
    assert r.status_code == 200
    patch_company.assert_not_awaited()
    assert "flash--success" not in r.text


@pytest.fixture
def reconcile_ui():
    from httpx import ASGITransport, AsyncClient
    from test_helpers import make_test_token
    from ui.app import app as ui_app

    setter = AsyncMock(return_value={"entry": {
        "id": "7", "label": "Order 7", "reason": "has a refund",
        "signature": "sig-7", "reconciled": True,
    }})
    with patch("ui.api_client.get_company", AsyncMock(return_value={
                "id": "00000000-0000-0000-0000-000000000002", "settings": {}, "current_role": "owner",
            })), \
         patch("ui.api_client.set_woocommerce_order_reconciled", setter):
        yield AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui"), \
            {"celerp_token": make_test_token(role="owner")}, setter


@pytest.mark.asyncio
async def test_mark_reconciled_route_sends_the_reviewed_signature(reconcile_ui):
    client, cookies, setter = reconcile_ui
    async with client:
        r = await client.post("/settings/connectors/woocommerce/attention/7/reconciled",
                              data={"signature": "sig-7"}, cookies=cookies)
    assert r.status_code == 200
    setter.assert_awaited_once_with(cookies["celerp_token"], "7", "sig-7")
    assert 'id="connector-attention-woocommerce-7"' in r.text
    assert 'hx-delete="/settings/connectors/woocommerce/attention/7/reconciled"' in r.text


@pytest.mark.asyncio
async def test_undo_reconciled_route_withdraws_the_mark(reconcile_ui):
    client, cookies, setter = reconcile_ui
    setter.return_value = {"entry": {
        "id": "7", "label": "Order 7", "reason": "has a refund", "signature": "sig-7",
        "reconciled": False,
    }}
    async with client:
        r = await client.delete("/settings/connectors/woocommerce/attention/7/reconciled",
                                cookies=cookies)
    assert r.status_code == 200
    setter.assert_awaited_once_with(cookies["celerp_token"], "7", None)
    assert 'hx-post="/settings/connectors/woocommerce/attention/7/reconciled"' in r.text


@pytest.mark.asyncio
async def test_mark_reconciled_route_explains_a_refusal(reconcile_ui):
    from ui.api_client import APIError

    client, cookies, setter = reconcile_ui
    setter.side_effect = APIError(409, "This order changed in WooCommerce; refresh to review the change")
    entry = {"id": "7", "label": "Order 7", "reason": "has a refund", "signature": "sig-8"}
    with patch("ui.routes.settings_connectors.attention_entries", AsyncMock(return_value=[entry])):
        async with client:
            r = await client.post("/settings/connectors/woocommerce/attention/7/reconciled",
                                  data={"signature": "sig-7"}, cookies=cookies)
    assert r.status_code == 200
    assert "This order changed in WooCommerce" in r.text
    assert "sig-8" in r.text


def test_attention_list_offers_mark_and_undo():
    from fasthtml.common import to_xml
    from ui.routes.settings_connectors import _connector_status_view, _open_attention_count

    entries = [
        {"id": "1", "label": "Order 1", "reason": "has a refund", "signature": "sig-1"},
        {"id": "2", "label": "Order 2", "reason": "is refunded", "signature": "sig-2", "reconciled": True},
        {"id": "3", "label": "Order 3", "reason": "Unknown product"},
    ]
    html = to_xml(_connector_status_view("woocommerce", {}, "en", attention=entries))
    assert 'hx-post="/settings/connectors/woocommerce/attention/1/reconciled"' in html
    assert "sig-1" in html
    assert 'hx-delete="/settings/connectors/woocommerce/attention/2/reconciled"' in html
    assert "Marked reconciled" in html
    assert "attention/3/reconciled" not in html
    assert _open_attention_count(entries) == 2


@pytest.mark.asyncio
async def test_attention_read_failure_shows_no_list():
    from ui.routes import settings_connectors as sc

    with patch.object(sc, "attention_entries", AsyncMock(side_effect=RuntimeError("db down"))):
        assert await sc._attention("00000000-0000-0000-0000-000000000002", "woocommerce") == []


@pytest.mark.asyncio
async def test_mark_reconciled_route_is_woocommerce_only(reconcile_ui):
    client, cookies, setter = reconcile_ui
    async with client:
        r = await client.post("/settings/connectors/shopify/attention/7/reconciled",
                              data={"signature": "sig-7"}, cookies=cookies)
    assert r.status_code == 200
    assert "flash--warning" in r.text
    setter.assert_not_awaited()
