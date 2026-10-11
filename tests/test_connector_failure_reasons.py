# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Why a connector sync failed is shown in plain words on the connector's page, under
its status table, not as a hover title: the raw error text a sync stored is mapped to a
plain reason with what to do, with a generic reason for anything unrecognised. The
Failed badge points to those reasons, and so does the summary on the connectors list."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fasthtml.common import to_xml

from ui.i18n import t
from ui.routes.settings_connectors import (
    _connector_status_view,
    _last_sync_info,
    sync_failure_reason,
)


def _r(**kw):
    base = dict(finished_at=datetime.now(timezone.utc), status="failed",
                created_count=0, updated_count=0, entity="products", errors=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_access_refused_says_to_connect_again():
    raw = "Shopify API error: Client error '401 Unauthorized' for url 'https://x.myshopify.com/admin'"
    reason = sync_failure_reason(raw, "Shopify")
    assert "Shopify refused" in reason and "Disconnect" in reason and "Web Access" in reason
    assert "401" not in reason and "myshopify" not in reason


def test_unreachable_says_to_check_the_connection():
    reason = sync_failure_reason("Unexpected error: ReadTimeout('timed out')", "Xero")
    assert "couldn't reach Xero" in reason and "Sync now" in reason


def test_rate_limited_says_to_wait():
    assert "Wait a few minutes" in sync_failure_reason("Xero API error: 429 Too Many Requests", "Xero")


def test_record_errors_keep_the_record_and_explain_the_cause():
    reason = sync_failure_reason("SKU ABC-1: Server error '503 Service Unavailable'", "WooCommerce")
    assert reason.startswith("SKU ABC-1: ")
    assert "WooCommerce had a problem on its side" in reason


def test_known_runner_errors_map_to_plain_reasons():
    assert "already running" in sync_failure_reason("products sync already in progress", "Shopify")
    assert "direction" in sync_failure_reason("orders sync blocked by direction=outbound", "Shopify")
    assert "Update Celerp" in sync_failure_reason("Update Celerp to continue syncing Xero.", "Xero")


def test_unrecognised_errors_get_the_generic_reason_without_the_raw_text():
    reason = sync_failure_reason("Unexpected error: KeyError('line_items')", "Shopify")
    assert reason == t("connectors.fail_generic", service="Shopify")
    assert "KeyError" not in reason and "GitHub" in reason


def test_reasons_follow_the_page_language():
    raw = "Shopify API error: 401 Unauthorized"
    assert sync_failure_reason(raw, "Shopify", "th") == t("connectors.fail_access", "th", service="Shopify")
    assert sync_failure_reason(raw, "Shopify", "th") != sync_failure_reason(raw, "Shopify", "en")


def test_status_view_lists_reasons_inline_and_the_badge_points_to_them():
    errs = ["Shopify API error: 401 Unauthorized", "Unexpected error: KeyError('x')"]
    out = to_xml(_connector_status_view("shopify", {"products": _r(errors=errs)}))
    assert 'id="connector-sync-reasons"' in out
    assert "Shopify refused" in out
    assert t("connectors.fail_generic", service="Shopify") in out
    assert 'href="#connector-sync-reasons"' in out
    assert "title=" not in out and "KeyError" not in out


def test_no_reasons_section_after_a_clean_sync():
    out = to_xml(_connector_status_view("shopify", {"products": _r(status="success", created_count=3)}))
    assert "connector-sync-reasons" not in out


def test_connectors_list_summary_links_a_failed_sync_to_its_reasons():
    out = to_xml(_last_sync_info(_r(errors=["boom"]), href="/settings/connectors/shopify"))
    assert 'href="/settings/connectors/shopify#connector-sync-reasons"' in out
    ok = to_xml(_last_sync_info(_r(status="success", created_count=1), href="/settings/connectors/shopify"))
    assert "connector-sync-reasons" not in ok
