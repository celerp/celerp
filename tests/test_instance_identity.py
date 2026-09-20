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
import threading
import time

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


    def test_activation_write_races_unrelated_config_update_without_lost_state(
            self, tmp_path, monkeypatch):
        """Both writers hold the same lock across read+mutation+write. Under the
        old read-then-write pattern the module writer could snapshot before the
        activation write and later erase the freshly persisted credential."""
        mod, _ = _reload_config(tmp_path, monkeypatch)
        mod.write_config({"modules": {"enabled": []}})

        activation_inside_write = threading.Event()
        release_activation = threading.Event()
        errors = []
        real_write = mod._write_config_unlocked

        def blocked_write(cfg):
            if threading.current_thread().name == "activation-writer":
                activation_inside_write.set()
                if not release_activation.wait(2):
                    raise TimeoutError("test barrier timed out")
            real_write(cfg)

        monkeypatch.setattr(mod, "_write_config_unlocked", blocked_write)

        def activation_writer():
            try:
                mod.record_cloud_activation(
                    "gw-race", "iid-race", public_url="https://race.celerp.com")
            except Exception as exc:
                errors.append(exc)

        def module_writer():
            try:
                mod.set_enabled_modules(["inventory"])
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=activation_writer, name="activation-writer")
        t1.start()
        assert activation_inside_write.wait(1)

        t2 = threading.Thread(target=module_writer, name="module-writer")
        t2.start()
        time.sleep(0.05)
        assert t2.is_alive(), "unrelated writer should be blocked by the RMW lock"

        release_activation.set()
        t1.join(2)
        t2.join(2)
        assert not t1.is_alive() and not t2.is_alive()
        assert errors == []

        cfg = mod.read_config()
        assert cfg["cloud"]["token"] == "gw-race"
        assert cfg["cloud"]["instance_id"] == "iid-race"
        assert cfg["cloud"]["public_url"] == "https://race.celerp.com"
        assert "inventory" in cfg["modules"]["enabled"]


# ---------------------------------------------------------------------------
# Startup activation probe - _try_auto_activate (celerp/main.py)
# ---------------------------------------------------------------------------

