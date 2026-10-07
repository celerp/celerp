# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A start that holds the records back says, per cause and in plain words, what failed,
what still works, what waits, and what to do; a refused change says the same; and
Doctor's report shows the failed step with its error.

Restarting comes first, then Doctor (report only) and a bug report. Enabling a module is
advised only when a turned-off module is the cause; repairs and restoring a backup never are.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from celerp.models.notification import Notification
from test_helpers import in_language, sidebar_label
from ui import i18n

pytestmark = pytest.mark.asyncio

_TITLE = "An update step failed while Celerp started"
_NEVER = ("Doctor fix", "run Doctor", "repair", "restore", "backup")


def _guard_raises(exc):
    async def guard(session):
        raise exc
    return guard


def _guard_unknown(*event_types):
    async def guard(session):
        return {"changed": True, "rebuilt": False, "current": False,
                "unknown_event_types": sorted(event_types)}
    return guard


async def _guard_current(session):
    return {"changed": False, "rebuilt": False, "current": True}


# cause id -> (upgrade guard, failed start hooks, words the notice and refusal must carry,
#              whether enabling a module is the advice)
_CAUSES = {
    "update_failed": (
        _guard_raises(RuntimeError("could not serialize access")), [],
        ["Updating the stored records to this release"], False),
    "module_off": (
        _guard_unknown("mfg.run.created"), [],
        ["Manufacturing", "turned off"], True),
    "module_not_installed": (
        _guard_unknown("zz.widget.made"), [],
        ["a module that is not installed"], False),
    "module_start_failed": (
        _guard_current, [("celerp-manufacturing", "ZeroDivisionError: division by zero")],
        ["Starting the Manufacturing module"], False),
}


async def _start(monkeypatch, guard, hook_failures):
    from celerp.main import _bring_data_current, app

    async def fire(slot, **kwargs):
        return list(hook_failures)

    monkeypatch.setattr("celerp.services.dev_release_guard.run_upgrade_guard", guard)
    monkeypatch.setattr("celerp.modules.slots.fire_lifecycle", fire)
    assert await _bring_data_current(app, modules_ready=True) is False
    return app


async def _no_hook_fails(slot, **kwargs):
    return []


async def _notice(session, company_id) -> Notification:
    session.expire_all()
    [notice] = (await session.execute(select(Notification).where(
        Notification.company_id == company_id, Notification.title == _TITLE))).scalars().all()
    return notice


@pytest.mark.parametrize("cause", list(_CAUSES))
async def test_the_held_back_notice_and_refusal_name_the_cause_in_plain_words(
        client, session, auth, monkeypatch, cause):
    guard, hooks, words, enable = _CAUSES[cause]
    await _start(monkeypatch, guard, hooks)

    notice = await _notice(session, auth["company_id"])
    body = notice.body
    for word in words:
        assert word in body, body
    assert notice.title == _TITLE and _TITLE not in body, body
    assert "You can still view all your records" in body
    assert "Changes to records are paused" in body
    assert "low-stock alerts" in body and "online stores" in body
    assert ("Enable Manufacturing in Modules, then restart Celerp." in body) is enable, body
    assert ("in Modules" in body) is enable, body
    if not enable:
        assert "Restart Celerp. If this notice comes back, open Doctor" in body, body
    assert "Report a bug" in body
    assert notice.action_url == ("/modules" if enable else "/doctor")
    for phrase in _NEVER:
        assert phrase not in body, (phrase, body)

    r = await client.post("/items", json={"sku": "HB-1", "name": "Held", "sell_by": "piece"},
                          headers=auth["headers"])
    assert r.status_code == 503, r.text
    refusal = r.json()["detail"]["message"]
    assert refusal.startswith("Changes are paused because"), refusal
    assert "You can still view records" in refusal
    for word in words:
        assert word in refusal, refusal
    assert ("in Modules" in refusal) is enable, refusal
    if not enable:
        assert "open Doctor" in refusal and "Report a bug" in refusal, refusal
    for phrase in _NEVER:
        assert phrase not in refusal, (phrase, refusal)


