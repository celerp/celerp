# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Shared test utilities importable from any test location.

This file lives at the repo root. It is on the pytest pythonpath (pyproject.toml:
  pythonpath = [".", ".."]) so any test file can import it directly:

    from test_helpers import make_test_token, authed_cookies, REPO_ROOT

tests/helpers.py is a shim that re-exports from here for backward compatibility.
"""
from __future__ import annotations

import asyncio
import base64
import gc
import json
import os
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI

from celerp.modules.loader import _BUNDLED_MODULES_DIRS, is_core_folded, load_all, register_api_routes
from celerp.routers import search as search_router_mod

# Repo root: this file lives at repo root, so parent == repo root.
REPO_ROOT = Path(__file__).resolve().parent

# An upload racing a Delete retries its write in a savepoint, and SQLAlchemy reports that
# the item row it flushes replaces the one its session held. Expected in those race tests.
UPLOAD_DELETE_RACE_WARNINGS = pytest.mark.filterwarnings(
    "ignore:New instance <Projection at .* conflicts with persistent instance:sqlalchemy.exc.SAWarning",
    "ignore:Identity map already had an identity for .*Projection:sqlalchemy.exc.SAWarning",
)

# The root conftest always sets DATABASE_URL to the test Postgres before this is
# imported; the default is just a sane placeholder for the Postgres-only app.
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql+asyncpg://celerp:celerp@localhost:5432/celerp")

# The Windows account names the release build runs the packaged app under (build.yml
# win-names), one per way a name can break: scripts outside the ANSI code page, a
# 20-character name, a supplementary-plane character, accents the code page holds, and
# characters Windows maps to look-alike ASCII. Tests of user text in file names and
# downloads run over the same list.
ACCOUNT_NAMES = {
    "thai": "สมชาย",
    "thai-20": "สมชายใจดีมีทรัพย์มาก",
    "arabic": "محمد",
    "chinese": "张伟",
    "supplementary": "𠮷 Test",
    "accented": "José Müller",
    "fullwidth": "Ｊｏｓｅ Ｌｉｍａ",
    "arabic-indic-digits": "Ali ٣٤",
    "ascii": "Celerp Upgrade",
}

_crm_src = os.path.join(os.path.dirname(__file__), "..", "premium_modules", "celerp-sales-funnel")
_crm_available = os.path.isfile(os.path.join(_crm_src, "celerp_sales_funnel", "__init__.py"))


def mark_running(monkeypatch, *names: str) -> None:
    """Record *names* as loaded. The unit harness mounts module routes directly, so the
    loader would otherwise list none of them as running."""
    from celerp.modules import loader
    monkeypatch.setattr(loader, "_loaded", [*loader._loaded, *({"name": n, "version": "1.0.0"} for n in names)])


async def until(predicate, timeout: float = 10.0) -> None:
    """Wait for ``predicate`` (sync or async), polling; fails the test at ``timeout``."""
    async def _wait():
        while True:
            value = predicate()
            if asyncio.iscoroutine(value):
                value = await value
            if value:
                return
            await asyncio.sleep(0.01)
    await asyncio.wait_for(_wait(), timeout)


async def until_a_lock_waits(engine, timeout: float = 10.0) -> None:
    """Wait until some transaction of the test database waits on a lock. pg_stat_activity
    is a snapshot per transaction, so each poll reads on a connection of its own."""
    from sqlalchemy import text

    async def _waiting() -> bool:
        async with engine.connect() as conn:
            return bool((await conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND wait_event_type = 'Lock'"))).scalar_one())
    await until(_waiting, timeout)


async def settled_question(client, headers: dict, conversation_id: str, question_id) -> dict:
    """A thread question's state (GET .../questions/{id}) once its job has ended. The
    test's requests and the job share one connection, which runs one query at a time,
    so the state is read once the job's task has ended, never while it runs."""
    from celerp.ai import jobs
    await until(lambda: not jobs.task_alive(uuid.UUID(str(question_id))))
    r = await client.get(f"/ai/conversations/{conversation_id}/questions/{question_id}", headers=headers)
    assert r.status_code == 200, r.text
    state = r.json()
    assert state["state"] != "running", state
    return state


