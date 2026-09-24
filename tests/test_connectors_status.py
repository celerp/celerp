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
async def test_release_connector_ownership_clears_work_and_resets_cursor(_db_engine):
    import sqlalchemy as sa
    from celerp.connectors.ownership import (
        ConnectorOwnershipError,
        claim_connector_ownership,
        lock_connector_operation,
        release_connector_ownership,
    )
    from celerp.db import get_session_ctx
    from celerp.models.company import Company
    from celerp.models.connector_config import OutboundQueue
    from celerp.models.sync_run import SyncRun

    company_uuid = uuid.uuid4()
    cid = str(company_uuid)
    connector = f"release-{uuid.uuid4().hex[:10]}"
    async with get_session_ctx() as session:
        session.add(Company(
            id=company_uuid,
            name="Connector Release Co",
            slug=f"connector-release-{company_uuid.hex[:8]}",
            settings={},
        ))
        await session.flush()
        await claim_connector_ownership(
            session, cid, connector, default_sync_frequency="realtime"
        )
        session.add(OutboundQueue(
            company_id=cid, connector=connector, entity_type="inventory",
            entity_id="10", status="pending", retry_count=0,
        ))
        await session.flush()

        await release_connector_ownership(session, cid, connector)
        await session.flush()

        assert await session.scalar(sa.select(sa.func.count()).select_from(OutboundQueue).where(
            OutboundQueue.company_id == cid,
            OutboundQueue.connector == connector,
        )) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(SyncRun).where(
            SyncRun.company_id == cid,
            SyncRun.connector == connector,
            SyncRun.entity == "__connector_reset__",
        )) == 1
        with pytest.raises(ConnectorOwnershipError):
            await lock_connector_operation(session, cid, connector)
        await session.rollback()


def test_entitlement_cta_renders_trial_link():
    """When connecting is blocked by no subscription, the CTA shows a start-trial
    link to the same-origin in-app mint route (not a raw error, and not an extra
    hop through /settings/cloud). The route resolves the destination and mints the
    single-use handoff token server-side at click, so the pre-click URL carries no
    named instance and no celerp.com host."""
    from fasthtml.common import to_xml
    from ui.routes.settings_connectors import _entitlement_cta
    out = to_xml(_entitlement_cta())
    assert "/commercial/checkout" in out
    assert "intent=subscribe" in out
    assert "sku=cloud" in out
    assert 'target="_blank"' in out
    # Fail-closed: no instance_id leaks and no cross-host celerp.com handoff is
    # baked into the pre-click URL.
    assert "instance_id=" not in out
    assert "celerp.com" not in out
    assert "subscription" in out.lower()


# ── H3: broker-supplied OAuth authorize_url validation ────────────────────────

def test_is_safe_authorize_url():
    """Only an https URL with no tag-breakout chars / non-web scheme is safe to
    open/inject (guards connector_oauth_redirect against a hostile broker payload)."""
    from ui.security import is_safe_authorize_url
    assert is_safe_authorize_url("https://shop.myshopify.com/admin/oauth/authorize?client_id=x") is True
    assert is_safe_authorize_url("") is False
    assert is_safe_authorize_url("http://evil.example/oauth") is False        # not https
    assert is_safe_authorize_url("javascript:alert(1)") is False              # non-web scheme
    assert is_safe_authorize_url("data:text/html,evil") is False              # non-web scheme
    assert is_safe_authorize_url("https://x/</script><script>evil()</script>") is False  # tag breakout
    assert is_safe_authorize_url("https://x/\x00abc") is False                # control char


@pytest.mark.asyncio
async def test_get_connector_config_adopts_legacy_instance_row(_db_engine):
    from unittest.mock import patch
    import sqlalchemy as sa

    from celerp.db import get_session_ctx
    from celerp.models.company import Company
    from celerp.models.connector_config import ConnectorConfig
    from ui.routes.settings_connectors import _get_connector_config

    company_uuid = uuid.uuid4()
    company_id = str(company_uuid)
    legacy_id = f"inst-{uuid.uuid4().hex[:10]}"
    try:
        async with get_session_ctx() as session:
            session.add(Company(
                id=company_uuid,
                name="Legacy Connector Co",
                slug=f"legacy-connector-{company_uuid.hex[:8]}",
                settings={},
            ))
            session.add(ConnectorConfig(
                company_id=legacy_id,
                connector="woocommerce",
                sync_frequency="realtime",
            ))
            await session.commit()
        with patch("celerp.config.ensure_instance_id", return_value=legacy_id), \
             patch("celerp.connectors.ownership.ensure_instance_id", return_value=legacy_id):
            cfg = await _get_connector_config(company_id, "woocommerce")
            assert cfg is not None
            assert cfg.company_id == company_id
            assert await _get_connector_config(company_id, "woocommerce") is not None
    finally:
        async with get_session_ctx() as session:
            await session.execute(
                sa.delete(ConnectorConfig).where(
                    ConnectorConfig.company_id.in_([company_id, legacy_id])
                )
            )
            await session.execute(sa.delete(Company).where(Company.id == company_uuid))
            await session.commit()