class TestAutoActivateProbe:
    """Startup is observational unless a durable verifier proves interrupted recovery."""

    @staticmethod
    def _prepare(tmp_path, monkeypatch):
        mod, cfg_file = _reload_config(tmp_path, monkeypatch)
        mod.settings.gateway_http_url = "https://relay.test"
        mod.settings.gateway_token = ""
        import asyncio as _aio
        _real_sleep = _aio.sleep
        async def _no_sleep(_delay):
            await _real_sleep(0)
        monkeypatch.setattr("asyncio.sleep", _no_sleep)
        return mod, cfg_file

    @respx.mock
    async def test_payload_identifies_instance_and_first_boot(self, tmp_path, monkeypatch):
        mod, _ = self._prepare(tmp_path, monkeypatch)
        route = respx.post("https://relay.test/auth/checkin").respond(200, json={"ok": True})
        from celerp.main import _try_auto_activate
        await _try_auto_activate()
        assert route.call_count == 1
        import json as _json
        body = _json.loads(route.calls[0].request.content)
        assert body["instance_id"] == mod.settings.gateway_instance_id
        assert body["version"]
        assert body["platform"] in ("Linux", "Darwin", "Windows")
        assert body["first_boot"] is True
        assert "activation_verifier" not in body

    @respx.mock
    async def test_second_boot_is_not_first_boot(self, tmp_path, monkeypatch):
        mod, cfg_file = self._prepare(tmp_path, monkeypatch)
        route = respx.post("https://relay.test/auth/checkin").respond(200, json={"ok": True})
        from celerp.main import _try_auto_activate
        await _try_auto_activate()
        assert cfg_file.exists()
        await _try_auto_activate()
        import json as _json
        first = _json.loads(route.calls[0].request.content)
        second = _json.loads(route.calls[1].request.content)
        assert first["first_boot"] is True
        assert second["first_boot"] is False
        assert second["instance_id"] == first["instance_id"]

    @respx.mock
    async def test_checkin_transport_failure_is_not_retried_or_activated(self, tmp_path, monkeypatch):
        self._prepare(tmp_path, monkeypatch)
        checkin = respx.post("https://relay.test/auth/checkin")
        checkin.side_effect = httpx.ConnectError("offline")
        activate = respx.post("https://relay.test/auth/activate").respond(
            200, json={"gateway_token": "must-not-be-requested"})
        from celerp.main import _try_auto_activate
        await _try_auto_activate()
        assert checkin.call_count == 1
        assert activate.call_count == 0

    @respx.mock
    async def test_verifier_recovery_retries_transport_errors_then_stops_on_http(self, tmp_path, monkeypatch):
        mod, _ = self._prepare(tmp_path, monkeypatch)
        mod.ensure_instance_id()
        verifier = mod.ensure_activation_verifier()
        route = respx.post("https://relay.test/auth/activate")
        route.side_effect = [
            httpx.ConnectError("relay restarting"),
            httpx.ConnectError("relay restarting"),
            httpx.Response(401, json={"detail": "proof not approved"}),
        ]
        from celerp.main import _try_auto_activate
        await _try_auto_activate()
        assert route.call_count == 3
        import json as _json
        body = _json.loads(route.calls[-1].request.content)
        assert body["activation_verifier"] == verifier

    @respx.mock
    async def test_verifier_recovery_persists_credentials_and_consumes_verifier(self, tmp_path, monkeypatch):
        mod, _ = self._prepare(tmp_path, monkeypatch)
        iid = mod.ensure_instance_id()
        verifier = mod.ensure_activation_verifier()
        route = respx.post("https://relay.test/auth/activate").respond(200, json={
            "gateway_token": "gw-tok",
            "public_url": None,
            "tos_version": "2026-01",
        })
        from celerp.gateway import client as _gw
        _prev_client = _gw.get_client()
        _gw.set_client(None)
        try:
            from celerp.main import _try_auto_activate
            await _try_auto_activate()
        finally:
            _gw.set_client(_prev_client)
        assert route.call_count == 1
        import json as _json
        sent = _json.loads(route.calls[0].request.content)
        assert sent["activation_verifier"] == verifier
        cfg = mod.read_config()
        assert cfg["cloud"]["token"] == "gw-tok"
        assert cfg["cloud"]["instance_id"] == iid
        assert "activation_verifier" not in cfg["cloud"]
        assert mod.settings.activation_verifier == ""

    @respx.mock
    async def test_free_tier_recovery_does_not_start_gateway_ws_client(self, tmp_path, monkeypatch):
        mod, _ = self._prepare(tmp_path, monkeypatch)
        mod.ensure_instance_id()
        mod.ensure_activation_verifier()
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
            assert _gw.get_client() is None
        finally:
            _gw.set_client(_prev_client)
        assert mod.settings.gateway_token == "gw-tok-free"

    @respx.mock
    async def test_entitled_recovery_starts_gateway_ws_client(self, tmp_path, monkeypatch):
        mod, _ = self._prepare(tmp_path, monkeypatch)
        mod.ensure_instance_id()
        mod.ensure_activation_verifier()
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
            assert _gw.get_client() is not None
        finally:
            _gw.set_client(_prev_client)

    @respx.mock
    async def test_legacy_without_verifier_uses_observational_checkin(self, tmp_path, monkeypatch):
        mod, _ = self._prepare(tmp_path, monkeypatch)
        legacy_iid = "00000000-0000-4000-8000-000000000001"
        mod.write_config({"cloud": {"instance_id": legacy_iid}})
        mod.settings.gateway_instance_id = legacy_iid
        mod.settings.activation_verifier = ""
        checkin = respx.post("https://relay.test/auth/checkin").respond(200, json={"ok": True})
        activate = respx.post("https://relay.test/auth/activate").respond(
            200, json={"gateway_token": "must-not-be-requested"})
        from celerp.main import _try_auto_activate
        await _try_auto_activate()
        assert checkin.call_count == 1
        assert activate.call_count == 0

    @respx.mock
    async def test_legacy_old_relay_404_falls_back_to_one_activation(self, tmp_path, monkeypatch):
        mod, _ = self._prepare(tmp_path, monkeypatch)
        legacy_iid = "00000000-0000-4000-8000-000000000002"
        mod.write_config({"cloud": {"instance_id": legacy_iid}})
        mod.settings.gateway_instance_id = legacy_iid
        mod.settings.activation_verifier = ""
        checkin = respx.post("https://relay.test/auth/checkin").respond(404)
        activate = respx.post("https://relay.test/auth/activate").respond(
            404, json={"detail": "no sub"})
        from celerp.main import _try_auto_activate
        await _try_auto_activate()
        assert checkin.call_count == 1
        assert activate.call_count == 1

    @respx.mock
    async def test_legacy_checkin_transport_ambiguity_never_activates(self, tmp_path, monkeypatch):
        mod, _ = self._prepare(tmp_path, monkeypatch)
        legacy_iid = "00000000-0000-4000-8000-000000000003"
        mod.write_config({"cloud": {"instance_id": legacy_iid}})
        mod.settings.gateway_instance_id = legacy_iid
        mod.settings.activation_verifier = ""
        checkin = respx.post("https://relay.test/auth/checkin")
        checkin.side_effect = httpx.ReadTimeout("ambiguous check-in")
        activate = respx.post("https://relay.test/auth/activate").respond(
            200, json={"gateway_token": "must-not-be-requested"})
        from celerp.main import _try_auto_activate
        await _try_auto_activate()
        assert checkin.call_count == 1
        assert activate.call_count == 0


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



