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

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from celerp.modules.loader import Admission

# Boot reads and maintains the real database; it needs the schema to exist whichever
# test runs first.
pytestmark = pytest.mark.usefixtures("_db_engine")


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


@contextmanager
def _mock_db():
    """Make celerp.main.lifecycle_engine.begin() a no-op (no real DDL at boot), and
    the version fence, which joins through that engine's URL, a held no-op.

    Boot's create_all runs on the lifecycle engine, so that is the one to stub."""
    mock_conn = AsyncMock()
    mock_conn.run_sync = AsyncMock()
    mock_begin = MagicMock()
    mock_begin.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_begin.__aexit__ = AsyncMock(return_value=False)
    mock_engine = MagicMock()
    mock_engine.begin = MagicMock(return_value=mock_begin)
    mock_engine.url.render_as_string.return_value = "postgresql+asyncpg://celerp@localhost/mocked"
    with patch("celerp.main.lifecycle_engine", mock_engine), \
         patch("celerp.migrations.compatibility.Fence.join", return_value=MagicMock()):
        yield


_NO_MODULES = Admission(admitted=[], refused={})


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
    monkeypatch.setattr("celerp.modules.loader.admit_modules", lambda *a, **k: _NO_MODULES)
    monkeypatch.setattr(
        "celerp.modules.migrations_runner.run_migration_phase",
        AsyncMock(return_value=_NO_MODULES),
    )
    monkeypatch.setattr("celerp.modules.loader.load_all", lambda *a, **k: [])
    monkeypatch.setattr("celerp.modules.loader.register_api_routes", lambda *a, **k: None)
    monkeypatch.setattr("celerp.modules.loader.demoted_first_party", lambda *a, **k: [])
    monkeypatch.setattr("celerp.db.LifecycleSessionLocal", lambda: _FakeSession(rollback_spy))
    # Boot's connector adoption reads the companies table; this test has no schema.
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
    monkeypatch.setattr("celerp.modules.loader.admit_modules", lambda *a, **k: _NO_MODULES)
    monkeypatch.setattr(
        "celerp.modules.migrations_runner.run_migration_phase",
        AsyncMock(return_value=_NO_MODULES),
    )
    monkeypatch.setattr("celerp.modules.loader.load_all", lambda *a, **k: [])
    monkeypatch.setattr("celerp.modules.loader.register_api_routes", lambda *a, **k: None)

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
    try:
        with _mock_db():
            async with main_mod.lifespan(MagicMock()):
                pass
    finally:
        slots.clear()
        settings.gateway_token = saved_token
        settings.celerp_public_url = saved_public

    fire.assert_not_awaited()
    associate.assert_not_awaited()
    adopt.assert_awaited_once()


