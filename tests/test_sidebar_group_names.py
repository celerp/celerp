# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The sidebar's group headings read in the user's language, like the items under them."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fasthtml.common import to_xml

from ui.components.shell import _sidebar, module_nav

_LOCALES = Path(__file__).resolve().parents[1] / "ui" / "locales"
_LANGS = sorted(p.stem for p in _LOCALES.glob("*.json"))
_HEADER = re.compile(r'class="sidebar-group-header">\s*(?:<a [^>]*>(?:⚙️ )?([^<]*)</a>|<span>([^<]*)</span>)')


def _groups() -> set[str]:
    return {item["group"] for item in module_nav(None) if item.get("group")}


def test_every_group_the_sidebar_shows_has_a_name_in_every_language():
    groups = _groups()
    assert {"Sales Documents", "Purchasing Documents", "Inventory", "Manufacturing",
            "Subscriptions", "Contacts"} <= groups, groups
    for lang in _LANGS:
        catalog = json.loads((_LOCALES / f"{lang}.json").read_text(encoding="utf-8"))
        missing = [g for g in groups if f"nav.group.{g.lower().replace(' ', '_')}" not in catalog]
        assert not missing, (lang, missing)


@pytest.mark.parametrize("lang", [lang for lang in _LANGS if lang != "en"])
def test_the_group_headings_are_translated(lang):
    catalog = json.loads((_LOCALES / f"{lang}.json").read_text(encoding="utf-8"))
    xml = to_xml(_sidebar("dashboard", lang=lang, role="owner", request=None, settings={}))
    shown = {a or b for a, b in _HEADER.findall(xml)}
    assert shown, xml
    expected = {catalog[f"nav.group.{g.lower().replace(' ', '_')}"] for g in _groups()
                if f"nav.group.{g.lower().replace(' ', '_')}" in catalog}
    assert shown == expected, (lang, shown, expected)
    if lang == "de":
        assert not shown & {"Sales Documents", "Purchasing Documents", "Subscriptions", "Contacts"}, shown
