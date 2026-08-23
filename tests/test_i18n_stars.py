# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the star badge-claim result page.

``ui/routes/stars.py`` builds the claim-result page (and its success-body
interpolation) by calling ``t()`` inside the render helpers, so a request in a
non-English language gets translated output. These tests register a sentinel
language ``xx`` (via the module i18n seam) and assert the sentinel text reaches
the rendered output while ``xx`` is the active language. They are red against a
tree that hardcodes the English copy instead of calling ``t()`` at render time.
"""

import pytest

from ui import i18n
from ui.routes.stars import _claim_result_page, _claim_success_body

# Sentinel catalog: one unmistakable value per key the claim-result page renders.
_XX = {
    "stars.back_to_celerp": "XX_BACK_TO_CELERP",
    "stars.claim_success_body": "XX_RECOGNISED {label}",
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


def test_claim_result_page_translates_back_link():
    out = _claim_result_page("Some title", "Some body")
    assert "XX_BACK_TO_CELERP" in out
    assert "Some title" in out
    assert "Some body" in out


def test_claim_success_body_translates_with_interpolation():
    out = _claim_success_body("Ada")
    assert out == "XX_RECOGNISED <strong>Ada</strong>"


def test_claim_result_page_embeds_translated_success_body():
    body = _claim_success_body("Ada")
    out = _claim_result_page("Thank you", body)
    assert "XX_RECOGNISED" in out
    assert "<strong>Ada</strong>" in out
