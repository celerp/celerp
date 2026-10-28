# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Waiting for an API and a UI a test boots as processes of their own."""
from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx


def wait_until_started(api_url: str, timeout: float = 60.0) -> None:
    """Wait until the API serves module routes. Each answers 503 "still starting" until the UI
    process has reported the modules it started (ModuleStartupMiddleware), which can come after
    both processes first answer. /items is the inventory module's, which every boot runs."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{api_url}/items", timeout=2).status_code != 503:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise RuntimeError(f"{api_url} was still starting after {timeout}s")


class Supervised:
    """An API and a UI run the way `celerp start` runs them: when the API exits after asking
    for a restart (the sentinel beside its config file, as turning on a module does), both
    start again. Without this a test that reaches a restart is left with no API."""

    def __init__(self, spawn: Callable[[], tuple[subprocess.Popen, subprocess.Popen]], sentinel: Path):
        self._spawn, self._sentinel = spawn, sentinel
        self._lock = threading.Lock()
        self._stopped = False
        self.procs = spawn()
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self) -> None:
        while not self._stopped:
            with self._lock:
                api, ui = self.procs
                if not self._stopped and api.poll() is not None and self._sentinel.exists():
                    self._sentinel.unlink()
                    _stop(ui)
                    self.procs = self._spawn()
            time.sleep(0.2)

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            for proc in self.procs:
                _stop(proc)


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
