# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Instance identity tests: persistence across restarts + the startup activation probe.

A packaged install has no config.toml on first boot. The instance id, gateway
token, and backup encryption key must still be persisted so the install keeps
one stable identity across restarts, and the startup probe must ride through
transient network failures.
"""
from __future__ import annotations

import importlib

import httpx
import respx

# Shared config-reload helper; importing the autouse fixture registers it for
# this module too, restoring celerp.config after each test.
from test_config import _reload_config, _restore_config_module  # noqa: F401


# ---------------------------------------------------------------------------
# Instance identity - first-boot persistence (ensure_instance_id / persist_cloud_settings)
# ---------------------------------------------------------------------------

class TestInstanceIdentityFirstBoot:
    """The instance id must survive restarts even when config.toml does not
    exist yet, otherwise every launch of a packaged install looks like a brand
    new instance to the relay."""

    def test_ensure_instance_id_creates_config_file(self, tmp_path, monkeypatch):
        mod, cfg_file = _reload_config(tmp_path, monkeypatch)
        assert not cfg_file.exists()
        iid = mod.ensure_instance_id()
        assert iid
        assert cfg_file.exists()
        assert mod.read_config()["cloud"]["instance_id"] == iid

    def test_ensure_instance_id_stable_across_restart(self, tmp_path, monkeypatch):
        mod, _ = _reload_config(tmp_path, monkeypatch)
        iid = mod.ensure_instance_id()
        # Simulate app restart: reload module (fresh Settings), load cloud config
        importlib.reload(mod)
        mod.load_cloud_config()
        assert mod.ensure_instance_id() == iid

    def test_ensure_instance_id_stable_with_partial_config(self, tmp_path, monkeypatch):
        """A config.toml without [cloud] (e.g. written by the setup wizard) must
        gain the id without losing existing sections."""
        mod, _ = _reload_config(tmp_path, monkeypatch)
        mod.write_config({"modules": {"enabled": ["inventory"]}})
        iid = mod.ensure_instance_id()
        importlib.reload(mod)
        mod.load_cloud_config()
        assert mod.ensure_instance_id() == iid
        cfg = mod.read_config()
        assert "inventory" in cfg["modules"]["enabled"]

    def test_persist_cloud_settings_creates_file_and_keeps_values(self, tmp_path, monkeypatch):
        mod, cfg_file = _reload_config(tmp_path, monkeypatch)
        assert not cfg_file.exists()
        mod.persist_cloud_settings(token="tok-1", instance_id="iid-1", backup_encryption_key="key-1")
        cfg = mod.read_config()
        assert cfg["cloud"]["token"] == "tok-1"
        assert cfg["cloud"]["instance_id"] == "iid-1"
        assert cfg["cloud"]["backup_encryption_key"] == "key-1"

    def test_persist_cloud_settings_skips_falsy_never_erases(self, tmp_path, monkeypatch):
        mod, _ = _reload_config(tmp_path, monkeypatch)
        mod.persist_cloud_settings(token="tok-1", instance_id="iid-1", backup_encryption_key="key-1")
        mod.persist_cloud_settings(token="tok-2", instance_id="iid-1", public_url=None, backup_encryption_key="")
        cfg = mod.read_config()
        assert cfg["cloud"]["token"] == "tok-2"
        assert cfg["cloud"]["backup_encryption_key"] == "key-1"


# ---------------------------------------------------------------------------
# Startup activation probe - _try_auto_activate (celerp/main.py)
# ---------------------------------------------------------------------------

class TestAutoActivateProbe:
    """The startup probe must identify the instance on every boot path,
    retry transient transport failures, and persist credentials on success
    even when config.toml does not exist yet."""

    @staticmethod
    def _prepare(tmp_path, monkeypatch):
        mod, cfg_file = _reload_config(tmp_path, monkeypatch)
        mod.settings.gateway_http_url = "https://relay.test"
        mod.settings.gateway_token = ""
        # Retry delays must not slow the suite down
        import asyncio as _aio
        _real_sleep = _aio.sleep
        async def _no_sleep(_delay):
            await _real_sleep(0)
        monkeypatch.setattr("asyncio.sleep", _no_sleep)
        return mod, cfg_file

    @respx.mock
    async def test_payload_identifies_instance_and_first_boot(self, tmp_path, monkeypatch):
        mod, cfg_file = self._prepare(tmp_path, monkeypatch)
        route = respx.post("https://relay.test/auth/activate").respond(404, json={"detail": "no sub"})
        from celerp.main import _try_auto_activate
        await _try_auto_activate()
        assert route.call_count == 1  # HTTP status is final: no retry on 404
        import json as _json
        body = _json.loads(route.calls[0].request.content)
        assert body["instance_id"] == mod.settings.gateway_instance_id
        assert body["version"]
        assert body["platform"] in ("Linux", "Darwin", "Windows")
        assert body["first_boot"] is True

    @respx.mock
    async def test_second_boot_is_not_first_boot(self, tmp_path, monkeypatch):
        mod, cfg_file = self._prepare(tmp_path, monkeypatch)
        route = respx.post("https://relay.test/auth/activate").respond(404, json={"detail": "no sub"})
        from celerp.main import _try_auto_activate
        await _try_auto_activate()          # first boot creates config.toml
        assert cfg_file.exists()
        await _try_auto_activate()
        import json as _json
        body = _json.loads(route.calls[1].request.content)
        assert body["first_boot"] is False
        # Identity is stable across the two calls
        first = _json.loads(route.calls[0].request.content)
        assert body["instance_id"] == first["instance_id"]

    @respx.mock
    async def test_retries_transport_errors_then_succeeds(self, tmp_path, monkeypatch):
        mod, _ = self._prepare(tmp_path, monkeypatch)
        route = respx.post("https://relay.test/auth/activate")
        route.side_effect = [
            httpx.ConnectError("relay restarting"),
            httpx.ConnectError("relay restarting"),
            httpx.Response(404, json={"detail": "no sub"}),
        ]
        from celerp.main import _try_auto_activate
        await _try_auto_activate()
        assert route.call_count == 3

    @respx.mock
    async def test_gives_up_silently_after_transport_failures(self, tmp_path, monkeypatch):
        mod, _ = self._prepare(tmp_path, monkeypatch)
        route = respx.post("https://relay.test/auth/activate")
        route.side_effect = httpx.ConnectError("offline")
        from celerp.main import _try_auto_activate
        await _try_auto_activate()  # must not raise
        assert route.call_count == 3

    @respx.mock
    async def test_success_persists_credentials_without_config_file(self, tmp_path, monkeypatch):
        mod, cfg_file = self._prepare(tmp_path, monkeypatch)
        assert not cfg_file.exists()
        respx.post("https://relay.test/auth/activate").respond(200, json={
            "gateway_token": "gw-tok",
            "public_url": "https://abc.celerp.com",
            "tos_version": "2026-01",
        })
        # Pre-set a gateway client so the probe does not start a real WS client
        from celerp.gateway import client as _gw
        _prev_client = _gw.get_client()
        _gw.set_client(object())
        try:
            from celerp.main import _try_auto_activate
            await _try_auto_activate()
        finally:
            _gw.set_client(_prev_client)
        cfg = mod.read_config()
        assert cfg["cloud"]["token"] == "gw-tok"
        assert cfg["cloud"]["instance_id"] == mod.settings.gateway_instance_id
        assert cfg["cloud"]["public_url"] == "https://abc.celerp.com"
        assert cfg["cloud"]["backup_encryption_key"]  # generated and persisted

    @respx.mock
    async def test_free_tier_response_does_not_start_gateway_ws_client(self, tmp_path, monkeypatch):
        """/auth/activate now issues a gateway_token to free tier too (decision
        3, marketplace plan - buy an official module without Connect), but
        withholds public_url since free tier has no tunnel. The WS client
        must not start for that case - there is nothing for it to serve, and
        the free instance has no business holding a persistent gateway
        connection at all."""
        mod, _ = self._prepare(tmp_path, monkeypatch)
        respx.post("https://relay.test/auth/activate").respond(200, json={
            "gateway_token": "gw-tok-free",
            "public_url": None,
        })
        from celerp.gateway import client as _gw
        _prev_client = _gw.get_client()
        _gw.set_client(None)
        try:
            from celerp.main import _try_auto_activate
            await _try_auto_activate()
            assert _gw.get_client() is None, "WS client must not start without a public_url"
        finally:
            _gw.set_client(_prev_client)
        assert mod.settings.gateway_token == "gw-tok-free"  # credential still applied

    @respx.mock
    async def test_entitled_response_starts_gateway_ws_client(self, tmp_path, monkeypatch):
        """Regression guard for the gate above: a response WITH public_url
        must still start the WS client as before. GatewayClient.run is
        stubbed to a no-op so no real network connection is attempted."""
        mod, _ = self._prepare(tmp_path, monkeypatch)
        respx.post("https://relay.test/auth/activate").respond(200, json={
            "gateway_token": "gw-tok-paid",
            "public_url": "https://abc.celerp.com",
        })
        from celerp.gateway import client as _gw

        async def _noop_run(self):
            return None

        monkeypatch.setattr(_gw.GatewayClient, "run", _noop_run)
        _prev_client = _gw.get_client()
        _gw.set_client(None)
        try:
            from celerp.main import _try_auto_activate
            await _try_auto_activate()
            assert _gw.get_client() is not None, "WS client must start when a public_url is granted"
        finally:
            _gw.set_client(_prev_client)


class TestStickyDisconnect:
    """An explicit Cloud disconnect must hold until the user reconnects:
    the startup probe never quietly re-links a disconnected install."""

    @respx.mock
    async def test_disconnected_install_never_probes(self, tmp_path, monkeypatch):
        mod, _ = _reload_config(tmp_path, monkeypatch)
        mod.settings.gateway_http_url = "https://relay.test"
        mod.settings.gateway_token = ""
        mod.settings.cloud_disconnected = True
        route = respx.post("https://relay.test/auth/activate").respond(
            200, json={"gateway_token": "tok-1"})
        from celerp.main import _try_auto_activate
        await _try_auto_activate()
        assert route.call_count == 0
        assert mod.settings.gateway_token == ""

    def test_disconnect_flag_survives_restart(self, tmp_path, monkeypatch):
        mod, _ = _reload_config(tmp_path, monkeypatch)
        mod.write_config({"cloud": {"disconnected": True}})
        mod.load_cloud_config()
        assert mod.settings.cloud_disconnected is True

    def test_boot_while_disconnected_withholds_credential_but_keeps_identity(
            self, tmp_path, monkeypatch):
        # The credential stays in config for a one-click reconnect, but must not go
        # live on boot: gateway_token/public_url are withheld (tunnel down,
        # share-minting off, probe skipped), while instance_id still loads.
        mod, _ = _reload_config(tmp_path, monkeypatch)
        mod.settings.gateway_token = ""
        mod.settings.celerp_public_url = ""
        mod.settings.gateway_instance_id = ""
        mod.settings.cloud_disconnected = False
        mod.write_config({"cloud": {
            "token": "tok-keep", "instance_id": "iid-9",
            "public_url": "https://co.celerp.app", "disconnected": True}})
        mod.load_cloud_config()
        assert mod.settings.cloud_disconnected is True
        assert mod.settings.gateway_token == "", "credential must not go live while disconnected"
        assert mod.settings.celerp_public_url == ""
        assert mod.settings.gateway_instance_id == "iid-9", "identity still loads"
        # And the credential is still on disk for the reconnect path to re-apply.
        assert mod.read_config()["cloud"]["token"] == "tok-keep"

    def test_connected_config_loads_credential(self, tmp_path, monkeypatch):
        # The mirror of the above: with no disconnect flag, the credential loads
        # and the tunnel comes up as before.
        mod, _ = _reload_config(tmp_path, monkeypatch)
        mod.settings.gateway_token = ""
        mod.settings.celerp_public_url = ""
        mod.settings.cloud_disconnected = False
        mod.write_config({"cloud": {
            "token": "tok-live", "instance_id": "iid-9",
            "public_url": "https://co.celerp.app"}})
        mod.load_cloud_config()
        assert mod.settings.gateway_token == "tok-live"
        assert mod.settings.celerp_public_url == "https://co.celerp.app"
        assert mod.settings.cloud_disconnected is False
