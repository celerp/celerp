# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Installing, removing and restarting modules, and changing the database and
file storage, are for the installation owner. Pages offer those controls only
to them; a company admin sees what is installed and a plain note instead."""
from __future__ import annotations

import json

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fasthtml.common import FastHTML
from httpx import ASGITransport, AsyncClient

from test_helpers import make_test_token
from ui.i18n import t

_MODULES = [
    {"name": "mod-off", "label": "Mod Off", "version": "1.0", "author": "A",
     "enabled": False, "running": False},
    {"name": "mod-pending", "label": "Mod Pending", "version": "1.0", "author": "A",
     "enabled": True, "running": False},
]
_CATALOG = [
    {"id": "paid-mod", "name": "Paid", "tier": "official", "author": "A",
     "license": "Proprietary", "price_monthly": 15.0},
    {"id": "free-mod", "name": "Free", "tier": "verified", "author": "A",
     "license": "MIT"},
    {"id": "comm-mod", "name": "Community", "tier": "community", "author": "A",
     "license": "MIT", "repo": "https://example.test/comm-mod"},
]
_INSTALL_CONTROLS = (
    'hx-post="/modules/import"',
    'hx-get="/modules/mod-off/delete-options"',
    'hx-post="/modules/restart"',
    'id="open-modules-folder-btn"',
)


@pytest.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                           follow_redirects=False) as c:
        yield c


def _cookies():
    return {"celerp_token": make_test_token(role="admin")}


def _owner(value: bool):
    return patch("ui.api_client.installation_owner", new=AsyncMock(return_value=value), create=True)


async def _modules_page(ui_client, owner: bool) -> str:
    with _owner(owner), patch("ui.api_client.get_modules", new=AsyncMock(return_value=_MODULES)), \
            patch("ui.routes.modules_page._modules_dir_display", return_value="/data/modules"):
        r = await ui_client.get("/modules", cookies=_cookies())
    assert r.status_code == 200
    return r.text


@pytest.mark.asyncio
async def test_company_admin_is_not_offered_installation_controls(ui_client):
    body = await _modules_page(ui_client, owner=False)
    for control in _INSTALL_CONTROLS:
        assert control not in body, control
    assert t("modules.owner_only", "en") in body
    # Choosing which installed modules the company uses stays theirs.
    assert 'hx-post="/modules/mod-off/enable"' in body
    assert 'hx-post="/modules/mod-pending/disable"' in body


@pytest.mark.asyncio
async def test_installation_owner_is_offered_installation_controls(ui_client):
    body = await _modules_page(ui_client, owner=True)
    for control in _INSTALL_CONTROLS:
        assert control in body, control
    assert t("modules.owner_only", "en") not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [False, True])
async def test_marketplace_offers_buying_and_installing_to_the_owner_only(ui_client, owner):
    with _owner(owner), \
            patch("ui.marketplace_catalog.fetch_catalog", new=AsyncMock(return_value=(_CATALOG, False))), \
            patch("ui.api_client.get_modules", new=AsyncMock(return_value=[])), \
            patch("ui.api_client.module_licenses", new=AsyncMock(return_value=[])), \
            patch("ui.api_client.account_status", new=AsyncMock(return_value={})):
        r = await ui_client.get("/modules/marketplace-panel", cookies=_cookies())
    body = r.text
    assert "Paid" in body and "Free" in body
    for control in ('hx-post="/modules/buy', 'hx-post="/modules/marketplace-download"'):
        assert (control in body) is owner, control
    assert ("Only the installation owner" in body) is not owner


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [False, True])
async def test_community_offers_downloading_to_the_owner_only(ui_client, owner):
    with _owner(owner), \
            patch("ui.marketplace_catalog.fetch_catalog", new=AsyncMock(return_value=(_CATALOG, False))), \
            patch("ui.api_client.get_modules", new=AsyncMock(return_value=[])):
        r = await ui_client.post("/modules/community-ack", cookies=_cookies())
    body = r.text
    assert "Community" in body
    assert ('hx-post="/modules/community-download"' in body) is owner
    assert ("Only the installation owner" in body) is not owner


# -- database and file storage ------------------------------------------------

@pytest.fixture
async def cloud_client(monkeypatch):
    import ui.routes.settings_cloud as sc
    monkeypatch.setattr(sc, "_check_permission", AsyncMock(return_value=None))
    monkeypatch.setattr(sc, "_token", lambda req: "tok")
    monkeypatch.setattr(sc, "_commercial_state", AsyncMock(
        return_value={"feature_flags": {"external_db": True, "external_storage": True}}))
    app = FastHTML()
    sc.setup_routes(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t",
                           cookies={"celerp_token": "tok"}) as c:
        yield c, sc


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/settings/cloud/save-infra", "/settings/cloud/restore-db"])
async def test_company_admin_cannot_change_database_or_storage(cloud_client, monkeypatch, path):
    client, sc = cloud_client
    saved = MagicMock()
    for fn in ("_save_infra_packaged", "_save_infra_selfhosted",
               "_restore_db_packaged", "_restore_db_selfhosted"):
        monkeypatch.setattr(sc, fn, saved)
    with _owner(False):
        r = await client.post(path, data={"db_host": "h", "db_name": "n", "db_user": "u"})
    assert r.status_code == 403
    saved.assert_not_called()


def test_infrastructure_tab_offers_the_settings_to_the_owner_only():
    from fasthtml.common import to_xml
    import ui.routes.settings_cloud as sc
    company_admin = to_xml(sc._infrastructure_tab(owner=False))
    assert "/settings/cloud/save-infra" not in company_admin
    assert "Only the installation owner can change the database and file storage." in company_admin
    with patch.object(sc, "_packaged_infra_or_none", return_value=None):
        assert "/settings/cloud/save-infra" in to_xml(sc._infrastructure_tab(owner=True))


@pytest.mark.asyncio
async def test_a_module_that_failed_to_load_names_the_reason_and_what_to_do(ui_client):
    """The toast names the module, keeps the loader's reason, and says what to do."""
    reason = "api_routes 'celerp.routers.health' does not resolve to source inside the module."
    broken = [{"name": "bad-mod", "label": "Bad Mod", "version": "1.0", "author": "A",
               "enabled": True, "running": False, "load_error": reason}]
    with _owner(True), patch("ui.api_client.get_modules", new=AsyncMock(return_value=broken)), \
            patch("ui.routes.modules_page._modules_dir_display", return_value="/data/modules"):
        r = await ui_client.get("/modules", cookies=_cookies())
    assert json.dumps(t("modules.load_failed", "en", label="Bad Mod", error=reason))[1:-1] in r.text
