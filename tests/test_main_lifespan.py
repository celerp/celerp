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
    monkeypatch.setattr("celerp.db.LifecycleSessionLocal", lambda: _FakeSession(rollback_spy))

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
