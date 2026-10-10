# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The search syntax panel explains field carry-over, exact identifier fields, `all:`,
the scan flow and the Not found line, in every shipped language."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fasthtml.common import to_xml

from ui.components.shell import search_help

_LOCALES = Path(__file__).resolve().parent.parent / "ui" / "locales"

# Every key the identifier-aware search added, in the help panel and on the list.
_NEW_KEYS = (
    "shell.search_help_carry", "shell.search_help_carry_result",
    "shell.search_help_exact", "shell.search_help_exact_result",
    "shell.search_help_all", "shell.search_help_all_result",
    "shell.search_help_scan", "shell.search_help_scan_result",
    "shell.search_help_not_found", "shell.search_help_not_found_result",
    "shell.search_help_example_scan_result",
    "inventory.search_not_found",
    "inventory.search_exact_barcode", "inventory.search_exact_rfid_epc",
    "inventory.search_exact_gtin", "inventory.search_exact_sku",
)


def _catalog(lang: str) -> dict:
    return json.loads((_LOCALES / f"{lang}.json").read_text(encoding="utf-8"))


def test_every_new_key_in_all_twelve_catalogs():
    """Red statement: none of these keys existed before the change."""
    langs = sorted(p.stem for p in _LOCALES.glob("*.json"))
    assert len(langs) == 12
    for lang in langs:
        cat = _catalog(lang)
        missing = [k for k in _NEW_KEYS if not cat.get(k, "").strip()]
        assert not missing, f"{lang} is missing {missing}"
        assert not any("—" in cat[k] for k in _NEW_KEYS), f"{lang} has an em dash"


@pytest.mark.parametrize("lang", ["en", "th"])
def test_search_help_renders_new_rows(lang):
    """Red statement: the panel had no carry-over, exact, all:, scan or Not found rows
    and no scan example."""
    cat = _catalog(lang)
    xml = to_xml(search_help(lang))
    for code in ("barcode: 1042, 1043", "barcode: 1042", "barcode: 1042, all: ring",
                 "barcode: 1042, 1099", "barcode: 1042, 1043, 1044, 1045, 1046"):
        assert f"<code>{code}</code>" in xml
    for k in _NEW_KEYS:
        if k.startswith("shell."):
            assert cat[k] in xml, k


def test_scoped_and_or_rows_no_longer_claim_a_single_field():
    """Red statement: the scoped row said a field matches only that field and every
    text field matched any part; the OR row said nothing about the carried field."""
    en = _catalog("en")
    assert "matches only that field" not in en["shell.search_help_scoped"]
    assert "whole value" in en["shell.search_help_scoped"]
    assert "field" in en["shell.search_help_or"]
