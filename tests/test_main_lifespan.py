# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The on_modules_ready boot block is guarded.

The lifespan fires the `on_modules_ready` hooks and commits their work on a
shared session. A hook that fails mid-work poisons that session, so the commit
raises; without a guard the raise crashes boot and the app never comes up. This
drives the real `lifespan` with the module block forced and a poisoned session,
and asserts boot survives and the session is rolled back.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


async def _boom_hook(session=None, **kwargs):
    """An on_modules_ready hook that fails, standing in for one whose DB work
    poisons the shared boot session."""
    raise RuntimeError("hook exploded during on_modules_ready")


class _FakeSession:
    """Async-context session whose commit() refuses (a poisoned transaction).
    All fakes share one rollback spy so the guard's rollback is observable."""

    def __init__(self, rollback_spy: AsyncMock):
        self.commit = AsyncMock(side_effect=RuntimeError("commit refused: transaction aborted"))
        self.rollback = rollback_spy
        self.execute = AsyncMock()
        self.close = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _mock_db():
    """Make celerp.main.lifecycle_engine.begin() a no-op (no real DDL at boot).

    Boot's create_all runs on the lifecycle engine, so that is the one to stub."""
    mock_conn = AsyncMock()
    mock_conn.run_sync = AsyncMock()
    mock_begin = MagicMock()
    mock_begin.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_begin.__aexit__ = AsyncMock(return_value=False)
    mock_engine = MagicMock()
    mock_engine.begin = MagicMock(return_value=mock_begin)
    return patch("celerp.main.lifecycle_engine", mock_engine)


@pytest.mark.asyncio
async def test_modules_ready_commit_guarded(monkeypatch):
    """A failing on_modules_ready hook does not crash boot: the raise is caught,
    the session is rolled back, and startup runs through to serving."""
    import celerp.main as main_mod
    from celerp.config import settings
    from celerp.modules import slots

    rollback_spy = AsyncMock()

    # Force the module-load path so the on_modules_ready block runs, with the
    # heavy module machinery stubbed out.
    monkeypatch.setattr(main_mod, "_MODULE_DIR", "/tmp/modules-forced")
    monkeypatch.setenv("ENABLED_MODULES", "test-mod")
    monkeypatch.setattr(
        "celerp.modules.migrations_runner.run_migration_phase",
        AsyncMock(return_value=({"test-mod"}, {})),
    )
    monkeypatch.setattr("celerp.modules.loader.load_all", lambda *a, **k: [])
    monkeypatch.setattr("celerp.modules.loader.register_api_routes", lambda *a, **k: None)
    monkeypatch.setattr("celerp.modules.loader.record_load_error", lambda *a, **k: None)
    monkeypatch.setattr("celerp.modules.loader.demoted_first_party", lambda *a, **k: [])
    made: list[_FakeSession] = []

    def _session():
        s = _FakeSession(rollback_spy)
        if not made:  # the upgrade guard's session, ahead of the hooks', commits
            s.commit = AsyncMock()
        made.append(s)
        return s

    monkeypatch.setattr("celerp.db.LifecycleSessionLocal", _session)
    monkeypatch.setattr("celerp.services.dev_release_guard.run_upgrade_guard",
                        AsyncMock(return_value={"changed": False, "rebuilt": False, "current": True}))
    monkeypatch.setattr("celerp.connectors.outbound_queue.adopt_legacy_connector_configs", AsyncMock())

    # Keep the relay tunnel down (no public url, no live share).
    monkeypatch.setattr("celerp.gateway.has_active_share", AsyncMock(return_value=False))
    saved_token, saved_public = settings.gateway_token, settings.celerp_public_url
    settings.gateway_token = "test-token"
    settings.celerp_public_url = None

    slots.clear()
    slots.register("on_modules_ready", {"handler": f"{__name__}:_boom_hook", "_module": "test-mod"})

    entered = False
    try:
        with _mock_db():
            async with main_mod.lifespan(MagicMock()):
                entered = True
    finally:
        slots.clear()
        settings.gateway_token = saved_token
        settings.celerp_public_url = saved_public

    assert entered, "boot did not survive a failing on_modules_ready hook"
    assert rollback_spy.await_count >= 1, "the poisoned boot session was not rolled back"


@pytest.mark.asyncio
async def test_update_verification_boot_skips_runtime_side_effects(monkeypatch):
    import celerp.main as main_mod
    from celerp import runtime
    from celerp.config import settings
    from celerp.modules import slots

    monkeypatch.setenv(runtime.UPDATE_VERIFY_ENV, "1")
    monkeypatch.setattr(main_mod, "_MODULE_DIR", "/tmp/modules-forced")
    monkeypatch.setenv("ENABLED_MODULES", "test-mod")
    monkeypatch.setattr(
        "celerp.modules.migrations_runner.run_migration_phase",
        AsyncMock(return_value=({"test-mod"}, {})),
    )
    monkeypatch.setattr("celerp.modules.loader.load_all", lambda *a, **k: [])
    monkeypatch.setattr("celerp.modules.loader.register_api_routes", lambda *a, **k: None)
    monkeypatch.setattr("celerp.modules.loader.record_load_error", lambda *a, **k: None)

    fire = AsyncMock()
    associate = AsyncMock()
    adopt = AsyncMock()
    monkeypatch.setattr("celerp.modules.slots.fire_lifecycle", fire)
    monkeypatch.setattr("celerp.gateway.bootstrap.associate_partner_deployment", associate)
    monkeypatch.setattr("celerp.connectors.outbound_queue.adopt_legacy_connector_configs", adopt)

    saved_token, saved_public = settings.gateway_token, settings.celerp_public_url
    settings.gateway_token = ""
    settings.celerp_public_url = None
    slots.clear()
    app = MagicMock()
    try:
        with _mock_db():
            async with main_mod.lifespan(app):
                pass
    finally:
        slots.clear()
        settings.gateway_token = saved_token
        settings.celerp_public_url = saved_public

    fire.assert_not_awaited()
    associate.assert_not_awaited()
    adopt.assert_awaited_once()


