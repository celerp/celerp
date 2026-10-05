# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Each company chooses its own modules; the installation loads every module
some company uses.

Turning a module off for one company leaves it loaded and working for the
others, while that company's requests to it are refused, its menu and slot
entries are left out and its per-company hooks do not run.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from celerp.modules import loader, slots
from test_helpers import make_test_token, register_admin


def _uid() -> str:
    return uuid.uuid4().hex[:8]


_API_BODY = """\
from fastapi import APIRouter, Depends
from celerp.services.auth import get_current_company_id

router = APIRouter()


@router.get("/{inner}/ping")
async def ping(company_id=Depends(get_current_company_id)):
    return {"company_id": str(company_id)}


def setup_api_routes(app):
    app.include_router(router)
"""

_UI_BODY = """\
from starlette.responses import PlainTextResponse


def setup_ui_routes(app):
    app.router.add_route("/{inner}/page", lambda r: PlainTextResponse("module page"))
"""


def _write_module(base: Path, manifest: dict, files: dict[str, str]) -> None:
    pkg = base / manifest["name"]
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(f"PLUGIN_MANIFEST = {manifest!r}\n")
    for rel, body in files.items():
        (pkg / rel).parent.mkdir(parents=True, exist_ok=True)
        (pkg / rel).write_text(body)


@pytest.fixture
def module_dir(tmp_path, monkeypatch):
    """A module directory the loader and the dependency resolver both read."""
    from celerp.models.base import Base

    base = tmp_path / "modules"
    base.mkdir()
    monkeypatch.setenv("MODULE_DIR", str(base))
    before_mods, before_tables = set(sys.modules), set(Base.metadata.tables)
    yield base
    from celerp.main import app
    from ui.app import app as ui_app
    names = {p.name for p in base.iterdir()}
    loader._remove_routes(app, names)
    loader._remove_routes(ui_app, names)
    for name in names:
        loader._admitted.pop(name, None)
        loader._load_errors.pop(name, None)
    for key in set(Base.metadata.tables) - before_tables:
        Base.metadata.remove(Base.metadata.tables[key])
    for key in set(sys.modules) - before_mods:
        if key.startswith("acme"):
            sys.modules.pop(key, None)


def _routed_module(base: Path) -> tuple[str, str]:
    """A module with one API route (company-scoped) and one page."""
    name, inner = f"acme-{_uid()}", f"acme_{_uid()}"
    _write_module(base, {"name": name, "version": "1.0.0",
                         "api_routes": f"{inner}.api", "ui_routes": f"{inner}.ui"}, {
        f"{inner}/__init__.py": "",
        f"{inner}/api.py": _API_BODY.replace("{inner}", inner),
        f"{inner}/ui.py": _UI_BODY.replace("{inner}", inner),
    })
    from celerp.main import app
    from ui.app import app as ui_app
    loaded = loader.load_all(str(base), {name})
    loader.register_api_routes(app, loaded)
    loader.register_ui_routes(ui_app, loaded)
    assert loader.is_running(name), loader.load_errors()
    return name, inner


async def _two_companies(client) -> tuple[dict, dict]:
    a = {"Authorization": f"Bearer {await register_admin(client)}"}
    r = await client.post("/companies", json={"name": f"Second {_uid()}"}, headers=a)
    assert r.status_code == 200, r.text
    return a, {"Authorization": f"Bearer {r.json()['access_token']}"}


def _configured() -> list[str]:
    from celerp.config import read_config
    return list(read_config().get("modules", {}).get("enabled") or [])


