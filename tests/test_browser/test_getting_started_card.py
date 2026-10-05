# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The dashboard "Bring in your data" card: the restore and move boxes read the same
as on the setup page, each import link carries its icon, and the close button hides
the card for good only when "Don't show this again" is ticked."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from .start_options_checks import assert_start_options_layout

pytestmark = pytest.mark.browser

_LOCALES = Path(__file__).resolve().parents[2] / "ui" / "locales"
_CARD = "#getting-started-card"


def _catalog(lang: str) -> dict:
    return json.loads((_LOCALES / f"{lang}.json").read_text())


@pytest.mark.parametrize("width,lang", [(1280, "en"), (390, "en"), (1280, "de")])
def test_card_options_side_by_side_and_flush_left(page: Page, fresh_company, width, lang):
    cat = _catalog(lang)
    page.set_viewport_size({"width": width, "height": 900})
    page.set_extra_http_headers({"Accept-Language": f"{lang};q=0.9"})
    page.goto("/dashboard")
    expect(page.locator(_CARD)).to_be_visible()
    assert_start_options_layout(page, _CARD, cat["setup.option_restore_title"], cat["setup.option_move_title"])


def test_close_without_tick_comes_back_on_reload(page: Page, fresh_company):
    page.goto("/dashboard")
    card = page.locator(_CARD)
    expect(card).to_be_visible()
    expect(card.locator("input[type=checkbox][name=forever]")).not_to_be_checked()
    card.locator("#getting-started-dismiss").click()
    expect(page.locator(_CARD)).to_have_count(0)
    page.reload()
    expect(page.locator(_CARD)).to_be_visible()


def test_close_with_tick_stays_gone_after_reload(page: Page, fresh_company):
    cat = _catalog("en")
    page.goto("/dashboard")
    card = page.locator(_CARD)
    box = card.get_by_label(cat["dashboard.getting_started_forever"])
    expect(box).to_be_visible()
    box.check()
    card.locator("#getting-started-dismiss").click()
    expect(page.locator(_CARD)).to_have_count(0)
    page.reload()
    expect(page.locator(".kpi-grid").first).to_be_visible()
    expect(page.locator(_CARD)).to_have_count(0)
    settings = fresh_company.get("/companies/me").json().get("settings") or {}
    assert settings.get("getting_started_dismissed") is True
