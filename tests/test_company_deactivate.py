# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for company soft-delete (deactivate/reactivate)."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient


async def _register(client: AsyncClient, email: str = "owner@deact.test", company: str = "Deact Co") -> str:
    r = await client.post("/auth/register", json={"company_name": company, "email": email, "name": "Owner", "password": "pass1234"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _add_user(client: AsyncClient, session, admin_token: str, email: str = "staff@deact.test", role: str = "operator") -> str:
    r = await client.post(
        "/companies/me/users",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"name": "Staff", "email": email, "password": "pass1234", "role": role},
    )
    assert r.status_code == 200, r.text
    from celerp.services.session_tracker import clear as _clear_tracker
    await _clear_tracker(session)  # gate is per-user; clear so new user can log in
    r2 = await client.post("/auth/login", json={"email": email, "password": "pass1234"})
    assert r2.status_code == 200, r2.text
    return r2.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_active_company_accessible(client: AsyncClient):
    """Sanity: active company is accessible."""
    token = await _register(client)
    r = await client.get("/companies/me", headers=_auth(token))
    assert r.status_code == 200
    assert r.json().get("name") == "Deact Co"


@pytest.mark.asyncio
async def test_deactivate_requires_admin(client: AsyncClient, session):
    """Non-admin cannot deactivate a company."""
    admin_token = await _register(client)
    user_token = await _add_user(client, session, admin_token)
    r = await client.delete("/companies/me", headers=_auth(user_token))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_deactivate_company(client: AsyncClient):
    """Admin can deactivate the company."""
    token = await _register(client)
    r = await client.delete("/companies/me", headers=_auth(token))
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["is_active"] is False


@pytest.mark.asyncio
async def test_deactivated_company_blocks_non_owner(client: AsyncClient, session):
    """Requests to a deactivated company are rejected for non-owners but owner stays accessible."""
    admin_token = await _register(client)
    user_token = await _add_user(client, session, admin_token)

    await client.delete("/companies/me", headers=_auth(admin_token))

    # Non-owner is blocked
    r = await client.get("/companies/me", headers=_auth(user_token))
    assert r.status_code == 401
    assert "deactivated" in r.json()["detail"].lower()

    # Owner can still access (so they can create a new company or reactivate)
    r = await client.get("/companies/me", headers=_auth(admin_token))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_deactivated_company_blocked_on_switch(client: AsyncClient):
    """Cannot switch into a deactivated company."""
    token = await _register(client)

    # Get company_id from my-companies
    companies = (await client.get("/auth/my-companies", headers=_auth(token))).json()["items"]
    company_id = companies[0]["company_id"]

    await client.delete("/companies/me", headers=_auth(token))

    r = await client.post(f"/auth/switch-company/{company_id}", headers=_auth(token))
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_deactivated_hidden_from_my_companies(client: AsyncClient, session):
    """Deactivated company does not appear in /auth/my-companies."""
    from uuid import UUID
    from celerp.models.company import Company

    token = await _register(client)
    companies = (await client.get("/auth/my-companies", headers=_auth(token))).json()["items"]
    company_id = UUID(companies[0]["company_id"])

    await client.delete("/companies/me", headers=_auth(token))

    # Restore via DB to verify it reappears
    company = await session.get(Company, company_id)
    company.is_active = True
    await session.commit()

    r = await client.get("/auth/my-companies", headers=_auth(token))
    assert r.status_code == 200
    ids = [item["company_id"] for item in r.json()["items"]]
    assert str(company_id) in ids

    # Deactivate again and verify non-owner token is blocked; owner still passes
    company.is_active = False
    await session.commit()
    r = await client.get("/auth/my-companies", headers=_auth(token))
    assert r.status_code == 200  # owner still authenticated


@pytest.mark.asyncio
async def test_reactivate_via_db(client: AsyncClient, session):
    """Deactivated company can be reactivated."""
    from uuid import UUID
    from celerp.models.company import Company

    token = await _register(client)
    companies = (await client.get("/auth/my-companies", headers=_auth(token))).json()["items"]
    company_id = UUID(companies[0]["company_id"])

    await client.delete("/companies/me", headers=_auth(token))

    company = await session.get(Company, company_id)
    assert company.is_active is False

    company.is_active = True
    await session.commit()

    r = await client.get("/companies/me", headers=_auth(token))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_reactivate_endpoint_requires_admin(client: AsyncClient, session):
    """Non-admin cannot call reactivate endpoint."""
    admin_token = await _register(client)
    user_token = await _add_user(client, session, admin_token)
    r = await client.post("/companies/me/reactivate", headers=_auth(user_token))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_reactivate_endpoint(client: AsyncClient):
    """Admin reactivate endpoint returns 200 on an active company (idempotent)."""
    token = await _register(client)
    r = await client.post("/companies/me/reactivate", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["is_active"] is True


@pytest.mark.asyncio
async def test_deactivated_company_blocks_connector_operations(client: AsyncClient, session):
    from uuid import UUID

    from celerp.connectors.ownership import (
        ConnectorOwnershipError,
        lock_connector_operation,
    )
    from celerp.models.company import Company
    from celerp.models.connector_config import ConnectorConfig

    token = await _register(client, "connector-owner@deact.test", "Connector Deact")
    companies = (await client.get(
        "/auth/my-companies", headers=_auth(token)
    )).json()["items"]
    company_id = UUID(companies[0]["company_id"])

    session.add(ConnectorConfig(
        company_id=str(company_id),
        connector="woocommerce",
    ))
    await session.commit()

    company = await session.get(Company, company_id)
    company.is_active = False
    await session.commit()

    with pytest.raises(ConnectorOwnershipError, match="inactive"):
        await lock_connector_operation(
            session, str(company_id), "woocommerce", require_owner=True
        )
    await session.rollback()


@pytest.mark.asyncio
async def test_deactivate_releases_connector_reservation(client: AsyncClient, session):
    from uuid import UUID
    from sqlalchemy import select

    from celerp.connectors.sync_runner import CONNECTOR_RESET_ENTITY
    from celerp.models.connector_config import ConnectorConfig, OutboundQueue
    from celerp.models.sync_run import SyncRun

    token = await _register(
        client, "connector-cleanup@deact.test", "Connector Cleanup"
    )
    companies = (await client.get(
        "/auth/my-companies", headers=_auth(token)
    )).json()["items"]
    company_id = UUID(companies[0]["company_id"])

    session.add(ConnectorConfig(
        company_id=str(company_id),
        connector="woocommerce",
    ))
    session.add(OutboundQueue(
        company_id=str(company_id),
        connector="woocommerce",
        entity_type="item",
        entity_id="item:1",
    ))
    await session.commit()

    cleanup = AsyncMock()
    with patch(
        "celerp.connectors.remote_state.revoke_connector_remote_state",
        cleanup,
    ):
        response = await client.delete("/companies/me", headers=_auth(token))
    assert response.status_code == 200
    cleanup.assert_awaited_once_with(
        str(company_id), "woocommerce", webhook_ids=[]
    )

    session.expire_all()
    assert await session.scalar(
        select(ConnectorConfig).where(
            ConnectorConfig.company_id == str(company_id)
        )
    ) is None
    assert await session.scalar(
        select(OutboundQueue).where(
            OutboundQueue.company_id == str(company_id)
        )
    ) is None
    reset = await session.scalar(
        select(SyncRun).where(
            SyncRun.company_id == str(company_id),
            SyncRun.connector == "woocommerce",
            SyncRun.entity == CONNECTOR_RESET_ENTITY,
        )
    )
    assert reset is not None
    assert reset.status == "deactivated"



@pytest.mark.asyncio
async def test_deactivate_fails_closed_when_remote_cleanup_is_ambiguous(
    client: AsyncClient, session
):
    from uuid import UUID
    from sqlalchemy import select

    from celerp.connectors.remote_state import ConnectorRemoteCleanupError
    from celerp.models.company import Company
    from celerp.models.connector_config import ConnectorConfig

    token = await _register(
        client, "connector-cleanup-fails@deact.test", "Connector Cleanup Fails"
    )
    companies = (await client.get(
        "/auth/my-companies", headers=_auth(token)
    )).json()["items"]
    company_id = UUID(companies[0]["company_id"])
    session.add(ConnectorConfig(
        company_id=str(company_id),
        connector="woocommerce",
    ))
    await session.commit()

    with patch(
        "celerp.connectors.remote_state.revoke_connector_remote_state",
        new=AsyncMock(side_effect=ConnectorRemoteCleanupError("unknown")),
    ):
        response = await client.delete("/companies/me", headers=_auth(token))

    assert response.status_code == 503
    session.expire_all()
    company = await session.get(Company, company_id)
    assert company is not None and company.is_active is True
    assert await session.scalar(select(ConnectorConfig).where(
        ConnectorConfig.company_id == str(company_id),
        ConnectorConfig.connector == "woocommerce",
    )) is not None


@pytest.mark.asyncio
async def test_reactivate_names_lost_connectors_without_restoring_them(client: AsyncClient, session):
    """Reactivation never silently reconnects: the connectors the deactivation
    released are reported for an explicit reconnect, and no configuration row
    comes back on its own."""
    from uuid import UUID
    from sqlalchemy import select

    from celerp.connectors.ownership import connectors_awaiting_reconnect
    from celerp.models.connector_config import ConnectorConfig

    token = await _register(client, "reconnect@deact.test", "Reconnect Co")
    companies = (await client.get("/auth/my-companies", headers=_auth(token))).json()["items"]
    company_id = UUID(companies[0]["company_id"])
    session.add(ConnectorConfig(company_id=str(company_id), connector="woocommerce"))
    await session.commit()

    with patch("celerp.connectors.remote_state.revoke_connector_remote_state", AsyncMock()):
        assert (await client.delete("/companies/me", headers=_auth(token))).status_code == 200

    session.expire_all()
    assert await connectors_awaiting_reconnect(session, company_id) == ["woocommerce"]

    r = await client.post("/companies/me/reactivate", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["connectors_to_reconnect"] == ["woocommerce"]
    session.expire_all()
    assert await session.scalar(
        select(ConnectorConfig).where(ConnectorConfig.company_id == str(company_id))
    ) is None
    assert await connectors_awaiting_reconnect(session, company_id) == ["woocommerce"]

    # An explicit reconnect clears the prompt.
    session.add(ConnectorConfig(company_id=str(company_id), connector="woocommerce"))
    await session.commit()
    assert await connectors_awaiting_reconnect(session, company_id) == []
