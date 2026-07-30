# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for the connector live-status helpers: in-progress detection, aggregate status,
the self-re-triggering polling fragment (load delay present only while running / forced),
the per-entity status table, and the latest-run-per-entity DB read."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fasthtml.common import to_xml

from ui.routes.settings_connectors import (
    _any_in_progress,
    _connector_status_view,
    _entity_runs,
    _entity_status_table,
    _overall_status,
)


def _r(**kw):
    base = dict(finished_at=datetime.now(timezone.utc), status="success",
                created_count=1, updated_count=0, entity="products", errors=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_any_in_progress():
    assert _any_in_progress({"products": _r(finished_at=None)})
    assert not _any_in_progress({"products": _r()})
    assert not _any_in_progress({})


def test_overall_status_precedence():
    assert _overall_status({"a": _r(status="success"), "b": _r(status="failed")}) == "failed"
    assert _overall_status({"a": _r(status="success"), "b": _r(status="partial")}) == "partial"
    assert _overall_status({"a": _r(status="success")}) == "success"


def test_status_view_polls_only_while_in_progress():
    running = to_xml(_connector_status_view("shopify", {"products": _r(finished_at=None, status="running")}))
    assert "load delay:2s" in running
    assert "/settings/connectors/shopify/status?polling=1" in running
    # terminal -> no trigger, polling stops
    done = to_xml(_connector_status_view("shopify", {"products": _r()}))
    assert "load delay" not in done
    # force_poll re-enables one poll right after kicking off a sync
    forced = to_xml(_connector_status_view("shopify", {"products": _r()}, force_poll=True))
    assert "load delay:2s" in forced


def test_entity_status_table_rows_and_empty():
    assert "Never synced" in to_xml(_entity_status_table({}))
    out = to_xml(_entity_status_table({"products": _r(created_count=1200, updated_count=3)}))
    assert "Products" in out and "+1,200 ~3" in out


@pytest.mark.asyncio
async def test_entity_runs_returns_latest_per_entity(_db_engine):
    from celerp.db import get_session_ctx
    from celerp.models.sync_run import SyncRun

    cid = f"co-{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)
    async with get_session_ctx() as s:
        s.add_all([
            SyncRun(company_id=cid, connector="shopify", entity="products", started_at=now - timedelta(minutes=5), finished_at=now - timedelta(minutes=5),
                    created_count=1, updated_count=0, skipped_count=0, errors_json=None, status="success"),
            SyncRun(company_id=cid, connector="shopify", entity="products", started_at=now, finished_at=now,
                    created_count=9, updated_count=0, skipped_count=0, errors_json=None, status="success"),
            SyncRun(company_id=cid, connector="shopify", entity="orders", started_at=now, finished_at=now,
                    created_count=2, updated_count=0, skipped_count=0, errors_json=None, status="success"),
        ])
        await s.commit()
    runs = await _entity_runs(cid, "shopify")
    assert set(runs) == {"products", "orders"}
    assert runs["products"].created_count == 9  # latest, not the older run


@pytest.mark.asyncio
async def test_clear_connector_config_removes_row(_db_engine):
    """Disconnect's cleanup deletes the ConnectorConfig (stored secret/webhook-ids +
    direction/frequency) so a later reconnect starts clean."""
    from ui.routes.settings_connectors import (
        _clear_connector_config,
        _ensure_connector_config,
        _get_connector_config,
    )

    cid = f"co-{uuid.uuid4().hex[:10]}"
    await _ensure_connector_config(cid, "woocommerce", "website")
    assert await _get_connector_config(cid, "woocommerce") is not None
    await _clear_connector_config(cid, "woocommerce")
    assert await _get_connector_config(cid, "woocommerce") is None


def test_entitlement_cta_renders_trial_link():
    """When connecting is blocked by no subscription, the CTA shows a start-trial
    link straight to the relay's subscribe flow (not a raw error, and not an
    extra hop through /settings/cloud)."""
    from fasthtml.common import to_xml
    from ui.routes.settings_connectors import _entitlement_cta
    out = to_xml(_entitlement_cta())
    assert "celerp.com/subscribe" in out
    assert "plan=cloud" in out
    assert "instance_id=" in out
    assert 'target="_blank"' in out
    assert "subscription" in out.lower()


# ── H3: broker-supplied OAuth authorize_url validation ────────────────────────

def test_is_safe_authorize_url():
    """Only an https URL with no tag-breakout chars / non-web scheme is safe to
    open/inject (guards connector_oauth_redirect against a hostile broker payload)."""
    from ui.routes.settings_connectors import _is_safe_authorize_url
    assert _is_safe_authorize_url("https://shop.myshopify.com/admin/oauth/authorize?client_id=x") is True
    assert _is_safe_authorize_url("") is False
    assert _is_safe_authorize_url("http://evil.example/oauth") is False        # not https
    assert _is_safe_authorize_url("javascript:alert(1)") is False              # non-web scheme
    assert _is_safe_authorize_url("data:text/html,evil") is False              # non-web scheme
    assert _is_safe_authorize_url("https://x/</script><script>evil()</script>") is False  # tag breakout
    assert _is_safe_authorize_url("https://x/\x00abc") is False                # control char
