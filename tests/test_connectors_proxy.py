# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests pinning the connector relay-call boundary: the UI process holds no relay
session (the gateway WebSocket client lives in the API process), so every relay
credential/token operation must go through the API process proxy endpoints."""
from __future__ import annotations

import uuid
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
    assert "Celerp couldn't load your connections" not in html


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
        ui_client.cookies.update(cookies)
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
            r = await ui_client.get("/settings/connectors/woocommerce")
            r_invalid = await ui_client.get("/settings/connectors/not-a-platform")

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
            client.cookies.update({"celerp_token": token})
            response = await client.delete(
                "/settings/connectors/woocommerce/disconnect",
            )
    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect", ["", "&redirect=1"])
async def test_failed_disconnect_offers_disconnect_anyway_with_a_warning(redirect):
    from httpx import ASGITransport, AsyncClient
    from test_helpers import make_test_token
    from ui.app import app as ui_app

    token = make_test_token(role="owner")
    revoke = AsyncMock(return_value={"ok": False, "error": "relay_error"})
    with patch(
        "ui.api_client.get_company",
        AsyncMock(return_value={
            "id": "00000000-0000-0000-0000-000000000002",
            "settings": {},
            "current_role": "owner",
        }),
    ), patch("ui.api_client.delete_connector_credentials", revoke):
        async with AsyncClient(
            transport=ASGITransport(app=ui_app), base_url="http://ui"
        ) as client:
            client.cookies.update({"celerp_token": token})
            normal = await client.delete(
                f"/settings/connectors/woocommerce/disconnect?x=1{redirect}",
            )
            forced = await client.delete(
                f"/settings/connectors/woocommerce/disconnect?force=1{redirect}",
            )

    html = normal.content.decode()
    assert "Nothing was disconnected or changed." in html
    assert "Disconnect anyway" in html
    escaped = redirect.replace("&", "&amp;")
    assert f'hx-delete="/settings/connectors/woocommerce/disconnect?force=1{escaped}"' in html
    assert "may keep Celerp webhooks" in html
    target = (
        "#connector-disconnect-result-woocommerce" if redirect
        else "#connector-card-woocommerce"
    )
    assert f'hx-target="{target}"' in html
    assert revoke.await_args_list[0].kwargs == {"force": False}
    assert revoke.await_args_list[1].kwargs == {"force": True}
    assert "Disconnect anyway" not in forced.content.decode()


def test_connector_detail_shows_disconnect_outcome_under_its_buttons():
    from fasthtml.common import to_xml
    from ui.routes.settings_connectors import _connector_detail_body

    html = to_xml(_connector_detail_body(
        {"id": "woocommerce", "category": "website"}, {}, None
    ))
    button = html.split('hx-delete="/settings/connectors/woocommerce/disconnect?redirect=1"')[1]
    button = button.split(">", 1)[0]
    assert 'hx-target="#connector-disconnect-result-woocommerce"' in button
    assert 'hx-swap="innerHTML"' in button
    assert 'id="connector-disconnect-result-woocommerce"' in html


def test_connector_detail_controls_swap_into_the_detail_page():
    """Every control on the detail page swaps into an element that page has, so a
    changed direction or frequency shows at once (the connectors list's card is
    not on it)."""
    import re

    from fasthtml.common import to_xml
    from ui.routes.settings_connectors import _connector_detail_body

    html = to_xml(_connector_detail_body({"id": "celerp", "category": "accounting"}, {}, None))
    targets = re.findall(r'hx-target="#([^"]+)"', html)
    assert targets and [t for t in targets if f'id="{t}"' not in html] == []
    assert 'hx-post="/settings/connectors/celerp/direction?detail=1"' in html
    assert 'hx-post="/settings/connectors/celerp/frequency?detail=1"' in html


@pytest.mark.asyncio
async def test_direction_set_on_the_detail_page_returns_its_toggle(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from fasthtml.common import to_xml
    from fastapi import FastAPI
    from starlette.requests import Request

    import celerp.connectors.ownership as ownership
    import celerp.db as db
    from ui.routes import settings_connectors as routes

    config = SimpleNamespace(direction="both")

    class Session:
        async def commit(self):
            pass

    @asynccontextmanager
    async def session_ctx():
        yield Session()

    async def lock(session, company_id, platform, require_owner=False):
        return config

    async def allowed(request, permission):
        return None

    monkeypatch.setattr(db, "get_session_ctx", session_ctx)
    monkeypatch.setattr(ownership, "lock_connector_operation", lock)
    monkeypatch.setattr(routes, "_token", lambda request: "jwt")
    monkeypatch.setattr(routes, "_check_permission", allowed)
    monkeypatch.setattr(routes, "_request_company_id", lambda request: str(uuid.UUID(int=1)))
    monkeypatch.setattr(routes, "authorize_in_process", lambda session, request, key: None)
    monkeypatch.setattr("celerp.services.company_lock.lock_company", AsyncMock())
    app = FastAPI()
    routes.setup_routes(app)
    endpoint = next(r.endpoint for r in app.routes
                    if getattr(r, "path", "") == "/settings/connectors/{platform}/direction")

    async def receive():
        return {"type": "http.request", "body": b"direction=outbound", "more_body": False}

    request = Request({
        "type": "http", "method": "POST", "path": "/settings/connectors/celerp/direction",
        "query_string": b"detail=1", "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
    }, receive)
    html = to_xml(await endpoint(request, "celerp"))
    assert config.direction == "outbound"
    assert 'class="connector-direction-toggle"' in html and "connector-card" not in html
    outbound = html.split(">Outbound<")[0].rsplit("<button", 1)[1]
    assert "btn--primary" in outbound


def _ui_owner_patches():
    return patch(
        "ui.api_client.get_company",
        AsyncMock(return_value={
            "id": "00000000-0000-0000-0000-000000000002",
            "settings": {},
            "current_role": "owner",
        }),
    )


@pytest.mark.asyncio
async def test_celerp_connector_returns_to_the_accounting_tab():
    """The accountant connector's way back, and where a finished disconnect lands,
    is its own Accounting tab."""
    from httpx import ASGITransport, AsyncClient
    from test_helpers import make_test_token
    from ui.app import app as ui_app
    from ui.routes import settings_connectors as routes

    token = make_test_token(role="owner")
    with _ui_owner_patches(), \
            patch("ui.api_client.delete_connector_credentials", AsyncMock(return_value={"ok": True})), \
            patch.object(routes, "_fetch_catalog", AsyncMock(return_value=([], "", False))), \
            patch.object(routes, "_get_connector_config", AsyncMock(return_value=None)), \
            patch.object(routes, "_entity_runs", AsyncMock(return_value={})), \
            patch.object(routes, "_attention", AsyncMock(return_value=[])), \
            patch.object(routes, "_from_accountant_section", AsyncMock(return_value="")):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as client:
            client.cookies.update({"celerp_token": token})
            page = await client.get("/settings/connectors/celerp")
            gone = await client.delete("/settings/connectors/celerp/disconnect?redirect=1")

    assert 'href="/settings/cloud?tab=accounting"' in page.text
    assert "tab=website" not in page.text
    assert gone.headers["HX-Redirect"] == "/settings/cloud?tab=accounting"


@pytest.mark.asyncio
async def test_connector_refusals_show_the_reason_without_the_status_code():
    from httpx import ASGITransport, AsyncClient
    from test_helpers import make_test_token
    from ui.api_client import APIError
    from ui.app import app as ui_app

    token = make_test_token(role="owner")
    refused = AsyncMock(side_effect=APIError(401, "No active Celerp Connect session."))
    with _ui_owner_patches(), patch("ui.api_client.delete_connector_credentials", refused), \
            patch("ui.api_client.start_connector_sync", refused):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as client:
            client.cookies.update({"celerp_token": token})
            gone = await client.delete("/settings/connectors/celerp/disconnect?redirect=1")
            sync = await client.post("/settings/connectors/celerp/sync")

    for html in (gone.text, sync.text):
        assert "API 401" not in html
    assert "No active Celerp Connect session." in gone.text
    # Sync stays behind Connect, and the refusal says so in the connector's own words.
    assert "Accountant Sync needs Celerp Connect" in sync.text
    assert "Get Celerp Connect" in sync.text
    assert "isn't connected" not in sync.text


@pytest.mark.asyncio
async def test_accountant_pairing_refused_without_connect_offers_connect():
    from httpx import ASGITransport, AsyncClient
    from test_helpers import make_test_token
    from ui.api_client import APIError
    from ui.app import app as ui_app

    token = make_test_token(role="owner")
    refused = AsyncMock(side_effect=APIError(401, "No active Celerp Connect session."))
    with _ui_owner_patches(), patch("ui.api_client.store_connector_credentials", refused), \
            patch("celerp.services.peer_access.parse_pairing_link",
                  return_value=("https://firm.example", "key")):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as client:
            client.cookies.update({"celerp_token": token})
            paired = await client.post("/settings/connectors/celerp/connect-apikey",
                                       data={"pairing_link": "cpl.x.y"})

    assert "Accountant Sync needs Celerp Connect" in paired.text
    assert "Get Celerp Connect" in paired.text
    assert "API 401" not in paired.text


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
async def test_connector_no_company_owns_offers_a_reset_to_the_installation_owner():
    """A connector no company owns can be reset by the installation owner, who
    is told it disconnects it for the entire installation. Anyone else is told
    who can reset it and sees no action they cannot perform."""
    base = {**_OAUTH, "connected": False, "ownership": "unassigned", "local_claim": False}
    owner = await _card_html([{**base, "can_reset": True}])
    assert "no company is assigned to it" in owner
    assert 'hx-delete="/settings/connectors/quickbooks/reset"' in owner
    assert "disconnects the legacy integration for the entire installation" in owner
    assert "oauth-redirect" not in owner

    other = await _card_html([{**base, "can_reset": False}])
    assert "Ask the installation owner to reset it" in other
    assert "hx-delete" not in other
    assert "oauth-redirect" not in other


@pytest.mark.asyncio
async def test_failed_reset_changes_nothing_and_offers_to_reset_anyway():
    from httpx import ASGITransport, AsyncClient
    from test_helpers import make_test_token
    from ui.app import app as ui_app

    reset = AsyncMock(return_value={"ok": False, "error": "relay_error"})
    with patch(
        "ui.api_client.get_company",
        AsyncMock(return_value={
            "id": "00000000-0000-0000-0000-000000000002",
            "settings": {},
            "current_role": "owner",
        }),
    ), patch("ui.api_client.reset_unassigned_connector", reset):
        async with AsyncClient(
            transport=ASGITransport(app=ui_app), base_url="http://ui"
        ) as client:
            client.cookies.update({"celerp_token": make_test_token(role="owner")})
            normal = await client.delete(
                "/settings/connectors/woocommerce/reset",
            )
            forced = await client.delete(
                "/settings/connectors/woocommerce/reset?force=1",
            )

    html = normal.content.decode()
    assert "Nothing was disconnected or changed." in html
    assert 'hx-delete="/settings/connectors/woocommerce/reset?force=1"' in html
    assert 'hx-target="#connector-card-woocommerce"' in html
    assert reset.await_args_list[0].kwargs == {"force": False}
    assert reset.await_args_list[1].kwargs == {"force": True}
    assert "Disconnect anyway" not in forced.content.decode()


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
        client.cookies.update(cookies)
        r = await client.post("/settings/connectors/woocommerce/deposit-account",
                              data={"woocommerce_deposit_account": "1200"})
    assert r.status_code == 200
    patch_company.assert_awaited_once_with(cookies["celerp_token"], {"woocommerce_deposit_account": "1200"})
    assert "flash--success" in r.text and 'id="connector-deposit-woocommerce"' in r.text


@pytest.mark.asyncio
async def test_deposit_account_route_rejects_an_unknown_account(deposit_ui):
    client, cookies, patch_company = deposit_ui
    async with client:
        client.cookies.update(cookies)
        r = await client.post("/settings/connectors/woocommerce/deposit-account",
                              data={"woocommerce_deposit_account": "9999"})
    assert r.status_code == 200
    assert "9999 isn't an active bank account." in r.text
    patch_company.assert_not_awaited()


@pytest.mark.asyncio
async def test_deposit_account_route_add_new_redirects_to_bank_accounts(deposit_ui):
    client, cookies, patch_company = deposit_ui
    async with client:
        client.cookies.update(cookies)
        r = await client.post("/settings/connectors/woocommerce/deposit-account",
                              data={"woocommerce_deposit_account": "__new__"})
    assert r.status_code == 204
    assert r.headers["HX-Redirect"] == "/settings/accounting/bank-accounts/new"
    patch_company.assert_not_awaited()


@pytest.mark.asyncio
async def test_deposit_account_route_is_woocommerce_only(deposit_ui):
    client, cookies, patch_company = deposit_ui
    async with client:
        client.cookies.update(cookies)
        r = await client.post("/settings/connectors/shopify/deposit-account",
                              data={"woocommerce_deposit_account": "1200"})
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
        client.cookies.update(cookies)
        r = await client.post("/settings/connectors/woocommerce/attention/7/reconciled",
                              data={"signature": "sig-7"})
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
        client.cookies.update(cookies)
        r = await client.delete("/settings/connectors/woocommerce/attention/7/reconciled")
    assert r.status_code == 200
    setter.assert_awaited_once_with(cookies["celerp_token"], "7", None)
    assert 'hx-post="/settings/connectors/woocommerce/attention/7/reconciled"' in r.text


@pytest.mark.asyncio
async def test_mark_reconciled_route_explains_a_refusal(reconcile_ui):
    from ui.api_client import APIError

    client, cookies, setter = reconcile_ui
    setter.side_effect = APIError(409, "This order changed in WooCommerce. Select Try again to review the change.")
    entry = {"id": "7", "label": "Order 7", "reason": "has a refund", "signature": "sig-8"}
    with patch("celerp.connectors.sync_runner.attention_entries", AsyncMock(return_value=[entry])):
        async with client:
            client.cookies.update(cookies)
            r = await client.post("/settings/connectors/woocommerce/attention/7/reconciled",
                                  data={"signature": "sig-7"})
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


def test_reconciled_attention_entries_sit_in_a_collapsed_section():
    """Open entries come first; ones marked reconciled sit in a collapsed
    Reconciled section under them, each still offering Undo."""
    from fasthtml.common import to_xml
    from ui.routes.settings_connectors import _attention_list

    entries = [
        {"id": "4", "label": "Order 4", "reason": "is refunded", "signature": "sig-4", "reconciled": True},
        {"id": "5", "label": "Order 5", "reason": "has a refund", "signature": "sig-5"},
    ]
    html = to_xml(_attention_list("woocommerce", entries, "en"))
    open_part, _, reconciled_part = html.partition("<details")
    assert "attention/5/reconciled" in open_part
    assert "attention/4/reconciled" not in open_part
    assert "Reconciled (1)" in reconciled_part
    assert 'hx-delete="/settings/connectors/woocommerce/attention/4/reconciled"' in reconciled_part
    assert "<details open" not in html


@pytest.mark.asyncio
async def test_attention_read_failure_shows_no_list():
    from ui.routes import settings_connectors as sc

    with patch("celerp.connectors.sync_runner.attention_entries", AsyncMock(side_effect=RuntimeError("db down"))):
        assert await sc._attention("00000000-0000-0000-0000-000000000002", "woocommerce") == []


@pytest.mark.asyncio
async def test_mark_reconciled_route_is_woocommerce_only(reconcile_ui):
    client, cookies, setter = reconcile_ui
    async with client:
        client.cookies.update(cookies)
        r = await client.post("/settings/connectors/shopify/attention/7/reconciled",
                              data={"signature": "sig-7"})
    assert r.status_code == 200
    assert "flash--warning" in r.text
    setter.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_refused_direction_keeps_the_buttons_and_says_why():
    import json
    from httpx import ASGITransport, AsyncClient
    from celerp.connectors.ownership import ConnectorOwnershipError
    from test_helpers import make_test_token
    from ui.app import app as ui_app

    token = make_test_token(role="owner")
    refused = AsyncMock(side_effect=ConnectorOwnershipError("Another company holds this connection."))
    with _ui_owner_patches(), patch("celerp.connectors.ownership.lock_connector_operation", refused):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as client:
            client.cookies.update({"celerp_token": token})
            r = await client.post("/settings/connectors/celerp/direction?detail=1",
                                  data={"direction": "outbound"})

    # Nothing is swapped, so the toggle stays where it was, and the reason is a toast.
    assert r.headers.get("HX-Reswap") == "none"
    toast = json.loads(r.headers["HX-Trigger"])["celerpToast"]
    assert toast["message"] == "Another company holds this connection."
    assert toast["type"] == "error"
