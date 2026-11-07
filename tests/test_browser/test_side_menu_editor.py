# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test: Settings > Side menu changes the side menu on the page where it is made.

Hiding, showing, moving and resetting redraw the side menu beside the editor, so the owner
sees the result without opening another page. The redrawn menu still opens and closes its
groups, and on a phone the menu button still opens it.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from .conftest import answer_confirms, wait_for_topbar

pytestmark = pytest.mark.browser


def _open_editor(page: Page, ui_server: str):
    page.goto(f"{ui_server}/settings/menu")
    wait_for_topbar(page)
    editor = page.locator(".menu-editor")
    expect(editor).to_be_visible()
    return editor


def _group_row(editor):
    """A group with at least two rows that can be hidden: (group id, its section, first row label)."""
    for section in editor.locator(".menu-editor__section[data-group]").all():
        rows = section.locator(".menu-editor__row:has(button.btn--secondary)")
        if rows.count() >= 2:
            label = rows.first.locator(".menu-editor__label").inner_text().strip()
            return section.get_attribute("data-group"), label
    raise AssertionError("no group with two rows that can be hidden")


def _menu_labels(page: Page, gid: str) -> list[str]:
    links = page.locator(f"nav.sidebar .sidebar-group[data-group='{gid}'] .sidebar-group-items a.nav-link")
    return [s.strip() for s in links.all_inner_texts()]


def _row(editor, label: str):
    return editor.locator(".menu-editor__row", has=editor.page.locator(".menu-editor__label", has_text=label)).first


def test_hide_move_and_reset_show_in_the_side_menu_on_the_same_page(page: Page, ui_server, fresh_company):
    editor = _open_editor(page, ui_server)
    gid, label = _group_row(editor)
    before = _menu_labels(page, gid)
    assert label in before, before

    _row(editor, label).locator("button.btn--secondary").click()
    expect(_row(editor, label).locator(".menu-editor__badge")).to_be_visible()
    expect(page.locator(f"nav.sidebar .sidebar-group[data-group='{gid}'] a.nav-link", has_text=label)).to_have_count(0)

    _row(editor, label).locator("button.btn--secondary").click()
    expect(page.locator(f"nav.sidebar .sidebar-group[data-group='{gid}'] a.nav-link", has_text=label)).to_have_count(1)
    assert _menu_labels(page, gid) == before

    _row(editor, label).locator(".menu-editor__move button").nth(1).click()  # down
    expect(_row(editor, label).locator(".menu-editor__move button").first).to_be_enabled()
    moved = _menu_labels(page, gid)
    assert moved.index(label) == before.index(label) + 1, (before, moved)

    # The redrawn side menu still folds its groups.
    group = page.locator(f"nav.sidebar .sidebar-group[data-group='{gid}']")
    was_open = "sidebar-group--open" in (group.get_attribute("class") or "")
    group.locator(".sidebar-group-arrow").click()
    assert ("sidebar-group--open" in (group.get_attribute("class") or "")) is not was_open

    answer_confirms(page)
    page.locator(".menu-editor button", has_text="Reset").last.click()
    expect(page.locator(f"nav.sidebar .sidebar-group[data-group='{gid}'] a.nav-link").first).to_be_visible()
    page.wait_for_function(
        "([gid, want]) => [...document.querySelectorAll(`nav.sidebar .sidebar-group[data-group='${gid}'] .sidebar-group-items a.nav-link`)]"
        ".map(a => a.innerText.trim()).join('|') === want", arg=[gid, "|".join(before)])


def test_a_change_saved_elsewhere_is_kept_when_the_page_saves(page: Page, ui_server, fresh_company):
    """Another tab hides one entry after this page loaded; hiding a second here keeps both."""
    api = fresh_company
    editor = _open_editor(page, ui_server)
    gid, label = _group_row(editor)
    section = editor.locator(f".menu-editor__section[data-group='{gid}']")
    other = section.locator(".menu-editor__row:has(button.btn--secondary)").nth(1)
    other_id, other_label = other.get_attribute("data-entry"), other.locator(".menu-editor__label").inner_text().strip()
    current = api.get("/companies/me/nav-overrides")
    r = api.patch("/companies/me/nav-overrides", json={"hidden_entries": [other_id]},
                  headers={"If-Match": current.headers["etag"]})
    assert r.status_code == 200, r.text

    _row(editor, label).locator("button.btn--secondary").click()
    expect(_row(editor, label).locator(".menu-editor__badge")).to_be_visible()
    expect(_row(editor, other_label).locator(".menu-editor__badge")).to_be_visible()
    shown = _menu_labels(page, gid)
    assert label not in shown and other_label not in shown, shown


def test_the_menu_button_opens_the_side_menu_after_a_save_on_a_phone(page: Page, ui_server, fresh_company):
    page.set_viewport_size({"width": 390, "height": 844})
    editor = _open_editor(page, ui_server)
    gid, label = _group_row(editor)
    _row(editor, label).locator("button.btn--secondary").click()
    expect(_row(editor, label).locator(".menu-editor__badge")).to_be_visible()
    _row(editor, label).locator("button.btn--secondary").click()
    expect(_row(editor, label).locator(".menu-editor__badge")).to_have_count(0)

    page.locator(".sidebar-toggle").click()
    expect(page.locator("nav.sidebar.sidebar--open")).to_be_visible()
    expect(page.locator("nav.sidebar a.nav-link", has_text=label).first).to_be_visible()
