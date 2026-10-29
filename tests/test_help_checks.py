"""Each mechanical help content check flags exactly its defect fixture and nothing on the good set,
so a check whose rule is deleted fails here even while every shipped page is valid."""
from __future__ import annotations

from pathlib import Path

import pytest

from celerp.help import checks

FIXTURES = Path(__file__).parent / "fixtures" / "help"
MODULE = "celerp-inventory"
# The bad set leaves one nav entry of the fixture module without a page.
NAV = {MODULE: ["/inventory", "/inventory?status=sold", "/settings/inventory"]}
GOOD_NAV = {MODULE: ["/inventory", "/inventory?status=sold"]}

EXPECTED = {
    "check_schema": {"placeholder.json", "per-locale.json"},
    "check_label_keys": {"unknown-label.json"},
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


def test_coverage_check_flags_uncovered_nav_entry():
    messages = checks.check_module_coverage(_pages("bad"), modules=NAV)
    assert len(messages) == 1 and "/settings/inventory" in messages[0]
    assert checks.check_module_coverage(_pages("good"), modules=GOOD_NAV) == []
    # An empty page set covers nothing.
    assert checks.check_module_coverage([], modules=GOOD_NAV)


def test_every_check_is_exercised():
    defined = {n for n in dir(checks) if n.startswith("check_")}
    assert defined == set(EXPECTED) | {"check_module_coverage"}
