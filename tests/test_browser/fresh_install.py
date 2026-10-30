# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A new install for tests: an API and a UI process on their own empty database, started
as the desktop app starts them, so they load only the modules a new install loads.

The suite's shared servers already carry a seeded owner and every module, so a test that
needs a new install (or a process that has not loaded a module) uses this instead, through
the ``install`` fixture (one per test module) or ``fresh`` (the same, with no owner yet).
"""
from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from boot_support import Supervised, wait_until_started

REPO = Path(__file__).resolve().parents[2]
_worker = os.environ.get("PYTEST_XDIST_WORKER", "")
_offset = int(_worker[2:]) if _worker[2:].isdigit() else 0
_BASE = int(os.environ.get("BROWSER_TEST_PORT_BASE", "18000"))
API_PORT = _BASE + 200 + _offset
UI_PORT = _BASE + 300 + _offset


def _pg(dbname: str):
    import psycopg2
    parts = urlsplit(os.environ["DATABASE_URL"].replace("+asyncpg", ""))
    conn = psycopg2.connect(host=parts.hostname, port=parts.port, user=parts.username,
                            password=parts.password, dbname=dbname)
    conn.autocommit = True
    return conn


def _wait_http(url: str, proc: subprocess.Popen, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server for {url} exited with {proc.returncode}")
        try:
            httpx.get(url, timeout=1.0)
            return
        except httpx.HTTPError:
            time.sleep(0.2)
    raise RuntimeError(f"{url} did not answer within {timeout}s")


# What a new install's servers inherit from the machine. Everything else the test process holds
# (ENABLED_MODULES and the data folder the shared browser servers set, any PYTHONPATH) is left
# out, as the desktop app leaves it out, so this install loads only what a new install loads
# whichever tests ran before it.
MACHINE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT")


def install_env() -> dict:
    return {k: os.environ[k] for k in MACHINE_ENV if k in os.environ}


class FreshInstall:
    """An API and a UI process on their own database, with no owner yet."""

    def __init__(self) -> None:
        url = os.environ["DATABASE_URL"]
        parts = urlsplit(url)
        self.db = parts.path.lstrip("/") + "_first_run"
        self.db_url = url.rsplit("/", 1)[0] + "/" + self.db
        self.api = f"http://127.0.0.1:{API_PORT}"
        self.ui = f"http://127.0.0.1:{UI_PORT}"
        self.tmp = Path(tempfile.mkdtemp(prefix="celerp-first-run-"))
        self.servers: Supervised | None = None

    def _spawn(self, app: str, port: int, env: dict) -> subprocess.Popen:
        with open(self.tmp / f"{port}.log", "ab") as log:
            return subprocess.Popen(
                [sys.executable, "-m", "uvicorn", app, "--host", "127.0.0.1", "--port", str(port)],
                cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT,
            )

    def start(self) -> None:
        for port in (API_PORT, UI_PORT):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    raise RuntimeError(f"port {port} is in use; set BROWSER_TEST_PORT_BASE")
        cfg = self.tmp / "config"
        shutil.rmtree(cfg, ignore_errors=True)
        cfg.mkdir()
        self.restart_requested = cfg / ".restart_requested"
        self.env = install_env()
        self.env.update({
            "DATA_DIR": str(self.tmp / "data"),
            "JWT_SECRET": secrets.token_urlsafe(32),
            "DATABASE_URL": self.db_url,
            "CELERP_CONFIG": str(cfg / "config.toml"),
            "MODULE_DIR": ",".join(str(REPO / d) for d in ("default_modules", "premium_modules")
                                   if (REPO / d).is_dir()),
            "API_URL": self.api,
            "CELERP_API_URL": self.api,
        })
        self.servers = Supervised(self._launch, self.restart_requested)
        wait_until_started(self.api)

    def _launch(self) -> tuple[subprocess.Popen, subprocess.Popen]:
        procs = [self._spawn("celerp.main:app", API_PORT, self.env)]
        try:
            _wait_http(f"{self.api}/health", procs[0])
            procs.append(self._spawn("ui.app:app", UI_PORT, self.env))
            _wait_http(f"{self.ui}/health", procs[1])
        except Exception:
            for proc in procs:
                proc.kill()
            raise
        return procs[0], procs[1]

    def stop(self) -> None:
        if self.servers:
            self.servers.stop()
            self.servers = None

    def reset(self) -> None:
        """Back to a new install: no owner, no company, a fresh config."""
        self.stop()
        conn = _pg(self.db)
        try:
            with conn.cursor() as cur:
                cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        finally:
            conn.close()
        self.start()

    def ensure_new(self) -> None:
        """Empty the install again when an earlier test finished setup on it."""
        if httpx.get(f"{self.api}/auth/bootstrap-status", timeout=10).json().get("bootstrapped"):
            self.reset()

    def company(self, ctx) -> dict:
        """The signed-in browser's company, read with its own session token."""
        token = next(c["value"] for c in ctx.cookies() if c["name"] == "celerp_token")
        r = httpx.get(f"{self.api}/companies/me", headers={"Authorization": f"Bearer {token}"}, timeout=10)
        r.raise_for_status()
        return r.json()


@contextmanager
def new_install():
    """A FreshInstall on its own database, started, and removed with its database afterwards."""
    fresh = FreshInstall()
    conn = _pg("postgres")
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{fresh.db}" WITH (FORCE)')
            cur.execute(f'CREATE DATABASE "{fresh.db}"')
    finally:
        conn.close()
    fresh.start()
    try:
        yield fresh
    finally:
        fresh.stop()
        conn = _pg("postgres")
        try:
            with conn.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{fresh.db}" WITH (FORCE)')
        finally:
            conn.close()
        shutil.rmtree(fresh.tmp, ignore_errors=True)
