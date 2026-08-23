# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the global search results partial.

The search partial resolves each result's status label at render time via the
shared ``display_enum`` helper (raw status -> ``enum.<domain>.<raw>`` -> translated
label), so a request in a non-English language gets a translated status chip. This
test proves that by registering a sentinel language ``xx`` and asserting the
sentinel text reaches the rendered chip while ``xx`` is active. It is red against a
tree that title-cases the raw status instead of resolving it through ``t()``.
"""

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.routes.search import _status_tag

# Sentinel catalog: one unmistakable value for a status the search bar renders.
_XX = {
    "enum.item_status.sold": "XX_SOLD_STATUS",
}


@pytest.fixture(autouse=True)
def _xx_lang():
    """Register the sentinel language, make it active, and reset both the registry
    and the context language afterwards so nothing leaks between tests."""
    i18n.clear_registry()
    i18n._cached_load.cache_clear()
    i18n.register_catalog("xx", _XX)
    i18n.set_lang("xx")
    yield
    i18n.set_lang("en")
    i18n.clear_registry()
    i18n._cached_load.cache_clear()


def test_status_tag_translates_enum_label():
    """A status with a catalog entry renders its translated label in the chip."""
    html = to_xml(_status_tag("sold", "item_status"))
    assert "XX_SOLD_STATUS" in html
    assert "search-result-status" in html


def test_status_tag_unknown_value_falls_back():
    """A raw status with no catalog entry still renders readable text (display-only
    fallback), never a raw slug or the bare key."""
    html = to_xml(_status_tag("merged", "item_status"))
    assert "Merged" in html
    assert "enum.item_status" not in html


def test_status_tag_empty_status_renders_no_chip():
    """An empty status produces no chip at all."""
    assert _status_tag("", "item_status") == ""
