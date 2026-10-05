# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Playwright: layout at the two widths people actually use.

At a laptop width the installed modules table keeps every row's action button in view (the
name/description cell wraps instead of pushing the action column out of the table's box).
On a phone the notification panel opens inside the screen instead of hanging off its left edge.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from test_helpers import make_test_token

pytestmark = pytest.mark.browser


def _shipped_module_rows() -> list[dict]:
    """The bundled modules as the API lists them: real names, descriptions and dependencies."""
    from celerp.modules.loader import read_manifest_metadata
    rows = []
    for pkg in sorted((Path(__file__).parents[2] / "default_modules").iterdir()):
        if not (pkg / "__init__.py").exists() or pkg.name.startswith(("_", ".")):
            continue
        m = read_manifest_metadata(pkg)
        rows.append({"name": pkg.name, "label": m.get("display_name") or pkg.name,
                     "version": m.get("version", ""), "description": m.get("description", ""),
                     "author": m.get("author", ""), "depends_on": list(m.get("depends_on") or []),
                     "enabled": True, "running": True, "is_default": True, "source": "default"})
    return rows


def test_installed_modules_keep_their_action_buttons_in_view_at_laptop_width(playwright):
    """The installed list is rendered by the real route over the shipped modules and loaded
    with the real assets, then measured at 1440px wide."""
    from starlette.testclient import TestClient
    from ui.app import app as ui_app
    client = TestClient(ui_app, base_url="http://ui")
    token = {"celerp_token": make_test_token(role="admin")}

    def serve(route):
        r = client.get(route.request.url.removeprefix("http://ui"), cookies=token)
        route.fulfill(status=r.status_code, body=r.content,
                      headers={"content-type": r.headers.get("content-type", "text/html")})

    browser = playwright.chromium.launch()
    try:
        with patch("ui.api_client.get_modules", new=AsyncMock(return_value=_shipped_module_rows())), \
                patch("ui.api_client.installation_owner", new=AsyncMock(return_value=True), create=True), \
                patch("ui.routes.modules_page._modules_dir_display", return_value="/data/modules"):
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.route("http://ui/**", serve)
            page.goto("http://ui/modules", wait_until="load")
            page.wait_for_selector("#local-modules-panel table tbody td:last-child button", timeout=10000)
            m = page.evaluate("""() => {
                const t = document.querySelector('#local-modules-panel table');
                const edge = Math.min(innerWidth, t.parentElement.getBoundingClientRect().right);
                const btns = [...t.querySelectorAll('tbody td:last-child button')];
                return {buttons: btns.length, table_w: t.scrollWidth, box_w: t.parentElement.clientWidth,
                        hidden: btns.filter(b => b.getBoundingClientRect().right > edge + 1).length};
            }""")
    finally:
        browser.close()
    assert m["buttons"] > 0, m
    assert m["hidden"] == 0, f"action buttons pushed past the table's visible edge: {m}"
    assert m["table_w"] <= m["box_w"] + 1, f"the table scrolls sideways at 1440px: {m}"


def test_the_notification_panel_opens_inside_a_phone_screen(page, ui_server):
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{ui_server}/dashboard", wait_until="domcontentloaded")
    page.locator(".notif-bell-btn").click()
    page.wait_for_selector("#notif-panel", state="visible", timeout=10000)
    r = page.evaluate("""() => { const r = document.getElementById('notif-panel').getBoundingClientRect();
        return {left: r.left, right: r.right, vw: document.documentElement.clientWidth}; }""")
    assert r["left"] >= 0 and r["right"] <= r["vw"], f"panel runs off the screen: {r}"
