# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""End-to-end browser journeys for moving a company in while another one keeps running.

Journey 1: the owner's working session stays on their company through the whole
migration of another one, which opens only when the owner chooses.
Journey 7: a start whose response never reaches the browser is recovered, never
duplicated.
Journey 8: discard returns the user to useful work even while file storage fails,
and the files are removed later by the startup sweep.

The shared API server runs in this process, so a journey can hold the background
runner or make file storage fail without touching any other server.
"""

from __future__ import annotations

import asyncio
import re
import threading
import uuid
from urllib.parse import urlsplit

import pytest

from .test_migration_wizard_browser import (
    _pg_admin,
    _run_id,
    _through_review,
    _upload,
    _verify_and_finish,
    _wait_ready,
    first_run_page,  # noqa: F401 - fixture
    first_run_ui,  # noqa: F401 - fixture
)

pytestmark = pytest.mark.browser

_WAIT_MS = 60_000


def _session_company(context) -> str:
    from celerp.services.auth import decode_access_token
    token = next(c["value"] for c in context.cookies() if c["name"] == "celerp_token")
    return decode_access_token(token)["company_id"]


def _db_rows(sql: str, *params) -> list[tuple]:
    import os
    conn = _pg_admin(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def _company_count(name: str) -> int:
    return _db_rows("SELECT count(*) FROM companies WHERE name = %s", name)[0][0]


def _wait_status(page, label: str) -> None:
    page.wait_for_selector(f'text="{label}"', timeout=_WAIT_MS)


@pytest.fixture
def held_runner(monkeypatch):
    """Hold the background runner of the next start until the test releases it."""
    from celerp.services import migrations

    release = threading.Event()
    tasks: set[asyncio.Task] = set()

    def schedule(run_id):
        async def later():
            await asyncio.to_thread(release.wait, 120)
            await migrations.run_migration(run_id)
        task = asyncio.get_running_loop().create_task(later())
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    monkeypatch.setattr(migrations, "schedule_run", schedule)
    yield release
    release.set()


def _lose_start_response(page, path: str) -> None:
    """Submit the start form; the server handles it, but its response, cookies included,
    never reaches the browser."""
    context = page.context
    cookies = context.cookies()
    handled: list[int] = []

    def lose(route):
        handled.append(route.fetch(max_redirects=0).status)
        route.abort()

    page.route(f"**{path}", lose)
    page.click('button:has-text("Create company and migrate")', no_wait_after=True)
    waited = 0
    while not handled and waited < 100:
        page.wait_for_timeout(100)
        waited += 1
    page.unroute(f"**{path}")
    assert handled == [303], handled
    context.clear_cookies()
    if cookies:
        context.add_cookies(cookies)


def _start_additional(page, company_name: str) -> None:
    page.goto("/setup/new-company")
    page.click('a:has-text("Move a company")')
    page.wait_for_url(re.compile(r"/setup/new-company/migrate$"))
    _upload(page, "Example Bookkeeping")
    _through_review(page, company_name)


def test_journey_1_existing_business_keeps_running(page, fresh_company, held_runner):
    context = page.context
    company_a = fresh_company.get("/companies/me").json()
    cookies_before = {c["name"]: c["value"] for c in context.cookies()}
    moved = f"Moved {uuid.uuid4().hex[:6]}"

    _start_additional(page, moved)
    page.click('button:has-text("Create company and migrate")')
    page.wait_for_url(re.compile(r"/migrations/[0-9a-f-]{36}$"))
    run_id = _run_id(page)
    # Starting leaves the working session exactly as it was.
    assert {c["name"]: c["value"] for c in context.cookies()} == cookies_before
    assert _session_company(context) == company_a["id"]

    # Another tab keeps operating Company A while the migration runs in the background.
    other = context.new_page()
    try:
        other.goto("/")
        assert company_a["name"] in other.content()
        loc = fresh_company.get("/companies/me/locations").json()["items"][0]["id"]
        r = fresh_company.post("/items", json={"sku": "J1-A", "name": "Journey item", "quantity": 1,
                                               "location_id": loc, "sell_by": "piece",
                                               "status": "available"})
        assert r.status_code == 200, r.text
        item_id = r.json()["id"]
        r = fresh_company.post("/docs", json={
            "doc_type": "invoice", "total": 10.0,
            "line_items": [{"entity_id": item_id, "sku": "J1-A", "name": "Journey item",
                            "quantity": 1, "unit_price": 10.0, "sell_by": "piece"}]})
        assert r.status_code == 200, r.text
        doc_id = r.json()["id"]
        assert fresh_company.post(f"/docs/{doc_id}/finalize").status_code == 200
        r = fresh_company.post(f"/docs/{doc_id}/payment", json={
            "amount": 10.0, "payment_date": "2026-07-01", "bank_account": "1111"})
        assert r.status_code == 200, r.text
        other.goto(f"/docs/{doc_id}")
        assert other.locator("text=Journey item").count() >= 1
    finally:
        other.close()

    # Cancel, let the runner acknowledge it, then resume to the end.
    page.goto(f"/migrations/{run_id}")
    page.click('button:has-text("Cancel")')
    page.wait_for_url(re.compile(rf"/migrations/{run_id}$"))
    held_runner.set()
    page.reload()
    page.wait_for_selector('button:has-text("Resume")', timeout=_WAIT_MS)
    page.click('button:has-text("Resume")')
    page.wait_for_url(re.compile(rf"/migrations/{run_id}$"))
    _wait_ready(page, run_id)
    _verify_and_finish(page, run_id, moved)

    # Finishing does not move the session; Open company does, through the company switch.
    assert _session_company(context) == company_a["id"]
    page.click('a:has-text("Open company")')
    page.wait_for_load_state()
    assert "/migrations/" not in page.url and "error=" not in page.url, page.url
    moved_id = _db_rows("SELECT id FROM companies WHERE name = %s", moved)[0][0]
    assert _session_company(context) == str(moved_id)
    assert moved in page.content()


def test_journey_7_lost_start_response_additional_company(page, fresh_company):
    moved = f"Moved {uuid.uuid4().hex[:6]}"
    _start_additional(page, moved)
    _lose_start_response(page, "/setup/new-company/migrate/start")
    assert _company_count(moved) == 1

    # The browser still holds the scan: going back to the wizard leads to the run it started.
    page.goto("/setup/new-company/migrate/review")
    page.wait_for_url(re.compile(r"/migrations/[0-9a-f-]{36}$"))
    run_id = _run_id(page)
    assert _company_count(moved) == 1
    runs = _db_rows("SELECT r.id FROM migration_runs r JOIN companies c ON c.id = r.company_id "
                    "WHERE c.name = %s", moved)
    assert [str(r[0]) for r in runs] == [run_id]
    _wait_ready(page, run_id)


def test_journey_7_lost_start_response_first_run(first_run_page):
    page = first_run_page
    page.goto("/setup")
    page.click('a:has-text("Move a company")')
    page.wait_for_url(re.compile(r"/setup/migrate$"))
    _upload(page, "Example Bookkeeping")
    _through_review(page, "Harbor Goods Ltd")
    page.fill('input[name="name"]', "First Owner")
    page.fill('input[name="email"]', "owner@example.com")
    page.fill('input[name="password"]', "correct-horse-9")
    page.fill('input[name="confirm_password"]', "correct-horse-9")
    _lose_start_response(page, "/setup/migrate/start")
    assert not [c for c in page.context.cookies() if c["name"] == "celerp_token"]

    # Signing in lands on the unfinished migration, which the owner can resume or discard.
    page.goto("/login")
    page.fill('input[name="email"]', "owner@example.com")
    page.fill('input[name="password"]', "correct-horse-9")
    page.click('button[type="submit"]')
    # The lost response's session still counts as the one direct connection, so the
    # owner confirms replacing it.
    page.click('button:has-text("Continue (sign out the other user)")')
    page.wait_for_load_state()
    assert re.search(r"/migrations/[0-9a-f-]{36}$", page.url), (page.url, page.content()[-1500:])
    run_id = _run_id(page)
    _wait_ready(page, run_id)
    page.click('a:has-text("Discard migration")')
    page.wait_for_url(re.compile(rf"/migrations/{run_id}/discard$"))
    page.click('button:has-text("Discard migration")')
    page.wait_for_url(re.compile(r"/setup$"))
    assert page.locator("text=Move a company").count() >= 1


def _sweep() -> None:
    """The startup sweep, run on its own engine and event loop."""
    import os

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from celerp.services import migrations

    async def run():
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                await migrations.housekeeping(s)
        finally:
            await engine.dispose()

    errors: list[BaseException] = []

    def target():
        try:
            asyncio.run(run())
        except BaseException as exc:  # surfaced to the test below
            errors.append(exc)
    thread = threading.Thread(target=target)
    thread.start()
    thread.join(60)
    assert not errors, errors


def test_journey_8_discard_while_storage_is_unavailable(page, fresh_company, monkeypatch):
    from celerp.services import attachments
    from celerp.services import migration_scan_store as store

    company_a = fresh_company.get("/companies/me").json()
    moved = f"Moved {uuid.uuid4().hex[:6]}"
    _start_additional(page, moved)
    page.click('button:has-text("Create company and migrate")')
    page.wait_for_url(re.compile(r"/migrations/[0-9a-f-]{36}$"))
    run_id = _run_id(page)
    _wait_ready(page, run_id)
    moved_id = str(_db_rows("SELECT id FROM companies WHERE name = %s", moved)[0][0])
    assert store.run_dir(uuid.UUID(run_id)).is_dir()

    def refuse(path):
        raise OSError("device busy")

    async def refuse_company(self, company_id):
        raise OSError("device busy")

    with monkeypatch.context() as m:
        m.setattr(store, "_remove_tree", refuse)
        m.setattr(attachments.LocalBackend, "delete_company", refuse_company)
        page.goto(f"/migrations/{run_id}/discard")
        page.click('button:has-text("Discard migration")')
        page.wait_for_url(lambda url: "/migrations/" not in urlsplit(url).path, timeout=_WAIT_MS)
        # Straight back to the working company, with the files kept for a later cleanup.
        assert _session_company(page.context) == company_a["id"]
        assert company_a["name"] in page.content()
        assert _company_count(moved) == 0
        tasks = _db_rows("SELECT company_id, run_ids FROM migration_cleanup_tasks WHERE company_id = %s", moved_id)
        assert len(tasks) == 1 and tasks[0][1] == [run_id]
        assert store.run_dir(uuid.UUID(run_id)).is_dir()

        # Another migration can start straight away.
        _start_additional(page, f"Again {uuid.uuid4().hex[:6]}")

    # With storage back, the startup sweep finishes the cleanup; a repeat has nothing to do.
    _sweep()
    assert _db_rows("SELECT 1 FROM migration_cleanup_tasks WHERE company_id = %s", moved_id) == []
    assert not store.run_dir(uuid.UUID(run_id)).exists()
    _sweep()
