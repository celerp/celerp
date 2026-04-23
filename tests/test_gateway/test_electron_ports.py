# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Regression tests: GatewayClient port resolution must use env vars in Electron.

These tests prevent reintroduction of the bug where GatewayClient proxied
relay traffic to static defaults (8000/8080) instead of the dynamic ports
chosen by Electron at runtime.

Root cause: Electron picks apiPort/uiPort dynamically via getFreePort() and
passes them as CELERP_API_URL / CELERP_UI_URL env vars. The old code read
ports from config.toml only (defaulting to 8000/8080), so relay proxy
requests were forwarded to the wrong ports in Electron.

Fix: _resolve_ports() reads CELERP_API_URL and CELERP_UI_URL (highest
priority), then config.toml [server] section, then static defaults.
"""

from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
from unittest.mock import patch

from celerp.gateway.client import GatewayClient


@pytest.fixture
def client():
    return GatewayClient(
        gateway_token="tok",
        instance_id="iid",
        gateway_url="wss://relay.celerp.com/ws/connect",
    )


# ── _port_from_url ────────────────────────────────────────────────────────────

class TestPortFromUrl:
    def test_extracts_port_from_http_url(self, client):
        with patch.dict(os.environ, {"CELERP_TEST_URL": "http://127.0.0.1:54321"}):
            assert client._port_from_url("CELERP_TEST_URL", 9999) == 54321

    def test_returns_fallback_when_env_var_missing(self, client):
        env = {k: v for k, v in os.environ.items() if k != "CELERP_TEST_URL"}
        with patch.dict(os.environ, env, clear=True):
            assert client._port_from_url("CELERP_TEST_URL", 1234) == 1234

    def test_returns_fallback_when_env_var_empty(self, client):
        with patch.dict(os.environ, {"CELERP_TEST_URL": ""}):
            assert client._port_from_url("CELERP_TEST_URL", 4321) == 4321

    def test_returns_fallback_when_no_port_in_url(self, client):
        with patch.dict(os.environ, {"CELERP_TEST_URL": "https://relay.celerp.com"}):
            # No explicit port in URL - falls back
            assert client._port_from_url("CELERP_TEST_URL", 7777) == 7777


# ── _resolve_ports ────────────────────────────────────────────────────────────

class TestResolvePorts:
    def _clean_env(self):
        """Return env dict without any port-related env vars."""
        strip = {"CELERP_API_URL", "CELERP_UI_URL", "API_URL"}
        return {k: v for k, v in os.environ.items() if k not in strip}

    def test_electron_env_vars_take_priority(self, client):
        """CELERP_API_URL and CELERP_UI_URL override everything else."""
        env = {
            **self._clean_env(),
            "CELERP_API_URL": "http://127.0.0.1:41000",
            "CELERP_UI_URL": "http://127.0.0.1:42000",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch("celerp.gateway.client.GatewayClient._resolve_ports",
                       wraps=client._resolve_ports):
                api_port, ui_port = client._resolve_ports()
        assert api_port == 41000
        assert ui_port == 42000

    def test_api_url_fallback_for_api_port(self, client):
        """Legacy API_URL (set by Electron for UI process) used as fallback."""
        env = {
            **self._clean_env(),
            "API_URL": "http://127.0.0.1:43000",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch("celerp.config.read_config", return_value={}):
                api_port, ui_port = client._resolve_ports()
        assert api_port == 43000
        # UI port falls back to config/default since CELERP_UI_URL absent
        assert ui_port == 8080

    def test_config_toml_used_when_no_env_vars(self, client):
        """config.toml [server] section is authoritative when env vars absent."""
        env = self._clean_env()
        cfg = {"server": {"api_port": 9001, "ui_port": 9002}}
        with patch.dict(os.environ, env, clear=True):
            with patch("celerp.config.read_config", return_value=cfg):
                api_port, ui_port = client._resolve_ports()
        assert api_port == 9001
        assert ui_port == 9002

    def test_static_defaults_as_last_resort(self, client):
        """Falls back to 8000/8080 only when nothing else provides a port."""
        env = self._clean_env()
        with patch.dict(os.environ, env, clear=True):
            with patch("celerp.config.read_config", return_value={}):
                api_port, ui_port = client._resolve_ports()
        assert api_port == 8000
        assert ui_port == 8080

    def test_no_static_port_hardcode_at_init(self):
        """GatewayClient.__init__ must NOT cache ports as instance attributes.

        Ensures we can never regress to the old pattern of reading ports at
        construction time (which would silently use wrong defaults in Electron).
        """
        c = GatewayClient(
            gateway_token="t", instance_id="i",
            gateway_url="wss://relay.celerp.com/ws/connect",
        )
        assert not hasattr(c, "_ui_port"), \
            "_ui_port must not be set at init; use _resolve_ports() at call time"
        assert not hasattr(c, "_api_port"), \
            "_api_port must not be set at init; use _resolve_ports() at call time"