@pytest.mark.asyncio
async def test_connector_claim_rejects_different_company_owner(_db_engine):
    from celerp.connectors.ownership import ConnectorOwnershipError, claim_connector_ownership
    from celerp.db import get_session_ctx
    from celerp.models.company import Company
    from celerp.models.connector_config import ConnectorConfig

    company_a_uuid = uuid.uuid4()
    company_b_uuid = uuid.uuid4()
    company_a = str(company_a_uuid)
    company_b = str(company_b_uuid)
    connector = f"owner-reject-{uuid.uuid4().hex[:10]}"
    async with get_session_ctx() as session:
        session.add_all([
            Company(
                id=company_a_uuid,
                name="Connector Owner A",
                slug=f"connector-owner-a-{company_a_uuid.hex[:8]}",
                settings={},
            ),
            Company(
                id=company_b_uuid,
                name="Connector Owner B",
                slug=f"connector-owner-b-{company_b_uuid.hex[:8]}",
                settings={},
            ),
            ConnectorConfig(
                company_id=company_a,
                connector=connector,
                direction="both",
            ),
        ])
        await session.flush()

        with pytest.raises(ConnectorOwnershipError):
            await claim_connector_ownership(session, company_b, connector)
        assert await claim_connector_ownership(
            session, company_a, connector
        ) is not None
        await session.rollback()


@pytest.mark.asyncio
async def test_connector_ownership_merges_legacy_operational_state(_db_engine):
    import json
    from unittest.mock import patch
    import sqlalchemy as sa

    from celerp.connectors.ownership import claim_connector_ownership
    from celerp.db import get_session_ctx
    from celerp.models.company import Company
    from celerp.models.connector_config import ConnectorConfig

    company_uuid = uuid.uuid4()
    company_id = str(company_uuid)
    legacy_id = f"inst-{uuid.uuid4().hex[:10]}"
    connector = f"ownership-merge-{uuid.uuid4().hex[:10]}"
    async with get_session_ctx() as session:
        session.add_all([
            Company(
                id=company_uuid,
                name="Connector Legacy Merge Co",
                slug=f"connector-legacy-merge-{company_uuid.hex[:8]}",
                settings={},
            ),
            ConnectorConfig(
                company_id=company_id, connector=connector,
                webhook_ids_json=json.dumps(["11"]), webhook_secret=None,
                direction="inbound",
            ),
            ConnectorConfig(
                company_id=legacy_id, connector=connector,
                webhook_ids_json=json.dumps(["12"]), webhook_secret="legacy-secret",
                direction="both",
            ),
        ])
        await session.flush()

        with patch("celerp.connectors.ownership.ensure_instance_id", return_value=legacy_id):
            row = await claim_connector_ownership(session, company_id, connector)
            await session.flush()
            assert row is not None

        rows = (await session.execute(sa.select(ConnectorConfig).where(
            ConnectorConfig.connector == connector,
            ConnectorConfig.company_id.in_([company_id, legacy_id]),
        ))).scalars().all()
        assert len(rows) == 1
        assert rows[0].company_id == company_id
        assert set(rows[0].webhook_ids) == {"11", "12"}
        assert rows[0].webhook_secret == "legacy-secret"
        await session.rollback()


def test_pending_oauth_connector_exposes_disconnect():
    from types import SimpleNamespace
    from fasthtml.common import to_xml
    from ui.routes.settings_connectors import _connector_card

    card = _connector_card(
        {
            "id": "quickbooks",
            "name": "QuickBooks",
            "category": "accounting",
            "auth_type": "oauth",
            "connected": False,
            "entities": [],
        },
        None,
        "https://relay.example",
        "company-1",
        config=SimpleNamespace(direction="both", sync_frequency="manual"),
    )
    html = to_xml(card)
    assert '/settings/connectors/quickbooks/oauth-redirect' in html
    assert 'hx-delete="/settings/connectors/quickbooks/disconnect"' in html


def test_pending_apikey_connector_exposes_disconnect():
    from types import SimpleNamespace
    from fasthtml.common import to_xml
    from ui.routes.settings_connectors import _connector_card

    card = _connector_card(
        {
            "id": "woocommerce",
            "name": "WooCommerce",
            "category": "website",
            "auth_type": "api_key",
            "connected": False,
            "entities": [],
        },
        None,
        "https://relay.example",
        "company-1",
        config=SimpleNamespace(direction="both", sync_frequency="realtime"),
    )
    html = to_xml(card)
    assert '/settings/connectors/woocommerce/connect-apikey' in html
    assert 'hx-delete="/settings/connectors/woocommerce/disconnect"' in html


@pytest.mark.asyncio
async def test_needs_plan_keeps_owned_connector_disconnect_visible():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    from fasthtml.common import to_xml
    from ui.routes.settings_connectors import connectors_tab_content

    config = SimpleNamespace(
        connector="quickbooks",
        direction="both",
        sync_frequency="manual",
    )
    with patch(
        "ui.routes.settings_connectors._fetch_catalog",
        new=AsyncMock(return_value=([], "plan required", True)),
    ), patch(
        "ui.routes.settings_connectors._owned_connector_configs",
        new=AsyncMock(return_value=[config]),
    ):
        html = to_xml(await connectors_tab_content(
            "en", "token", "accounting", "company-1"
        ))

    assert "quickbooks" in html.lower()
    assert 'hx-delete="/settings/connectors/quickbooks/disconnect"' in html