async def asked_in_thread(client, headers: dict, conversation_id: str, body: dict, run_agent=None) -> dict:
    """Send a thread question and wait for its job: the send answers 202, the
    question's assistant message is returned. ``run_agent`` (a mock) stands in for the
    agent run when given."""
    from contextlib import nullcontext
    from unittest.mock import patch
    with patch("celerp.ai.service.run_agent", run_agent) if run_agent is not None else nullcontext():
        r = await client.post(f"/ai/conversations/{conversation_id}/query", headers=headers, json=body)
        assert r.status_code == 202, r.text
        return (await settled_question(client, headers, conversation_id, r.json()["job_id"]))["message"]


async def drain_jobs(release=lambda: None) -> None:
    """Let every running AI job finish, then shut the runner down. A task cancelled in
    the middle of a query would leave the test's one shared connection unusable for
    its teardown, so jobs are drained, never cancelled, before the client and session
    go. ``release`` is called on every poll (a fake service lets held calls go)."""
    from celerp.ai import jobs

    def _drained() -> bool:
        release()
        return not jobs.running_tasks()
    try:
        await until(_drained)
    finally:
        await jobs.shutdown()


def make_test_token(
    role: str = "owner",
    user_id: str = "00000000-0000-0000-0000-000000000001",
    company_id: str = "00000000-0000-0000-0000-000000000002",
    modules: list[str] | None = None,
) -> str:
    """Create a minimal JWT cookie value with a decodable payload for UI role checks.

    NOT cryptographically signed - only used so that get_role() can decode
    the role claim from the base64 payload. Signature verification never runs in tests.
    """
    payload_dict: dict = {"sub": user_id, "company_id": company_id, "role": role, "exp": 9999999999}
    if modules is not None:
        payload_dict["modules"] = modules
    payload = json.dumps(payload_dict)
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()
    return f"header.{payload_b64}.sig"


def assert_not_permitted_redirect(response) -> None:
    """A page the caller's role may not open redirects to the dashboard and hands
    it the one-shot "no access" notice (ui.security.not_permitted_redirect)."""
    assert response.status_code == 302, response.status_code
    assert response.headers["location"] == "/dashboard"
    assert "celerp_notice=not_permitted" in response.headers.get("set-cookie", "")


def authed_cookies(role: str = "owner") -> dict:
    """Return cookies dict with a properly-formed test token for the given role."""
    return {"celerp_token": make_test_token(role=role)}


async def make_authed_token(session, user_id: str, company_id: str, role: str) -> str:
    """Mint a signed, DB-valid v2 access token for an already-seeded user.

    Fetches the user's current DB nonce, creating it as issuance does for a user
    that has none, so the token passes ``validate_access_token``'s nonce-equality
    check. Use this anywhere a test needs to call an authenticated endpoint as a
    specific seeded user without going through the login gate (which allows only
    one active session per user). The user, its ``UserCompany`` membership and the
    company must already exist.
    """
    from celerp.credentials import create_access_token
    from celerp.services.session_tracker import get_nonce, lock_auth_state
    snonce = await get_nonce(session, str(user_id))
    if not snonce:
        snonce = (await lock_auth_state(session, str(user_id))).nonce
        await session.commit()
    token, _ = create_access_token(str(user_id), str(company_id), role, snonce=snonce)
    return token


@asynccontextmanager
async def signed_request(session, company_id, user_id, role: str = "operator"):
    """Run the body as a request *user_id* signed with its own access token: the
    real get_auth_context dependency, entered on *session*."""
    from starlette.requests import Request

    from celerp.services.auth import get_auth_context
    token = await make_authed_token(session, str(user_id), str(company_id), role)
    request = Request({"type": "http", "method": "POST", "path": "/module/route", "headers": [],
                       "query_string": b""})
    dependency = get_auth_context(request, token, session)
    await dependency.__anext__()
    try:
        yield
    finally:
        await dependency.aclose()


async def provision_company_books(session, company_id) -> None:
    """Give a company created directly in a test what provisioning gives every new
    company: each module's starter data, including the chart of accounts and its
    posting accounts. Tests that post to the ledger need it."""
    from celerp.modules import slots

    await slots.fire_lifecycle("on_company_created", session=session, company_id=company_id)
    await session.flush()