@pytest.mark.asyncio
async def test_company_that_turned_a_module_off_is_refused_while_another_keeps_it(
        client, module_dir):
    name, inner = _routed_module(module_dir)
    a, b = await _two_companies(client)
    for h in (a, b):
        assert (await client.post(f"/companies/me/modules/{name}/enable", headers=h)).status_code == 200

    r = await client.post(f"/companies/me/modules/{name}/disable", headers=a)
    assert r.status_code == 200, r.text

    # Still loaded: the other company uses it.
    assert name in _configured()
    assert loader.is_running(name)
    assert (await client.get(f"/{inner}/ping", headers=b)).status_code == 200
    refused = await client.get(f"/{inner}/ping", headers=a)
    assert refused.status_code == 403
    assert refused.json()["detail"] == "This module is turned off for your company."


@pytest.mark.asyncio
@pytest.mark.parametrize("uses,status", [(False, 403), (True, 200)])
async def test_module_page_follows_the_company_choice(module_dir, uses, status):
    name, inner = _routed_module(module_dir)
    from ui.app import app as ui_app
    settings = {"enabled_modules": [name] if uses else []}
    with patch("ui.api_client.get_company", new=AsyncMock(return_value={"settings": settings})):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as c:
            r = await c.get(f"/{inner}/page", cookies={"celerp_token": make_test_token()})
    assert r.status_code == status
    if status == 403:
        assert "This module is turned off for your company." in r.text
    else:
        assert r.text == "module page"


@pytest.mark.asyncio
async def test_last_company_turning_a_module_off_takes_it_out_of_the_load_set(
        client, module_dir):
    name, _inner = _routed_module(module_dir)
    a, b = await _two_companies(client)
    for h in (a, b):
        await client.post(f"/companies/me/modules/{name}/enable", headers=h)
    await client.post(f"/companies/me/modules/{name}/disable", headers=a)
    assert name in _configured()
    r = await client.post(f"/companies/me/modules/{name}/disable", headers=b)
    assert r.json()["restart_required"] is False
    assert name not in _configured()


def _dependent_pair(base: Path) -> tuple[str, str]:
    dep, main = f"acme-dep-{_uid()}", f"acme-main-{_uid()}"
    _write_module(base, {"name": dep, "version": "1.0.0"}, {})
    _write_module(base, {"name": main, "version": "1.0.0", "depends_on": [dep]}, {})
    return dep, main


@pytest.mark.asyncio
async def test_turning_a_module_on_turns_on_what_it_needs_for_that_company(client, module_dir):
    dep, main = _dependent_pair(module_dir)
    a, b = await _two_companies(client)
    r = await client.post(f"/companies/me/modules/{main}/disable", headers=b)
    assert r.status_code == 200, r.text
    r = await client.post(f"/companies/me/modules/{main}/enable", headers=a)
    assert r.status_code == 200, r.text
    assert r.json()["also_enabled"] == [dep]
    assert {dep, main} <= set(r.json()["enabled_modules"])
    assert {dep, main} <= set(_configured())
    # The other company's own choice is untouched.
    b_settings = (await client.get("/companies/me", headers=b)).json()["settings"]
    assert not {dep, main} & set(b_settings["enabled_modules"])


@pytest.mark.asyncio
async def test_turning_off_a_module_the_company_still_needs_is_refused(client, module_dir):
    dep, main = _dependent_pair(module_dir)
    a, _b = await _two_companies(client)
    await client.post(f"/companies/me/modules/{main}/enable", headers=a)
    r = await client.post(f"/companies/me/modules/{dep}/disable", headers=a)
    assert r.status_code == 409
    assert "Turn that off first." in r.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["delete", "purge-data"])
async def test_module_another_company_uses_cannot_be_removed(client, module_dir, action):
    _dep, main = _dependent_pair(module_dir)
    a, b = await _two_companies(client)
    await client.post(f"/companies/me/modules/{main}/enable", headers=b)
    await client.post(f"/companies/me/modules/{main}/disable", headers=a)
    r = await client.post(f"/companies/me/modules/{main}/{action}", headers=a)
    assert r.status_code == 409, r.text
    assert (module_dir / main).is_dir()


