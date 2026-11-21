# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The update e2e boots real installs; they must never check in to the production relay."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from urllib.parse import urlparse

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("e2e_update", _ROOT / "scripts" / "e2e_update.py")
_e2e = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_e2e)


def test_install_points_relay_at_loopback(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_HTTP_URL", "https://relay.celerp.com")
    install = _e2e.Install(tmp_path, "relay")
    assert urlparse(install.env["GATEWAY_HTTP_URL"]).hostname == "127.0.0.1"


def test_wheel_build_env_overrides_workflow_pinned_version(monkeypatch):
    # CI pins SETUPTOOLS_SCM_PRETEND_VERSION_FOR_CELERP for every job's checkout, and
    # setuptools_scm prefers that package-specific override over the generic one this
    # script sets per variant, so it must not leak into a wheel built at a specific
    # release version.
    monkeypatch.setenv("SETUPTOOLS_SCM_PRETEND_VERSION_FOR_CELERP", "0.0.0")
    env = _e2e._wheel_build_env("90.0.1")
    assert env["SETUPTOOLS_SCM_PRETEND_VERSION"] == "90.0.1"
    assert "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_CELERP" not in env


def test_free_ports_stay_below_every_ephemeral_range():
    # Outbound connections take ports from the OS's ephemeral range (Linux from
    # 32768, macOS and Windows from 49152), so a port chosen there can be taken
    # between choosing it and the server binding it.
    ports = _e2e.free_ports(8)
    assert len(set(ports)) == 8
    assert all(1024 <= port < 32768 for port in ports), ports
