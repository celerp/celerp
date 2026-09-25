# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The /system/update routes: who sees what, who may install, and that the
version installed is always the one the server found."""

from __future__ import annotations

import pytest

from celerp.routers import system
from celerp.services import update
from test_helpers import invite_user, register_admin


@pytest.fixture
def cfg_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CELERP_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr(update, "installed_version", lambda: "1.0.0")
    monkeypatch.setattr(update, "self_update_blockers", lambda: [])
    monkeypatch.setattr(update, "available_update", lambda: "1.1.0")
    monkeypatch.setitem(update._check, "latest", None)
    monkeypatch.setitem(update._check, "error", "")
    sigterms = []
    monkeypatch.setattr(system, "_send_sigterm", lambda: sigterms.append(1))
    return tmp_path, sigterms


async def _owner_and_member(client, session):
    owner_h = {"Authorization": f"Bearer {await register_admin(client)}"}
    member = await invite_user(client, session, owner_h, "member@example.test", "admin")
    return owner_h, {"Authorization": f"Bearer {member}"}


@pytest.mark.asyncio
async def test_status_owner_can_install_member_is_told_to_ask(client, session, cfg_dir):
    owner_h, member_h = await _owner_and_member(client, session)
    owner = (await client.get("/system/update", headers=owner_h)).json()
    member = (await client.get("/system/update", headers=member_h)).json()
    assert owner["can_install"] is True and owner["reason"] == ""
    assert member["can_install"] is False and member["reason"] == "administrator"
    assert owner["current"] == "1.0.0" and owner["auto"] is True
    assert owner["installing"] is False and owner["last_result"] is None


@pytest.mark.asyncio
async def test_status_requires_login(client, cfg_dir):
    assert (await client.get("/system/update")).status_code == 401


@pytest.mark.asyncio
async def test_status_shows_blocker_to_owner(client, session, cfg_dir, monkeypatch):
    owner_h, _ = await _owner_and_member(client, session)
    monkeypatch.setattr(update, "self_update_blockers", lambda: ["not_writable"])
    body = (await client.get("/system/update", headers=owner_h)).json()
    assert body["can_install"] is False and body["reason"] == "not_writable"


@pytest.mark.asyncio
async def test_owner_install_requests_the_server_found_version(client, session, cfg_dir):
    tmp_path, sigterms = cfg_dir
    owner_h, _ = await _owner_and_member(client, session)
    r = await client.post("/system/update", headers=owner_h, json={"version": "9.9.9"})
    assert r.status_code == 202, r.text
    assert r.json()["installing"] == "1.1.0"
    assert (tmp_path / ".restart_requested").read_text() == "update 1.1.0"
    assert sigterms == [1]
    status = (await client.get("/system/update", headers=owner_h)).json()
    assert status["installing"] is True


@pytest.mark.asyncio
async def test_second_install_while_one_is_pending_is_refused(client, session, cfg_dir):
    tmp_path, sigterms = cfg_dir
    owner_h, _ = await _owner_and_member(client, session)
    assert (await client.post("/system/update", headers=owner_h)).status_code == 202
    r = await client.post("/system/update", headers=owner_h)
    assert r.status_code == 409 and r.json()["detail"] == "in_progress"
    assert sigterms == [1]


@pytest.mark.asyncio
async def test_member_and_anonymous_cannot_install(client, session, cfg_dir):
    tmp_path, sigterms = cfg_dir
    _, member_h = await _owner_and_member(client, session)
    assert (await client.post("/system/update", headers=member_h)).status_code == 403
    assert (await client.post("/system/update")).status_code == 401
    assert not (tmp_path / ".restart_requested").exists()
    assert sigterms == []


@pytest.mark.parametrize("setup, code", [
    (lambda mp: mp.setattr(update, "self_update_blockers", lambda: ["unsupervised"]), "unsupervised"),
    (lambda mp: mp.setattr(update, "available_update", lambda: None), "current"),
    (lambda mp: mp.setattr(update, "available_update",
                           lambda: (_ for _ in ()).throw(update.UpdateError("offline"))), "check_failed"),
])
@pytest.mark.asyncio
async def test_install_refused_with_reason(client, session, cfg_dir, monkeypatch, setup, code):
    tmp_path, sigterms = cfg_dir
    owner_h, _ = await _owner_and_member(client, session)
    setup(monkeypatch)
    r = await client.post("/system/update", headers=owner_h)
    assert r.status_code == 409 and r.json()["detail"] == code
    assert not (tmp_path / ".restart_requested").exists()
    assert sigterms == []


@pytest.mark.asyncio
async def test_check_refreshes_the_cached_answer(client, session, cfg_dir):
    owner_h, member_h = await _owner_and_member(client, session)
    assert (await client.post("/system/update/check", headers=member_h)).status_code == 403
    r = await client.post("/system/update/check", headers=owner_h)
    assert r.status_code == 200 and r.json()["latest"] == "1.1.0"
    assert (await client.get("/system/update", headers=member_h)).json()["latest"] == "1.1.0"


@pytest.mark.asyncio
async def test_auto_setting_is_saved(client, session, cfg_dir):
    owner_h, member_h = await _owner_and_member(client, session)
    r = await client.patch("/system/update/settings", headers=owner_h, json={"auto": False})
    assert r.status_code == 200
    assert (await client.get("/system/update", headers=owner_h)).json()["auto"] is False
    assert (await client.patch("/system/update/settings", headers=member_h,
                               json={"auto": True})).status_code == 403


@pytest.mark.parametrize("body", [{"auto": "no"}, {"auto": 1}, {}])
@pytest.mark.asyncio
async def test_auto_setting_rejects_non_booleans(client, session, cfg_dir, body):
    owner_h, _ = await _owner_and_member(client, session)
    r = await client.patch("/system/update/settings", headers=owner_h, json=body)
    assert r.status_code == 422
