# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The dashboard card's links open a list page with ?hint=import, and the page shows
an arrow on its Import button: "Click Import to upload your file". The arrow goes on
any click, on Esc, or when the button scrolls away, and never comes back on refresh.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.browser

_HINT_TEXT = "Click Import to upload your file"
_CARD_LINKS = [("Products", "/inventory"), ("Customers & suppliers", "/contacts/customers"),
               ("Documents", "/docs")]


def _clear_session_registry() -> None:
    """Wipe session_registry rows so a second user can log in."""
    import psycopg2
    from urllib.parse import urlsplit
    parts = urlsplit(os.environ["DATABASE_URL"].replace("+asyncpg", ""))
    conn = psycopg2.connect(host=parts.hostname, port=parts.port, user=parts.username,
                            password=parts.password, dbname=parts.path.lstrip("/"))
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM session_registry;")
    conn.close()


def _boxes(page: Page):
    btn = page.locator("[data-import-hint]")
    tip = page.locator(".import-arrow")
    expect(tip).to_be_visible()
    expect(tip).to_contain_text(_HINT_TEXT)
    return btn.bounding_box(), tip.bounding_box()


def _in_viewport(page: Page, box) -> bool:
    vw, vh = page.viewport_size["width"], page.viewport_size["height"]
    return box["x"] >= 0 and box["y"] >= 0 and box["x"] + box["width"] <= vw and box["y"] + box["height"] <= vh


@pytest.mark.parametrize("width", [390, 1280])
def test_card_link_shows_arrow_at_import_button(page: Page, fresh_company, width):
    page.set_viewport_size({"width": width, "height": 800})
    for label, path in _CARD_LINKS:
        page.goto("/dashboard")
        card = page.locator("#getting-started-card")
        expect(card).to_be_visible()
        card.get_by_role("link", name=label, exact=True).click()
        page.wait_for_url(f"**{path}*")
        btn, tip = _boxes(page)
        assert _in_viewport(page, btn), (path, width, btn)
        assert _in_viewport(page, tip), (path, width, tip)
        # The arrow sits right under the button and overlaps it horizontally.
        assert 0 <= tip["y"] - (btn["y"] + btn["height"]) <= 16, (path, width, btn, tip)
        assert tip["x"] < btn["x"] + btn["width"] and btn["x"] < tip["x"] + tip["width"], (path, width)
        # No sideways scroll on the page.
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (path, width)


def test_arrow_dismissed_by_click_and_esc(page: Page, fresh_company):
    page.goto("/inventory?hint=import")
    expect(page.locator(".import-arrow")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator(".import-arrow")).to_have_count(0)
    expect(page.locator("[data-import-hint]")).not_to_have_class("import-arrow-pulse")

    page.goto("/docs?hint=import")
    expect(page.locator(".import-arrow")).to_be_visible()
    page.locator("h1.page-title").click()
    expect(page.locator(".import-arrow")).to_have_count(0)


def test_arrow_goes_when_the_button_scrolls_away(page: Page, fresh_company):
    page.set_viewport_size({"width": 390, "height": 500})
    page.goto("/inventory?hint=import")
    expect(page.locator(".import-arrow")).to_be_visible()
    page.evaluate("document.body.style.minHeight = '3000px'")
    page.mouse.wheel(0, 1200)
    expect(page.locator(".import-arrow")).to_have_count(0)


def test_arrow_not_shown_again_after_refresh(page: Page, fresh_company):
    page.goto("/contacts/customers?hint=import&q=")
    expect(page.locator(".import-arrow")).to_be_visible()
    assert "hint=import" not in page.url
    assert "q=" in page.url, "other query parameters are kept"
    page.reload()
    page.wait_for_load_state("load")
    expect(page.locator(".import-arrow")).to_have_count(0)


def test_no_arrow_without_import_button(page: Page, fresh_company, api_server):
    email = f"viewer-{uuid.uuid4().hex[:8]}@celerp.test"
    r = fresh_company.post("/companies/me/users", json={"email": email, "name": "Viewer", "role": "viewer",
                                                        "password": "Viewer12345!"})
    assert r.status_code == 200, r.text
    _clear_session_registry()
    lr = httpx.post(f"{api_server}/auth/login", json={"email": email, "password": "Viewer12345!"}, timeout=10)
    assert lr.status_code == 200, lr.text
    token = lr.json()["access_token"]
    page.context.add_cookies([{"name": "celerp_token", "value": token, "domain": "127.0.0.1", "path": "/"}])
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto("/inventory?hint=import")
    page.wait_for_load_state("load")
    expect(page.locator("[data-import-hint]")).to_have_count(0)
    expect(page.locator(".import-arrow")).to_have_count(0)
    expect(page.locator("h1.page-title")).to_be_visible()
    assert not [e for e in errors if "import" in e.lower() or "hint" in e.lower()], errors


def test_arrow_respects_reduced_motion(page: Page, fresh_company):
    page.emulate_media(reduced_motion="reduce")
    page.goto("/inventory?hint=import")
    expect(page.locator(".import-arrow")).to_be_visible()
    anim = page.evaluate("""() => [
        getComputedStyle(document.querySelector('[data-import-hint]')).animationName,
        getComputedStyle(document.querySelector('.import-arrow')).animationName]""")
    assert anim == ["none", "none"], anim
    page.emulate_media(reduced_motion="no-preference")
    page.goto("/inventory?hint=import")
    expect(page.locator(".import-arrow")).to_be_visible()
    moving = page.evaluate("getComputedStyle(document.querySelector('[data-import-hint]')).animationName")
    assert moving != "none"


_LOCALE_DIR = Path(__file__).resolve().parents[2] / "ui" / "locales"
_CARD_KEYS = ("dashboard.getting_started_title", "dashboard.getting_started_products",
              "dashboard.getting_started_contacts", "dashboard.getting_started_documents",
              "dashboard.getting_started_where", "dashboard.getting_started_demo",
              "setup.option_restore_title", "setup.option_move_title")


@pytest.mark.parametrize("width", [390, 1280])
def test_card_and_arrow_in_german(page: Page, fresh_company, width):
    """INV-20 German gate: the card, its options and the arrow line are German for a
    German browser, with no English left and nothing scrolling sideways."""
    en = json.loads((_LOCALE_DIR / "en.json").read_text())
    de = json.loads((_LOCALE_DIR / "de.json").read_text())
    page.set_viewport_size({"width": width, "height": 800})
    page.set_extra_http_headers({"Accept-Language": "de-DE,de;q=0.9"})
    page.goto("/dashboard")
    card = page.locator("#getting-started-card")
    expect(card).to_be_visible()
    text = card.inner_text()
    for key in _CARD_KEYS:
        assert de[key] != en[key], f"{key} is not translated"
        assert de[key] in text, f"{key}: {de[key]!r} missing"
        assert en[key] not in text, f"{key}: English {en[key]!r} on the German card"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    card.get_by_role("link", name=de["dashboard.getting_started_products"], exact=True).click()
    page.wait_for_url("**/inventory*")
    tip = page.locator(".import-arrow")
    expect(tip).to_contain_text(de["shell.import_hint"])
    assert en["shell.import_hint"] not in tip.inner_text()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