TZ = "Pacific/Kiritimati"  # the timezone of the company company_auth creates


async def company_auth(session, cid, uid) -> dict:
    """A company with its books and an admin, and the admin's request headers."""
    from celerp.models.accounting import UserCompany
    from celerp.models.company import Company, User

    session.add(Company(id=cid, name="CostCo", slug=f"costco-{cid.hex[:8]}",
                        settings={"currency": "USD", "timezone": TZ}))
    session.add(User(id=uid, email=f"admin-{cid.hex[:8]}@test.co", name="Admin", auth_hash="x", is_active=True))
    await session.flush()
    session.add(UserCompany(id=uuid.uuid4(), user_id=uid, company_id=cid, role="admin", is_active=True))
    await provision_company_books(session, cid)
    await session.commit()
    token = await make_authed_token(session, str(uid), str(cid), "admin")
    return {"headers": {"Authorization": f"Bearer {token}"}, "company_id": cid, "user_id": uid}


async def ensure_user(session, user_id) -> None:
    """Insert a minimal users row if absent.

    Postgres enforces foreign keys (e.g. session_registry.user_id → users) that
    SQLite silently ignored, so tests that reference a synthetic user id must
    materialize it first.
    """
    import uuid as _uuid
    from celerp.models.company import User
    uid = _uuid.UUID(str(user_id))
    if await session.get(User, uid) is None:
        session.add(User(id=uid, email=f"u-{uid}@test.example", name="Test User"))
        await session.flush()


async def ensure_company(session, company_id=None):
    """Insert a minimal companies row (a sign-in session belongs to a company) and return its id."""
    import uuid as _uuid
    from celerp.models.company import Company
    cid = _uuid.UUID(str(company_id)) if company_id else _uuid.uuid4()
    if await session.get(Company, cid) is None:
        session.add(Company(id=cid, name="Test Co", slug=f"test-{cid.hex}"))
        await session.flush()
    return cid


async def seed_member(session, role: str = "operator", *, active: bool = True):
    """Insert a company, a user and their membership with ``role``; return (company_id, user_id)."""
    import uuid as _uuid
    from celerp.models.accounting import UserCompany
    company_id, user_id = await ensure_company(session), _uuid.uuid4()
    await ensure_user(session, user_id)
    session.add(UserCompany(user_id=user_id, company_id=company_id, role=role, is_active=active))
    await session.flush()
    return company_id, user_id


async def clear_sample_items(session, company_id) -> None:
    """Remove the sample items a new company starts with, so the test's next item
    import only adds items (an import that clears them cannot be undone)."""
    from celerp.services.demo import delete_untouched_demo_items
    await delete_untouched_demo_items(session, uuid.UUID(str(company_id)))
    await session.commit()


