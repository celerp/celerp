# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The API side of automatic updates: when the hourly tick installs, and the
one notification per company after every attempt."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select, update as sa_update

from celerp.models.company import Company
from celerp.models.notification import Notification
from celerp.services import update
from ui.i18n import localize_notification
from test_helpers import register_admin

# 20:30 UTC is 03:30 in Bangkok (inside the window) and outside it in UTC.
BANGKOK_NIGHT = datetime(2026, 9, 26, 20, 30, tzinfo=timezone.utc)


@pytest.fixture
def cfg_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CELERP_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr(update, "installed_version", lambda: "1.0.0")
    monkeypatch.setattr(update, "self_update_blockers", lambda: [])
    monkeypatch.setattr(update, "available_update", lambda: "1.1.0")
    return tmp_path


async def _owner_company_timezone(client, session, tz):
    await register_admin(client)
    await session.execute(sa_update(Company).values(settings={"timezone": tz}))
    await session.commit()


async def _tick(session, now=BANGKOK_NIGHT):
    restarts = []
    target = await update.auto_update_tick(session, now, lambda: restarts.append(1))
    return target, restarts


@pytest.mark.asyncio
async def test_installs_inside_the_window_in_the_company_time_zone(client, session, cfg_dir):
    await _owner_company_timezone(client, session, "Asia/Bangkok")
    target, restarts = await _tick(session)
    assert target == "1.1.0" and restarts == [1]
    assert (cfg_dir / ".restart_requested").read_text() == "update 1.1.0"


@pytest.mark.asyncio
async def test_outside_the_window_only_checks(client, session, cfg_dir):
    await _owner_company_timezone(client, session, "UTC")
    update._check["latest"] = None
    target, restarts = await _tick(session)
    assert target is None and restarts == []
    assert update._check["latest"] == "1.1.0"
    assert not (cfg_dir / ".restart_requested").exists()


@pytest.mark.parametrize("setup", [
    lambda mp, d: update.set_auto(False),
    lambda mp, d: mp.setattr(update, "self_update_blockers", lambda: ["not_writable"]),
    lambda mp, d: update.write_state({"failed_versions": ["1.1.0"]}),
    lambda mp, d: update.request_update("1.1.0"),
], ids=["auto_off", "blocked", "failed_before", "already_requested"])
@pytest.mark.asyncio
async def test_never_installs_when_it_should_not(client, session, cfg_dir, monkeypatch, setup):
    await _owner_company_timezone(client, session, "Asia/Bangkok")
    setup(monkeypatch, cfg_dir)
    before = (cfg_dir / ".restart_requested").read_text() if (cfg_dir / ".restart_requested").exists() else None
    target, restarts = await _tick(session)
    assert target is None and restarts == []
    after = (cfg_dir / ".restart_requested").read_text() if (cfg_dir / ".restart_requested").exists() else None
    assert after == before


@pytest.mark.asyncio
async def test_a_person_can_retry_a_version_that_failed_overnight(cfg_dir):
    update.write_state({"failed_versions": ["1.1.0"]})
    assert update.request_available() == "1.1.0"


def _last_result(ok):
    return {"last_result": {"ok": ok, "outcome": update.OK if ok else update.FAILED,
                            "from": "1.0.0", "to": "1.1.0", "reason": "install_failed",
                            "at": "t", "notified": False}}


@pytest.mark.parametrize("ok, title", [
    (True, "Celerp was updated to 1.1.0"),
    (False, "Celerp could not update to 1.1.0"),
])
@pytest.mark.asyncio
async def test_notifies_every_company_once(client, session, cfg_dir, ok, title):
    headers = {"Authorization": f"Bearer {await register_admin(client)}"}
    session.add(Company(name="Second Co", slug="second-co", settings={}))
    await session.commit()
    update.write_state(_last_result(ok))

    assert await update.notify_last_result(session) == 2
    assert await update.notify_last_result(session) == 0

    rows = (await session.execute(
        select(Notification).where(Notification.category == "system"))).scalars().all()
    assert len(rows) == 2 and {r.priority for r in rows} == {"high"}
    assert update.read_state()["last_result"]["notified"] is True
    # The API carries readable text and the message key the UI translates from.
    shown = (await client.get("/notifications", headers=headers)).json()["items"]
    assert [n["title"] for n in shown] == [title]
    assert shown[0]["message_key"] == ("notif.update_ok" if ok else "notif.update_failed")
    if not ok:
        assert "still on 1.0.0" in shown[0]["body"] and "not changed" in shown[0]["body"]
        assert update.reason_text("install_failed") in shown[0]["body"]
        german = localize_notification(shown[0], "de")
        assert german["title"] == "Celerp konnte nicht auf 1.1.0 aktualisiert werden"
        assert "die neue Version konnte nicht installiert werden" in german["body"]
        assert "Ihre Daten wurden nicht geändert" in german["body"]


@pytest.mark.asyncio
async def test_unreadable_update_record_notifies_nobody(client, session, cfg_dir):
    (cfg_dir / update.STATE_FILE).write_text("{torn")
    assert await update.notify_last_result(session) == 0


def test_status_with_unreadable_update_record_shows_an_update_running(cfg_dir):
    (cfg_dir / update.STATE_FILE).write_text("{torn")
    body = update.status(owner=True)
    assert body["last_result"] is None and body["installing"] is True


def test_every_failure_reason_is_translated_in_every_locale():
    from pathlib import Path
    import json
    for path in sorted((Path(__file__).parents[1] / "ui" / "locales").glob("*.json")):
        catalog = json.loads(path.read_text(encoding="utf-8"))
        missing = [c for c in (*update.REASON_CODES, "unknown") if f"update.reason.{c}" not in catalog]
        assert not missing, f"{path.name}: {missing}"
