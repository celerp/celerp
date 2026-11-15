"""Each mechanical help content check flags exactly its defect fixture and nothing on the good set,
so a check whose rule is deleted fails here even while every shipped page is valid."""
from __future__ import annotations

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
    "check_images": {"missing-image.json", "unmarked-image.json"},
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


def test_every_check_is_exercised():
    defined = {n for n in dir(checks) if n.startswith("check_")}
    assert defined == set(EXPECTED) | {"check_screen_help"}