def _transactions_left_open(url) -> list[tuple]:
    """Client connections to ``url``'s database still inside a transaction, read on a
    blocking connection so nothing else on the event loop runs while it looks."""
    import time

    import psycopg2

    conn = psycopg2.connect(host=url.host, port=url.port, user=url.username,
                            password=url.password, dbname=url.database)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            deadline = time.monotonic() + 2
            while True:
                cur.execute(
                    "SELECT pid, state, query FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                    "AND backend_type = 'client backend' AND xact_start IS NOT NULL")
                rows = cur.fetchall()
                if not rows or time.monotonic() > deadline:
                    return rows
                time.sleep(0.1)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_shutdown_leaves_no_connection_in_a_transaction(monkeypatch):
    """Shutdown stops the background work boot started and waits for it: a payment
    reconcile still mid-statement when the app stops leaves no connection open in a
    transaction. Such a connection would hold its table locks until garbage collection,
    blocking any later schema change or TRUNCATE on the same database."""
    import asyncio

    from sqlalchemy import select, text

    import celerp.db
    import celerp.main as main_mod
    from celerp.config import settings
    from celerp.models.payment_closure import PaymentClosure
    from celerp.services import payments

    in_flight = asyncio.Event()

    async def _report_recoveries_in_flight(session):
        await session.execute(select(PaymentClosure.operation_id))
        in_flight.set()
        await session.execute(text("SELECT pg_sleep(30)"))

    monkeypatch.setattr(payments, "report_recoveries", _report_recoveries_in_flight)
    monkeypatch.setattr(main_mod, "_try_auto_activate", AsyncMock())

    saved_token, saved_public = settings.gateway_token, settings.celerp_public_url
    settings.gateway_token = ""
    settings.celerp_public_url = None
    url = celerp.db.engine.url
    try:
        async with main_mod.lifespan(MagicMock()):
            await asyncio.wait_for(in_flight.wait(), 30)
            for _ in range(100):
                if any("pg_sleep" in q for _, _, q in _transactions_left_open(url)):
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("the reconcile statement never reached the database")
        left_open = _transactions_left_open(url)
    finally:
        settings.gateway_token = saved_token
        settings.celerp_public_url = saved_public

    assert left_open == [], f"shutdown left connections in a transaction: {left_open}"


@pytest.mark.asyncio
async def test_shutdown_returns_with_every_boot_task_finished(monkeypatch):
    """Every background task boot starts (payment reconcile, cleanup loops, connector
    schedulers, reorder alerts, update checks, the relay check-in, the backup scheduler)
    has finished by the time shutdown returns, including one that takes a moment to
    close its work after being told to stop."""
    import asyncio

    import celerp.main as main_mod
    from celerp.config import settings

    stopped: list[str] = []

    def _slow_to_stop(name: str):
        async def _task(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0.3)  # closing its work, e.g. a connection
                stopped.append(name)
                raise
        return _task

    async def _stops_at_once(*args, **kwargs):
        await asyncio.Event().wait()

    tasks = {
        "celerp.services.payments.reconcile_payments_loop": "reconcile",
        "celerp.services.session_tracker.run_jti_cleanup_loop": "jti cleanup",
        "celerp.connectors.outbound_queue.outbound_queue_loop": "outbound connectors",
        "celerp.connectors.daily_scheduler.scheduler_loop_all": "connector schedule",
        "celerp.services.reorder.reorder_alert_loop": "reorder alerts",
        "celerp.services.update.update_loop": "update checks",
        "celerp.services.backup_scheduler._db_backup_loop": "backups",
        "celerp.main._try_auto_activate": "relay check-in",
    }
    for target, name in tasks.items():
        monkeypatch.setattr(target, _slow_to_stop(name))
    monkeypatch.setattr("celerp.ai.cleanup.run_cleanup_loop", _stops_at_once)

    saved = (settings.gateway_token, settings.celerp_public_url,
             settings.backup_encryption_key, settings.backup_enabled)
    settings.gateway_token = ""
    settings.celerp_public_url = "https://acme.celerp.com"
    settings.backup_encryption_key = "k"
    settings.backup_enabled = True
    try:
        async with main_mod.lifespan(MagicMock()):
            await asyncio.sleep(0)  # every task is running
        finished = sorted(stopped)
    finally:
        (settings.gateway_token, settings.celerp_public_url,
         settings.backup_encryption_key, settings.backup_enabled) = saved

    assert finished == sorted(tasks.values())


@pytest.mark.asyncio
async def test_shutdown_ends_the_process_when_a_task_will_not_stop(monkeypatch, caplog):
    """Waiting for background tasks at shutdown is bounded: a task that ignores being
    cancelled ends the process once the grace period is over, before shutdown goes on
    to give up the database, and the task is named in the log."""
    import asyncio
    import logging

    import celerp.main as main_mod

    class _Ended(BaseException):
        pass

    def _exit(code):
        raise _Ended(code)

    release = asyncio.Event()

    async def _stubborn():
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    monkeypatch.setattr(main_mod, "_SHUTDOWN_GRACE_S", 0.2)
    monkeypatch.setattr(main_mod._os, "_exit", _exit)
    stubborn = asyncio.create_task(_stubborn(), name="stubborn")
    quick = asyncio.create_task(asyncio.sleep(3600), name="quick")
    await asyncio.sleep(0)  # both are running
    try:
        with caplog.at_level(logging.CRITICAL, logger="celerp.main"), pytest.raises(_Ended) as ended:
            await asyncio.wait_for(main_mod._stop_background_tasks([stubborn, quick, None]), 5)
        assert ended.value.args == (1,)
        assert quick.cancelled()
        assert "stubborn" in caplog.text
    finally:
        release.set()
        await stubborn


async def _boot_with(monkeypatch, *, sweep, reconcile) -> bool:
    import celerp.main as main_mod
    from celerp.config import settings
    from celerp.services import company_backup, company_backup_files

    monkeypatch.setattr(company_backup_files, "sweep_transient_files", sweep)
    monkeypatch.setattr(company_backup, "reconcile_landings", reconcile)
    monkeypatch.setattr("celerp.gateway.has_active_share", AsyncMock(return_value=False))
    saved_token, saved_public = settings.gateway_token, settings.celerp_public_url
    settings.gateway_token = ""
    settings.celerp_public_url = None
    entered = False
    try:
        with _mock_db():
            async with main_mod.lifespan(MagicMock()):
                entered = True
    finally:
        settings.gateway_token = saved_token
        settings.celerp_public_url = saved_public
    return entered


@pytest.mark.asyncio
async def test_a_failed_backup_file_sweep_still_cleans_up_unfinished_restores(monkeypatch):
    """The two startup clean-ups are independent: the restore clean-up still runs when the
    sweep of leftover backup files fails, and neither stops boot."""
    swept: list[bool] = []

    def _sweep():
        swept.append(True)
        raise OSError("backup folder unreadable")

    reconcile = AsyncMock()
    assert await _boot_with(monkeypatch, sweep=_sweep, reconcile=reconcile)
    assert swept == [True]
    reconcile.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_failed_restore_clean_up_does_not_stop_boot(monkeypatch):
    swept: list[bool] = []
    reconcile = AsyncMock(side_effect=RuntimeError("database unavailable"))
    assert await _boot_with(monkeypatch, sweep=lambda: swept.append(True), reconcile=reconcile)
    assert swept == [True]
    reconcile.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_stops_migration_runs(monkeypatch):
    """A migration run still going when the app stops is stopped with the boot tasks."""
    import asyncio
    import uuid

    import celerp.main as main_mod
    from celerp.config import settings
    from celerp.services import migrations

    stopped = []

    async def _run(run_id):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopped.append(run_id)
            raise

    monkeypatch.setattr(migrations, "run_migration", _run)
    monkeypatch.setattr(main_mod, "_try_auto_activate", AsyncMock())
    saved_token, saved_public = settings.gateway_token, settings.celerp_public_url
    settings.gateway_token = ""
    settings.celerp_public_url = None
    run_id = uuid.uuid4()
    try:
        async with main_mod.lifespan(MagicMock()):
            migrations.schedule_run(run_id)
            await asyncio.sleep(0)
    finally:
        settings.gateway_token = saved_token
        settings.celerp_public_url = saved_public
    assert stopped == [run_id]