def test_activation_verifier_survives_restart_until_credential_is_durable(tmp_path, monkeypatch):
    mod, _ = _reload_config(tmp_path, monkeypatch)
    iid = mod.ensure_instance_id()
    verifier = mod.ensure_activation_verifier()
    assert verifier
    assert mod.read_config()["cloud"]["activation_verifier"] == verifier

    importlib.reload(mod)
    mod.load_cloud_config()
    assert mod.ensure_instance_id() == iid
    assert mod.ensure_activation_verifier() == verifier

    mod.record_cloud_activation(
        "gw-token", iid, public_url=None, expected_verifier=verifier)
    cfg = mod.read_config()
    assert cfg["cloud"]["token"] == "gw-token"
    assert "activation_verifier" not in cfg["cloud"]
    assert cfg["cloud"]["public_url"] == ""
    assert mod.settings.activation_verifier == ""



def test_activation_verifier_converges_across_processes(tmp_path):
    import os
    import subprocess
    import sys

    cfg = tmp_path / "config.toml"
    env = os.environ.copy()
    env["CELERP_CONFIG"] = str(cfg)
    env["ALLOW_INSECURE_JWT"] = "true"
    code = (
        "from celerp import config; "
        "config.settings.activation_verifier=''; "
        "print(config.ensure_activation_verifier())"
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env)
        for _ in range(6)
    ]
    outputs = []
    for proc in procs:
        out, err = proc.communicate(timeout=20)
        assert proc.returncode == 0, err
        outputs.append(out.strip())
    assert all(outputs)
    assert len(set(outputs)) == 1

    import tomllib
    with open(cfg, "rb") as f:
        persisted = tomllib.load(f)
    assert persisted["cloud"]["activation_verifier"] == outputs[0]



def test_instance_identity_and_verifier_converge_across_processes(tmp_path):
    import os
    import subprocess
    import sys

    cfg = tmp_path / "fresh-config.toml"
    env = os.environ.copy()
    env["CELERP_CONFIG"] = str(cfg)
    env["ALLOW_INSECURE_JWT"] = "true"
    code = (
        "from celerp import config; "
        "config.settings.gateway_instance_id=''; "
        "config.settings.activation_verifier=''; "
        "print(config.ensure_instance_id(), config.ensure_activation_verifier())"
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env)
        for _ in range(6)
    ]
    values = []
    for proc in procs:
        out, err = proc.communicate(timeout=20)
        assert proc.returncode == 0, err
        values.append(tuple(out.strip().split()))
    assert all(len(v) == 2 for v in values)
    assert len({v[0] for v in values}) == 1
    assert len({v[1] for v in values}) == 1

    import tomllib
    with open(cfg, "rb") as f:
        persisted = tomllib.load(f)["cloud"]
    assert persisted["instance_id"] == values[0][0]
    assert persisted["activation_verifier"] == values[0][1]
