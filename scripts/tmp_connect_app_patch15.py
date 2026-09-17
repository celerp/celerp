from pathlib import Path
import re


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if text.count(old) != 1:
        raise RuntimeError(f"{path}: expected one match, got {text.count(old)}")
    p.write_text(text.replace(old, new))


def regex_once(path: str, pattern: str, replacement: str) -> None:
    p = Path(path)
    text = p.read_text()
    new, n = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if n != 1:
        raise RuntimeError(f"{path}: regex matched {n} times")
    p.write_text(new)


# An instance id is identity, not an unfinished recovery attempt. Create the
# verifier only when an account-proof flow actually begins; patch5 already makes
# that creation cross-process safe. Ordinary fresh boots therefore use the
# observation-only /auth/checkin path instead of probing /auth/activate with an
# unapproved verifier.
replace_once(
    "celerp/config.py",
    '''    import secrets as _secrets\n    import uuid as _uuid\n    iid = str(_uuid.uuid4())\n    settings.gateway_instance_id = iid\n    if not settings.activation_verifier:\n        settings.activation_verifier = _secrets.token_urlsafe(32)\n\n    # Persist identity and proof secret together. A fresh install therefore has\n    # one verifier before multiple API workers can serve an account-link request.\n    try:\n        persist_cloud_settings(\n            instance_id=iid, activation_verifier=settings.activation_verifier)\n''',
    '''    import uuid as _uuid\n    iid = str(_uuid.uuid4())\n    settings.gateway_instance_id = iid\n\n    # Persist identity immediately. Recovery proof material is created lazily by\n    # ensure_activation_verifier() when an email/Google account proof begins.\n    try:\n        persist_cloud_settings(instance_id=iid)\n''',
)

# Rewrite only the pre-existing startup-probe methods. Patch12's separate legacy
# no-verifier rollout tests remain intact and continue proving old-relay fallback.
p = Path("tests/test_instance_identity.py")
text = p.read_text()
start = text.index("class TestAutoActivateProbe:")
legacy = text.index("    @respx.mock\n    async def test_legacy_without_verifier_uses_observational_checkin", start)
prefix = text[:start]
suffix = text[legacy:]
new_class = r'''class TestAutoActivateProbe:
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

'''
p.write_text(prefix + new_class + suffix)
