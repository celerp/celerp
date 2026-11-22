"""Each mechanical help content check flags exactly its defect fixture and nothing on the good set,
so a check whose rule is deleted fails here even while every shipped page is valid."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from celerp.help import checks

FIXTURES = Path(__file__).parent / "fixtures" / "help"
MODULE = "celerp-inventory"
# Screens of the fixture module and the help page each names, over the good page set.
GOOD_SCREENS = {MODULE: [("/inventory", "inventory.add-item"), ("/inventory?status=sold", "inventory.add-item")]}
BAD_SCREENS = {
    MODULE: [
        ("/inventory", "inventory.add-item"),
        ("/settings/inventory", None),             # names no page
        ("/inventory/import", "inventory.gone"),   # names a page nobody ships
        ("/docs?type=invoice", "inventory.add-item"),  # names a page that does not list the screen
    ],
    "celerp-docs": [("/inventory", "inventory.add-item")],  # names another module's page
}

EXPECTED = {
    "check_schema": {"placeholder.json", "per-locale.json"},
    "check_label_keys": {"unknown-label.json", "label-with-blank.json"},
    "check_routes": {"unknown-route.json"},
    "check_permissions": {"unknown-permission.json"},
    "check_images": {"missing-image.json", "null-image.json", "unmarked-image.json"},
}


def _pages(kind: str) -> list[checks.PageFile]:
    return checks.collect(MODULE, FIXTURES / kind)


def _reported(messages: list[str]) -> set[str]:
    return {m.split(":", 1)[0] for m in messages}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_check_flags_its_fixture(name):
    check = getattr(checks, name)
    assert _reported(check(_pages("bad"))) == EXPECTED[name]
    assert check(_pages("good")) == []


def test_screen_help_check_flags_each_screen_without_its_page():
    messages = checks.check_screen_help(_pages("good"), modules=BAD_SCREENS)
    assert sorted(m.split(":", 2)[1].strip() for m in messages) == [
        "/docs?type=invoice", "/inventory", "/inventory/import", "/settings/inventory"]
    assert all(m.startswith(("celerp-inventory:", "celerp-docs:")) for m in messages)
    assert checks.check_screen_help(_pages("good"), modules=GOOD_SCREENS) == []
    # An empty page set resolves nothing.
    assert checks.check_screen_help([], modules=GOOD_SCREENS)


# Signed-in screens of the app and the content hash of each screen's handlers.
SCREENS = {
    "/inventory": {"handlers": ["ui.routes.inventory.inventory_page"], "digest": "a" * 64},
    "/settings/inventory": {"handlers": ["ui.routes.settings_inventory.settings_page"], "digest": "b" * 64},
}


def test_screen_coverage_names_each_screen_no_page_covers():
    assert checks.check_screen_coverage(_pages("good"), screens=SCREENS) == [
        "/settings/inventory: no help page covers this screen"]
    assert checks.check_screen_coverage(_pages("good"), screens={"/inventory": SCREENS["/inventory"]}) == []


def test_page_audit_flags_a_page_that_covers_no_screen():
    other = {"/settings/inventory": SCREENS["/settings/inventory"]}
    assert _reported(checks.check_covers_screen(_pages("good"), screens=other)) == {"add-item.json"}
    assert checks.check_covers_screen(_pages("good"), screens=SCREENS) == []
    # A route naming a tab or a type covers the screen its path serves.
    page = _pages("good")[0]
    tabbed = checks.PageFile(path=page.path, data={**page.data, "routes": ["/settings/inventory?tab=units"]},
                             module=page.module, help_dir=page.help_dir)
    assert checks.check_covers_screen([tabbed], screens=SCREENS) == []
    assert checks.check_screen_coverage([tabbed], screens=SCREENS) == ["/inventory: no help page covers this screen"]


def _stamped(screens) -> list[checks.PageFile]:
    page = _pages("good")[0]
    return [checks.PageFile(path=page.path, data={**page.data, "checked": checks.page_checked(page.data, screens)},
                            module=page.module, help_dir=page.help_dir)]


def test_checked_stamp_flags_a_missing_or_stale_value():
    assert _reported(checks.check_checked(_pages("good"), screens=SCREENS)) == {"add-item.json"}
    assert checks.check_checked(_stamped(SCREENS), screens=SCREENS) == []
    changed = {**SCREENS, "/inventory": {**SCREENS["/inventory"], "digest": "c" * 64}}
    stale = checks.check_checked(_stamped(SCREENS), screens=changed)
    assert len(stale) == 1 and stale[0].startswith("add-item.json:")
    assert "ui.routes.inventory.inventory_page" in stale[0] and "stamp inventory.add-item" in stale[0]
    # A screen the page does not cover never re-opens it.
    unrelated = {**SCREENS, "/settings/inventory": {**SCREENS["/settings/inventory"], "digest": "d" * 64}}
    assert checks.check_checked(_stamped(SCREENS), screens=unrelated) == []


def test_stamp_writes_the_value_once_in_place():
    text = (FIXTURES / "good" / "add-item.json").read_text(encoding="utf-8")
    once = checks.stamp_text(text, "e" * 64)
    assert json.loads(once) == {**json.loads(text), "checked": "e" * 64}
    again = checks.stamp_text(once, "f" * 64)
    assert json.loads(again)["checked"] == "f" * 64 and again.count('"checked"') == 1


def test_every_check_is_exercised():
    # Every check_* in checks.py, so a new one fails here until its own test exists somewhere.
    defined = {n for n in dir(checks) if n.startswith("check_")}
    # check_screen_help/_coverage/_covers_screen/_checked need a screens map, not the good/bad fixtures;
    # check_ledger/check_translations need a real ledger and catalogs, not a page fixture. Each has its
    # own test group here or in test_help_export.py / test_help_translations.py.
    assert defined == set(EXPECTED) | {"check_screen_help", "check_screen_coverage", "check_covers_screen",
                                       "check_checked", "check_ledger", "check_translations"}
