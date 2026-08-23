# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the Web Access connectors tab.

The connectors settings surface builds every user-facing label, how-it-works step,
entity name, sync-status word, and error message by calling ``t()`` at render time,
so a request in a non-English language gets translated output. These tests prove it
by registering a sentinel language ``xx`` and asserting the sentinel text reaches
the rendered output while ``xx`` is active. They are red against a tree that resolves
any of these strings at import time or hardcodes English.

Each conversion mechanism used in settings_connectors.py is covered at least once:
a module-level step-key dict, a module-level entity-label dict, a raw-enum display
label, and an interpolated error string.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

from fasthtml.common import to_xml

from ui import i18n
from ui.routes.settings_connectors import (
    _connector_card,
    _entity_status_table,
    _last_sync_info,
    _validate_platform,
)

# Sentinel catalog: one unmistakable value per mechanism the connectors tab renders.
_XX = {
    "connectors.how_shopify_1": "XX_STEP1",              # module-level step-key dict
    "connectors.entity_products": "XX_PRODUCTS",         # module-level entity-label dict
    "connectors.consumer_key_placeholder": "XX_CONSUMER_KEY",  # form placeholder
    "enum.sync_status.success": "XX_SUCCESS",            # raw-enum display label
    "connectors.unknown_connector": "XX_UNKNOWN {platform}",   # interpolated error
}


import pytest


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


def _card(cid, **kw):
    c = {"id": cid, "name": cid.title(), "description": "", "entities": ["products"],
         "connected": False, "category": "website"}
    c.update(kw)
    return to_xml(_connector_card(c, None, "https://relay.example", "iid", config=None, lang="xx"))


def test_how_it_works_step_translates():
    # Module-level step-key dict resolved at render time.
    assert "XX_STEP1" in _card("shopify", auth_type="oauth")


def test_entity_label_translates_in_status_table():
    # Module-level entity-label dict resolved at render time.
    run = SimpleNamespace(finished_at=datetime.now(timezone.utc), status="success",
                          created_count=1, updated_count=0, errors=None)
    out = to_xml(_entity_status_table({"products": run}, "xx"))
    assert "XX_PRODUCTS" in out


def test_consumer_key_placeholder_translates():
    # WooCommerce API-key form placeholder resolved at render time.
    assert "XX_CONSUMER_KEY" in _card("woocommerce", auth_type="apikey")


def test_sync_status_enum_translates():
    # Raw SyncRun status enum shown through an enum.sync_status.* display label.
    run = SimpleNamespace(finished_at=datetime.now(timezone.utc), status="success",
                          created_count=1, updated_count=0)
    assert "XX_SUCCESS" in to_xml(_last_sync_info(run))


def test_unknown_connector_error_interpolates():
    # Interpolated error string: sentinel value plus the platform substituted in.
    out = to_xml(_validate_platform("bogus-platform"))
    assert "XX_UNKNOWN" in out
    assert "bogus-platform" in out
