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
