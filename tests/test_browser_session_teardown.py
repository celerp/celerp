# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The browser session's API server leaves the test database empty when it stops, and a reset
that meets a held lock fails at once instead of hanging the session.

The server and the database are stand-ins: a real reset here would drop the schema of the
database the rest of the unit suite is using."""
from __future__ import annotations

import importlib.util
import sys
import threading
import types
from pathlib import Path

import pytest

_CONFTEST = Path(__file__).parent / "test_browser" / "conftest.py"


@pytest.fixture
def browser_conftest():
    spec = importlib.util.spec_from_file_location("browser_conftest_under_test", _CONFTEST)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_browser_session_leaves_database_empty(browser_conftest, monkeypatch):
    events: list[str] = []

    class Server:
        def __init__(self, config):
            self.should_exit = False
            self.stopped = threading.Event()

        def run(self):
            events.append("serving")
            while not self.should_exit:
                self.stopped.wait(0.01)
            events.append("stopped")

    # The server pins the module list for its own process; this one gets its value back.
    monkeypatch.delenv("ENABLED_MODULES", raising=False)
    monkeypatch.setattr(browser_conftest, "_require_free_port", lambda port: None)
    monkeypatch.setattr(browser_conftest, "_wait_for_port", lambda port: None)
    monkeypatch.setattr(browser_conftest, "_reset_pg_database", lambda: events.append("reset"))
    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(
        Config=lambda *a, **k: None, Server=Server))

    session = browser_conftest._serve_api()
    next(session)
    assert events == ["reset", "serving"]
    with pytest.raises(StopIteration):
        next(session)
    assert events == ["reset", "serving", "stopped", "reset"]


def test_database_reset_fails_on_a_held_lock_instead_of_waiting(browser_conftest, monkeypatch):
    executed: list[str] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql):
            executed.append(sql)

    class Connection:
        autocommit = False

        def cursor(self):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "psycopg2", types.SimpleNamespace(connect=lambda **k: Connection()))
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:5432/x_test")
    browser_conftest._reset_pg_database()
    assert executed and executed[0].startswith("SET lock_timeout")
    assert "DROP SCHEMA public CASCADE" in executed[-1]