async def doc_version(client, headers: dict, doc_id: str) -> int:
    """The document's current version: a PATCH that replaces line_items names the version it read."""
    r = await client.get(f"/docs/{doc_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["version"]


async def default_location_id(client, headers: dict) -> str:
    """Return the company's real default (or first) location id.

    Postgres enforces ledger.location_id → locations, so tests must use a real
    location rather than a random UUID. Registration seeds a 'Head Office'.
    """
    r = await client.get("/companies/me/locations", headers=headers)
    items = r.json().get("items", [])
    for it in items:
        if it.get("is_default"):
            return it["id"]
    return items[0]["id"]


async def merge_items(client, *, json: dict, headers: dict | None = None):
    """Merge the way a user does: preview the merge, then confirm it with the preview's
    fingerprint. A refused preview leaves the merge to give its own refusal."""
    if "plan_fingerprint" not in json:
        preview = await client.post("/items/merge/preview", headers=headers, json=json)
        if preview.status_code == 200:
            json = {**json, "plan_fingerprint": preview.json()["plan_fingerprint"]}
    return await client.post("/items/merge", headers=headers, json=json)


async def sell_item(client, headers: dict, item_id: str, unit_price: float = 150.0) -> str:
    """Sell all of a lot the way a user does: an invoice for it, finalized, then
    fulfilled. A status edit cannot mark stock sold. Returns the invoice id."""
    r = await client.get(f"/items/{item_id}", headers=headers)
    assert r.status_code == 200, r.text
    item = r.json()
    qty = float(item.get("quantity") or 1)
    r = await client.post("/docs", headers=headers, json={"doc_type": "invoice", "total": unit_price * qty, "line_items": [
        {"entity_id": item_id, "sku": item.get("sku"), "name": item.get("name") or "Item", "quantity": qty,
         "unit_price": unit_price, "sell_by": item.get("sell_by") or "piece"}]})
    assert r.status_code == 200, r.text
    inv = r.json()["id"]
    for path, body in ((f"/docs/{inv}/finalize", {}), (f"/docs/{inv}/fulfill-lines", {"line_entity_ids": [item_id]})):
        r = await client.post(path, headers=headers, json=body)
        assert r.status_code == 200, r.text
    return inv


async def reserve_item(client, headers: dict, item_id: str, unit_price: float = 150.0) -> str:
    """Reserve a lot the way a user does: on a finalized invoice that names it. A status
    edit cannot reserve stock. Returns the invoice id."""
    r = await client.get(f"/items/{item_id}", headers=headers)
    assert r.status_code == 200, r.text
    item = r.json()
    qty = float(item.get("quantity") or 1)
    r = await client.post("/docs", headers=headers, json={"doc_type": "invoice", "total": unit_price * qty, "line_items": [
        {"entity_id": item_id, "sku": item.get("sku"), "name": item.get("name") or "Item", "quantity": qty,
         "unit_price": unit_price, "sell_by": item.get("sell_by") or "piece"}]})
    assert r.status_code == 200, r.text
    inv = r.json()["id"]
    for path, body in ((f"/docs/{inv}/finalize", {}),
                       (f"/docs/{inv}/reserve-lines", {"new_status": "reserved", "line_entity_ids": [item_id]})):
        r = await client.post(path, headers=headers, json=body)
        assert r.status_code == 200, r.text
    return inv


async def create_location(client, headers: dict, name: str = "Warehouse 2") -> str:
    """Create a real location and return its id (for transfer-target tests)."""
    r = await client.post("/companies/me/locations", headers=headers,
                          json={"name": name, "type": "warehouse"})
    return r.json()["id"]


async def import_sent_po(client, headers: dict, **data) -> str:
    """Seed a purchase order that was sent to its supplier elsewhere and imported.

    In the app a PO stays a draft until finalized, which turns it into a bill, so an
    outstanding PO only reaches the reports through import."""
    entity_id = f"doc:{uuid.uuid4()}"
    r = await client.post("/docs/import", headers=headers, json={
        "entity_id": entity_id, "event_type": "doc.created",
        "data": {"doc_type": "purchase_order", "status": "sent", "tax": 0, **data},
        "source": "import:test", "idempotency_key": f"po-{uuid.uuid4()}",
    })
    assert r.status_code == 200, r.text
    return entity_id


# ── Role/permission bootstrap ─────────────────────────────────────────────────

async def register_admin(client) -> str:
    """Register first-admin (creates owner role) and return access token."""
    r = await client.post(
        "/auth/register",
        json={"company_name": "Perm Co", "email": "admin@perm.example", "name": "Admin", "password": "adminpw12"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def invite_user(client, session, admin_headers: dict, email: str, role: str) -> str:
    """Create a user with the given role and return their access token."""
    from celerp.services.session_tracker import clear as _clear_tracker
    r = await client.post(
        "/companies/me/users",
        json={"email": email, "name": role.title(), "role": role, "password": "userpw123"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    await _clear_tracker(session)  # clear so the new user can log in
    r2 = await client.post("/auth/login", json={"email": email, "password": "userpw123"})
    assert r2.status_code == 200, r2.text
    return r2.json()["access_token"]


async def create_item(client, headers: dict, location_id: str, sku: str = "SKU-PERM") -> str:
    """Create an item as admin and return its entity_id."""
    r = await client.post(
        "/items",
        json={"sku": sku, "name": "Perm Item", "quantity": 5, "location_id": location_id, "cost_price": 100.0, "sell_by": "piece", "status": "available"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def perm_setup(client, session):
    """Bootstrap: admin token, manager token, operator token, location_id, item_id."""
    admin_tok = await register_admin(client)
    admin_h = {"Authorization": f"Bearer {admin_tok}"}

    loc_r = await client.post(
        "/companies/me/locations",
        json={"name": "Main", "type": "warehouse", "address": None, "is_default": True},
        headers=admin_h,
    )
    location_id = loc_r.json()["id"]
    item_id = await create_item(client, admin_h, location_id)

    manager_tok = await invite_user(client, session, admin_h, "mgr@perm.example", "manager")
    operator_tok = await invite_user(client, session, admin_h, "operator@perm.example", "operator")

    return {
        "admin_h":    {"Authorization": f"Bearer {admin_tok}"},
        "manager_h":  {"Authorization": f"Bearer {manager_tok}"},
        "staff_h":    {"Authorization": f"Bearer {operator_tok}"},   # alias for legacy tests
        "operator_h": {"Authorization": f"Bearer {operator_tok}"},
        "location_id": location_id,
        "item_id": item_id,
    }


async def grant_permission(client, admin_headers: dict, permission: str, role: str) -> None:
    """Set *permission* so exactly *role* and the roles above it hold it, through the
    owner-gated permissions matrix endpoint.

    Role grants are per-role now, so this helper reproduces the single threshold its
    callers rely on: it sets each role's cell to granted when the role is at or above
    *role* (clamped up to the permission's floor) and to revoked when it is below.
    That is the exact effect the old threshold override produced - every role above
    the line holds the permission and every role below it does not - so callers that
    "raise a key above operator" keep working unchanged. admin_headers must be an
    owner token (register_admin creates the owner); grants can only be written
    through this endpoint.
    """
    from celerp.services.auth import ROLE_LEVELS
    from celerp.services.permissions import PERMISSIONS

    perm = next(p for p in PERMISSIONS if p.key == permission)
    target = max(ROLE_LEVELS[role], ROLE_LEVELS[perm.floor_role])
    for role_key, level in ROLE_LEVELS.items():
        r = await client.patch(
            "/companies/me/role-permissions",
            json={"perm_key": permission, "role_key": role_key, "granted": level >= target},
            headers=admin_headers,
        )
        assert r.status_code == 200, r.text


def _bundled_pluggable_names() -> set[str]:
    """Every bundled default module the loader actually loads (core-folded
    ai/backup/connectors are wired at construction, never via load_all)."""
    root = _BUNDLED_MODULES_DIRS[0]
    return {
        p.name for p in root.iterdir()
        if p.is_dir() and (p / "__init__.py").exists() and not is_core_folded(p.name)
    }


def in_language(lang: str, detail) -> str:
    """A refusal as a reader in ``lang`` sees it."""
    from ui import i18n

    i18n.set_lang(lang)
    try:
        return i18n.refusal_text(detail)
    finally:
        i18n.set_lang("en")


def notice_in(lang: str, item: dict) -> dict:
    """A notice from ``GET /notifications`` as the bell lists it to a reader in ``lang``."""
    from ui.routes.notifications import _in_reader_language

    from ui import i18n

    i18n.set_lang(lang)
    try:
        return json.loads(_in_reader_language(json.dumps({"items": [dict(item)]}).encode()))["items"][0]
    finally:
        i18n.set_lang("en")


def sidebar_label(item: str, lang: str) -> str:
    """A sidebar item's label as a sentence names it: without its icon."""
    import re

    from ui import i18n

    return re.sub(r"^\W+", "", i18n.t(item, lang))


def real_agent_app() -> FastAPI:
    """The real Celerp API surface with every first-party module enabled.

    Built the same way `celerp.main` builds the live app: the core search and help
    routers plus every bundled pluggable module's routes. `docs_url`/`redoc_url` are off
    exactly as in production, so FastAPI's own `/docs` never shadows the
    documents module. The autouse loader-reset fixture tears `_loaded` and the
    slot registry back down after the test.
    """
    from celerp.routers import help as help_router_mod

    app = FastAPI(docs_url=None, redoc_url=None)
    app.include_router(search_router_mod.router, tags=["search"])
    app.include_router(help_router_mod.router, tags=["help"])
    loaded = load_all(_BUNDLED_MODULES_DIRS[0], _bundled_pluggable_names())
    register_api_routes(app, loaded)
    app.openapi_schema = None
    return app


@contextmanager
def invoices_booking_one_lot_twice():
    """Finalize invoices the way releases before the double booking check did, so a test can
    build the data such a release left behind: two open invoices that both booked one lot."""
    from unittest.mock import patch

    async def _unchecked(*_args, **_kwargs) -> None:
        return None

    with patch("celerp_docs.routes._refuse_unfillable_invoice_lines", _unchecked):
        yield


async def ui_get(headers: dict, path: str, *, lang: str | None = None):
    """GET a UI page as the user whose API ``headers`` are given, the UI talking to the real
    API in process (the ``client`` fixture's database and transaction), in ``lang`` when given."""
    from unittest.mock import patch

    from httpx import ASGITransport, AsyncClient

    from celerp.main import app
    from ui.app import app as ui_app

    def _bridged(tok, timeout=10.0):
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Authorization": f"Bearer {tok}"}, follow_redirects=True)

    token = headers["Authorization"].removeprefix("Bearer ")
    with patch("ui.api_client._client", _bridged):
        cookies = {"celerp_token": token, **({"celerp_lang": lang} if lang else {})}
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                               cookies=cookies) as ui:
            return await ui.get(path)


@contextmanager
def released_migration_history(database_url: str = DATABASE_URL):
    """The database with its migration history recorded at head, as every Celerp install
    has it from its first start and a create_all test database does not. Afterwards the
    history the database had before is put back, or none."""
    import psycopg2
    from alembic.script import ScriptDirectory
    from celerp.alembic_config import build_alembic_config
    head = ScriptDirectory.from_config(build_alembic_config()).get_current_head()
    conn = psycopg2.connect(database_url.replace("postgresql+asyncpg://", "postgresql://"))
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.alembic_version') IS NOT NULL")
        had = cur.fetchone()[0]
        before = []
        if had:
            cur.execute("SELECT version_num FROM alembic_version")
            before = [row[0] for row in cur.fetchall()]
        cur.execute("CREATE TABLE IF NOT EXISTS alembic_version "
                    "(version_num varchar(32) NOT NULL PRIMARY KEY)")
        cur.execute("DELETE FROM alembic_version")
        cur.execute("INSERT INTO alembic_version VALUES (%s)", (head,))
    try:
        yield head
    finally:
        with conn.cursor() as cur:
            if had:
                cur.execute("CREATE TABLE IF NOT EXISTS alembic_version "
                            "(version_num varchar(32) NOT NULL PRIMARY KEY)")
                cur.execute("DELETE FROM alembic_version")
                for revision in before:
                    cur.execute("INSERT INTO alembic_version VALUES (%s)", (revision,))
            else:
                cur.execute("DROP TABLE IF EXISTS alembic_version")
        conn.close()


async def settle_background() -> None:
    """Wait for the background work the app started on this event loop (and any work
    that work starts) to finish, so none of it is cut off when the loop closes."""
    from celerp.services import background

    loop = asyncio.get_running_loop()
    while pending := [t for t in background._BG_TASKS if t.get_loop() is loop and not t.done()]:
        await asyncio.gather(*pending, return_exceptions=True)


def fastest_cpu_seconds(*runs, times: int = 3) -> list[float]:
    """The least CPU time each of ``runs`` spends over ``times`` rounds, the runs called in turn
    in every round, with the garbage collector paused (its passes over everything the test
    process holds are not the runs'). CPU time, not the wall clock, which other processes on a
    busy machine stretch; in turn, so a load that comes and goes falls on every run alike."""
    took = [float("inf")] * len(runs)
    for _ in range(times):
        for i, run in enumerate(runs):
            gc.collect()
            gc.disable()
            try:
                began = time.process_time()
                run()
                took[i] = min(took[i], time.process_time() - began)
            finally:
                gc.enable()
    return took
