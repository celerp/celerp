# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the subscriptions routes.

The subscription list/detail helpers build every user-facing label, status
badge, frequency label, table header, and control tooltip by calling ``t()`` (or
``display_enum()``) at render time, so a request in a non-English language gets
translated output. These tests register a sentinel language ``xx`` and assert
its text reaches the rendered output while ``xx`` is active. They are red against
a tree that hardcodes the English strings.
"""

import pytest

from fasthtml.common import Div, to_xml

from ui import i18n
from ui.routes.subscriptions import (
    _status_badge,
    _sub_table,
    _sub_status_cards,
    _schedule_inputs,
    _sub_action_controls,
)

# Sentinel catalog: one unmistakable value per mechanism the routes render.
_XX = {
    # enum display labels (display_enum -> enum.<domain>.<raw>)
    "enum.subscription_status.active": "XX_ACTIVE",
    "enum.subscription_status.draft": "XX_DRAFT",
    "enum.subscription_frequency.monthly": "XX_MONTHLY",
    # plain reused-key label rendered in output
    "status.all_issued": "XX_ALLISSUED",
    # interpolated label
    "subscriptions.docs_generated": "XX_GENERATED {n}",
    # attribute (title/placeholder) labels
    "th.frequency": "XX_FREQTITLE",
    "subscriptions.custom_interval_title": "XX_CUSTOMTITLE",
}


@pytest.fixture(autouse=True)
def _xx_lang():
    """Register the sentinel language, make it active, and reset the registry and
    context language afterwards so nothing leaks between tests."""
    i18n.clear_registry()
    i18n._cached_load.cache_clear()
    i18n.register_catalog("xx", _XX)
    i18n.set_lang("xx")
    yield
    i18n.set_lang("en")
    i18n.clear_registry()
    i18n._cached_load.cache_clear()


def test_status_badge_translates_enum_label():
    # Mechanism: display_enum status label.
    assert "XX_ACTIVE" in to_xml(_status_badge("active"))


def test_sub_table_translates_status_and_frequency():
    # Mechanisms: enum status badge + enum frequency label in a table row.
    row = {"id": "sub:1", "frequency": "monthly", "status": "active"}
    html = to_xml(_sub_table([row], "sales"))
    assert "XX_MONTHLY" in html
    assert "XX_ACTIVE" in html


def test_status_cards_translate_labels():
    # Mechanisms: enum card label + plain reused-key label ("All Issued").
    html = to_xml(_sub_status_cards([{"status": "active"}], "", "sales"))
    assert "XX_ALLISSUED" in html
    assert "XX_ACTIVE" in html


def test_schedule_inputs_translate_options_and_tooltips():
    # Mechanisms: enum option label + attribute (title) labels.
    html = to_xml(Div(*_schedule_inputs("doc:1", {"frequency": "monthly"})))
    assert "XX_MONTHLY" in html
    assert "XX_FREQTITLE" in html
    assert "XX_CUSTOMTITLE" in html


def test_generated_count_translates_with_interpolation():
    # Mechanism: interpolated label ("Generated: {n}").
    left, _right = _sub_action_controls(
        "doc:1", {"status": "active", "generated_doc_ids": ["a", "b", "c"]}
    )
    html = to_xml(Div(*left))
    assert "XX_GENERATED 3" in html
