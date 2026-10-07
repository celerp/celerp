# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser journeys for restoring a company backup that may already have a company here:
a new company, an existing one that gets its missing team, a deactivated one that is
reactivated instead of copied, and the refusals and stale previews around them."""

from __future__ import annotations

import asyncio
import os
import re
import threading
import uuid

import httpx
import pytest

from .test_migration_wizard_browser import _pg_admin

pytestmark = pytest.mark.browser

_WAIT_MS = 30_000
_SETTINGS = "/settings/restore-backup"
_NEW_COMPANY = "/setup/new-company/restore-backup"
_LIMITS = httpx.Limits(keepalive_expiry=3.0)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _db(sql: str, *params) -> list[tuple]:
    conn = _pg_admin(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else []
    finally:
        conn.close()


def _session_company(context) -> str:
    from celerp.services.auth import decode_access_token
    token = next(c["value"] for c in context.cookies() if c["name"] == "celerp_token")
    return decode_access_token(token)["company_id"]


def _cookie(context, token: str) -> None:
    context.clear_cookies()
    context.add_cookies([{"name": "celerp_token", "value": token, "domain": "127.0.0.1", "path": "/"}])


def _client(api_server: str, token: str) -> httpx.Client:
    return httpx.Client(base_url=api_server, headers={"Authorization": f"Bearer {token}"}, timeout=30,
                        limits=_LIMITS)


def _backup(client, tmp_path) -> tuple[bytes, str]:
    r = client.get("/company-backups/download")
    assert r.status_code == 200, r.text
    path = tmp_path / f"{uuid.uuid4().hex[:8]}.celerp-company"
    path.write_bytes(r.content)
    return r.content, str(path)


def _api_restore(client, data: bytes, mode: str = "new_company") -> dict:
    r = client.post("/company-backups/read", files={"file": ("books.celerp-company", data)}, data={"mode": mode})
    assert r.status_code == 200, r.text
    body = r.json()
    r = client.post("/company-backups/restore", json={"upload_token": body["upload_token"], "mode": mode,
                                                      "plan_fingerprint": body["plan_fingerprint"]})
    assert r.status_code in (200, 201), r.text
    return r.json()


def _add_user(client, role: str, email: str | None = None) -> tuple[str, str]:
    email = email or f"team-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/companies/me/users", json={"email": email, "name": "Team Member", "role": role,
                                                  "password": "TeamMember123!"})
    assert r.status_code == 200, r.text
    return r.json()["id"], email


def _token_for(user_id: str, company_id: str) -> str:
    """A session for the user on the company, issued the way sign-in issues one."""
    out: dict = {}

    async def issue():
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

        from celerp.models.company import Company, User
        from celerp.credentials import issue_token_pair
        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with AsyncSession(engine, expire_on_commit=False) as s:
                user = await s.get(User, uuid.UUID(user_id))
                company = await s.get(Company, uuid.UUID(company_id))
                out["token"] = (await issue_token_pair(s, user=user, company_id=company.id,
                                                      expected_snonce=None))["access_token"]
        finally:
            await engine.dispose()

    worker = threading.Thread(target=lambda: asyncio.run(issue()))
    worker.start()
    worker.join(60)
    return out["token"]


def _upload(page, base: str, path: str) -> None:
    page.goto(base)
    page.set_input_files("#file", path)
    page.click('button:has-text("Continue")')
    page.wait_for_load_state("domcontentloaded")


def _members(company_id: str) -> set[tuple[str, str, bool]]:
    return {tuple(r) for r in _db(
        "SELECT user_id::text, role, is_active FROM user_companies WHERE company_id = %s", company_id)}


def _active(company_id: str) -> bool:
    return _db("SELECT is_active FROM companies WHERE id = %s", company_id)[0][0]


def _named(name: str) -> int:
    return _db("SELECT count(*) FROM companies WHERE name = %s", name)[0][0]


def _restored_name(source: dict) -> str:
    """A restore beside the backed-up company takes the next free name."""
    return f"{source['name']} (Restored)"


def _deactivate(api_server: str, token: str) -> None:
    with _client(api_server, token) as c:
        r = c.delete("/companies/me")
        assert r.status_code == 200, r.text


# ── Journeys ─────────────────────────────────────────────────────────────────

def test_restore_creates_new_company_browser(page, fresh_company, tmp_path):
    source = fresh_company.get("/companies/me").json()
    _, path = _backup(fresh_company, tmp_path)
    _upload(page, _NEW_COMPANY, path)
    assert page.locator('text="Nothing has been written yet."').count() == 1
    assert page.locator("text=already restored").count() == 0
    page.click('button:has-text("Restore company")')
    page.wait_for_url(re.compile(r"/setup/new-company/restore-backup/done"), timeout=_WAIT_MS)
    assert page.locator('text="Company restored"').count() == 1
    assert _session_company(page.context) != source["id"]
    assert _named(source["name"]) == 1 and _named(_restored_name(source)) == 1
    # The company switcher tells the two apart.
    switcher = page.request.get("/topbar-company-switcher").text()
    names = re.findall(r"<option[^>]*>([^<]*)</option>", switcher)
    assert source["name"] in names and _restored_name(source) in names, names


def test_restore_existing_destination_adds_team_browser(page, fresh_company, tmp_path):
    source = fresh_company.get("/companies/me").json()
    clerk, _ = _add_user(fresh_company, "viewer")
    data, path = _backup(fresh_company, tmp_path)
    dest = _api_restore(fresh_company, data)["company_id"]
    assert clerk not in {m[0] for m in _members(dest)}

    _upload(page, _SETTINGS, path)
    text = page.content()
    assert f"This backup was already restored here as {_restored_name(source)}." in text
    assert f"Team members who get access to {_restored_name(source)} with their roles in this company: 1" in text
    assert page.locator('button:has-text("Restore company")').count() == 0
    page.click('button:has-text("Add team and open company")')
    page.wait_for_url(re.compile(r"/settings/restore-backup/done"), timeout=_WAIT_MS)
    assert "Team members given access to this company with their current roles: 1" in page.content()
    assert (clerk, "viewer", True) in _members(dest)
    assert _session_company(page.context) == dest
    assert _named(source["name"]) == 1 and _named(_restored_name(source)) == 1


def test_restore_inactive_destination_reactivates_browser(page, fresh_company, api_server, tmp_path):
    source = fresh_company.get("/companies/me").json()
    data, path = _backup(fresh_company, tmp_path)
    restored = _api_restore(fresh_company, data)
    dest = restored["company_id"]
    _deactivate(api_server, restored["access_token"])
    assert _active(dest) is False

    _upload(page, _NEW_COMPANY, path)
    assert "which is now deactivated" in page.content()
    assert page.locator('button:has-text("Restore company")').count() == 0
    page.click('button:has-text("Reactivate existing company")')
    page.wait_for_url(re.compile(r"/setup/new-company/restore-backup/done"), timeout=_WAIT_MS)
    assert page.locator('text="Company reactivated"').count() == 1
    assert f"{_restored_name(source)} is active again." in page.content()
    assert _active(dest) is True
    assert _session_company(page.context) == dest
    assert _named(source["name"]) == 1 and _named(_restored_name(source)) == 1


def test_restore_refused_for_non_owner_browser(page, fresh_company, api_server, tmp_path):
    """An owner of the backed-up company who is only a manager of the company it was restored
    as opens that company, but the team is not added and the preview says so."""
    source = fresh_company.get("/companies/me").json()
    second, email = _add_user(fresh_company, "owner")
    clerk, _ = _add_user(fresh_company, "viewer")
    data, path = _backup(fresh_company, tmp_path)
    restored = _api_restore(fresh_company, data)
    dest = restored["company_id"]
    with _client(api_server, restored["access_token"]) as c:
        _add_user(c, "manager", email)
    before = _members(dest)

    _cookie(page.context, _token_for(second, source["id"]))
    _upload(page, _SETTINGS, path)
    text = page.content()
    assert f"Team members not added, because only an owner of {_restored_name(source)} can add them: 1" in text
    assert page.locator('button:has-text("Add team and open company")').count() == 0
    page.click('button:has-text("Open existing company")')
    page.wait_for_url(re.compile(r"/settings/restore-backup/done"), timeout=_WAIT_MS)
    assert _session_company(page.context) == dest
    assert _members(dest) == before
    assert clerk not in {m[0] for m in _members(dest)}


def test_restore_existing_destination_unchanged_browser(page, fresh_company, tmp_path):
    source = fresh_company.get("/companies/me").json()
    data, path = _backup(fresh_company, tmp_path)
    dest = _api_restore(fresh_company, data)["company_id"]
    company_row = _db("SELECT to_jsonb(c)::text FROM companies c WHERE id = %s", dest)
    ledger = _db("SELECT count(*) FROM ledger WHERE company_id = %s", dest)
    members = _members(dest)

    _upload(page, _NEW_COMPANY, path)
    assert f"This backup was already restored here as {_restored_name(source)}." in page.content()
    assert page.locator('button:has-text("Restore company")').count() == 0
    page.click('button:has-text("Open existing company")')
    page.wait_for_url(re.compile(r"/setup/new-company/restore-backup/done"), timeout=_WAIT_MS)
    assert _session_company(page.context) == dest
    assert _named(source["name"]) == 1 and _named(_restored_name(source)) == 1
    assert _db("SELECT to_jsonb(c)::text FROM companies c WHERE id = %s", dest) == company_row
    assert _db("SELECT count(*) FROM ledger WHERE company_id = %s", dest) == ledger
    assert _members(dest) == members


def test_restore_inactive_destination_refused_browser(page, fresh_company, api_server, tmp_path):
    """Someone who is not the deactivated company's owner is refused without learning which
    company it is or that it is deactivated."""
    source = fresh_company.get("/companies/me").json()
    second, email = _add_user(fresh_company, "owner")
    data, path = _backup(fresh_company, tmp_path)
    restored = _api_restore(fresh_company, data)
    dest = restored["company_id"]
    with _client(api_server, restored["access_token"]) as c:
        _add_user(c, "manager", email)
    _deactivate(api_server, restored["access_token"])

    _cookie(page.context, _token_for(second, source["id"]))
    _upload(page, _NEW_COMPANY, path)
    text = page.content()
    assert "already restored here as a company you are not a member of" in text
    assert "deactivated" not in text and dest not in text
    assert page.locator('button:has-text("Reactivate existing company")').count() == 0
    assert page.locator('button:has-text("Restore company")').count() == 0
    assert _active(dest) is False
    assert _named(source["name"]) == 1 and _named(_restored_name(source)) == 1


def test_restore_stale_preview_browser(page, fresh_company, tmp_path):
    """A preview that no longer holds is shown again, updated, and nothing is written until
    the owner confirms the updated one."""
    source = fresh_company.get("/companies/me").json()
    first, _ = _add_user(fresh_company, "viewer")
    data, path = _backup(fresh_company, tmp_path)
    dest = _api_restore(fresh_company, data)["company_id"]

    _upload(page, _SETTINGS, path)
    assert f"Team members who get access to {_restored_name(source)} with their roles in this company: 1" in page.content()
    second, _ = _add_user(fresh_company, "manager")
    before = _members(dest)
    page.click('button:has-text("Add team and open company")')
    page.wait_for_load_state("domcontentloaded")
    text = page.content()
    assert "Something changed since this preview. Check the updated preview before continuing." in text
    assert f"Team members who get access to {_restored_name(source)} with their roles in this company: 2" in text
    assert _members(dest) == before

    page.click('button:has-text("Add team and open company")')
    page.wait_for_url(re.compile(r"/settings/restore-backup/done"), timeout=_WAIT_MS)
    assert {(first, "viewer", True), (second, "manager", True)} <= _members(dest)
