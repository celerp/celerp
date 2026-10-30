# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Shared real-database setup for connector tests: a company with its books, catalog
items, a connection set to given directions, and the connector services' own sessions
pointed at the test's savepoint-isolated session."""
from __future__ import annotations

import contextlib
import sys
import uuid
from types import SimpleNamespace

import pytest

from test_helpers import provision_company_books


def ungated_connector_takes(*paths: str):
    """The lock checker's ungated takes of a connection row or a first-sync record inside the
    block (``lock_rank.ungated_takes``)."""
    return sys.modules["lock_rank"].ungated_takes(("row:connector_configs", "row:connector_first_syncs"), *paths)


async def seed_company(session, name: str = "Store") -> uuid.UUID:
    """A company with an owner and its books, as provisioning gives every new company,
    committed as the signup that creates it is: connector work runs on companies that
    already exist, so it never shares a transaction with their creation."""
    from celerp.models.accounting import UserCompany
    from celerp.models.company import Company, User

    cid, uid = uuid.uuid4(), uuid.uuid4()
    session.add(User(
        id=uid, email=f"{name.lower()}-{uid.hex[:8]}@example.test", name=f"{name} Owner",
        auth_hash=None,
    ))
    await session.flush()
    session.add(Company(
        id=cid, name=name, slug=f"{name.lower()}-{cid.hex[:8]}", settings={"currency": "USD"},
    ))
    await session.flush()
    session.add(UserCompany(user_id=uid, company_id=cid, role="owner", is_active=True))
    await provision_company_books(session, cid)
    await session.commit()
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
    await session.commit()  # a person's create is its own request


async def item_state(session, company_id, entity_id: str) -> dict:
    from celerp.models.projections import Projection

    row = await session.get(
        Projection, {"company_id": company_id, "entity_id": entity_id}, populate_existing=True
    )
    return dict(row.state or {}) if row is not None else {}


async def connect(session, company_id, connector, directions: dict | None = None, *,
                  first_sync: str = "pending", **fields):
    """The company's connection to ``connector`` (a registered name or a connector),
    set to ``directions``, with its first-sync record in state ``first_sync``, committed
    as connecting is its own request."""
    from celerp.models.connector_config import ConnectorConfig

    config = ConnectorConfig(
        company_id=str(company_id), connector=getattr(connector, "name", connector),
        **({"entity_directions": directions} if directions is not None else {}), **fields,
    )
    session.add(config)
    await session.flush()
    await record_first_sync(session, config, first_sync, connector=connector)
    await session.commit()
    return config


async def connected(company_id, connector, directions: dict | None = None, *,
                    first_sync: str = "completed", **fields):
    """``connect`` through the app's own sessions, for tests that run
    connector work through its execution on the real database."""
    from celerp.db import get_session_ctx

    async with get_session_ctx() as session:
        return await connect(session, company_id, connector, directions, first_sync=first_sync, **fields)


def stand_in_connector(monkeypatch, prefix: str, *, like: str = "xero") -> str:
    """A registered connector of a name no other test uses, behaving as ``like``, so a
    test's connection never collides with another's."""
    from celerp.connectors import registry

    name = f"{prefix}-{uuid.uuid4().hex[:10]}"
    base = type(registry.get(like))
    monkeypatch.setitem(registry._registry, name, type(base.__name__, (base,), {"name": name})())
    return name


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


CONNECTOR_SESSION_TOKEN = "test-session-token-abc123"


@pytest.fixture
def connector_routes(connector_session, monkeypatch):
    """The /connectors routes as on an install signed in to Celerp Cloud: they need the
    X-Session-Token this yields, the relay gives a store context, and the connector
    services' own sessions are the test session."""
    from unittest.mock import AsyncMock

    import celerp.gateway.state as gw_state
    from celerp.connectors.base import ConnectorContext

    async def _context(company_id, connector_name, **_kwargs):
        return ConnectorContext(company_id=str(company_id), access_token="tok", store_handle="test.myshopify.com")

    monkeypatch.setattr("celerp.connectors.relay_token.fetch_context", AsyncMock(side_effect=_context))
    gw_state.set_session_token(CONNECTOR_SESSION_TOKEN)
    yield CONNECTOR_SESSION_TOKEN
    gw_state.set_session_token("")


def stand_in_execution(monkeypatch, ctx=None):
    """Run connector work against the test's session without a lease: the stand-in
    reads the connection and its first-sync record and gives the real eligibility
    answer (ExecutionRefused when it refuses), then yields an execution carrying
    ``ctx`` as the relay's context, or the context the relay gives when ``ctx`` is
    None. For unit tests of what runs inside an execution."""
    import sqlalchemy as sa

    from celerp import connectors
    from celerp.connectors import execution
    from celerp.models.connector_config import ConnectorConfig

    @contextlib.asynccontextmanager
    async def _execution(company_id, connector, purpose, resources, **_):
        from celerp.db import get_session_ctx

        if isinstance(connector, str):
            connector = connectors.get(connector)
        async with get_session_ctx() as session:
            config = await session.scalar(sa.select(ConnectorConfig).where(
                ConnectorConfig.company_id == str(company_id), ConnectorConfig.connector == connector.name))
            first_sync = config.first_sync if config is not None else None
            for resource in resources:
                reason = execution.eligible(connector, config, first_sync, purpose, resource)
                if reason is not None:
                    raise execution.ExecutionRefused(reason)
        ex = SimpleNamespace(company_id=str(company_id), connector=connector.name, purpose=purpose,
                             resources=list(resources), ctx=ctx)
        if ex.ctx is None:
            ex.ctx = await execution._credentials(ex)
        yield ex

    monkeypatch.setattr(execution, "connector_execution", _execution)
