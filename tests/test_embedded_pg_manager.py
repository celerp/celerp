# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Unit tests for the in-house embedded-Postgres cluster manager
(celerp.embedded_pg internals). Fast: subprocess is mocked; no cluster,
no celerp-postgres needed."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from celerp import embedded_pg


# Original _env captured before the autouse fixture patches it, so the ICU test
# can exercise the real logic against a fake celerp_postgres module.
_REAL_ENV = embedded_pg._env


def _ok(*a, **k):
    return subprocess.CompletedProcess(a, 0, stdout="", stderr="")


def _fail(*a, **k):
    return subprocess.CompletedProcess(a, 1, stdout="boom-out", stderr="boom-err")


@pytest.fixture(autouse=True)
def _fake_provider(monkeypatch):
    """Manager units never touch real binaries or env-append logic."""
    monkeypatch.setattr(embedded_pg, "_tool", lambda n: f"/fake/bin/{n}")
    monkeypatch.setattr(embedded_pg, "_env", lambda: os.environ.copy())
    embedded_pg._STARTED.clear()


def test_socket_dir_deterministic_and_short(tmp_path):
    deep = tmp_path / ("d" * 60) / ("e" * 60) / "pgdata"
    s1, s2 = embedded_pg._socket_dir(deep), embedded_pg._socket_dir(deep)
    assert s1 == s2                                # same pgdata -> same dir
    assert s1 != embedded_pg._socket_dir(tmp_path / "other")
    # AF_UNIX limit is 107 chars incl. the socket filename (~25 chars)
    assert len(str(s1)) < 60


def test_win_port_persisted_and_reused(tmp_path):
    with patch("socket.socket") as ms:
        ms.return_value.__enter__.return_value.getsockname.return_value = ("127.0.0.1", 54321)
        assert embedded_pg._win_port(tmp_path) == 54321
    # Second call must NOT consult the OS again — the file wins.
    assert embedded_pg._win_port(tmp_path) == 54321
    assert (tmp_path / "celerp.port").read_text() == "54321"


def test_uri_shapes():
    assert (
        embedded_pg._uri("/tmp/celerp-pg-ab12", None, "celerp")
        == "postgresql+asyncpg://postgres@/celerp?host=/tmp/celerp-pg-ab12"
    )
    assert (
        embedded_pg._uri("127.0.0.1", 54321, "celerp")
        == "postgresql+asyncpg://postgres@127.0.0.1:54321/celerp"
    )
    assert ":5432" not in embedded_pg._uri("/s", None, "celerp")


def test_start_skipped_when_running(tmp_path):
    with patch.object(embedded_pg, "_is_running", return_value=True), \
         patch("subprocess.run", side_effect=_ok) as mrun:
        embedded_pg._start(tmp_path)
    started = [c for c in mrun.call_args_list if "start" in str(c)]
    assert not started, "pg_ctl start must not run when the cluster is up"


def test_start_registers_atexit_stop(tmp_path):
    with patch.object(embedded_pg, "_is_running", return_value=False), \
         patch("subprocess.run", side_effect=_ok), \
         patch("atexit.register") as mreg:
        embedded_pg._start(tmp_path)
    assert tmp_path in embedded_pg._STARTED
    mreg.assert_called_once_with(embedded_pg._stop_all)


def test_start_failure_surfaces_server_log(tmp_path):
    (tmp_path / "server.log").write_text("FATAL: broken pin\n")
    with patch.object(embedded_pg, "_is_running", return_value=False), \
         patch("subprocess.run", side_effect=_fail):
        with pytest.raises(RuntimeError) as ei:
            embedded_pg._start(tmp_path)
    msg = str(ei.value)
    assert "boom-err" in msg and "broken pin" in msg


def test_initdb_failure_surfaces_output(tmp_path):
    with patch("subprocess.run", side_effect=_fail):
        with pytest.raises(RuntimeError, match="initdb failed"):
            embedded_pg._initdb(tmp_path)


def test_ensure_cluster_skips_initdb_when_initialized(tmp_path, monkeypatch):
    pgdata = embedded_pg.pgdata_dir(tmp_path)
    pgdata.mkdir(parents=True)
    (pgdata / "PG_VERSION").write_text("17")
    monkeypatch.setattr(embedded_pg, "_initdb",
                        lambda *_: (_ for _ in ()).throw(AssertionError("initdb ran")))
    monkeypatch.setattr(embedded_pg, "_start", lambda p: ("/sock", None))
    monkeypatch.setattr(embedded_pg, "_ensure_app_database", lambda h, p: None)
    uri = embedded_pg.ensure_cluster(tmp_path)
    assert uri.endswith("/celerp?host=/sock")


def test_wipe_stops_then_removes(tmp_path):
    pgdata = embedded_pg.pgdata_dir(tmp_path)
    pgdata.mkdir(parents=True)
    (pgdata / "PG_VERSION").write_text("17")
    calls = []
    with patch.object(embedded_pg, "_is_running", return_value=True), \
         patch("subprocess.run", side_effect=lambda *a, **k: calls.append(a[0]) or _ok()):
        embedded_pg.wipe(tmp_path)
    assert not pgdata.exists()
    assert any("stop" in c for c in calls[0]), "stop must precede rmtree"


def test_wipe_missing_is_noop(tmp_path):
    embedded_pg.wipe(tmp_path)  # must not raise


def test_env_sets_icu_data_when_bundled(monkeypatch):
    """The real _env must export ICU_DATA iff the provider bundles ICU data
    (musl wheels) — postmaster and initdb both need it there."""
    import sys as _sys

    class FakeCP:
        @staticmethod
        def icu_data_dir():
            return "/site/celerp_postgres/pginstall/share/icu"

    monkeypatch.setitem(_sys.modules, "celerp_postgres", FakeCP)
    assert _REAL_ENV()["ICU_DATA"].endswith("share/icu")

    class FakeCPNone:
        @staticmethod
        def icu_data_dir():
            return None

    monkeypatch.setitem(_sys.modules, "celerp_postgres", FakeCPNone)
    # No bundled data (glibc/mac/win wheels): ambient env passes through untouched.
    assert _REAL_ENV().get("ICU_DATA") == os.environ.get("ICU_DATA")
