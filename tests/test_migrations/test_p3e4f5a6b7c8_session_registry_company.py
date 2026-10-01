# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The session company revision is the one head. It clears sessions that cannot be
attributed to a company and signs everyone out, ties every new session to its company so
removing the company ends them, and reverses cleanly."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from company_backup_support import company, member, owner
from migration_support import OWNER_EMAIL, auth, real_client  # noqa: F401

from .conftest import fresh_db, run_migration_ops  # noqa: F401

MODULE = "p3e4f5a6b7c8_session_registry_company"


def test_revision_is_the_single_head_after_import_operation_key():
    from alembic.script import ScriptDirectory

    from celerp.alembic_config import build_alembic_config

    script = ScriptDirectory.from_config(build_alembic_config())
    assert script.get_heads() == ["p3e4f5a6b7c8"]
    assert script.get_revision("p3e4f5a6b7c8").down_revision == "o2d3e4f5a6b7"
    ids = [r.revision for r in script.walk_revisions()]
    assert len(ids) == len(set(ids)), "a revision id is declared twice"


@pytest.fixture()
def sessions_db():
    base_url = os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2")
    schema = f"migsess_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()
    engine = create_engine(base_url, connect_args={"options": f"-csearch_path={schema}"})
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE companies (id UUID PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE session_registry (jti VARCHAR(64) PRIMARY KEY, user_id UUID NOT NULL, "
                          "expiry TIMESTAMPTZ NOT NULL)"))
        conn.execute(text("CREATE TABLE user_auth_state (user_id UUID PRIMARY KEY, nonce VARCHAR(64) NOT NULL, "
                          "evicted_by_ip VARCHAR(64))"))
    yield engine
    engine.dispose()
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


def _later():
    return datetime.now(timezone.utc) + timedelta(hours=1)


def test_session_company_upgrade_and_downgrade(sessions_db):
    a, b = uuid.uuid4(), uuid.uuid4()
    with sessions_db.begin() as conn:
        conn.execute(text("INSERT INTO companies (id) VALUES (:a), (:b)"), {"a": a, "b": b})
        conn.execute(text("INSERT INTO session_registry (jti, user_id, expiry) VALUES ('old', :u, :e)"),
                     {"u": uuid.uuid4(), "e": _later()})
        conn.execute(text("INSERT INTO user_auth_state (user_id, nonce, evicted_by_ip) "
                          "VALUES (:u, 'n1', '10.0.0.1'), (:v, 'n2', NULL)"), {"u": uuid.uuid4(), "v": uuid.uuid4()})

    run_migration_ops(sessions_db, MODULE)
    with sessions_db.begin() as conn:
        # A session from before cannot be attributed to a company and is cleared, and
        # every login's sign-in generation is renewed.
        assert conn.execute(text("SELECT count(*) FROM session_registry")).scalar_one() == 0
        states = conn.execute(text("SELECT nonce, evicted_by_ip FROM user_auth_state")).all()
        assert len({n for n, _ in states} - {"n1", "n2"}) == 2 and {ip for _, ip in states} == {None}
        for jti, cid in (("on-a", a), ("on-b", b)):
            conn.execute(text("INSERT INTO session_registry (jti, user_id, company_id, expiry) "
                              "VALUES (:j, :u, :c, :e)"), {"j": jti, "u": uuid.uuid4(), "c": cid, "e": _later()})
        conn.execute(text("DELETE FROM companies WHERE id = :a"), {"a": a})
        assert list(conn.execute(text("SELECT jti FROM session_registry")).scalars()) == ["on-b"]
    with pytest.raises(IntegrityError):
        with sessions_db.begin() as conn:
            conn.execute(text("INSERT INTO session_registry (jti, user_id, expiry) VALUES ('none', :u, :e)"),
                         {"u": uuid.uuid4(), "e": _later()})

    run_migration_ops(sessions_db, MODULE, "downgrade")
    with sessions_db.connect() as conn:
        cols = set(conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'session_registry'")).scalars())
    assert "company_id" not in cols


@pytest.fixture()
def real_engine(fresh_db, monkeypatch):  # noqa: F811
    """The API's engine on a throwaway database that starts empty and is migrated by the test."""
    import celerp.db
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(fresh_db[0], poolclass=NullPool)
    monkeypatch.setattr(celerp.db, "engine", engine)
    yield engine


def _migrate_to(fresh, revision: str) -> None:
    from alembic import command

    from celerp.alembic_config import build_alembic_config

    os.environ["DATABASE_URL"] = fresh[0]
    command.upgrade(build_alembic_config(), revision)


async def _sign_in_before_the_upgrade(engine, user_id, company_id, role: str) -> tuple[str, str]:
    """An access and refresh token pair as the release before this one issued them, with
    its session registered the way that release registered it."""
    from celerp.services.auth import create_access_token, create_refresh_token, validate_access_token
    from celerp.services.session_tracker import get_nonce
    from migration_support import maker

    async with maker(engine)() as s:
        snonce = await get_nonce(s, str(user_id))
        access, jti = create_access_token(str(user_id), str(company_id), role, snonce=snonce)
        await s.execute(text("INSERT INTO session_registry (jti, user_id, expiry) VALUES (:j, :u, :e)"),
                        {"j": jti, "u": user_id, "e": _later()})
        await s.commit()
        assert (await validate_access_token(s, access)).user.id == user_id
    return access, create_refresh_token(str(user_id), str(company_id), snonce=snonce)


async def test_upgrading_signs_everyone_out_and_then_lets_one_person_sign_in(fresh_db, real_engine,  # noqa: F811
                                                                            real_client, monkeypatch):
    from fastapi import HTTPException

    from celerp.services.auth import validate_access_token
    from celerp.services.session_tracker import active_user_ids
    from migration_support import maker

    _migrate_to(fresh_db, "o2d3e4f5a6b7")
    boss = await owner(real_engine)
    a = await company(real_engine, boss, "Harbor Goods Ltd", "alpha")
    worker = await owner(real_engine, "member@example.com", "Worker")
    await member(real_engine, worker, a, "admin")
    old = [await _sign_in_before_the_upgrade(real_engine, boss, a, "owner"),
           await _sign_in_before_the_upgrade(real_engine, worker, a, "admin")]

    _migrate_to(fresh_db, "p3e4f5a6b7c8")

    async with maker(real_engine)() as s:
        assert await active_user_ids(s) == set()
        for access, _ in old:
            with pytest.raises(HTTPException) as rejected:
                await validate_access_token(s, access)
            assert rejected.value.status_code == 401
    for access, refresh in old:
        assert (await real_client.get("/companies/me", headers=auth(access))).status_code == 401
        assert (await real_client.post("/auth/token/refresh", json={"refresh_token": refresh})).status_code == 401

    # Without the relay one person may be signed in: of two signing in now, one gets in.
    monkeypatch.setattr("celerp.gateway.state.get_session_token", lambda: None)
    results = [await real_client.post("/auth/login", json={"email": email, "password": password})
               for email, password in ((OWNER_EMAIL, "ownerpw123"), ("member@example.com", "userpw1234"))]
    assert [r.status_code for r in results] == [200, 409], [r.text for r in results]
    assert results[1].json()["detail"] == "direct_connection_limit"
    me = await real_client.get("/companies/me", headers=auth(results[0].json()["access_token"]))
    assert me.status_code == 200 and me.json()["id"] == str(a)
    async with maker(real_engine)() as s:
        assert await active_user_ids(s) == {str(boss)}
