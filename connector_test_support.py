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


async def connect(session, company_id, connector, directions: dict | None = None, *,
                  first_sync: str = "pending", **fields):
    """The company's connection to ``connector`` (a registered name or a connector),
    set to ``directions``, with its first-sync record in state ``first_sync``."""
    from celerp.models.connector_config import ConnectorConfig

    config = ConnectorConfig(
        company_id=str(company_id), connector=getattr(connector, "name", connector),
        **({"entity_directions": directions} if directions is not None else {}), **fields,
    )
    session.add(config)
    await session.flush()
    await record_first_sync(session, config, first_sync, connector=connector)
    return config


async def connected(company_id, connector, directions: dict | None = None, *,
                    first_sync: str = "completed", **fields):
    """``connect``, committed through the app's own sessions, for tests that run
    connector work through its execution on the real database."""
    from celerp.db import get_session_ctx

    async with get_session_ctx() as session:
        config = await connect(session, company_id, connector, directions, first_sync=first_sync, **fields)
        await session.commit()
    return config


async def record_first_sync(session, config, state: str, *, connector=None) -> None:
    """Give ``config`` a first-sync record in ``state``; every resource of its
    directions completed when the record is."""
    from celerp import connectors
    from celerp.connectors.execution import plan_resources
    from celerp.models.connector_first_sync import ConnectorFirstSync

    if connector is None or isinstance(connector, str):
        connector = connectors.get(config.connector)
    progress = ({r: {"state": "completed"} for r in plan_resources(
        connector, config.entity_directions)} if state == "completed" else {})
    session.add(ConnectorFirstSync(company_id=str(config.company_id), connector=config.connector,
                                   state=state, progress=progress))
    await session.flush()
    await session.refresh(config, ["first_sync"])


@pytest.fixture
def connector_session(session, monkeypatch):
    """Point the connector services' own SessionLocal and get_session_ctx at the test session."""
    @contextlib.asynccontextmanager
    async def _same():
        yield session

    monkeypatch.setattr("celerp.db.SessionLocal", _same)
    monkeypatch.setattr("celerp.db.get_session_ctx", _same)
    return session
