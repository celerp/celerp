# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Shared real-database setup for connector tests: a company with its books, catalog
items, a connection set to given directions, and the connector services' own sessions
pointed at the test's savepoint-isolated session."""
from __future__ import annotations

import contextlib
import uuid

import pytest

from test_helpers import provision_company_books


async def seed_company(session, name: str = "Store") -> uuid.UUID:
    """A company with an owner and its books, as provisioning gives every new company."""
    from celerp.models.accounting import UserCompany
    from celerp.models.company import Company, User

    cid, uid = uuid.uuid4(), uuid.uuid4()
    session.add(Company(
        id=cid, name=name, slug=f"{name.lower()}-{cid.hex[:8]}", settings={"currency": "USD"},
    ))
    session.add(User(
        id=uid, email=f"{name.lower()}-{uid.hex[:8]}@example.test", name=f"{name} Owner",
        auth_hash=None,
    ))
    await session.flush()
    session.add(UserCompany(user_id=uid, company_id=cid, role="owner", is_active=True))
    await provision_company_books(session, cid)
    return cid


async def add_item(session, company_id, entity_id: str, sku: str, **fields) -> None:
    """One catalog item, as a person would create it."""
    from celerp.events.engine import emit_event

    await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="item",
        event_type="item.created",
        data={"sku": sku, "name": fields.pop("name", sku), "sell_by": "piece", **fields},
        actor_id=None, location_id=None, source="api",
        idempotency_key=f"create:{entity_id}", metadata_={},
    )
    await session.flush()


async def item_state(session, company_id, entity_id: str) -> dict:
    from celerp.models.projections import Projection

    row = await session.get(
        Projection, {"company_id": company_id, "entity_id": entity_id}, populate_existing=True
    )
    return dict(row.state or {}) if row is not None else {}


async def connect(session, company_id, connector: str, directions: dict | None = None, **fields):
    """The company's connection to ``connector``, set to ``directions``."""
    from celerp.models.connector_config import ConnectorConfig

    config = ConnectorConfig(
        company_id=str(company_id), connector=connector,
        **({"entity_directions": directions} if directions is not None else {}), **fields,
    )
    session.add(config)
    await session.flush()
    return config


@pytest.fixture
def connector_session(session, monkeypatch):
    """Point the connector services' own SessionLocal and get_session_ctx at the test session."""
    @contextlib.asynccontextmanager
    async def _same():
        yield session

    monkeypatch.setattr("celerp.db.SessionLocal", _same)
    monkeypatch.setattr("celerp.db.get_session_ctx", _same)
    return session