@pytest.mark.asyncio
async def test_company_hooks_run_only_for_modules_the_company_uses(session):
    from test_helpers import ensure_company
    from celerp.services.company_lock import locked_company

    ran: list[str] = []

    async def hook(**kw):
        ran.append(kw["tag"])

    row = await locked_company(session, await ensure_company(session))
    row.settings = {**(row.settings or {}), "enabled_modules": ["acme-on"]}
    await session.flush()
    with patch("celerp.modules.slots.resolve_handler", return_value=hook):
        slots.register("doc_finalize_hook", {"handler": "x:on", "_module": "acme-on"})
        slots.register("doc_finalize_hook", {"handler": "x:off", "_module": "acme-off"})
        await slots.fire_lifecycle("doc_finalize_hook", session=session,
                                   company_id=row.id, tag="fired")
    assert ran == ["fired"]


def test_category_defaults_come_only_from_modules_the_company_uses():
    from celerp.services.field_schema import all_category_schemas

    slots.register("category_schema", {"category": "Gems", "fields": [{"key": "carat"}],
                                       "_module": "acme-gems"})
    assert "Gems" not in all_category_schemas({"enabled_modules": []})
    assert all_category_schemas({"enabled_modules": ["acme-gems"]})["Gems"] == [{"key": "carat"}]


# ── The Modules page follows the company's own choice ─────────────────────────

def _row(name, *, enabled, running, is_default=False, load_error=None):
    return {"name": name, "label": name, "version": "1.0", "author": "", "depends_on": [],
            "enabled": enabled, "running": running, "load_error": load_error,
            "is_default": is_default, "source": "default" if is_default else "sideloaded",
            "installed_at": None, "demoted": False}


@pytest.mark.parametrize("is_default", [True, False])
def test_module_the_company_turned_off_offers_enable_while_others_keep_it_running(is_default):
    """Turning a module off for a company takes effect at once, even though it stays
    loaded for the other companies: the row says it is off and offers Enable, never a
    Running badge with only Disable, and no restart is asked for."""
    from fasthtml.common import to_xml
    from ui.i18n import t
    from ui.routes import modules_page as mp
    body = to_xml(mp._local_panel([_row("celerp-labels", enabled=False, running=True,
                                        is_default=is_default)], owner=True))
    assert 'hx-post="/modules/celerp-labels/enable"' in body
    assert "/modules/celerp-labels/disable" not in body
    assert f'>{t("modules.badge_disabled", "en")}<' in body
    assert f'>{t("modules.badge_running", "en")}<' not in body
    assert t("settings.restart_needed", "en") not in body
    assert t("settings._a_restart_is_required_for_module_changes_to_take", "en") not in body


def test_refused_module_does_not_ask_for_a_restart():
    """A restart does not fix a module Celerp refused, so it never raises the banner."""
    from fasthtml.common import to_xml
    from ui.i18n import t
    from ui.routes import modules_page as mp
    banner = t("settings._a_restart_is_required_for_module_changes_to_take", "en")
    refused = _row("acme-bad", enabled=True, running=False, load_error="Refused: not allowed")
    assert banner not in to_xml(mp._local_panel([refused], owner=True))
    waiting = _row("acme-new", enabled=True, running=False)
    assert banner in to_xml(mp._local_panel([waiting], owner=True))


@pytest.mark.asyncio
async def test_a_module_built_into_celerp_is_always_on_and_cannot_be_turned_off(client, module_dir):
    folded = sorted(loader.CORE_FOLDED)[0]
    _write_module(module_dir, {"name": folded, "version": "1.0.0"}, {})
    a, _b = await _two_companies(client)
    r = await client.post(f"/companies/me/modules/{folded}/disable", headers=a)
    assert r.status_code == 409, r.text
    assert "always on" in r.json()["detail"]
    listed = {m["name"]: m for m in (await client.get("/companies/me/modules", headers=a)).json()}
    assert listed[folded]["enabled"] is True and listed[folded]["running"] is True