_HELD_WHILE_NOT_CURRENT = {
    # Each reads the projections to change data (or what other systems are told).
    "celerp.services.status_doc_backfill.run_status_doc_backfill": "status_doc_backfill",
    "celerp.services.cogs_backfill.run_cogs_backfill": "cogs_backfill",
    "celerp.connectors.outbound_queue.outbound_queue_loop": "outbound_stock_sync",
    "celerp.connectors.daily_scheduler.scheduler_loop_all": "connector_sync",
    "celerp.services.reorder.reorder_alert_loop": "reorder_alerts",
}
_STARTED_WHEN_CURRENT = ["guard", "on_modules_ready", *_HELD_WHILE_NOT_CURRENT.values()]


async def _boot_in_order(monkeypatch, guard) -> tuple[list[str], AsyncMock, MagicMock]:
    """Boot with the module block forced, recording the order in which the upgrade guard,
    the on_modules_ready hooks and the startup work that reads the projections run, and
    the notices told to every company, and return the app the boot ran."""
    import celerp.main as main_mod
    from celerp.config import settings
    from celerp.modules import slots

    order: list[str] = []

    async def _guard(session):
        order.append("guard")
        return await guard()

    async def _fire(slot, **kwargs):
        if slot == "on_modules_ready":
            order.append("on_modules_ready")

    class _Session(_FakeSession):
        def __init__(self):
            super().__init__(AsyncMock())
            self.commit = AsyncMock()

    notify = AsyncMock()
    monkeypatch.setattr(main_mod, "_MODULE_DIR", "/tmp/modules-forced")
    monkeypatch.setenv("ENABLED_MODULES", "test-mod")
    monkeypatch.setattr(
        "celerp.modules.migrations_runner.run_migration_phase",
        AsyncMock(return_value=({"test-mod"}, {})),
    )
    monkeypatch.setattr("celerp.modules.loader.load_all", lambda *a, **k: [])
    monkeypatch.setattr("celerp.modules.loader.register_api_routes", lambda *a, **k: None)
    monkeypatch.setattr("celerp.modules.loader.record_load_error", lambda *a, **k: None)
    monkeypatch.setattr("celerp.modules.loader.demoted_first_party", lambda *a, **k: [])
    monkeypatch.setattr("celerp.db.LifecycleSessionLocal", _Session)
    monkeypatch.setattr("celerp.services.dev_release_guard.run_upgrade_guard", _guard)
    monkeypatch.setattr("celerp.modules.slots.fire_lifecycle", _fire)
    monkeypatch.setattr("celerp.notifications.service.notify_every_company", notify, raising=False)
    monkeypatch.setattr("celerp.connectors.outbound_queue.adopt_legacy_connector_configs", AsyncMock())
    monkeypatch.setattr("celerp.gateway.has_active_share", AsyncMock(return_value=False))
    for target, name in _HELD_WHILE_NOT_CURRENT.items():
        def _ran(*a, _name=name, **k):
            order.append(_name)
            return asyncio.sleep(0)
        monkeypatch.setattr(target, _ran)
    saved_token, saved_public = settings.gateway_token, settings.celerp_public_url
    settings.gateway_token = "test-token"
    settings.celerp_public_url = None
    slots.clear()
    app = MagicMock()
    try:
        with _mock_db():
            async with main_mod.lifespan(app):
                pass
    finally:
        slots.clear()
        settings.gateway_token = saved_token
        settings.celerp_public_url = saved_public
    return order, notify, app


@pytest.mark.asyncio
async def test_projections_are_rebuilt_before_the_modules_settle_data(monkeypatch):
    """The upgrade guard (which rebuilds stale projections) runs before the
    on_modules_ready hooks, which settle data by reading those projections."""
    order, notify, app = await _boot_in_order(
        monkeypatch, AsyncMock(return_value={"changed": False, "rebuilt": False, "current": True}))

    assert order == _STARTED_WHEN_CURRENT
    assert app.state.data_current is True
    notify.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", [
    AsyncMock(return_value={"changed": True, "rebuilt": False, "current": False}),
    AsyncMock(side_effect=RuntimeError("rebuild failed")),
], ids=["not_current", "guard_failed"])
async def test_the_modules_settle_nothing_while_the_projections_are_not_current(monkeypatch, guard):
    """A start that could not bring the projections current runs no on_modules_ready hook
    and none of the startup backfills or background work that read the projections (they
    would change data from stale ones), boots anyway with the app marked not current, and
    tells every company."""
    order, notify, app = await _boot_in_order(monkeypatch, guard)

    assert order == ["guard"]
    assert app.state.data_current is False
    notify.assert_awaited_once()
    assert notify.await_args.args[1:3] == ("system", "Stored records could not be brought up to date")
