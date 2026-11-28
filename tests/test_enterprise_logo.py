# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A partner-managed install wears the Celerp Enterprise mark.

The UI runs in its own process, so it learns the commercial mode from the store the
API writes on every accepted change (config.toml on a self-hosted install). These
tests drive that store, never the API's in-memory model: the logo and the browser-tab
icons follow it on every surface, a direct install (any SKU) keeps the standard mark,
and the partner link ending puts the standard mark back.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import pytest_asyncio
from fasthtml.common import Div, to_xml
from httpx import ASGITransport, AsyncClient

_REPO = Path(__file__).resolve().parents[1]

_STANDARD = {"/static/logo.png", "/static/favicon.ico", "/static/favicon-32x32.png",
             "/static/apple-touch-icon.png"}
_ENTERPRISE = {"/static/logo-enterprise.png", "/static/favicon-enterprise.ico",
               "/static/favicon-enterprise-32x32.png", "/static/apple-touch-icon-enterprise.png"}


def _partner(version: int) -> dict:
    return {
        "version": version, "schema_version": 1, "commercial_mode": "partner_managed",
        "implementation": {"partner_id": "p-1", "display_name": "Partner Co"},
    }


def _direct(version: int, subscription: dict | None = None) -> dict:
    ctx = {"version": version, "schema_version": 1, "commercial_mode": "celerp_direct"}
    if subscription is not None:
        ctx["subscription"] = subscription
    return ctx


def _store(ctx: dict | None) -> None:
    """Write (or clear) the commercial context the way the API persists it."""
    from celerp.config import _update_cloud_config

    def _mutate(cloud: dict) -> None:
        if ctx is None:
            cloud.pop("commercial_context_json", None)
        else:
            cloud["commercial_context_json"] = json.dumps(ctx, separators=(",", ":"))
    _update_cloud_config(_mutate)


@pytest.fixture(autouse=True)
def _clean_commercial_state():
    import celerp.gateway.state as gw_state
    _store(None)
    gw_state._commercial_context = {}
    gw_state.mirror_stored_commercial_context()
    yield
    _store(None)
    gw_state.mirror_stored_commercial_context()
    gw_state._commercial_context = {}


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as c:
        yield c


def _marks(html: str) -> set[str]:
    """Every Celerp mark the page references: the logo image and the tab icons."""
    found = set(re.findall(r'(?:src|href)="(/static/(?:logo|favicon|apple-touch-icon)[^"?]*)', html))
    return found


async def _login_marks(ui_client) -> set[str]:
    r = await ui_client.get("/login")
    assert r.status_code == 200
    return _marks(r.text)


def _shell_marks() -> set[str]:
    """The surfaces rendered outside a request: the nav-less shell, the full sidebar
    and the sign-in header."""
    from ui.components.shell import _sidebar, minimal_shell
    from ui.routes.auth import auth_header
    html = to_xml(minimal_shell(Div())) + to_xml(_sidebar("dashboard")) + to_xml(auth_header("t"))
    return _marks(html)


@pytest.mark.asyncio
async def test_direct_install_shows_the_standard_mark(ui_client):
    assert await _login_marks(ui_client) == _STANDARD
    assert _shell_marks() == _STANDARD


@pytest.mark.asyncio
async def test_partner_managed_install_shows_the_enterprise_mark_everywhere(ui_client):
    _store(_partner(1))
    assert await _login_marks(ui_client) == _ENTERPRISE
    assert _shell_marks() == _ENTERPRISE


@pytest.mark.asyncio
async def test_partner_link_ending_restores_the_standard_mark(ui_client):
    _store(_partner(1))
    assert await _login_marks(ui_client) == _ENTERPRISE
    _store(_direct(2))
    assert await _login_marks(ui_client) == _STANDARD
    assert _shell_marks() == _STANDARD


@pytest.mark.asyncio
async def test_the_relay_ending_the_partner_link_restores_the_standard_mark(ui_client):
    """The partner link starts and ends as a relay push the API accepts and persists;
    the UI follows the persisted store alone, never the API's in-memory model."""
    import celerp.gateway.state as gw_state
    assert gw_state.apply_commercial_context(_partner(1)) == "applied"
    gw_state._commercial_context = {}
    assert await _login_marks(ui_client) == _ENTERPRISE
    assert gw_state.apply_commercial_context(_direct(2)) == "applied"
    gw_state._commercial_context = _partner(1)
    assert await _login_marks(ui_client) == _STANDARD


@pytest.mark.asyncio
async def test_team_subscription_on_a_direct_install_keeps_the_standard_mark(ui_client):
    _store(_direct(1, {"status": "active", "tier": "team"}))
    assert await _login_marks(ui_client) == _STANDARD


@pytest.mark.asyncio
async def test_an_invalid_stored_context_reads_as_a_direct_install(ui_client):
    _store(_partner(1))
    assert await _login_marks(ui_client) == _ENTERPRISE
    bad = _partner(2)
    del bad["implementation"]
    _store(bad)
    assert await _login_marks(ui_client) == _STANDARD


@pytest.mark.asyncio
async def test_a_changed_store_rebuilds_a_model_cleared_in_process(ui_client, monkeypatch):
    """Whenever the store changes, the mark follows what it holds, even when the same
    partner context comes back after the model was cleared in between (an instance
    change clears it)."""
    import celerp.gateway.state as gw_state
    _store(_partner(1))
    assert await _login_marks(ui_client) == _ENTERPRISE
    monkeypatch.setattr(gw_state, "_instance_id", "instance-a")
    gw_state.set_instance_id("instance-b")
    _store(None)
    _store(_partner(1))
    assert await _login_marks(ui_client) == _ENTERPRISE


@pytest.mark.asyncio
async def test_every_mark_is_served(ui_client):
    for path in sorted(_STANDARD | _ENTERPRISE):
        r = await ui_client.get(path)
        assert r.status_code == 200, path
        assert r.headers["content-type"].startswith("image/"), path


def test_enterprise_logo_is_web_sized():
    from PIL import Image
    path = _REPO / "ui/static/logo-enterprise.png"
    assert path.stat().st_size < 300_000
    with Image.open(path) as im:
        assert im.width <= 1000


def test_no_surface_names_a_mark_file_outside_the_branding_resolver():
    """Every Celerp mark renders through ui/components/branding.py, so the
    partner-managed switch cannot miss a surface."""
    pattern = re.compile(r"/static/(?:logo|favicon|apple-touch-icon)")
    offenders = []
    for path in sorted((_REPO / "ui").rglob("*")):
        if path.suffix not in {".py", ".html", ".js", ".css"} or path.name == "branding.py":
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(_REPO)}:{n}")
    assert offenders == []