async def test_doctor_shows_the_failed_step_and_its_error(client, session, auth, monkeypatch):
    guard, hooks, _, _ = _CAUSES["module_start_failed"]
    await _start(monkeypatch, guard, hooks)

    r = await client.post("/admin/doctor", headers=auth["headers"])
    assert r.status_code == 200, r.text
    report = r.json()["held_back"]
    assert [(f["step"]["message"], f["error"]) for f in report["failures"]] == [
        ("Starting the Manufacturing module", "ZeroDivisionError: division by zero")]
    assert report["what_to_do"]["message"].startswith("Restart Celerp.")

    r = await client.get("/system/start-report", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["held_back"] == report

    r = await client.post("/admin/doctor?fix=true", headers=auth["headers"])
    assert r.status_code == 503, r.text
    assert "Starting the Manufacturing module" in r.json()["detail"]["message"]


async def test_doctor_reports_no_failed_step_once_the_records_are_current(
        client, session, auth, monkeypatch):
    from celerp.main import _bring_data_current, app

    guard, hooks, _, _ = _CAUSES["update_failed"]
    await _start(monkeypatch, guard, hooks)
    monkeypatch.setattr("celerp.services.dev_release_guard.run_upgrade_guard", _guard_current)
    monkeypatch.setattr("celerp.modules.slots.fire_lifecycle", _no_hook_fails)
    assert await _bring_data_current(app, modules_ready=True) is True

    r = await client.get("/system/start-report", headers=auth["headers"])
    assert r.json() == {"held_back": None}
    r = await client.post("/admin/doctor", headers=auth["headers"])
    assert r.json()["held_back"] is None


async def test_a_standing_notice_names_the_latest_cause(client, session, auth, monkeypatch):
    """A notice still unread from an earlier start is brought up to date, never left
    describing a cause that is no longer the one holding the records back."""
    guard, hooks, _, _ = _CAUSES["module_off"]
    await _start(monkeypatch, guard, hooks)
    guard, hooks, words, _ = _CAUSES["module_start_failed"]
    await _start(monkeypatch, guard, hooks)

    notice = await _notice(session, auth["company_id"])
    assert words[0] in notice.body and "turned off" not in notice.body, notice.body
    assert notice.action_url == "/doctor"


async def test_the_notice_is_cleared_once_a_later_start_brings_the_records_current(
        client, session, auth, monkeypatch):
    """Once the records are current, no unread notice still says changes are paused, so
    a notice that comes back always means a new failed start."""
    from celerp.main import _bring_data_current, app

    guard, hooks, _, _ = _CAUSES["update_failed"]
    await _start(monkeypatch, guard, hooks)
    assert not (await _notice(session, auth["company_id"])).read
    monkeypatch.setattr("celerp.services.dev_release_guard.run_upgrade_guard", _guard_current)
    monkeypatch.setattr("celerp.modules.slots.fire_lifecycle", _no_hook_fails)
    assert await _bring_data_current(app, modules_ready=True) is True

    assert (await _notice(session, auth["company_id"])).read
    await _start(monkeypatch, guard, hooks)
    session.expire_all()
    unread = (await session.execute(select(Notification).where(
        Notification.company_id == auth["company_id"], Notification.title == _TITLE,
        Notification.read == False))).scalars().all()  # noqa: E712
    assert len(unread) == 1


@pytest.mark.parametrize("cause", list(_CAUSES))
async def test_the_held_back_notice_and_refusal_read_in_the_readers_language(
        client, session, auth, monkeypatch, cause):
    """A German reader gets the notice and a refused change in German, naming the sidebar
    item to open by the label the German sidebar shows, and telling anyone who is not an
    admin to ask one."""
    guard, hooks, _, enable = _CAUSES[cause]
    await _start(monkeypatch, guard, hooks)

    notice = await _notice(session, auth["company_id"])
    assert notice.i18n, "the held-back notice carries no message keys"
    assert "If you are not an admin, ask an admin to do this." in notice.body, notice.body

    def shown(lang, part):
        return in_language(lang, {"message": getattr(notice, part), "message_key": notice.i18n[part],
                                  "params": notice.i18n.get("params") or {}})

    assert shown("de", "title") == i18n.t("held_back.title", lang="de") != _TITLE
    sidebar = sidebar_label("nav.modules" if enable else "nav.doctor", "de")
    german = shown("de", "body")
    assert sidebar in german, german
    for english in ("You can still view", "Restart Celerp", "in Modules", "open Doctor", "ask an admin"):
        assert english not in german, (english, german)

    r = await client.post("/items", json={"sku": "HB-DE", "name": "Held", "sell_by": "piece"},
                          headers=auth["headers"])
    assert r.status_code == 503, r.text
    refusal = r.json()["detail"]
    assert refusal["message"].startswith("Changes are paused because"), refusal
    german = in_language("de", refusal)
    assert sidebar in german and "Changes are paused" not in german, german


async def test_doctors_report_reads_in_the_readers_language(client, session, auth, monkeypatch):
    guard, hooks, _, _ = _CAUSES["module_start_failed"]
    await _start(monkeypatch, guard, hooks)

    report = (await client.get("/system/start-report", headers=auth["headers"])).json()["held_back"]
    assert in_language("de", report["title"]) == i18n.t("held_back.title", lang="de")
    [failure] = report["failures"]
    assert in_language("en", failure["step"]) == "Starting the Manufacturing module"
    assert in_language("de", failure["step"]) == i18n.t("held_back.step.module_start", lang="de",
                                                         module="Manufacturing")
    assert failure["error"] == "ZeroDivisionError: division by zero"
    what_to_do = in_language("de", report["what_to_do"])
    assert sidebar_label("nav.doctor", "de") in what_to_do and "Restart Celerp" not in what_to_do, what_to_do
