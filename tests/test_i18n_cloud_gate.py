# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the shared cloud-gate / upgrade-banner UI.

``upgrade_banner``, ``digest_upsell_modal``, and ``cloud_gate`` build their copy
by calling ``t()`` at render time (including the default CTA price, which used
to be a literal frozen into the function signature at import time), so a
request in a non-English language gets translated output. These tests register
a sentinel language ``xx`` (via the module i18n seam) and assert the sentinel
text reaches the rendered output. They are red against a tree that hardcodes
English for the digest-upsell copy, the "Continue on your own plan" button, or
the default CTA price.
"""

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.components.cloud_gate import upgrade_banner, digest_upsell_modal, cloud_gate

# Sentinel catalog: one unmistakable value per key this component renders.
# msg.29mo is an existing key (reused, not newly minted here) but is exercised
# through a new render-time code path (the price default used to be a literal
# baked into the function signature), so it is covered too.
_XX = {
    "cloud.requires_celerp_cloud": "XX_REQUIRES_CONNECT",
    "cloud.start_trial": "XX_START_TRIAL",
    "msg.29mo": "XX_29MO",
    "cloud.digest_upsell_feature": "XX_DIGEST_FEATURE",
    "cloud.digest_upsell_desc": "XX_DIGEST_DESC",
    "btn.continue_on_own_plan": "XX_CONTINUE_OWN_PLAN",
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


def test_upgrade_banner_translates_title_and_cta():
    html = to_xml(upgrade_banner("Encrypted Backup", "Keeps your data safe.", lang="xx"))
    assert "XX_REQUIRES_CONNECT" in html
    assert "XX_START_TRIAL" in html


def test_upgrade_banner_default_price_resolves_at_render():
    """The default price used to be a literal frozen into the function
    signature at import time; it must now resolve through t() at render."""
    html = to_xml(upgrade_banner("Encrypted Backup", "Keeps your data safe.", lang="xx"))
    assert "XX_29MO" in html


def test_upgrade_banner_explicit_price_is_not_translated():
    """An explicitly supplied price is caller data, not UI copy - it must pass
    through untouched rather than being looked up as a translation key."""
    html = to_xml(upgrade_banner("Encrypted Backup", "Keeps your data safe.",
                                  price="USD $9/mo", lang="xx"))
    assert "USD $9/mo" in html
    assert "XX_29MO" not in html


def test_digest_upsell_modal_translates_feature_desc_and_button():
    html = to_xml(digest_upsell_modal(lang="xx"))
    assert "XX_DIGEST_FEATURE" in html
    assert "XX_DIGEST_DESC" in html
    assert "XX_CONTINUE_OWN_PLAN" in html


def test_cloud_gate_passes_lang_through_to_banner():
    html = to_xml(cloud_gate(False, "Encrypted Backup", "Keeps your data safe.", lang="xx"))
    assert "XX_REQUIRES_CONNECT" in html
    assert "XX_29MO" in html
